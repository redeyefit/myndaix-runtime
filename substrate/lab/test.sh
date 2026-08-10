#!/bin/bash
# test.sh — tailnet-watch. Hermetic: probes + notifications shimmed via PATH/env,
# state in a scratch MYNDAIX_HOME. Run: bash substrate/lab/test.sh
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

SCRIPT="$(cd "$(dirname "$0")" && pwd)/tailnet-watch.sh"
SCRATCH="$(mktemp -d /tmp/tw-test.XXXXXX)"
trap 'rm -rf "$SCRATCH"' EXIT INT TERM
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  ok: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

# ---- shims ----
mkdir -p "$SCRATCH/bin" "$SCRATCH/home"
cat > "$SCRATCH/bin/nc" << 'EOF'
#!/bin/bash
exit "${TW_TEST_NC_EXIT:-0}"
EOF
cat > "$SCRATCH/bin/osascript" << 'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "${TW_TEST_NOTIFY_LOG:?}"
EOF
cat > "$SCRATCH/bin/ts-stub" << 'EOF'
#!/bin/bash
exit 1
EOF
chmod +x "$SCRATCH/bin/nc" "$SCRATCH/bin/osascript" "$SCRATCH/bin/ts-stub"

run_tick() {  # $1 = nc exit code for this tick
  MYNDAIX_HOME="$SCRATCH/home" \
  TW_HOST="203.0.113.1" \
  TW_TS_BIN="$SCRATCH/bin/ts-stub" \
  TW_NC_BIN="$SCRATCH/bin/nc" \
  TW_OSA_BIN="$SCRATCH/bin/osascript" \
  TW_TEST_NC_EXIT="$1" \
  TW_TEST_NOTIFY_LOG="$SCRATCH/notifications.log" \
  bash "$SCRIPT"
}
STATE="$SCRATCH/home/state/tailnet-watch.state"
NOTES="$SCRATCH/notifications.log"; : > "$NOTES"

echo "1. happy path — reachable, no alert, clean state"
run_tick 0 || bad "tick errored"
grep -q '^fails=0$' "$STATE" && ok "state reset" || bad "state not reset"
[ -s "$NOTES" ] && bad "unexpected notification" || ok "no notification"

echo "2. below threshold — 2 failures, no alert"
run_tick 1; run_tick 1
grep -q '^fails=2$' "$STATE" && ok "fails=2 persisted" || bad "fails wrong: $(cat "$STATE")"
[ -s "$NOTES" ] && bad "premature alert" || ok "no premature alert"

echo "3. threshold hit — 3rd failure alerts exactly once"
run_tick 1
grep -c 'unreachable' "$NOTES" | grep -qx 1 && ok "one alert" || bad "alert count: $(grep -c 'unreachable' "$NOTES")"

echo "4. dedup — 4th/5th failures do NOT re-alert within window"
run_tick 1; run_tick 1
grep -c 'unreachable' "$NOTES" | grep -qx 1 && ok "still one alert" || bad "re-alerted too soon"

echo "5. re-alert after window — aged alerted_at fires again"
python3 - "$STATE" << 'PYEOF'
import sys, re
p = sys.argv[1]; s = open(p).read()
s = re.sub(r'alerted_at=\d+', 'alerted_at=1', s)
open(p, 'w').write(s)
PYEOF
run_tick 1
grep -c 'unreachable' "$NOTES" | grep -qx 2 && ok "daily re-alert" || bad "no re-alert after window"

echo "6. recovery — heal notifies once and resets"
run_tick 0
grep -q 'reachable again' "$NOTES" && ok "recovery notice" || bad "no recovery notice"
grep -q '^fails=0$' "$STATE" && ok "state reset on heal" || bad "state not reset"
run_tick 0
grep -c 'reachable again' "$NOTES" | grep -qx 1 && ok "no duplicate recovery" || bad "duplicate recovery"

echo "7. corrupt state — garbage values fall back safe (octal trap incl.)"
printf 'fails=08\nalerted_at=banana\nfirst_fail_at=\n' > "$STATE"
run_tick 0 || bad "corrupt state crashed the tick"
grep -q '^fails=0$' "$STATE" && ok "corrupt state recovered" || bad "corrupt state persisted"

echo "7b. numeric-corrupt state — future alerted_at cannot suppress alerts"
printf 'fails=99\nalerted_at=99999999999\nfirst_fail_at=99999999999\n' > "$STATE"
run_tick 1 || bad "numeric-corrupt state crashed the tick"
grep -c 'unreachable' "$NOTES" | grep -qx 3 && ok "future alerted_at clamped, alert fired" || bad "future alerted_at suppressed alert"
: > "$NOTES"; run_tick 0 > /dev/null; : > "$NOTES"

echo "7c. tailscale fallback — nc fails but ts-ping succeeds = reachable"
cat > "$SCRATCH/bin/ts-ok" << 'EOF'
#!/bin/bash
exit 0
EOF
chmod +x "$SCRATCH/bin/ts-ok"
MYNDAIX_HOME="$SCRATCH/home" TW_HOST="203.0.113.1" TW_TS_BIN="$SCRATCH/bin/ts-ok" \
  TW_NC_BIN="$SCRATCH/bin/nc" TW_OSA_BIN="$SCRATCH/bin/osascript" TW_TEST_NC_EXIT=1 \
  TW_TEST_NOTIFY_LOG="$NOTES" bash "$SCRIPT" || bad "ts-fallback tick errored"
grep -q '^fails=0$' "$STATE" && ok "ts-ping fallback counts as reachable" || bad "fallback ignored"
[ -s "$NOTES" ] && bad "fallback tick notified" || ok "no notification on fallback success"

echo "8. structural — no GNU/BSD stat trap, no unquoted probe vars"
grep -q 'stat -c' "$SCRIPT" && bad "GNU stat crept in" || ok "no stat portability trap"
grep -q '10#\$v' "$SCRIPT" && ok "base-10 normalization present" || bad "10# guard missing"

echo; echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
