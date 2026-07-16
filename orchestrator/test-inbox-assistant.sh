#!/bin/bash
# test-inbox-assistant.sh — smoke test for inbox-assistant-tick.sh (the launchd wrapper).
# Proves the wrapper (a) parses, (b) FAILS CLOSED with a loud stderr line when the 1Password
# service-account token is missing OR empty in the login Keychain, and (c) honours the
# component-off contract: INBOX_ACCOUNTS unset -> exit 0 "not configured", with zero side
# effects (no op, no claude, no jefe drop).
#
# Isolation: every run gets env -i + a sandbox HOME (the live ~/.myndaix/.secrets is never
# sourced, no live jefe drop / launchd / network is ever touched) and INBOX_ACCOUNTS is never
# set, so the python module exits before any op/claude subprocess could fire. `op`, `claude`
# and `security` are stubbed as PATH-prepended fake binaries in the sandbox; the tick script
# re-prepends the system dirs to PATH (launchd hygiene), so a PATH stub can never shadow the
# real /usr/bin/security INSIDE it — the `security` stub therefore ALSO rides in as an
# exported function via BASH_ENV (bash resolves functions before PATH).
# Run: bash orchestrator/test-inbox-assistant.sh
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
DIR="$(cd "$(dirname "$0")" && pwd)"
TICK="$DIR/inbox-assistant-tick.sh"
REPO="$(cd "$DIR/.." && pwd)"
PASS=0; FAIL=0
ok(){ if [ "$1" = "1" ]; then PASS=$((PASS+1)); else FAIL=$((FAIL+1)); echo "  FAIL: $2"; fi; }

SBOX="$(mktemp -d "${TMPDIR:-/tmp}/inbox-assistant-test.XXXXXX")"
trap 'rm -rf "$SBOX"' EXIT
mkdir -p "$SBOX/bin" "$SBOX/home"

# --- stub binaries (fail CLOSED: any invocation that slips through is itself a bug) ---
cat > "$SBOX/bin/op" <<'STUB'
#!/bin/bash
echo "stub op invoked — no test may reach a real vault: $*" >&2; exit 1
STUB
cat > "$SBOX/bin/claude" <<'STUB'
#!/bin/bash
echo "stub claude invoked — no test may reach a real model" >&2; exit 1
STUB
cat > "$SBOX/bin/security" <<'STUB'
#!/bin/bash
[ -n "${STUB_NO_TOKEN:-}" ] && exit 1
[ -n "${STUB_EMPTY_TOKEN:-}" ] && exit 0
printf 'stub-op-token\n'
STUB
chmod +x "$SBOX/bin/op" "$SBOX/bin/claude" "$SBOX/bin/security"

# --- the same `security` behaviour as a BASH_ENV function (see header: beats the re-prepend) ---
cat > "$SBOX/stubs.sh" <<'FUNCS'
security(){
  [ -n "${STUB_NO_TOKEN:-}" ] && return 1
  [ -n "${STUB_EMPTY_TOKEN:-}" ] && return 0
  printf 'stub-op-token\n'
}
export -f security
FUNCS

run_tick(){  # run_tick [VAR=val ...] — the tick in a hermetic env; stdout/stderr -> $SBOX/{out,err}
  env -i HOME="$SBOX/home" PATH="$SBOX/bin:/usr/bin:/bin" BASH_ENV="$SBOX/stubs.sh" \
      ${1+"$@"} bash "$TICK" >"$SBOX/out" 2>"$SBOX/err"
}

# --- (a) the wrapper parses ---
bash -n "$TICK" 2>/dev/null
ok "$([ $? = 0 ] && echo 1)" "inbox-assistant-tick.sh parses (bash -n)"

# --- (b) missing keychain token -> exit 1 + a clear FATAL line on stderr ---
run_tick STUB_NO_TOKEN=1; rc=$?
ok "$([ "$rc" = 1 ] && echo 1)" "missing keychain token exits 1 (got rc=$rc)"
grep -q 'op.inbox-assistant.token' "$SBOX/err"
ok "$([ $? = 0 ] && echo 1)" "missing-token stderr names the keychain item"
grep -q 'FATAL' "$SBOX/err"
ok "$([ $? = 0 ] && echo 1)" "missing-token stderr is loud (FATAL)"

# --- (b2) token readable but EMPTY -> the same fail-closed exit 1 ---
run_tick STUB_EMPTY_TOKEN=1; rc=$?
ok "$([ "$rc" = 1 ] && echo 1)" "EMPTY keychain token exits 1 (got rc=$rc)"

# --- (c) INBOX_ACCOUNTS unset -> component off: exit 0, 'not configured', zero side effects ---
if [ -x "$REPO/.venv/bin/python" ]; then
  run_tick; rc=$?
  ok "$([ "$rc" = 0 ] && echo 1)" "INBOX_ACCOUNTS unset exits 0 (got rc=$rc)"
  grep -q 'not configured' "$SBOX/out"
  ok "$([ $? = 0 ] && echo 1)" "component-off run prints 'not configured'"
  ! grep -q 'stub op invoked' "$SBOX/err"
  ok "$([ $? = 0 ] && echo 1)" "op never invoked on the component-off path"
  ! grep -q 'stub claude invoked' "$SBOX/err"
  ok "$([ $? = 0 ] && echo 1)" "claude never invoked on the component-off path"
  ok "$([ ! -e "$SBOX/home/.myndaix" ] && echo 1)" "no jefe drop / .myndaix written in the sandbox HOME"
else
  ok "" "component-off case needs $REPO/.venv/bin/python — run 'uv sync' first"
fi

if [ "$FAIL" = 0 ]; then echo "ALL PASS ($PASS checks)"; else echo "FAILED ($FAIL of $((PASS+FAIL)))"; exit 1; fi
