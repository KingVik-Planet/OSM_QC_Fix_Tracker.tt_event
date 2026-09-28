"""
SQLite storage for the fix tracker.

One row per issue originally identified in quality_check_*.csv, keyed
by its source `s_no` (continuous and unique across all quality_check_N
files per OSM_Quality_Check's own numbering rule -- so it doubles
perfectly as our primary key, no separate hash needed).
"""
import sqlite3
import os
import glob
import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    s_no                INTEGER PRIMARY KEY,
    error_type          TEXT NOT NULL,
    username            TEXT,
    user_id             TEXT,
    osm_location_link   TEXT,
    changeset_id        TEXT,
    changeset_link      TEXT,
    osm_object_type     TEXT NOT NULL,
    osm_object_id       TEXT NOT NULL,
    time_utc            TEXT,
    country             TEXT,
    detail              TEXT,

    -- appended tracking columns --
    status              TEXT NOT NULL DEFAULT 'NYF',   -- 'Fixed' or 'NYF'
    date_fixed          TEXT,                          -- ISO date, or 'NYF' while open
    fixed_user          TEXT,
    fixed_changeset_id  TEXT,
    fixed_changeset_link TEXT,
    new_osm_object_id   TEXT,                          -- only if the object was replaced
    last_checked_utc    TEXT,
    check_note          TEXT                           -- why it's still NYF / inconclusive / unsupported
);
CREATE INDEX IF NOT EXISTS idx_issues_status ON issues(status);
CREATE INDEX IF NOT EXISTS idx_issues_type ON issues(osm_object_type, osm_object_id);
"""


def connect():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def insert_new_issue(conn, row):
    """row: dict matching the original quality_check_*.csv columns, plus s_no.
    Returns True if a row was actually inserted, False if s_no was
    already present (upstream occasionally emits a duplicate s_no --
    seen in practice -- which this silently and correctly dedupes)."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO issues
            (s_no, error_type, username, user_id, osm_location_link,
             changeset_id, changeset_link, osm_object_type, osm_object_id,
             time_utc, country, detail, status, date_fixed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'NYF', 'NYF')
        """,
        (
            row["s_no"], row["error_type"], row["username"], row["user_id"],
            row["osm_location_link"], row["changeset_id"], row["changeset_link"],
            row["osm_object_type"], row["osm_object_id"], row["time_utc"],
            row["country"], row["detail"],
        ),
    )
    return cur.rowcount > 0


def fetch_open_issues(conn, limit):
    """
    Oldest-checked-or-never-checked first, not lowest s_no first --
    this is what makes the recheck queue behave like a fair round-robin
    instead of always restarting from row 1. A row whose last_checked_utc
    is NULL (never actually evaluated yet) sorts before any row that has
    a real timestamp, and among timestamped rows the least-recently-
    checked goes first. See mark_deferred() vs mark_still_open() for how
    that timestamp only advances on a genuine evaluation, never on a skip.
    """
    cur = conn.execute(
        "SELECT * FROM issues WHERE status = 'NYF' "
        "ORDER BY COALESCE(last_checked_utc, '') ASC, s_no ASC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in cur.fetchall()]


def mark_fixed(conn, s_no, date_fixed_iso, fixed_user, fixed_changeset_id,
               fixed_changeset_link, new_osm_object_id, last_checked_iso, note=""):
    conn.execute(
        """
        UPDATE issues SET
            status = 'Fixed',
            date_fixed = ?,
            fixed_user = ?,
            fixed_changeset_id = ?,
            fixed_changeset_link = ?,
            new_osm_object_id = ?,
            last_checked_utc = ?,
            check_note = ?
        WHERE s_no = ?
        """,
        (date_fixed_iso, fixed_user, fixed_changeset_id, fixed_changeset_link,
         new_osm_object_id, last_checked_iso, note, s_no),
    )


def mark_still_open(conn, s_no, last_checked_iso, note=""):
    """Use this ONLY when a real recheck ran and genuinely confirmed the
    issue still holds -- this advances last_checked_utc, which pushes
    the row to the back of the round-robin queue (fair: it just had its
    turn). For anything that couldn't be evaluated this run (Overpass
    unreachable, circuit breaker open, inconclusive, unsupported type),
    use mark_deferred() instead so the row keeps its place in line."""
    conn.execute(
        "UPDATE issues SET last_checked_utc = ?, check_note = ? WHERE s_no = ?",
        (last_checked_iso, note, s_no),
    )


def mark_deferred(conn, s_no, note=""):
    """A row that was SKIPPED this run, not actually evaluated -- e.g.
    Overpass was unreachable, the circuit breaker was open, or the
    result was inconclusive/unsupported. Deliberately does NOT touch
    last_checked_utc, so this row keeps whatever (possibly NULL,
    possibly old) timestamp it already had and stays near the front of
    the round-robin queue next run instead of being wrongly treated as
    "just checked"."""
    conn.execute(
        "UPDATE issues SET check_note = ? WHERE s_no = ?",
        (note, s_no),
    )


def restore_from_snapshot(conn):
    """
    Rebuilds the issues table from the committed fix_status_snapshot_*.csv
    files, but ONLY if the local table is currently empty.

    This exists because GitHub Actions runners are ephemeral -- each
    hourly run gets a fresh checkout with no memory of the SQLite file
    any previous run wrote locally (it's gitignored on purpose, since
    committing a binary DB to git history is a bad idea). Without this,
    every run would start from scratch: re-ingesting every row as fresh
    NYF and wiping out every previously-recorded Fixed status.

    Since the snapshot CSVs ARE committed to git, they're present on
    every checkout, so this restores exactly where the last committed
    run left off before ingest/check run again.

    A self-hosted setup where tracker.db persists locally across runs
    (e.g. your own crontab) never hits the "empty" condition after its
    first run, so this is a no-op there -- the local DB stays authoritative
    and this never overwrites it with a potentially-older committed copy.
    """
    if max_ingested_s_no(conn) > 0:
        return 0  # local DB already has state -- treat it as authoritative

    import csv

    files = sorted(
        glob.glob(os.path.join(config.DATA_DIR, f"{config.SNAPSHOT_BASENAME}_*.csv")),
        key=lambda p: int(os.path.basename(p).rsplit("_", 1)[1].removesuffix(".csv")),
    )
    restored = 0
    for path in files:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO issues
                        (s_no, error_type, username, user_id, osm_location_link,
                         changeset_id, changeset_link, osm_object_type, osm_object_id,
                         time_utc, country, detail, status, date_fixed, fixed_user,
                         fixed_changeset_id, fixed_changeset_link, new_osm_object_id,
                         last_checked_utc, check_note)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["s_no"], row["error_type"], row["username"], row["user_id"],
                        row["osm_location_link"], row["changeset_id"], row["changeset_link"],
                        row["osm_object_type"], row["osm_object_id"], row["time_utc"],
                        row["country"], row["detail"], row["status"], row["date_fixed"],
                        row.get("fixed_user") or None, row.get("fixed_changeset_id") or None,
                        row.get("fixed_changeset_link") or None, row.get("new_osm_object_id") or None,
                        row.get("last_checked_utc") or None, row.get("check_note") or None,
                    ),
                )
                restored += 1
    conn.commit()
    return restored


def max_ingested_s_no(conn):
    cur = conn.execute("SELECT MAX(s_no) AS m FROM issues")
    row = cur.fetchone()
    return row["m"] or 0


def counts(conn):
    cur = conn.execute("SELECT status, COUNT(*) AS n FROM issues GROUP BY status")
    return {r["status"]: r["n"] for r in cur.fetchall()}


def export_snapshot_csv(conn):
    """
    Rewrites the full snapshot every run, split across
    fix_status_snapshot_1.csv, _2.csv, ... capped at
    config.SNAPSHOT_MAX_BYTES each -- same rotation convention as
    OSM_Quality_Check's own quality_check_N.csv, so a dashboard reading
    both series can treat them the same way.

    Since this is a full rewrite (not an append log), which row lands in
    which numbered file can shift slightly run to run as the table
    grows -- harmless, since every run regenerates every file from
    scratch anyway. Any leftover higher-numbered file from a run that
    needed more files than this one does gets cleaned up so stale
    duplicate data doesn't sit around forever.
    """
    import csv

    cur = conn.execute("SELECT * FROM issues ORDER BY s_no")
    rows = cur.fetchall()
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    os.makedirs(config.DATA_DIR, exist_ok=True)

    # Clean up the old, pre-rotation single-file snapshot if it's still
    # sitting there from before this was split into numbered files.
    legacy_path = os.path.join(config.DATA_DIR, f"{config.SNAPSHOT_BASENAME}.csv")
    if os.path.exists(legacy_path):
        os.remove(legacy_path)

    def _open(idx):
        path = os.path.join(config.DATA_DIR, f"{config.SNAPSHOT_BASENAME}_{idx}.csv")
        fh = open(path, "w", newline="", encoding="utf-8")
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        return fh, w

    file_index = 1
    fh, writer = _open(file_index)
    try:
        for r in rows:
            writer.writerow(dict(r))
            fh.flush()
            if fh.tell() >= config.SNAPSHOT_MAX_BYTES:
                fh.close()
                file_index += 1
                fh, writer = _open(file_index)
    finally:
        fh.close()

    # Remove any higher-numbered file left over from a previous run that
    # needed more files than this one does (shouldn't normally happen
    # since the table only grows, but kept as a safety net).
    for stale in glob.glob(os.path.join(config.DATA_DIR, f"{config.SNAPSHOT_BASENAME}_*.csv")):
        try:
            idx = int(os.path.basename(stale).rsplit("_", 1)[1].removesuffix(".csv"))
        except (ValueError, IndexError):
            continue
        if idx > file_index:
            os.remove(stale)
