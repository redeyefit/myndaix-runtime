#!/usr/bin/env bash
# proposer-tick.sh — launchd entrypoint for the S7 proposer (auto-capture's acting rung).
# Runs ONE bounded tick (reconcile + reap + propose), then exits (NOT a daemon). OFF unless
# $ORCH/PROPOSER_ENABLED exists. Design: docs/auto-capture-design.md (v0.6). Deploy: DEPLOY.md.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# liveness-fire: one unconditional stdout line per fire (a liveness watcher reads this .out mtime).
printf '[%s] [proposer-tick] tick fire\n' "$(date '+%Y-%m-%d %H:%M:%S')"

# repo root = this wrapper's parent dir's parent (orchestrator/..), portable across machines.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# The proposer authenticates to GitHub with its OWN dedicated bot PAT (N2: a SEPARATE identity from
# the automerge writer, NOT in the automerge author-allowlist). NOT a `&&`-chain: under set -e a
# non-final command in `A && B` is exempt, so the if-block runs `export` explicitly when readable.
TOKEN_FILE="${MYNDAIX_PROPOSER_TOKEN_FILE:-$HOME/.myndaix/.proposer-token}"
if [[ -r "$TOKEN_FILE" ]]; then
  GH_TOKEN="$(tr -d '\r\n' < "$TOKEN_FILE")"
  export GH_TOKEN
fi

# DSN default pinned to 127.0.0.1 (NOT `localhost`), matching the other ticks.
export MYNDAIX_DSN="${MYNDAIX_DSN:-postgresql://127.0.0.1/runtime}"
export PYTHONPATH="src"
exec "$REPO/.venv/bin/python" -m runtime.proposer tick
