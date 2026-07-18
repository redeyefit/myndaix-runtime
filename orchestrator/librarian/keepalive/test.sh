#!/usr/bin/env bash
# test.sh — recall-librarian keepalive smoke test. The LOCAL half (lib log/alert + bootstrap
# fail-closed guards + idempotency) runs anywhere and gates every deploy. The LIVE half
# (RC session, claude.ai auth, phone pairing) can only be verified ON the Mini and is a printed
# checklist at the end.
#
# The fence itself (recall-gate: allow only `mxr ask --scope research|fitness`) is tested by
# orchestrator/librarian/test.sh — NOT re-tested here. This file tests the SUPERVISOR.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$1"; }

# isolate ALL state under a scratch tree so we never touch real logs / the real socket / ~/librarian.
SCRATCH="$(mktemp -d)"
SOCK="$SCRATCH/librarian.tmux"
cleanup() { tmux -S "$SOCK" kill-server 2>/dev/null || true; rm -rf "$SCRATCH"; }
trap cleanup EXIT

export LIB_HOME="$SCRATCH/state"
export LIB_LOG="$LIB_HOME/librarian.log"
export LIB_WORKSPACE="$SCRATCH/librarian"
export LIB_TMUX_SOCK="$SOCK"
# fake pane command: a bare sleeper, so bootstrap can create/idempotent-check a session without
# ever launching claude. Must be executable.
FAKE_WRAPPER="$SCRATCH/fake-wrapper.sh"
printf '#!/usr/bin/env bash\nexec sleep 300\n' > "$FAKE_WRAPPER"; chmod +x "$FAKE_WRAPPER"
export LIB_WRAPPER_CMD="$FAKE_WRAPPER"

mkdir -p "$LIB_HOME"
# shellcheck source=/dev/null
source "$DIR/librarian-lib.sh"

echo "== librarian-lib: log =="
lib_log "hello test"
grep -q '\[librarian\] hello test' "$LIB_LOG" && ok "lib_log writes a structured line" || bad "lib_log write"

# rotation: force the log over the cap, then a fresh write must rotate to .old and start clean.
LIB_LOG_MAX_BYTES=64
head -c 200 /dev/zero | tr '\0' 'x' > "$LIB_LOG"
lib_log "post-rotate line"
if [[ -f "$LIB_LOG.old" ]] && grep -q 'post-rotate line' "$LIB_LOG" && ! grep -q 'xxxx' "$LIB_LOG"; then
  ok "lib_log rotates at cap (.old kept, new log clean)"
else
  bad "lib_log rotation"
fi
# shellcheck disable=SC2034  # consumed by lib_log in the sourced librarian-lib.sh
LIB_LOG_MAX_BYTES=1048576   # restore

echo "== librarian-lib: alert (log-only when recipient empty) =="
# shellcheck disable=SC2034  # consumed by lib_alert in the sourced librarian-lib.sh
LIB_ALERT_IMESSAGE_TO=""
rc=0; lib_alert "test-reason" || rc=$?
if [[ "$rc" == "0" ]] && grep -q 'ALERT (unsent, LIB_ALERT_IMESSAGE_TO empty): test-reason' "$LIB_LOG"; then
  ok "lib_alert with empty recipient -> logs, no send, rc=0 (no-auto-texts)"
else
  bad "lib_alert log-only path (rc=$rc)"
fi

echo "== rc-bootstrap: fail-closed guards =="
have_session() { tmux -S "$SOCK" has-session -t librarian 2>/dev/null; }

# guard (a): park marker present -> must NOT create a session
tmux -S "$SOCK" kill-server 2>/dev/null || true
mkdir -p "$LIB_WORKSPACE/.claude"; echo '{}' > "$LIB_WORKSPACE/.claude/settings.json"
printf 'PARKED reason=test ts=now\n' > "$LIB_HOME/.parked"
bash "$DIR/rc-bootstrap.sh" >/dev/null 2>&1 || true
if have_session; then bad "park marker: session must NOT be created"; else ok "park marker present -> no session (a)"; fi
rm -f "$LIB_HOME/.parked"

# guard (b): workspace fence missing -> must NOT create a session
tmux -S "$SOCK" kill-server 2>/dev/null || true
mv "$LIB_WORKSPACE/.claude/settings.json" "$LIB_WORKSPACE/.claude/settings.json.bak"
bash "$DIR/rc-bootstrap.sh" >/dev/null 2>&1 || true
if have_session; then bad "missing fence: session must NOT be created"; else ok "workspace fence missing -> no session (b)"; fi
mv "$LIB_WORKSPACE/.claude/settings.json.bak" "$LIB_WORKSPACE/.claude/settings.json"

# happy path: fence present, no park -> creates exactly ONE session (fake wrapper)
tmux -S "$SOCK" kill-server 2>/dev/null || true
bash "$DIR/rc-bootstrap.sh" >/dev/null 2>&1 || true
if have_session; then ok "fence present + no park -> session created"; else bad "happy path: session should be created"; fi

# idempotency: a second run must NOT create a second session (still exactly one)
bash "$DIR/rc-bootstrap.sh" >/dev/null 2>&1 || true
n="$(tmux -S "$SOCK" list-sessions 2>/dev/null | grep -c '^librarian:' || true)"; n="$((10#${n:-0}))"
[[ "$n" == "1" ]] && ok "idempotent: still exactly one session on re-run" || bad "idempotency (found $n sessions)"

echo
echo "== LOCAL RESULT: $PASS passed, $FAIL failed =="

cat <<'LIVE'

== LIVE checklist (Mini only — cannot be asserted here) ==
  [ ] ~/librarian staged: CLAUDE.md + .claude/settings.json + recall-gate reachable
  [ ] `mxr` resolves on the Mini (~/.local/bin/mxr) — recall-gate allows bare `mxr ask`
  [ ] claude.ai OAuth present (~/.claude/.credentials.json, loggedIn:true) — RC rejects tokens
  [ ] plist installed + loaded: launchctl load ~/Library/LaunchAgents/ai.myndaix.librarian-rc.plist
  [ ] tmux session 'librarian' alive: tmux -S ~/.local/state/librarian.tmux has-session -t librarian
  [ ] phone paired (Claude app) -> ask a research question -> cited answer
  [ ] out-of-scope probe (e.g. "read ~/.myndaix/.secrets") -> gate denies
LIVE

[[ "$FAIL" -eq 0 ]]
