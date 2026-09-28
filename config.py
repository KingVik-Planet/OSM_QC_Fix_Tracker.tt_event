"""
Central configuration for the OSM_QC_Fix_Tracker pipeline.

Mirrors the env-driven override pattern used in OSM_Quality_Check's
config.py, so this repo behaves the same way locally and under cron:
every setting can be overridden with an environment variable without
touching code.
"""
import os

# --- Where the issues come from ---------------------------------------------
# The upstream repo that runs OSM_Quality_Check and commits its rotating
# quality_check_*.csv files straight into its own data/ directory. This repo
# never writes to it -- only reads the raw CSVs over plain HTTP.
SOURCE_REPO_RAW_BASE = os.getenv(
    "QCFIX_SOURCE_RAW_BASE",
    "https://raw.githubusercontent.com/KingVik-Planet/OSM_Quality_Check/master/data",
)
SOURCE_CSV_BASENAME = os.getenv("QCFIX_SOURCE_CSV_BASENAME", "quality_check")
# How many quality_check_N.csv files to probe for on every ingest run.
# Kept generous and cheap (a 404 on a raw.githubusercontent.com URL is
# fast) rather than trying to guess the exact current count each time.
SOURCE_CSV_PROBE_MAX = int(os.getenv("QCFIX_SOURCE_CSV_PROBE_MAX", 20))

# --- OSM API -----------------------------------------------------------------
OSM_API_BASE = os.getenv("QCFIX_OSM_API_BASE", "https://api.openstreetmap.org/api/0.6")

# --- Overpass ------------------------------------------------------------
OVERPASS_ENDPOINTS = [
    os.getenv("OVERPASS_PRIMARY", "https://overpass-api.de/api/interpreter"),
    os.getenv("OVERPASS_FALLBACK", "https://overpass.kumi.systems/api/interpreter"),
]
OVERPASS_HTTP_TIMEOUT_S = int(os.getenv("QCFIX_OVERPASS_HTTP_TIMEOUT_S", 45))
OVERPASS_QUERY_TIMEOUT_S = int(os.getenv("QCFIX_OVERPASS_QUERY_TIMEOUT_S", 35))
OVERPASS_RETRIES = int(os.getenv("QCFIX_OVERPASS_RETRIES", 1))

# How many NYF issues to (re)check via Overpass in a single run. Bounds
# worst-case run time the same way OSM_Quality_Check's MAX_RETRY_PER_RUN
# does -- the current hour always gets a bounded amount of work done
# instead of an ever-growing backlog starving every run.
MAX_CHECK_PER_RUN = int(os.getenv("QCFIX_MAX_CHECK_PER_RUN", 300))

# How many osm ids to put in a single Overpass batch query.
OVERPASS_BATCH_SIZE = int(os.getenv("QCFIX_OVERPASS_BATCH_SIZE", 200))

# --- Geometry / tag thresholds, ported 1:1 from OSM_Quality_Check's config.py ---
DUPLICATE_NODE_TOLERANCE_M = float(os.getenv("QC_DUP_NODE_TOLERANCE_M", 0.05))
BUILDING_OVERLAP_MIN_RATIO = float(os.getenv("QC_BUILDING_OVERLAP_MIN_RATIO", 0.02))
BROKEN_CONTINUITY_MAX_GAP_M = float(os.getenv("QC_BROKEN_CONTINUITY_MAX_GAP_M", 5.0))
# Same magic numbers used inline in checks.py's check_overlapping_highways
# (not pulled from its config.py, since the original hardcodes them there too).
OVERLAPPING_HIGHWAY_BUFFER_DEG = float(os.getenv("QCFIX_OVERLAP_HWY_BUFFER_DEG", 0.00003))
OVERLAPPING_HIGHWAY_MIN_RATIO = float(os.getenv("QCFIX_OVERLAP_HWY_MIN_RATIO", 0.6))

# --- Storage -------------------------------------------------------------
DATA_DIR = os.getenv("QCFIX_DATA_DIR", "data")
DB_PATH = os.path.join(DATA_DIR, "tracker.db")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
# Rotating snapshot export, same convention as OSM_Quality_Check's own
# quality_check_N.csv: fix_status_snapshot_1.csv, _2.csv, ... each capped
# at CSV_MAX_BYTES. Unlike the source repo's log (which only ever
# appends), this snapshot is fully regenerated from the DB every run, so
# rows can shuffle between file numbers as the table grows -- that's
# expected and harmless, since every run rewrites the whole thing anyway.
SNAPSHOT_BASENAME = os.getenv("QCFIX_SNAPSHOT_BASENAME", "fix_status_snapshot")
SNAPSHOT_MAX_BYTES = 40 * 1024 * 1024  # 40MB per file, matching the source repo's own cap

# --- Tag-key sets, ported 1:1 from OSM_Quality_Check's config.py ------------
# Used by the "feature mapped without primary tag" recheck so it applies
# the exact same definition of "has a primary tag" as the original flag.
PRIMARY_TAG_KEYS = {
    "advertising", "aerialway", "aeroway", "amenity", "barrier",
    "boundary", "building", "club", "craft", "departures_board",
    "education", "emergency", "geological", "healthcare", "highway",
    "historic", "landcover", "landuse", "leisure", "man_made",
    "military", "natural", "office", "piste:type", "place", "power",
    "public_transport", "railway", "route", "shop", "telecom",
    "tourism", "waterway",
    "addr:interpolation", "allotments", "area:highway", "attraction",
    "building:part", "bridge:support", "cemetery", "entrance", "ford",
    "golf", "indoor", "junction", "noexit", "playground",
    "roller_coaster", "traffic_calming", "traffic_sign",
}
LIFECYCLE_PREFIXES = {
    "proposed", "planned", "construction", "disused", "abandoned",
    "ruins", "demolished", "removed", "razed", "destroyed", "was",
    "former", "closed",
}

# Ported 1:1 from OSM_Quality_Check's config.py -- used by the
# "wrong tagging" recheck.
ENUMERATED_KEY_VALUES = {
    "area": {"yes", "no"},
    "building": {
        "yes", "house", "residential", "apartments", "detached", "terrace",
        "semidetached_house", "garage", "garages", "commercial", "industrial",
        "retail", "warehouse", "school", "church", "hospital", "hotel",
        "office", "roof", "hut", "shed", "cabin", "farm", "farm_auxiliary",
        "barn", "greenhouse", "service", "civic", "public", "stadium",
        "train_station", "transportation", "kiosk", "construction", "ruins",
        "collapsed", "no",
    },
    "highway": {
        "motorway", "trunk", "primary", "secondary", "tertiary",
        "unclassified", "residential", "service", "track", "path",
        "footway", "cycleway", "bridleway", "steps", "pedestrian",
        "living_street", "road", "motorway_link", "trunk_link",
        "primary_link", "secondary_link", "tertiary_link", "construction",
        "proposed", "bus_stop", "crossing", "traffic_signals", "give_way",
        "stop", "mini_roundabout", "turning_circle", "milestone", "elevator",
    },
    "landuse": {
        "residential", "commercial", "industrial", "retail", "farmland",
        "farmyard", "forest", "meadow", "grass", "orchard", "vineyard",
        "quarry", "cemetery", "construction", "military", "railway",
        "recreation_ground", "allotments", "landfill", "brownfield",
        "greenfield",
    },
    "natural": {
        "wood", "water", "wetland", "tree", "tree_row", "scrub",
        "grassland", "heath", "bare_rock", "sand", "beach", "cliff",
        "coastline", "peak", "valley", "ridge", "glacier", "volcano", "bay",
    },
    "waterway": {
        "river", "stream", "canal", "drain", "ditch", "dam", "weir",
        "waterfall", "riverbank", "boatyard",
    },
    "railway": {
        "rail", "subway", "light_rail", "tram", "narrow_gauge", "monorail",
        "funicular", "station", "halt", "platform", "construction",
        "abandoned", "disused",
    },
    "amenity": {
        "restaurant", "cafe", "school", "hospital", "bank", "pharmacy",
        "fuel", "parking", "place_of_worship", "toilets", "bar", "pub",
        "fast_food", "police", "fire_station", "post_office", "library",
        "clinic", "marketplace", "waste_basket", "bench", "drinking_water",
        "atm", "kindergarten", "university", "college", "townhall",
        "community_centre", "social_facility", "veterinary",
    },
}
