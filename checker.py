"""
Rechecks currently-open (NYF) issues against OSM's CURRENT data and
flips status/fix columns for whichever ones no longer reproduce.

Batches every Overpass lookup needed across the whole run into as few
HTTP calls as possible (grouped in chunks of config.OVERPASS_BATCH_SIZE)
rather than one call per issue, and honours the same circuit-breaker /
per-run cap pattern as OSM_Quality_Check: if Overpass is down, log it
clearly, leave the rest queued as still-NYF, and let the next hourly
run pick up where this one left off.
"""
import logging
from datetime import datetime, timezone

import config
import db
import fetch
import recheck_rules as rr

log = logging.getLogger(__name__)


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _chunks(seq, size):
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _fetch_all(node_ids, way_ids):
    """
    Fetches every requested id in batches of config.OVERPASS_BATCH_SIZE,
    merging results. If Overpass becomes unavailable partway through,
    stops issuing further batches (the breaker is already open, so
    further calls would just fail instantly anyway) and returns what
    it has so far, plus everything not yet fetched folded into
    `unreached` so callers can treat those rows as inconclusive rather
    than wrongly concluding "missing".
    """
    all_nodes, all_ways, all_missing, unreached = {}, {}, set(), set()
    node_batches = list(_chunks(sorted(node_ids), config.OVERPASS_BATCH_SIZE))
    way_batches = list(_chunks(sorted(way_ids), config.OVERPASS_BATCH_SIZE))
    # Pair up node/way batches loosely -- simplest correct approach: fetch
    # each id-type's batches independently.
    for batch in node_batches:
        if fetch.overpass_circuit_is_open():
            unreached.update(batch)
            continue
        try:
            n, w, m = fetch.fetch_current_elements(batch, [])
            all_nodes.update(n)
            all_missing.update(m)
        except fetch.OverpassUnavailable:
            unreached.update(batch)
    for batch in way_batches:
        if fetch.overpass_circuit_is_open():
            unreached.update(batch)
            continue
        try:
            n, w, m = fetch.fetch_current_elements([], batch)
            all_nodes.update(n)   # way member nodes come back via '>;' -- capture them too
            all_ways.update(w)
            all_missing.update(m)
        except fetch.OverpassUnavailable:
            unreached.update(batch)
    return all_nodes, all_ways, all_missing, unreached


def _fix_info_from_current(osm_type, osm_id, nodes, ways):
    """Object still exists -- its current version's meta IS the fix's
    changeset/user/timestamp (the edit that last touched it)."""
    store = nodes if osm_type == "node" else ways
    meta = store.get(osm_id, {})
    return {
        "user": meta.get("user") or "unknown",
        "changeset_id": meta.get("changeset"),
        "date": meta.get("timestamp"),
        "new_osm_object_id": None,
    }


def _fix_info_from_deletion(osm_type, osm_id):
    """Object is gone from Overpass -- pull its final version from the
    live OSM API's history so we still get who/when/what changeset
    deleted it. Manual follow-up note: if it was replaced by a brand
    new object rather than genuinely removed, new_osm_object_id needs
    to be filled in by hand -- this isn't auto-detected (see README)."""
    last = fetch.fetch_last_version(osm_type, osm_id)
    if last is None:
        return {"user": "unknown", "changeset_id": None, "date": _now_iso(),
                "new_osm_object_id": None}
    return {
        "user": last.get("user") or "unknown",
        "changeset_id": last.get("changeset"),
        "date": last.get("timestamp") or _now_iso(),
        "new_osm_object_id": None,
    }


def run(conn):
    fetch.reset_circuit_breaker()
    open_issues = db.fetch_open_issues(conn, config.MAX_CHECK_PER_RUN)
    total_open = len(db.fetch_open_issues(conn, 10**9))
    log.info("Rechecking %d issue(s) this run (%d total still NYF, cap=%d)",
              len(open_issues), total_open, config.MAX_CHECK_PER_RUN)

    if not open_issues:
        log.info("Nothing to recheck this run.")
        return {"checked": 0, "fixed": 0, "still_open": 0, "deferred": 0}

    floating_rows = [r for r in open_issues if r["error_type"] in rr.NEEDS_ENDPOINT_LOOKUP]
    generic_rows = [r for r in open_issues if r["error_type"] not in rr.NEEDS_ENDPOINT_LOOKUP]

    node_ids, way_ids = set(), set()
    for row in open_issues:  # floating-highway rows still need their own way fetched
        n_ids, w_ids = rr.required_ids(row)
        node_ids.update(n_ids)
        way_ids.update(w_ids)

    nodes, ways, missing, unreached = _fetch_all(node_ids, way_ids)

    if unreached:
        log.warning(
            "Overpass circuit breaker tripped or endpoints exhausted -- "
            "%d id(s) could not be fetched this run, affected issues stay NYF",
            len(unreached),
        )

    fixed_count = 0
    still_open_count = 0
    deferred_count = 0
    now = _now_iso()

    for row in generic_rows:
        s_no = row["s_no"]
        n_ids, w_ids = rr.required_ids(row)
        if (n_ids | w_ids) & unreached:
            db.mark_still_open(conn, s_no, now, "deferred -- Overpass unreachable this run")
            deferred_count += 1
            continue

        outcome = rr.evaluate(row, nodes, ways, missing)

        if outcome.status == rr.FIXED:
            osm_type, osm_id = row["osm_object_type"], int(row["osm_object_id"])
            if osm_id in missing:
                info = _fix_info_from_deletion(osm_type, osm_id)
            else:
                info = _fix_info_from_current(osm_type, osm_id, nodes, ways)
            date_fixed = (info["date"] or now)[:10]  # ISO date portion
            db.mark_fixed(
                conn, s_no, date_fixed, info["user"],
                info["changeset_id"], fetch_changeset_link(info["changeset_id"]),
                info["new_osm_object_id"], now, outcome.note,
            )
            fixed_count += 1
        elif outcome.status == rr.STILL_ISSUE:
            db.mark_still_open(conn, s_no, now, outcome.note)
            still_open_count += 1
        else:  # INCONCLUSIVE or UNSUPPORTED
            db.mark_still_open(conn, s_no, now, outcome.note)
            deferred_count += 1

    for row in floating_rows:
        s_no = row["s_no"]
        self_id = int(row["osm_object_id"])
        if self_id in unreached:
            db.mark_still_open(conn, s_no, now, "deferred -- Overpass unreachable this run")
            deferred_count += 1
            continue
        if self_id in missing:
            info = _fix_info_from_deletion("way", self_id)
            db.mark_fixed(conn, s_no, (info["date"] or now)[:10], info["user"],
                          info["changeset_id"], fetch_changeset_link(info["changeset_id"]),
                          None, now, "way no longer exists")
            fixed_count += 1
            continue

        row["_current_way"] = ways.get(self_id)
        way = ways.get(self_id)
        if way is None or len(way.get("nodes", [])) < 2:
            db.mark_still_open(conn, s_no, now, "missing way data")
            deferred_count += 1
            continue

        if fetch.overpass_circuit_is_open():
            db.mark_still_open(conn, s_no, now, "deferred -- Overpass unreachable this run")
            deferred_count += 1
            continue

        try:
            endpoints_map = fetch.fetch_ways_touching_nodes([way["nodes"][0], way["nodes"][-1]])
        except fetch.OverpassUnavailable:
            db.mark_still_open(conn, s_no, now, "deferred -- Overpass unreachable this run")
            deferred_count += 1
            continue

        outcome = rr.recheck_floating_highway(row, endpoints_map)
        if outcome.status == rr.FIXED:
            info = _fix_info_from_current("way", self_id, nodes, ways)
            db.mark_fixed(conn, s_no, (info["date"] or now)[:10], info["user"],
                          info["changeset_id"], fetch_changeset_link(info["changeset_id"]),
                          None, now, outcome.note)
            fixed_count += 1
        else:
            db.mark_still_open(conn, s_no, now, outcome.note)
            still_open_count += 1

    conn.commit()
    db.export_snapshot_csv(conn, config.SNAPSHOT_CSV_PATH)

    log.info(
        "Recheck complete: %d fixed, %d still open, %d deferred (of %d checked this run)",
        fixed_count, still_open_count, deferred_count, len(open_issues),
    )
    return {"checked": len(open_issues), "fixed": fixed_count,
            "still_open": still_open_count, "deferred": deferred_count}


def fetch_changeset_link(changeset_id):
    if not changeset_id:
        return None
    return f"https://www.openstreetmap.org/changeset/{changeset_id}"
