"""
Pulls new rows from OSM_Quality_Check's data/quality_check_*.csv files
(read over plain HTTP from raw.githubusercontent.com -- no git clone,
no write access to that repo) and inserts any row this tracker hasn't
seen yet as a new NYF issue.

Only ever reads forward from the highest s_no already stored locally
-- this repo does not rescan from the beginning on every run, only
picks up what's new since the last run, per your instruction.
"""
import csv
import io
import logging
import requests

import config
import db

log = logging.getLogger(__name__)
HEADERS = {"User-Agent": "osm-qc-fix-tracker/1.0"}


def _fetch_csv_file(file_index):
    """Returns the raw CSV text for quality_check_{file_index}.csv, or
    None if that file doesn't exist (404 -- we've reached the end of
    the series)."""
    url = f"{config.SOURCE_REPO_RAW_BASE}/{config.SOURCE_CSV_BASENAME}_{file_index}.csv"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.text


def run(conn):
    """
    Walks quality_check_1.csv, _2.csv, ... until the first 404 (or the
    configured probe cap), inserting any row with s_no greater than
    what's already stored. Returns the number of new rows ingested.
    """
    already_have = db.max_ingested_s_no(conn)
    new_count = 0

    for file_index in range(1, config.SOURCE_CSV_PROBE_MAX + 1):
        text = _fetch_csv_file(file_index)
        if text is None:
            log.info("quality_check_%d.csv not found -- stopping (reached end of series)", file_index)
            break

        reader = csv.DictReader(io.StringIO(text))
        file_new = 0
        for row in reader:
            try:
                s_no = int(row["s_no"])
            except (KeyError, ValueError):
                continue
            if s_no <= already_have:
                continue
            if db.insert_new_issue(conn, row):
                file_new += 1
                new_count += 1
            else:
                log.info("s_no %d: duplicate in source CSV, skipped (already inserted)", s_no)

        log.info("quality_check_%d.csv: %d new row(s) ingested", file_index, file_new)

    conn.commit()
    log.info("Ingest complete: %d new issue(s) added (previously had up to s_no=%d)",
              new_count, already_have)
    return new_count
