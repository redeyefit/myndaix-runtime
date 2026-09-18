#!/usr/bin/env bash
# proposer-signal-test.sh — fast gate-logic smoke for proposer-signal.sh (~1s). The module's
# dedup logic lives in tests/test_proposer_signal.py; this covers the WRAPPER's two fail-safe
# branches only: idle when no credential exists, and refuse a loosened-perms credential. Neither
# branch reaches the ledger or python, so no DB is needed.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAP="$HERE/proposer-signal.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "FAIL: $1"; exit 1; }

# 1. no credential -> idle, exit 0, never reaches python/ledger
out="$(MYNDAIX_NOTIFY_CRED="$TMP/nope.env" bash "$WRAP" 2>&1)" || fail "idle path exited nonzero"
echo "$out" | grep -q "notifier idle" || fail "no-credential did not report idle"
echo "PASS idle-when-no-credential"

# 2. loosened perms (644) -> refuse to load, exit 0
cred="$TMP/loose.env"
printf 'SMTP_USER=x\nSMTP_PASS=y\nNOTIFY_TO=z\n' > "$cred"
chmod 644 "$cred"
out="$(MYNDAIX_NOTIFY_CRED="$cred" bash "$WRAP" 2>&1)" || fail "perms-refuse path exited nonzero"
echo "$out" | grep -q "refusing to load a loosened secret" || fail "loosened cred was not refused"
echo "PASS refuse-loosened-credential"

echo "ALL PASS (2)"
