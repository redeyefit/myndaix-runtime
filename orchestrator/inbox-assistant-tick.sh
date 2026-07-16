#!/usr/bin/env bash
# inbox-assistant-tick.sh — launchd entrypoint for the Inbox Assistant morning brief.
# Runs ONE bounded tick (pull -> classify -> label/draft -> deliver), then exits (NOT a daemon).
# OFF unless INBOX_ACCOUNTS is set in the environment — the wrapper exits 0 BEFORE the
# secrets/keychain gates (and the module double-checks, exiting 0 on an empty list too).
#
# Safe first run (classify + print the brief to stdout, touch NOTHING — no labels, no drafts,
# no deliveries, no cursor writes):
#   MYNDAIX_INBOX_DRY_RUN=1 orchestrator/inbox-assistant-tick.sh
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# repo root = this script's parent dir's parent (orchestrator/..), so the wrapper is
# portable across machines without a hardcoded path.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# Component-off short-circuit BEFORE the .secrets sourcing and the Keychain gate: on a
# factory between merge and runbook execution INBOX_ACCOUNTS is unset and the keychain
# item doesn't exist — that must be ONE quiet line and exit 0, not a daily FATAL.
if [[ -z "${INBOX_ACCOUNTS:-}" ]]; then
  echo "[inbox-assistant] not configured (INBOX_ACCOUNTS empty) — exiting"
  exit 0
fi

# ~/.myndaix/.secrets provides CLAUDE_CODE_OAUTH_TOKEN for the classify subprocess
# (`claude -p`). The set -a bracket exports everything the file defines; close it so
# nothing sourced later leaks into auto-export. Values are secrets — never echoed,
# and xtrace stays off in this script.
if [[ -r "$HOME/.myndaix/.secrets" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$HOME/.myndaix/.secrets"
  set +a
fi

# OP_SERVICE_ACCOUNT_TOKEN gates every 1Password read (OAuth client, per-account refresh
# tokens, Notion token). It lives in the macOS login Keychain — never on disk. Fail CLOSED:
# without it every account fails auth anyway, so one clear line in the launchd log beats N
# confusing per-account failures downstream. NOT a `&&`-chain (see automerge-tick.sh: set -e
# exempts non-final links, so a failed read could silently export nothing).
if ! OP_SERVICE_ACCOUNT_TOKEN="$(security find-generic-password -s 'op.inbox-assistant.token' -w)" \
    || [[ -z "$OP_SERVICE_ACCOUNT_TOKEN" ]]; then
  echo "FATAL: op.inbox-assistant.token not readable from the login Keychain (security find-generic-password failed or empty) — inbox tick aborted" >&2
  exit 1
fi
export OP_SERVICE_ACCOUNT_TOKEN

# DSN default pinned to 127.0.0.1 (NOT `localhost`): on the Mini the IPv6 loopback [::1]
# wedges intermittently, so asyncpg's `localhost` resolution flips to ::1 and hangs the
# full connect timeout → the tick exits 1. 127.0.0.1 forces IPv4 loopback (harmless on the
# MacBook). Matches the automerge + serve + mxr pin (session 2026-07-12).
export MYNDAIX_DSN="${MYNDAIX_DSN:-postgresql://127.0.0.1/runtime}"
export PYTHONPATH="src"
exec "$REPO/.venv/bin/python" -m runtime.inbox_assistant tick
