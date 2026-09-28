"""
Per-issue-type recheck rules.

Each function answers one question for one already-flagged row: "does
this specific condition still hold against OSM's CURRENT data?" -- using
the exact same thresholds and geometry logic as OSM_Quality_Check's
checks.py, so a row only flips to Fixed when the original rule would
genuinely no longer flag it, not on a looser approximation.

A row's `detail` text (written by checks.py's Issue.detail) is the only
place the "other" object referenced by a check (the other node/way it
crossed, duplicated, shared a junction with, etc.) is recorded, so this
module parses it with per-type regexes. If a future issue_type shows up
that isn't one of the patterns below, `evaluate()` returns UNSUPPORTED
rather than guessing -- that row stays NYF and is logged for manual
attention instead of being silently mis-scored.
"""
import re
import logging
from dataclasses import dataclass
from typing import Optional

from shapely.geometry import Point

import config
import geo_utils

log = logging.getLogger(__name__)

STILL_ISSUE = "still_issue"
FIXED = "fixed"
UNSUPPORTED = "unsupported"
INCONCLUSIVE = "inconclusive"  # couldn't determine from what was fetched


@dataclass
class RecheckOutcome:
    status: str  # one of the four constants above
    note: str = ""


# ---------------------------------------------------------------------------
# detail-text parsers -- return the "other" osm id(s) a check referenced
# ---------------------------------------------------------------------------

def _extract_way_ids(detail):
    return [int(x) for x in re.findall(r"way (\d+)", detail or "")]


def _extract_node_ids(detail):
    return [int(x) for x in re.findall(r"node (\d+)", detail or "")]


def required_ids(row):
    """
    Returns (node_ids, way_ids) that must be fetched from Overpass to
    evaluate this row -- the flagged object itself plus whatever other
    object(s) its detail text references.
    """
    osm_type, osm_id = row["osm_object_type"], int(row["osm_object_id"])
    detail = row.get("detail", "") or ""
    node_ids, way_ids = set(), set()

    if osm_type == "node":
        node_ids.add(osm_id)
    elif osm_type == "way":
        way_ids.add(osm_id)

    error_type = row["error_type"]
    if error_type == "duplicated node":
        node_ids.update(_extract_node_ids(detail))
    elif error_type in ("overlapping highway", "crossing buildings",
                        "overlapping buildings", "building inside building",
                        "crossing highway", "crossing way",
                        "broken highway continuity", "duplicated way"):
        way_ids.update(_extract_way_ids(detail))
    elif error_type == "node connected highway and building":
        way_ids.update(_extract_way_ids(detail))

    return node_ids, way_ids


def _node_coords_map(nodes):
    return {nid: (n["lon"], n["lat"]) for nid, n in nodes.items()
            if n.get("lon") is not None and n.get("lat") is not None}


def _has_primary_tag(tags):
    for key in tags:
        if key in config.PRIMARY_TAG_KEYS:
            return True
        if ":" in key:
            prefix, _, rest = key.partition(":")
            if prefix in config.LIFECYCLE_PREFIXES and rest in config.PRIMARY_TAG_KEYS:
                return True
    return False


# ---------------------------------------------------------------------------
# evaluate() -- dispatch table
# ---------------------------------------------------------------------------

def evaluate(row, nodes, ways, missing):
    """
    row: a dict with error_type, osm_object_type, osm_object_id, detail.
    nodes/ways/missing: the batch Overpass fetch result covering every
    id required_ids() asked for, for this and every other row checked
    in the same run.
    """
    error_type = row["error_type"]
    handler = _HANDLERS.get(error_type)
    if handler is None:
        return RecheckOutcome(UNSUPPORTED, f"no recheck rule ported for '{error_type}' yet")
    try:
        return handler(row, nodes, ways, missing)
    except Exception as e:
        log.warning("Recheck of %s %s (%s) raised %s -- treating as inconclusive",
                    row["osm_object_type"], row["osm_object_id"], error_type, e)
        return RecheckOutcome(INCONCLUSIVE, str(e))


def _duplicated_node(row, nodes, ways, missing):
    self_id = int(row["osm_object_id"])
    other_ids = _extract_node_ids(row.get("detail", ""))
    if self_id in missing:
        return RecheckOutcome(FIXED, "flagged node no longer exists")
    if self_id not in nodes or not other_ids:
        return RecheckOutcome(INCONCLUSIVE, "missing node data")
    self_n = nodes[self_id]
    for other_id in other_ids:
        if other_id in missing or other_id not in nodes:
            continue  # the other node is gone -- no longer a duplicate pair
        other_n = nodes[other_id]
        d = geo_utils.haversine_m(self_n["lat"], self_n["lon"], other_n["lat"], other_n["lon"])
        if d <= config.DUPLICATE_NODE_TOLERANCE_M:
            return RecheckOutcome(STILL_ISSUE, f"still {d:.2f}m from node {other_id}")
    return RecheckOutcome(FIXED, "no longer within tolerance of the other node")


def _feature_missing_primary_tag(row, nodes, ways, missing):
    osm_type, osm_id = row["osm_object_type"], int(row["osm_object_id"])
    store = nodes if osm_type == "node" else ways
    if osm_id in missing:
        return RecheckOutcome(FIXED, "object no longer exists")
    if osm_id not in store:
        return RecheckOutcome(INCONCLUSIVE, "missing object data")
    tags = store[osm_id].get("tags", {})
    if not tags:
        return RecheckOutcome(STILL_ISSUE, "still has no tags at all")
    if _has_primary_tag(tags):
        return RecheckOutcome(FIXED, "now has a recognised primary tag")
    return RecheckOutcome(STILL_ISSUE, f"still no primary tag among {list(tags.keys())}")


def _wrong_tagging(row, nodes, ways, missing):
    osm_type, osm_id = row["osm_object_type"], int(row["osm_object_id"])
    store = nodes if osm_type == "node" else ways
    if osm_id in missing:
        return RecheckOutcome(FIXED, "object no longer exists")
    if osm_id not in store:
        return RecheckOutcome(INCONCLUSIVE, "missing object data")
    tags = store[osm_id].get("tags", {})
    for key, allowed in config.ENUMERATED_KEY_VALUES.items():
        if key in tags and tags[key] not in allowed:
            value = tags[key]
            belongs_elsewhere = any(
                value in vals for k, vals in config.ENUMERATED_KEY_VALUES.items() if k != key
            )
            if belongs_elsewhere:
                return RecheckOutcome(STILL_ISSUE, f"'{key}={value}' still looks misplaced")
    return RecheckOutcome(FIXED, "tag no longer looks misplaced")


def _node_connected_highway_building(row, nodes, ways, missing):
    node_id = int(row["osm_object_id"])
    way_ids = _extract_way_ids(row.get("detail", ""))
    if node_id in missing:
        return RecheckOutcome(FIXED, "shared node no longer exists")
    if len(way_ids) < 2:
        return RecheckOutcome(INCONCLUSIVE, "could not parse both way ids from detail")
    highway_id, building_id = way_ids[0], way_ids[1]
    if highway_id in missing or building_id in missing:
        return RecheckOutcome(FIXED, "one of the two ways no longer exists")
    if highway_id not in ways or building_id not in ways:
        return RecheckOutcome(INCONCLUSIVE, "missing way data")
    if ways[highway_id].get("tags", {}).get("covered") == "yes":
        return RecheckOutcome(FIXED, "highway now tagged covered=yes (intentional)")
    still_shared = (node_id in ways[highway_id].get("nodes", [])
                    and node_id in ways[building_id].get("nodes", []))
    if still_shared:
        return RecheckOutcome(STILL_ISSUE, "node still shared by both ways")
    return RecheckOutcome(FIXED, "node no longer shared by both ways")


def _building_geometry(row, nodes, ways, missing):
    """Covers crossing buildings / overlapping buildings / building inside building."""
    self_id = int(row["osm_object_id"])
    other_ids = _extract_way_ids(row.get("detail", ""))
    if self_id in missing:
        return RecheckOutcome(FIXED, "flagged building no longer exists")
    if self_id not in ways or not other_ids:
        return RecheckOutcome(INCONCLUSIVE, "missing way data")
    node_coords = _node_coords_map(nodes)
    self_geom = geo_utils.build_way_geometry(ways[self_id]["nodes"], node_coords, ways[self_id].get("tags"))
    if self_geom is None or not self_geom.is_valid or self_geom.area == 0:
        return RecheckOutcome(FIXED, "building geometry no longer resolvable/valid")
    for other_id in other_ids:
        if other_id in missing or other_id not in ways:
            continue
        other_geom = geo_utils.build_way_geometry(ways[other_id]["nodes"], node_coords, ways[other_id].get("tags"))
        if other_geom is None or not other_geom.is_valid or other_geom.area == 0:
            continue
        if not self_geom.intersects(other_geom):
            continue
        inter_area = self_geom.intersection(other_geom).area
        ratio = inter_area / min(self_geom.area, other_geom.area)
        if ratio >= config.BUILDING_OVERLAP_MIN_RATIO or self_geom.within(other_geom) or other_geom.within(self_geom):
            return RecheckOutcome(STILL_ISSUE, f"still overlaps way {other_id} ({ratio:.0%})")
    return RecheckOutcome(FIXED, "no longer overlaps the referenced building")


def _way_crossing(row, nodes, ways, missing):
    """Covers crossing highway / crossing way (generic line-crossing check)."""
    self_id = int(row["osm_object_id"])
    other_ids = _extract_way_ids(row.get("detail", ""))
    if self_id in missing:
        return RecheckOutcome(FIXED, "flagged way no longer exists")
    if self_id not in ways or not other_ids:
        return RecheckOutcome(INCONCLUSIVE, "missing way data")
    node_coords = _node_coords_map(nodes)
    self_geom = geo_utils.build_way_geometry(ways[self_id]["nodes"], node_coords, ways[self_id].get("tags"))
    if self_geom is None:
        return RecheckOutcome(FIXED, "way geometry no longer resolvable")
    self_nodes = set(ways[self_id]["nodes"])
    for other_id in other_ids:
        if other_id in missing or other_id not in ways:
            continue
        if self_nodes & set(ways[other_id].get("nodes", [])):
            continue  # now properly connected at a shared node -- resolved
        other_geom = geo_utils.build_way_geometry(ways[other_id]["nodes"], node_coords, ways[other_id].get("tags"))
        if other_geom is not None and self_geom.crosses(other_geom):
            return RecheckOutcome(STILL_ISSUE, f"still crosses way {other_id} without a shared node")
    return RecheckOutcome(FIXED, "no longer crosses the referenced way without a shared node")


def _overlapping_highway(row, nodes, ways, missing):
    self_id = int(row["osm_object_id"])
    other_ids = _extract_way_ids(row.get("detail", ""))
    if self_id in missing:
        return RecheckOutcome(FIXED, "flagged highway no longer exists")
    if self_id not in ways or not other_ids:
        return RecheckOutcome(INCONCLUSIVE, "missing way data")
    node_coords = _node_coords_map(nodes)
    self_way = ways[self_id]
    self_class = self_way.get("tags", {}).get("highway")
    self_geom = geo_utils.build_way_geometry(self_way["nodes"], node_coords, self_way.get("tags"))
    if self_geom is None or self_geom.length == 0 or not self_class:
        return RecheckOutcome(FIXED, "highway geometry/class no longer matches")
    buffered = self_geom.buffer(config.OVERLAPPING_HIGHWAY_BUFFER_DEG)
    for other_id in other_ids:
        if other_id in missing or other_id not in ways:
            continue
        other_way = ways[other_id]
        if other_way.get("tags", {}).get("highway") != self_class:
            continue  # reclassified -- no longer a same-class duplicate
        other_geom = geo_utils.build_way_geometry(other_way["nodes"], node_coords, other_way.get("tags"))
        if other_geom is None or other_geom.length == 0:
            continue
        overlap_len = other_geom.intersection(buffered).length
        ratio = overlap_len / other_geom.length
        if ratio > config.OVERLAPPING_HIGHWAY_MIN_RATIO:
            return RecheckOutcome(STILL_ISSUE, f"still runs alongside way {other_id} ({ratio:.0%})")
    return RecheckOutcome(FIXED, "no longer runs alongside the referenced highway")


def _broken_highway_continuity(row, nodes, ways, missing):
    other_ids = _extract_way_ids(row.get("detail", ""))
    if len(other_ids) < 2:
        return RecheckOutcome(INCONCLUSIVE, "could not parse both way ids from detail")
    w1_id, w2_id = other_ids[0], other_ids[1]
    if w1_id in missing or w2_id in missing:
        return RecheckOutcome(FIXED, "one of the two ways no longer exists")
    if w1_id not in ways or w2_id not in ways:
        return RecheckOutcome(INCONCLUSIVE, "missing way data")
    w1, w2 = ways[w1_id], ways[w2_id]
    if set(w1.get("nodes", [])) & set(w2.get("nodes", [])):
        return RecheckOutcome(FIXED, "ways now share a node -- connected")
    node_coords = _node_coords_map(nodes)
    try:
        ends1 = [node_coords[w1["nodes"][0]], node_coords[w1["nodes"][-1]]]
        ends2 = [node_coords[w2["nodes"][0]], node_coords[w2["nodes"][-1]]]
    except (KeyError, IndexError):
        return RecheckOutcome(INCONCLUSIVE, "missing endpoint coordinates")
    for (lon1, lat1) in ends1:
        for (lon2, lat2) in ends2:
            d = geo_utils.haversine_m(lat1, lon1, lat2, lon2)
            if d <= config.BROKEN_CONTINUITY_MAX_GAP_M:
                return RecheckOutcome(STILL_ISSUE, f"still {d:.2f}m apart with no shared node")
    return RecheckOutcome(FIXED, "endpoints no longer close enough to look continuous")


def _duplicated_way(row, nodes, ways, missing):
    self_id = int(row["osm_object_id"])
    other_ids = _extract_way_ids(row.get("detail", ""))
    if self_id in missing:
        return RecheckOutcome(FIXED, "flagged way no longer exists")
    if self_id not in ways or not other_ids:
        return RecheckOutcome(INCONCLUSIVE, "missing way data")
    self_key = tuple(sorted(ways[self_id].get("nodes", [])))
    for other_id in other_ids:
        if other_id in missing or other_id not in ways:
            continue
        other_key = tuple(sorted(ways[other_id].get("nodes", [])))
        if self_key == other_key and len(self_key) >= 2:
            return RecheckOutcome(STILL_ISSUE, f"still shares all nodes with way {other_id}")
    return RecheckOutcome(FIXED, "no longer shares all nodes with the referenced way")


def _untagged_way(row, nodes, ways, missing):
    self_id = int(row["osm_object_id"])
    if self_id in missing:
        return RecheckOutcome(FIXED, "way no longer exists")
    if self_id not in ways:
        return RecheckOutcome(INCONCLUSIVE, "missing way data")
    if ways[self_id].get("tags"):
        return RecheckOutcome(FIXED, "way now has tags")
    return RecheckOutcome(STILL_ISSUE, "way still has no tags")


# 'floating highway' needs a live connectivity lookup (fetch_ways_touching_node),
# which the generic batch fetch doesn't cover -- handled separately in
# checker.py via recheck_floating_highway() below, not through this dispatch table.

def recheck_floating_highway(row, ways_for_endpoints):
    """
    ways_for_endpoints: {node_id: [way_dicts]} for this row's way's two
    endpoint nodes, from fetch.fetch_ways_touching_nodes(). Called
    separately from evaluate() because it needs a connectivity query,
    not just the object's own current state.
    """
    self_id = int(row["osm_object_id"])
    self_way = row.get("_current_way")  # attached by checker.py after the main batch fetch
    if self_way is None:
        return RecheckOutcome(FIXED, "way no longer exists")
    node_ids = self_way.get("nodes", [])
    if len(node_ids) < 2:
        return RecheckOutcome(INCONCLUSIVE, "way has fewer than 2 nodes")
    if self_way.get("tags", {}).get("noexit") == "yes":
        return RecheckOutcome(FIXED, "now explicitly tagged noexit=yes")
    start_ways = [w for w in ways_for_endpoints.get(node_ids[0], []) if w["id"] != self_id]
    end_ways = [w for w in ways_for_endpoints.get(node_ids[-1], []) if w["id"] != self_id]
    if start_ways or end_ways:
        return RecheckOutcome(FIXED, "at least one endpoint now connects to another way")
    return RecheckOutcome(STILL_ISSUE, "still isolated at both endpoints")


_HANDLERS = {
    "duplicated node": _duplicated_node,
    "feature mapped without primary tag": _feature_missing_primary_tag,
    "wrong tagging": _wrong_tagging,
    "node connected highway and building": _node_connected_highway_building,
    "crossing buildings": _building_geometry,
    "overlapping buildings": _building_geometry,
    "building inside building": _building_geometry,
    "crossing highway": _way_crossing,
    "crossing way": _way_crossing,
    "overlapping highway": _overlapping_highway,
    "broken highway continuity": _broken_highway_continuity,
    "duplicated way": _duplicated_way,
    "untagged way": _untagged_way,
    # NOT ported (changeset-scoped, not a re-checkable object state):
    #   "mass delete without revert tag", "mass upload (create)",
    #   "mass upload (modify)", "unclear changeset comment",
    #   "dense node cluster", "sudden highway classification change",
    #   "way end node near other way"
    # These fall through to UNSUPPORTED in evaluate() and stay NYF with
    # a logged note rather than being guessed at.
}

NEEDS_ENDPOINT_LOOKUP = {"floating highway"}
