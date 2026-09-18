#!/usr/bin/env bash
# proposer-signal.sh — launchd entry for the observe-only "skill class READY" email notifier.
# Reads state='ready' capture_candidate rows and emails Jefe ONCE per newly-ready class. Opens
# NO PR, never runs the proposer, mutates NOTHING in the ledger (READ-ONLY). IDLE (no-op, exit 0)
# until the send credential exists. Design: docs/proposer-ready-signal-design.md. Deploy: DEPLOY.md.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# liveness-fire: one unconditional stdout line per fire (mirrors the other tick wrappers).
printf '[%s] [proposer-signal] tick fire\n' "$(date '+%Y-%m-%d %H:%M:%S')"

# repo root = this wrapper's parent's parent (orchestrator/..), portable across machines.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# The send credential (a Gmail app-password) is a KEY=VALUE env file — SMTP_USER / SMTP_PASS /
# NOTIFY_TO — chmod 600. Its ABSENCE is the gate: without it we cannot deliver, so skip entirely
# rather than hit the ledger and log a pointless "send failed" every tick. Exit 0 so launchd
# doesn't thrash a job that is simply not provisioned yet.
CRED="${MYNDAIX_NOTIFY_CRED:-$HOME/.myndaix/.secrets/env/gmail-notify.env}"
if [[ ! -r "$CRED" ]]; then
  printf '[%s] [proposer-signal] no send credential at %s — notifier idle (drop the app-password there to activate)\n' \
    "$(date '+%Y-%m-%d %H:%M:%S')" "$CRED"
  exit 0
fi

# Fail-closed on a loosened secret: refuse anything more permissive than owner-only (a
# group/world-readable credential is a leak, not a config choice).
perms="$(stat -f '%A' "$CRED" 2>/dev/null || echo 000)"
if [[ "$perms" != "600" && "$perms" != "400" ]]; then
  printf '[%s] [proposer-signal] %s has perms %s (want 600/400) — refusing to load a loosened secret\n' \
    "$(date '+%Y-%m-%d %H:%M:%S')" "$CRED" "$perms"
  exit 0
fi

# Source ONLY this specific credential file (NEVER the .secrets dir). set -a exports the
# KEY=VALUEs into the env the python sender reads. Trusted operator-authored file.
set -a
# shellcheck disable=SC1090
. "$CRED"
set +a

# DSN pinned to 127.0.0.1 (not `localhost`), matching the other ticks.
export MYNDAIX_DSN="${MYNDAIX_DSN:-postgresql://127.0.0.1/runtime}"
export PYTHONPATH="src"
exec "$REPO/.venv/bin/python" -m runtime.proposersignal tick
