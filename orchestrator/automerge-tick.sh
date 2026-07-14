#!/usr/bin/env bash
# automerge-tick.sh — launchd entrypoint for the docs-only PR auto-merge gate (rung 4).
# Runs ONE bounded tick, then exits (NOT a daemon). OFF unless $ORCH/AUTOMERGE_ENABLED exists.
#
# Safe first run (decide + log, merge NOTHING):
#   MYNDAIX_AUTOMERGE_DRY_RUN=1 orchestrator/automerge-tick.sh
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# GH_TOKEN: a fine-grained PAT for the launchd context — `gh`'s keyring token is unreadable
# by a launchd agent. Provision a chmod-600 secret with exactly: Contents r/w + Pull requests
# r/w + Checks r + Metadata r on the target repo. (Deploy prereq; see docs/automerge-design.md §0.)
TOKEN_FILE="${MYNDAIX_AUTOMERGE_TOKEN_FILE:-$HOME/.myndaix/.automerge-token}"
[[ -r "$TOKEN_FILE" ]] && GH_TOKEN="$(tr -d '\r\n' < "$TOKEN_FILE")" && export GH_TOKEN

export MYNDAIX_DSN="${MYNDAIX_DSN:-postgresql://localhost/runtime}"
export PYTHONPATH="src"
exec "$REPO/.venv/bin/python" -m runtime.automerge tick
