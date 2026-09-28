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

Stored in `data/tracker.db` (SQLite) -- which is intentionally **not**
committed to git (binary diffs bloat repo history badly). Since GitHub
Actions runners are ephemeral, each hourly run starts with an empty
`tracker.db` and rebuilds it from the last committed
`fix_status_snapshot_*.csv` before doing anything else
(`db.restore_from_snapshot`) -- so state survives across runs via the
CSVs, which *are* committed, without ever putting the raw DB file in git.
A self-hosted setup where `tracker.db` persists locally between runs
skips this restore automatically (it only fires when the local table is
empty), so the local DB stays authoritative there instead.

A flat, spreadsheet-friendly
snapshot is exported to `data/fix_status_snapshot_1.csv`, `_2.csv`, ...
(rotating at 40MB per file, same convention as `quality_check_N.csv`)
after every run,
for feeding into a dashboard or the existing report the same way
`OSM_Quality_Check`'s own CSVs are read.

## How a run works (`python main.py`)

1. **`ingest.py`** -- walks `quality_check_1.csv`, `_2.csv`, ... from the
   source repo until the first 404, inserting only rows whose `s_no` is
   greater than what's already stored. Never reprocesses old rows.
2. **`checker.py`** -- takes up to `QCFIX_MAX_CHECK_PER_RUN` (default 300)
   still-open (`NYF`) issues, **oldest-checked-or-never-checked first**
   (a fair round-robin, not always row 1 onward -- see below), batches
   every Overpass lookup they need into as few HTTP calls as possible,
   and re-applies the *same rule* that originally flagged each one
   (ported from `checks.py`) against OSM's current data. If the
   condition no longer reproduces, the row flips to `Fixed` and its fix
   columns are filled in from the object's current version metadata (or,
   if the object was deleted, from its edit history) -- no separate
   `/history` call needed for objects that still exist, since Overpass's
   `out meta;` already returns the changeset/user/timestamp of whichever
   edit last touched them.

### Why a round-robin queue, not "always start from row 1"

If Overpass has a bad run and only gets through the first 50 of 300
rows before the circuit breaker trips, restarting from row 1 next time
would mean rows past #300 could go unchecked indefinitely -- a genuinely
fixed issue sitting further down the list would never get the chance to
be marked `Fixed`. Instead, `fetch_open_issues()` orders by
`last_checked_utc` (never-checked/NULL first, then oldest-checked), and
that timestamp only advances via `mark_still_open()` when a row was
*actually* evaluated. A row that was merely skipped this run (Overpass
unreachable, circuit breaker open, inconclusive result) goes through
`mark_deferred()` instead, which leaves its timestamp untouched -- so it
stays near the front of the queue and gets first priority next run,
rather than being wrongly treated as "just had its turn". Every row gets
a fair rotation through the queue instead of some rows monopolizing
every run's attention while others starve behind them.

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

## Running hourly via cron-job.org (recommended -- no token or script on any server)

This is the approach actually in use: an external scheduler
([cron-job.org](https://cron-job.org)) makes an hourly HTTP POST
straight to GitHub's API, which starts the `hourly-fix-check` GitHub
Actions workflow (`.github/workflows/hourly.yml`) the same way clicking
"Run workflow" would. That workflow runs `main.py` and commits+pushes
`data/` using GitHub Actions' own token -- nothing lives on any server
you manage, and no token is ever stored in this repo.

Requires **Settings → Actions → General → Workflow permissions** set to
"Read and write permissions" (see "Known limitations" below for why),
and a GitHub PAT with `repo` scope entered directly into cron-job.org's
own job configuration -- never into any file here. Full click-by-click
setup: ask Claude, or see cron-job.org's job pointed at

```
POST https://api.github.com/repos/KingVik-Planet/OSM_QC_Fix_Tracker.tt_event/actions/workflows/hourly.yml/dispatches
Headers: Authorization: Bearer <PAT>, Accept: application/vnd.github+json, X-GitHub-Api-Version: 2022-11-28
Body: {"ref":"master"}
Schedule: every hour
```

## Alternative: running hourly via your own crontab

If you'd rather run this on a server you control instead of via
cron-job.org, `run_and_commit.sh` is provided for that -- it runs
`main.py` then commits/pushes using a PAT you set as `QCFIX_GH_PAT` in
your own crontab line (never inside this repo). Not needed if you're
using the cron-job.org approach above, since GitHub Actions' own token
already handles the push in that case.

**One-time setup:**
1. Create a GitHub Personal Access Token (classic: `repo` scope;
   fine-grained: `Contents: Read and write`) scoped to this repo.
2. Make sure this repo's git remote uses `https://`, not `ssh://` (check
   with `git remote -v` -- the wrapper script rewrites the https URL to
   inject the token; switch remotes with
   `git remote set-url origin https://github.com/OWNER/REPO.git` if needed).
3. Set the token wherever cron will see it -- easiest is right in the
   crontab line itself (below), or in a file this user's shell sources.

**Crontab line:**
```
0 * * * * QCFIX_GH_PAT=ghp_xxxxxxxxxxxxxxxxxxxx /path/to/OSM_QC_Fix_Tracker/run_and_commit.sh >> /path/to/OSM_QC_Fix_Tracker/data/cron.log 2>&1
```

`run_and_commit.sh` runs `main.py`, then commits and pushes `data/` using
that token -- skipping the commit harmlessly if nothing changed, and
skipping it (with a log line, not a crash) if `QCFIX_GH_PAT` isn't set
at all, so you can also run it locally without ever pushing.

Notes:
- Set `QCFIX_PYTHON_BIN` (env var) if your venv's `python3` isn't at the
  default `./venv/bin/python3` the script assumes.
- `data/cron.log` accumulates the same style of run-summary lines you'd
  see running it manually -- tail it to watch circuit-breaker/deferred
  messages the same way you would in `OSM_Quality_Check`'s own log.
- The `.github/workflows/hourly.yml` file is still there and still works
  for manual `workflow_dispatch` test runs from the Actions tab -- just
  make sure **Settings → Actions → General → Workflow permissions** is
  set to "Read and write permissions" if you ever use it that way,
  since that setting overrides the `permissions:` block in the YAML.

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
