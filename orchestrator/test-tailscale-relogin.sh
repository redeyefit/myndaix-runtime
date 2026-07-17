#!/bin/bash
# test-tailscale-relogin.sh — hermetic smoke + safety harness for tailscale-relogin.sh.
# Stubs the `tailscale` CLI and drives every state path through throwaway temp dirs; never
# touches the real tailnet, ~/.myndaix, or launchd.
set -uo pipefail   # NOT -e: every check runs; tally pass/fail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON="$HERE/tailscale-relogin.sh"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0
ok(){ if eval "$1"; then echo "  ok: $2"; pass=$((pass+1)); else echo "  XX: $2"; fail=$((fail+1)); fi; }

# ---- fake tailscale CLI: `status --json` prints BackendState from FAKE_STATE; `up` exits FAKE_UP_RC.
# FAKE_STATE=UNREACHABLE -> `status` exits 1 with no output (dead/zombie daemon).
FAKE="$TMP/bin/tailscale"; mkdir -p "$TMP/bin"
cat > "$FAKE" <<'FEOF'
#!/bin/bash
if [ "$1" = "status" ]; then
  if [ "${FAKE_STATE:-Running}" = "UNREACHABLE" ]; then exit 1; fi
  printf '{"BackendState":"%s"}\n' "${FAKE_STATE:-Running}"
  exit 0
fi
if [ "$1" = "up" ]; then exit "${FAKE_UP_RC:-0}"; fi
exit 0
FEOF
chmod +x "$FAKE"

# a run helper: fresh home per scenario unless $KEEP given; returns the daemon's exit code
HOME_N=0
run(){ # run STATE [extra env assignments...]
  local state="$1"; shift
  local h="$TMP/home$HOME_N"; HOME_N=$((HOME_N+1))
  mkdir -p "$h/.myndaix/state" "$h/.myndaix/orchestrator" "$h/.myndaix/bridge/inbox/jefe"
  LAST_HOME="$h"
  env FAKE_STATE="$state" "$@" \
    TS_RELOGIN_CLI="$FAKE" \
    TS_RELOGIN_SECRETS="$h/.secrets" \
    TS_RELOGIN_STATE_DIR="$h/.myndaix/state" \
    TS_RELOGIN_OPERATOR_INBOX="$h/.myndaix/bridge/inbox/jefe" \
    HOME="$h" \
    /bin/bash "$DAEMON" > "$h/run.out" 2>&1
  return $?
}
alerts(){ ls "$LAST_HOME/.myndaix/bridge/inbox/jefe"/$1-*.md 2>/dev/null | wc -l | tr -d ' '; }

echo "== Running: healthy, no action =="
run Running
ok '[[ $? -eq 0 ]]' "Running -> exit 0"
ok '[[ ! -e "$LAST_HOME/.myndaix/state/ts-relogin-last-attempt" ]]' "Running -> no relogin attempt"

echo "== logged out below threshold: streak bumps, no up =="
H="$TMP/persist"; mkdir -p "$H/.myndaix/state" "$H/.myndaix/orchestrator" "$H/.myndaix/bridge/inbox/jefe"
printf 'TAILSCALE_AUTHKEY=tskey-fake-123\n' > "$H/.secrets"
common=(TS_RELOGIN_CLI="$FAKE" TS_RELOGIN_SECRETS="$H/.secrets" TS_RELOGIN_STATE_DIR="$H/.myndaix/state" TS_RELOGIN_OPERATOR_INBOX="$H/.myndaix/bridge/inbox/jefe" TS_RELOGIN_DRY_RUN=1 HOME="$H")
env FAKE_STATE=NeedsLogin "${common[@]}" /bin/bash "$DAEMON" > "$H/r1.out" 2>&1
ok '[[ "$(cat "$H/.myndaix/state/ts-relogin-streak")" == "1" ]]' "logged-out tick1 -> streak=1"
ok '[[ ! -e "$H/.myndaix/state/ts-relogin-last-attempt" ]]' "tick1 below threshold(2) -> no up attempt"

echo "== logged out AT threshold with key: DRY-RUN attempts (redacted), key never logged =="
env FAKE_STATE=NeedsLogin "${common[@]}" /bin/bash "$DAEMON" > "$H/r2.out" 2>&1
ok '[[ -e "$H/.myndaix/state/ts-relogin-last-attempt" ]]' "tick2 at threshold -> up attempt recorded"
ok 'grep -q "redacted" "$H/r2.out"' "the up command is logged with the key REDACTED"
ok '! grep -q "tskey-fake-123" "$H/r2.out" "$H/.myndaix/orchestrator/tailscale-relogin.log"' \
   "the auth key value NEVER appears in any log (security)"

echo "== cooldown: a third immediate tick does NOT re-attempt =="
cp "$H/.myndaix/state/ts-relogin-last-attempt" "$H/last.bak"
env FAKE_STATE=NeedsLogin "${common[@]}" /bin/bash "$DAEMON" > "$H/r3.out" 2>&1
ok 'grep -qi "cooldown" "$H/r3.out"' "within cooldown -> skips the up attempt"
ok '[[ "$(cat "$H/.myndaix/state/ts-relogin-last-attempt")" == "$(cat "$H/last.bak")" ]]' \
   "cooldown skip did not overwrite the last-attempt timestamp"

echo "== logged out, NO key: fail-closed + loud alert =="
run NeedsLogin TS_RELOGIN_THRESHOLD=1
rc=$?
ok '[[ '"$rc"' -ne 0 ]]' "logged out with no key -> non-zero exit (fail closed)"
ok '[[ "$(alerts ts-nokey-alert)" == "1" ]]' "a missing-key operator alert was dropped"

echo "== unreachable tailscaled (zombie): alert, never runs up =="
run UNREACHABLE
ok '[[ $? -eq 0 ]]' "zombie daemon -> exit 0 (not fatal)"
ok '[[ "$(alerts ts-zombie-alert)" == "1" ]]' "zombie daemon -> distinct reboot-needed alert"
ok '[[ ! -e "$LAST_HOME/.myndaix/state/ts-relogin-last-attempt" ]]' "zombie -> NO up attempt (up cannot fix it)"

echo "== NeedsMachineAuth: alert, no up =="
run NeedsMachineAuth
ok '[[ "$(alerts ts-machineauth-alert)" == "1" ]]' "NeedsMachineAuth -> admin-approval alert"
ok '[[ ! -e "$LAST_HOME/.myndaix/state/ts-relogin-last-attempt" ]]' "NeedsMachineAuth -> no up attempt"

echo "== shell hygiene =="
ok 'bash -n "'"$DAEMON"'"' "bash -n tailscale-relogin.sh"
HAVE_SC=1; command -v shellcheck >/dev/null 2>&1 || { HAVE_SC=0; echo "  --: SKIP shellcheck (absent)"; }
[[ "$HAVE_SC" == 1 ]] && ok 'shellcheck -S warning "'"$DAEMON"'" >/dev/null 2>&1' "shellcheck clean"
ok 'bash -n "'"$HERE/test-tailscale-relogin.sh"'"' "bash -n test-tailscale-relogin.sh"

echo "=================================================="
echo "  tailscale-relogin test: $pass ok, $fail fail"
echo "=================================================="
[[ "$fail" -eq 0 ]]
