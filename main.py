"""
Entry point for one hourly run:
  1. ingest.run()  -- pull any new rows from OSM_Quality_Check's CSVs
  2. checker.run() -- recheck open issues against current OSM data

Run manually with: python main.py
Run hourly via cron -- see README.md for the exact crontab line.
"""
import logging
import sys

import db
import ingest
import checker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def main():
    conn = db.connect()
    try:
        restored = db.restore_from_snapshot(conn)
        if restored:
            log.info("Restored %d row(s) from committed snapshot (fresh checkout, local DB was empty)", restored)
        new_count = ingest.run(conn)
        result = checker.run(conn)
        status_counts = db.counts(conn)
        log.info(
            "Run summary: %d new issue(s) ingested | checked=%d fixed=%d still_open=%d "
            "deferred=%d | totals: %s",
            new_count, result["checked"], result["fixed"], result["still_open"],
            result["deferred"], dict(status_counts),
        )
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("Run failed with an unhandled error")
        sys.exit(1)
