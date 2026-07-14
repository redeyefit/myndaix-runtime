#!/usr/bin/env bash
# mxr-read.sh — the ONLY sanctioned way Watch observes the ledger (§3.2, §3.8, H1/HIGH-2).
# Validates a single job id, execs `mxr get` with FIXED argv (no shell re-parse), then runs the
# SAME sanitize_untrusted pipeline as read-inbox — `mxr get` returns agent reply bodies and
# execution logs derived from attacker-influenceable PR content, so its output is untrusted too.
#
#   mxr-read.sh <JOB_ID>          # JOB_ID = 8..36 hex/hyphen chars (a uuid or a short prefix)
#
# Pre-approved in settings.json. A bare `Bash(mxr get *)` is NOT pre-approved (shell-mediated
# metachar edges, H1) — this typed wrapper is the whole allowed surface.
set -euo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$DIR/watch-lib.sh"

[[ $# -eq 1 ]] || { echo "mxr-read: exactly one argument (a job id)" >&2; exit 2; }
id="$1"
[[ "$id" =~ ^[0-9a-fA-F-]{8,36}$ ]] || { echo "mxr-read: bad job id shape (expect 8-36 hex/hyphen)" >&2; exit 2; }

command -v mxr >/dev/null 2>&1 || { echo "mxr-read: mxr not on PATH" >&2; exit 3; }

watch_log "mxr-read ACCEPT id=$id"
# fixed argv: the id can never become a flag or a second word past the regex. Capture then fence
# (never stream an agent-controlled body straight to the model).
out="$(mxr get "$id" 2>&1 || true)"
printf '%s' "$out" | sanitize_untrusted "ledger:$id"
