#!/usr/bin/env bash
# Wrapper for cron: runs one tracker cycle, then commits + pushes the
# updated data/ back to GitHub -- driven by this server's own cron, not
# GitHub Actions, so it authenticates with a Personal Access Token (PAT)
# instead of the (Actions-only) github-actions[bot] token that hit the
# 403 permission error.
#
# Setup, once:
#   1. Create a GitHub PAT with "repo" scope (classic) or
#      "Contents: Read and write" (fine-grained), scoped to this repo.
#   2. Export it wherever this script can see it -- e.g. add to this
#      user's crontab environment, or a .env file sourced below:
#        export QCFIX_GH_PAT=ghp_xxxxxxxxxxxxxxxxxxxx
#   3. Make sure the repo's remote uses HTTPS (not SSH), since the PAT
#      is injected into the HTTPS URL below.
#
# Crontab line (see README.md):
#   0 * * * * /path/to/OSM_QC_Fix_Tracker/run_and_commit.sh >> /path/to/OSM_QC_Fix_Tracker/data/cron.log 2>&1

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

# Adjust to wherever your venv actually lives:
PYTHON_BIN="${QCFIX_PYTHON_BIN:-$REPO_DIR/venv/bin/python3}"

echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) run start ==="

"$PYTHON_BIN" main.py

if [ -z "${QCFIX_GH_PAT:-}" ]; then
  echo "QCFIX_GH_PAT not set -- skipping commit/push (ran tracker only)"
  exit 0
fi

# Nothing to commit if this run touched no rows -- don't fail on that.
if git diff --quiet -- data/ && git diff --cached --quiet -- data/; then
  echo "No changes in data/ this run -- nothing to commit"
  exit 0
fi

REMOTE_URL="$(git config --get remote.origin.url)"
# Rewrite https://github.com/OWNER/REPO.git -> https://x-access-token:TOKEN@github.com/OWNER/REPO.git
AUTH_URL="$(echo "$REMOTE_URL" | sed -E "s#https://#https://x-access-token:${QCFIX_GH_PAT}@#")"

git config user.name "osm-qc-fix-tracker-cron"
git config user.email "osm-qc-fix-tracker-cron@localhost"

git add data/
git commit -m "Hourly fix-check run $(date -u +%Y-%m-%dT%H:%M:%SZ)"
git push "$AUTH_URL" HEAD:master

echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) run end ==="
