# OSM_QC_Fix_Tracker

Tracks whether issues already identified by
[`OSM_Quality_Check`](https://github.com/KingVik-Planet/OSM_Quality_Check)
have since been fixed on OpenStreetMap.

This repo is **read-only** with respect to `OSM_Quality_Check`: it never
clones it, never writes to it, and only reads its committed
`data/quality_check_*.csv` files over plain HTTP (raw.githubusercontent.com).
It does **not** rescan OSM from scratch -- it only ever looks at the exact
osm objects that `OSM_Quality_Check` already flagged, and asks Overpass /
the OSM API "does this specific condition still hold right now?"

## What it stores

One row per issue, keyed by its original `s_no` (continuous and unique
across every `quality_check_N.csv` file, per the source repo's own
numbering rule), with the original columns plus:

| column | meaning |
|---|---|
| `status` | `Fixed` or `NYF` |
| `date_fixed` | ISO date the fix was detected, or `NYF` while still open |
| `fixed_user` | OSM username of whoever made the fixing edit |
| `fixed_changeset_id` / `fixed_changeset_link` | the changeset that produced the current, fixed state |
| `new_osm_object_id` | only set if the object was deleted and manually confirmed to have been replaced by a new one -- **not auto-detected**, see Limitations |
| `last_checked_utc` | when this row was last rechecked |
| `check_note` | why it's still NYF / inconclusive / unsupported, for anything not a clean fix |

Stored in `data/tracker.db` (SQLite). A flat, spreadsheet-friendly
snapshot is exported to `data/fix_status_snapshot.csv` after every run,
for feeding into a dashboard or the existing report the same way
`OSM_Quality_Check`'s own CSVs are read.

## How a run works (`python main.py`)

1. **`ingest.py`** -- walks `quality_check_1.csv`, `_2.csv`, ... from the
   source repo until the first 404, inserting only rows whose `s_no` is
   greater than what's already stored. Never reprocesses old rows.
2. **`checker.py`** -- takes up to `QCFIX_MAX_CHECK_PER_RUN` (default 300)
   still-open (`NYF`) issues, batches every Overpass lookup they need
   into as few HTTP calls as possible, and re-applies the *same rule*
   that originally flagged each one (ported from `checks.py`) against
   OSM's current data. If the condition no longer reproduces, the row
   flips to `Fixed` and its fix columns are filled in from the object's
   current version metadata (or, if the object was deleted, from its
   edit history) -- no separate `/history` call needed for objects that
   still exist, since Overpass's `out meta;` already returns the
   changeset/user/timestamp of whichever edit last touched them.

## Resilience (mirrors `OSM_Quality_Check`'s `fetch.py`)

- Each Overpass endpoint is tried with a bounded number of retries,
  falling back to a mirror before giving up.
- A **circuit breaker** trips after a run's first full Overpass failure
  and stops calling it again for the rest of that run -- everything
  still queued gets deferred instead of paying a doomed retry cost.
- A **per-run cap** (`QCFIX_MAX_CHECK_PER_RUN`) bounds worst-case runtime
  even when Overpass is healthy but the backlog is large, so this hour's
  run always finishes in bounded time -- leftovers wait for the next run.
- Deferred/unreachable rows stay `NYF` with a `check_note` explaining why,
  never guessed at.

## Recheck rules ported so far

Ported 1:1 (same thresholds, same geometry logic) from `checks.py`:
`duplicated node`, `feature mapped without primary tag`, `wrong tagging`,
`node connected highway and building`, `crossing buildings`,
`overlapping buildings`, `building inside building`, `crossing highway`,
`crossing way`, `overlapping highway`, `broken highway continuity`,
`duplicated way`, `untagged way`, `floating highway`.

**Not yet portable** as an object-state recheck, since they're
changeset-scoped rather than tied to one lasting osm object:
`mass delete without revert tag`, `mass upload (create/modify)`,
`unclear changeset comment`, `dense node cluster`,
`sudden highway classification change`, `way end node near other way`.
Rows of these types stay `NYF` with `check_note = "no recheck rule ported
for '<type>' yet"` rather than being silently mis-scored -- flag if you
want any of these added.

## Setup

```bash
git clone <this-repo>
cd OSM_QC_Fix_Tracker
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py   # one manual run
```

## Running hourly via cron

The GitHub Actions schedule in `.github/workflows/hourly.yml` is
**commented out on purpose** -- you're running this from your own
crontab instead. Add this line (adjust the venv path):

```
0 * * * * cd /path/to/OSM_QC_Fix_Tracker && /path/to/venv/bin/python3 main.py >> data/cron.log 2>&1
```

Notes:
- Use the venv's own `python3` (`which python3` after activating it),
  not the bare system one -- cron doesn't source your shell profile.
- `cd`-ing into the repo first keeps the relative imports (`config`,
  `db`, etc.) resolving the same way they do when you run it by hand.
- `data/cron.log` accumulates the same style of run-summary lines you'd
  see running it manually -- tail it to watch circuit-breaker/deferred
  messages the same way you would in `OSM_Quality_Check`'s own log.

## Configuration

Every setting can be overridden with an environment variable -- see
`config.py` for the full list. The one you'll most likely need to
change is which repo/branch this reads from:

```
QCFIX_SOURCE_RAW_BASE=https://raw.githubusercontent.com/KingVik-Planet/OSM_Quality_Check/master/data
```

## Known limitations

- **`new_osm_object_id` is manual.** If a mapper fixes an issue by
  deleting the flagged object and redrawing a new one instead of editing
  it in place, OSM has no link between the old and new id. This tracker
  correctly marks the row `Fixed` (the old object is gone), but leaves
  `new_osm_object_id` blank -- fill it in by hand if you track that case.
- Un-ported changeset-scoped issue types (listed above) are never
  auto-rechecked; they stay `NYF` until a rule is added for them.
- `fixed_user`/`fixed_changeset_id` reflect the *current* version's
  edit, which is normally the fix -- but if an object was edited several
  times between being flagged and being rechecked, only the latest edit
  is captured, not necessarily the one that specifically fixed it.
