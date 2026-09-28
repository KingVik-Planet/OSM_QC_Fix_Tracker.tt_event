"""
Talks to Overpass and the live OSM API to re-check the CURRENT state of
osm objects that were already flagged in quality_check_*.csv.

Resilience pattern (circuit breaker + capped retry queue) is carried
over from OSM_Quality_Check's fetch.py: if Overpass is down, don't burn
the whole run retrying it -- stop, log clearly, and leave the rest for
the next hourly run.
"""
import logging
import requests

import config

log = logging.getLogger(__name__)
HEADERS = {"User-Agent": "osm-qc-fix-tracker/1.0"}


class OverpassUnavailable(Exception):
    """
    Raised when every Overpass mirror failed every retry, or the circuit
    breaker is already open. Callers MUST treat this as "unknown, retry
    later" -- never as "confirmed, object is gone".
    """
    pass


# --- Circuit breaker (resets every run, same as the original) --------------
_OVERPASS_CIRCUIT_THRESHOLD = 1
_overpass_consecutive_failures = 0


def overpass_circuit_is_open():
    return _overpass_consecutive_failures >= _OVERPASS_CIRCUIT_THRESHOLD


def reset_circuit_breaker():
    """Call once at the start of every run (fresh process anyway, but
    explicit is safer than relying on module-reload behaviour)."""
    global _overpass_consecutive_failures
    _overpass_consecutive_failures = 0


def _get(url, params=None, timeout=60):
    resp = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp


def _post_overpass(query):
    """
    Tries every configured Overpass endpoint in order, with per-endpoint
    retries, exactly like OSM_Quality_Check's fetch_overpass_context.
    Raises OverpassUnavailable if every mirror fails, or if the breaker
    is already open (skips straight to raising, no wasted retries).
    """
    global _overpass_consecutive_failures

    if overpass_circuit_is_open():
        log.info(
            "Overpass circuit breaker open (%d consecutive full failures this run) "
            "-- skipping instantly instead of retrying",
            _overpass_consecutive_failures,
        )
        raise OverpassUnavailable("circuit breaker open")

    last_err = None
    for endpoint in config.OVERPASS_ENDPOINTS:
        for attempt in range(1, config.OVERPASS_RETRIES + 1):
            try:
                resp = requests.post(
                    endpoint, data={"data": query}, headers=HEADERS,
                    timeout=config.OVERPASS_HTTP_TIMEOUT_S,
                )
                resp.raise_for_status()
                data = resp.json()
                _overpass_consecutive_failures = 0  # a mirror answered -- reset
                return data
            except requests.RequestException as e:
                last_err = e
                log.info("Overpass %s attempt %d/%d failed: %s",
                         endpoint, attempt, config.OVERPASS_RETRIES, e)

    _overpass_consecutive_failures += 1
    log.warning(
        "All Overpass endpoints failed after retries (%d consecutive full "
        "failures this run): %s", _overpass_consecutive_failures, last_err,
    )
    raise OverpassUnavailable(str(last_err))


def fetch_current_elements(node_ids, way_ids):
    """
    Batch-fetches the CURRENT state of the given node and way ids from
    Overpass, with metadata (out meta;) so the changeset id/user/timestamp
    of whichever edit produced the current version comes back in the
    same call -- no separate /history lookup needed.

    Returns (nodes, ways, missing_ids):
      nodes: {id: {"lat","lon","tags","version","changeset","user","uid","timestamp"}}
      ways:  {id: {"nodes":[...], "tags":{...}, "version","changeset","user","uid","timestamp"}}
      missing_ids: set of ids that came back empty -- i.e. NOT found by
        Overpass right now. This can mean "deleted", but Overpass also
        lags the live database by minutes, so callers that care about a
        confirmed deletion should cross-check with fetch_live_element()
        before concluding the object is gone for good.

    Raises OverpassUnavailable -- caller must queue these ids for a later
    run, never treat a raised exception as "confirmed missing".
    """
    if not node_ids and not way_ids:
        return {}, {}, set()

    parts = []
    if node_ids:
        parts.append("node(id:" + ",".join(str(i) for i in node_ids) + ");")
    if way_ids:
        parts.append("way(id:" + ",".join(str(i) for i in way_ids) + ");")

    query = f"""
    [out:json][timeout:{config.OVERPASS_QUERY_TIMEOUT_S}];
    (
      {' '.join(parts)}
    );
    out meta;
    >;
    out skel qt meta;
    """

    data = _post_overpass(query)

    nodes, ways = {}, {}
    found_node_ids, found_way_ids = set(), set()

    for el in data.get("elements", []):
        meta = {
            "version": el.get("version"),
            "changeset": el.get("changeset"),
            "user": el.get("user"),
            "uid": el.get("uid"),
            "timestamp": el.get("timestamp"),
        }
        if el["type"] == "node":
            nodes[el["id"]] = {"lat": el.get("lat"), "lon": el.get("lon"),
                                "tags": el.get("tags", {}), **meta}
            if el["id"] in way_ids:
                pass  # a node also happens to share an id space slot; no-op, ids are namespaced by type anyway
            found_node_ids.add(el["id"])
        elif el["type"] == "way":
            ways[el["id"]] = {"nodes": el.get("nodes", []), "tags": el.get("tags", {}), **meta}
            found_way_ids.add(el["id"])

    missing = (set(node_ids) - found_node_ids) | (set(way_ids) - found_way_ids)
    return nodes, ways, missing


def fetch_ways_touching_node(node_id):
    """
    Returns the list of current way ids that have node_id as a member --
    used to recheck 'floating highway' (does either endpoint now connect
    to something?) and similar connectivity rechecks, without needing a
    bounding-box context query.
    """
    query = f"""
    [out:json][timeout:{config.OVERPASS_QUERY_TIMEOUT_S}];
    way(bn:{node_id});
    out tags ids;
    """
    data = _post_overpass(query)
    return [{"id": el["id"], "tags": el.get("tags", {})} for el in data.get("elements", [])]


def fetch_ways_touching_nodes(node_ids):
    """
    Batched version of fetch_ways_touching_node: one Overpass call
    covering every given node id, used to recheck 'floating highway'
    connectivity for many rows in a single run instead of one query per
    endpoint. Returns {node_id: [{"id","tags"}, ...]}.
    """
    node_ids = [n for n in node_ids if n]
    if not node_ids:
        return {}
    # Overpass QL's way(bn:...) filter doesn't tell you which input node
    # each result way matched when you union several in one query, so
    # this issues one small query per endpoint node rather than trying to
    # cram them into a single ambiguous union. floating-highway rows are
    # normally a small slice of a run's batch, so this stays cheap; the
    # circuit breaker still protects it exactly like every other Overpass
    # call here.
    result = {}
    for nid in node_ids:
        try:
            result[nid] = fetch_ways_touching_node(nid)
        except OverpassUnavailable:
            raise
    return result


def fetch_live_element(osm_type, osm_id):
    """
    Confirms an object's status directly against the live OSM API (not
    Overpass, which can lag by minutes). Returns:
      {"visible": bool, "version", "changeset", "user", "uid", "timestamp",
       "tags", "nodes" (ways only), "lat"/"lon" (nodes only)}
    or None on a hard failure (network error) -- callers should treat
    None as "couldn't confirm", not as proof of anything.
    """
    try:
        resp = _get(f"{config.OSM_API_BASE}/{osm_type}/{osm_id}.json")
        el = resp.json().get("elements", [{}])[0]
        out = {
            "visible": el.get("visible", True),
            "version": el.get("version"),
            "changeset": el.get("changeset"),
            "user": el.get("user"),
            "uid": el.get("uid"),
            "timestamp": el.get("timestamp"),
            "tags": el.get("tags", {}),
        }
        if osm_type == "node":
            out["lat"], out["lon"] = el.get("lat"), el.get("lon")
        elif osm_type == "way":
            out["nodes"] = el.get("nodes", [])
        return out
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 410:
            return {"visible": False}  # confirmed deleted
        log.info("Live re-check of %s %s failed: %s", osm_type, osm_id, e)
        return None
    except Exception as e:
        log.info("Live re-check of %s %s failed: %s", osm_type, osm_id, e)
        return None


def fetch_last_version(osm_type, osm_id):
    """
    Fetches an object's full version history from the live OSM API and
    returns the metadata of its LAST version -- used when an object is
    confirmed gone from Overpass (deleted) so we can still report which
    changeset/user actually deleted it, since the single-element GET
    endpoint returns HTTP 410 with no body for a deleted object.
    Returns {"visible","version","changeset","user","uid","timestamp"}
    or None on failure.
    """
    try:
        resp = _get(f"{config.OSM_API_BASE}/{osm_type}/{osm_id}/history.json")
        elements = resp.json().get("elements", [])
        if not elements:
            return None
        last = elements[-1]
        return {
            "visible": last.get("visible", True),
            "version": last.get("version"),
            "changeset": last.get("changeset"),
            "user": last.get("user"),
            "uid": last.get("uid"),
            "timestamp": last.get("timestamp"),
        }
    except Exception as e:
        log.info("Could not fetch history for %s %s: %s", osm_type, osm_id, e)
        return None


def fetch_way_full_geometry(way_id):
    """
    Fetches a way plus all its member nodes' coordinates in one call
    (the live OSM API's /full endpoint), for recheck rules that need
    accurate current geometry rather than just tags/version.
    Returns (node_ids, node_coords, tags) or None on failure.
    """
    try:
        resp = _get(f"{config.OSM_API_BASE}/way/{way_id}/full.json")
        elements = resp.json().get("elements", [])
        way = next((e for e in elements if e["type"] == "way" and e["id"] == way_id), None)
        if way is None:
            return None
        node_coords = {e["id"]: (e["lon"], e["lat"]) for e in elements if e["type"] == "node"}
        return way.get("nodes", []), node_coords, way.get("tags", {})
    except Exception as e:
        log.info("Could not fetch full geometry for way %s: %s", way_id, e)
        return None
