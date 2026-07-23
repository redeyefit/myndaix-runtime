#!/usr/bin/env bash
# test.sh — recall-librarian keepalive smoke test. The LOCAL half (lib log/alert/validate_fence +
# bootstrap fail-closed guards + idempotency) runs anywhere and gates every deploy. The LIVE half
# (RC session, claude.ai auth, phone pairing) can only be verified ON the Mini and is a printed
# checklist at the end.
#
# The gate GRAMMAR (which exact commands recall-gate allows/denies) is tested by
# orchestrator/librarian/test.sh. This file tests the SUPERVISOR — including that it PROVES the
# fence (lib_validate_fence smoke-runs the real recall-gate) before launching.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REAL_HOOK="$(cd "$DIR/../hooks" && pwd)/recall-gate.sh"   # the real gate the fence must smoke-run
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

mkdir -p "$LIB_HOME" "$LIB_WORKSPACE/.claude"
# shellcheck source=/dev/null
source "$DIR/librarian-lib.sh"

# --- helper: write a VALID fence (deny-list + Bash PreToolUse hook -> the real recall-gate) ---
write_valid_fence() {
  local hook="${1:-$REAL_HOOK}"
  printf '%s\n' '{
  "permissions": { "defaultMode": "dontAsk", "allow": [],
    "deny": ["Read","Write","Edit","WebFetch","WebSearch","Agent","Glob","Grep"] },
  "hooks": { "PreToolUse": [
    { "matcher": "*", "hooks": [ { "type": "command", "command": "'"$hook"'" } ] } ] }
}' > "$LIB_WORKSPACE/.claude/settings.json"
}

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

echo "== lib_validate_fence (the fail-closed CRITICAL guard) =="
[[ -x "$REAL_HOOK" ]] && ok "real recall-gate hook is executable ($REAL_HOOK)" || bad "recall-gate hook not executable"

# valid fence -> passes (parse + deny-list + smoke deny(ls) + smoke allow(mxr ask))
write_valid_fence
lib_validate_fence "$LIB_WORKSPACE" && ok "valid fence -> validates (0)" || bad "valid fence should validate"

# missing settings -> fail
mv "$LIB_WORKSPACE/.claude/settings.json" "$SCRATCH/away.json"
lib_validate_fence "$LIB_WORKSPACE" && bad "missing settings must fail" || ok "missing settings.json -> fail-closed"
mv "$SCRATCH/away.json" "$LIB_WORKSPACE/.claude/settings.json"

# malformed JSON -> fail
printf '{ not json' > "$LIB_WORKSPACE/.claude/settings.json"
lib_validate_fence "$LIB_WORKSPACE" && bad "malformed JSON must fail" || ok "malformed settings.json -> fail-closed"

# weak deny-list (no Read/Write) -> fail
printf '%s\n' '{ "permissions": {"deny":["Edit"]}, "hooks": {"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"'"$REAL_HOOK"'"}]}]} }' \
  > "$LIB_WORKSPACE/.claude/settings.json"
lib_validate_fence "$LIB_WORKSPACE" && bad "weak deny-list must fail" || ok "deny-list missing Read/Write -> fail-closed"

# no Bash hook -> fail
printf '%s\n' '{ "permissions": {"deny":["Read","Write"]}, "hooks": {"PreToolUse":[]} }' \
  > "$LIB_WORKSPACE/.claude/settings.json"
lib_validate_fence "$LIB_WORKSPACE" && bad "no Bash hook must fail" || ok "no Bash PreToolUse hook -> fail-closed"

# hook path non-executable / wrong -> fail
write_valid_fence "/nonexistent/recall-gate.sh"
lib_validate_fence "$LIB_WORKSPACE" && bad "wrong hook path must fail" || ok "non-executable hook path -> fail-closed"

# a hook that ALLOWS everything -> fail (the deny smoke catches it: the exact fail-open class)
ALLOW_ALL="$SCRATCH/allow-all.sh"
printf '#!/usr/bin/env bash\necho '\''{"hookSpecificOutput":{"permissionDecision":"allow"}}'\''\n' > "$ALLOW_ALL"; chmod +x "$ALLOW_ALL"
write_valid_fence "$ALLOW_ALL"
lib_validate_fence "$LIB_WORKSPACE" && bad "allow-all hook must fail (fail-open class)" || ok "allow-everything hook -> fail-closed (deny smoke catches it)"

# a hook that DENIES everything -> fail (the allow smoke catches a dead-but-fenced gate)
DENY_ALL="$SCRATCH/deny-all.sh"
printf '#!/usr/bin/env bash\necho '\''{"hookSpecificOutput":{"permissionDecision":"deny"}}'\''\n' > "$DENY_ALL"; chmod +x "$DENY_ALL"
write_valid_fence "$DENY_ALL"
lib_validate_fence "$LIB_WORKSPACE" && bad "deny-all hook must fail (dead librarian)" || ok "deny-everything hook -> fail-closed (allow smoke catches it)"

# a STALE gate (allows research only, pre-company vintage) -> fail (kilabz PR#110 MEDIUM: the
# preflight must assert the exact scope policy, not just liveness)
STALE_GATE="$SCRATCH/stale-gate.sh"
cat > "$STALE_GATE" << 'STALEEOF'
#!/usr/bin/env bash
in="$(cat)"
if printf '%s' "$in" | grep -q 'scope research'; then
  echo '{"hookSpecificOutput":{"permissionDecision":"allow"}}'
else
  echo '{"hookSpecificOutput":{"permissionDecision":"deny"}}'
fi
STALEEOF
chmod +x "$STALE_GATE"
write_valid_fence "$STALE_GATE"
lib_validate_fence "$LIB_WORKSPACE" && bad "stale gate (research-only) must fail" || ok "stale gate missing an allowlisted scope -> fail-closed"

# a BLACKLIST-style gate (denies only the two known sensitive scopes, allows everything else) ->
# fail (PR#111 review HIGH: the synthetic canary scope can never be allowlisted; allowing it proves
# the gate is a blacklist)
BLACKLIST_GATE="$SCRATCH/blacklist-gate.sh"
cat > "$BLACKLIST_GATE" << 'BLEOF'
#!/usr/bin/env bash
in="$(cat)"
if printf '%s' "$in" | grep -Eq 'scope (personal|runtime)'; then
  echo '{"hookSpecificOutput":{"permissionDecision":"deny"}}'
else
  echo '{"hookSpecificOutput":{"permissionDecision":"allow"}}'
fi
BLEOF
chmod +x "$BLACKLIST_GATE"
write_valid_fence "$BLACKLIST_GATE"
lib_validate_fence "$LIB_WORKSPACE" && bad "blacklist gate must fail (canary scope allowed)" || ok "blacklist-style gate -> fail-closed (canary scope catches it)"

# a MALFORMED gate whose output contains BOTH decision strings -> fail (PR#111 review CRITICAL:
# substring grep would dual-match; the structural parse must reject non-single-JSON output)
DUAL_GATE="$SCRATCH/dual-gate.sh"
printf '#!/usr/bin/env bash\necho '\''{"debug":"permissionDecision: allow","hookSpecificOutput":{"permissionDecision":"deny"}}{"hookSpecificOutput":{"permissionDecision":"allow"}}'\''\n' > "$DUAL_GATE"
chmod +x "$DUAL_GATE"
write_valid_fence "$DUAL_GATE"
lib_validate_fence "$LIB_WORKSPACE" && bad "dual-decision malformed gate must fail" || ok "dual-decision malformed output -> fail-closed (structural parse)"

# a Bash-scoped matcher -> fail (PR#111 r2 HIGH: direct-exec probes can't prove live routing of
# non-Bash tools through a "Bash"-matched hook; only the universal matcher is certifiable)
printf '%s\n' '{ "permissions": {"deny":["Read","Write"]}, "hooks": {"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"'"$REAL_HOOK"'"}]}]} }' \
  > "$LIB_WORKSPACE/.claude/settings.json"
lib_validate_fence "$LIB_WORKSPACE" && bad "Bash-scoped matcher must fail" || ok "matcher \"Bash\" (non-universal) -> fail-closed"

# a gate that answers CORRECTLY but exits non-zero -> fail (PR#111 r2 HIGH: live Claude processes
# hook JSON only on exit 0, so a non-zero gate is ignored at runtime = unconfined under dontAsk)
EXIT1_GATE="$SCRATCH/exit1-gate.sh"
printf '#!/usr/bin/env bash\n"%s"\nexit 1\n' "$REAL_HOOK" > "$EXIT1_GATE"
chmod +x "$EXIT1_GATE"
write_valid_fence "$EXIT1_GATE"
lib_validate_fence "$LIB_WORKSPACE" && bad "correct-but-exit-1 gate must fail" || ok "right decision + non-zero exit -> fail-closed (runtime would ignore it)"

# a gate emitting the decision at the FLAT top level (not hookSpecificOutput) -> fail (PR#111 r2
# HIGH: the documented PreToolUse shape is nested; a flat-only gate may carry no live decision)
FLAT_GATE="$SCRATCH/flat-gate.sh"
printf '#!/usr/bin/env bash\necho '\''{"permissionDecision":"deny"}'\''\n' > "$FLAT_GATE"
chmod +x "$FLAT_GATE"
write_valid_fence "$FLAT_GATE"
lib_validate_fence "$LIB_WORKSPACE" && bad "flat-schema gate must fail" || ok "flat top-level decision -> fail-closed (nested shape required)"

echo "== rc-bootstrap: fail-closed guards =="
have_session() { tmux -S "$SOCK" has-session -t librarian 2>/dev/null; }

# guard (a): park marker present -> must NOT create a session (even with a VALID fence)
tmux -S "$SOCK" kill-server 2>/dev/null || true
write_valid_fence
printf 'PARKED reason=test ts=now\n' > "$LIB_HOME/.parked"
bash "$DIR/rc-bootstrap.sh" >/dev/null 2>&1 || true
if have_session; then bad "park marker: session must NOT be created"; else ok "park marker present -> no session (a)"; fi
rm -f "$LIB_HOME/.parked"

# guard (b1): fence missing -> must NOT create a session
tmux -S "$SOCK" kill-server 2>/dev/null || true
mv "$LIB_WORKSPACE/.claude/settings.json" "$SCRATCH/away.json"
bash "$DIR/rc-bootstrap.sh" >/dev/null 2>&1 || true
if have_session; then bad "missing fence: session must NOT be created"; else ok "workspace fence missing -> no session (b1)"; fi

# guard (b2): fence PRESENT but BROKEN (wrong hook path) -> must NOT create a session (the CRITICAL)
tmux -S "$SOCK" kill-server 2>/dev/null || true
write_valid_fence "/nonexistent/recall-gate.sh"
bash "$DIR/rc-bootstrap.sh" >/dev/null 2>&1 || true
if have_session; then bad "broken fence: session must NOT be created"; else ok "present-but-broken fence -> no session (b2, r1 CRITICAL)"; fi

# happy path: VALID fence, no park -> creates exactly ONE session (fake wrapper)
tmux -S "$SOCK" kill-server 2>/dev/null || true
write_valid_fence
bash "$DIR/rc-bootstrap.sh" >/dev/null 2>&1 || true
if have_session; then ok "valid fence + no park -> session created"; else bad "happy path: session should be created"; fi

# idempotency: a second run must NOT create a second session (still exactly one)
bash "$DIR/rc-bootstrap.sh" >/dev/null 2>&1 || true
n="$(tmux -S "$SOCK" list-sessions 2>/dev/null | grep -c '^librarian:' || true)"; n="$((10#${n:-0}))"
[[ "$n" == "1" ]] && ok "idempotent: still exactly one session on re-run" || bad "idempotency (found $n sessions)"

echo
echo "== LOCAL RESULT: $PASS passed, $FAIL failed =="

cat <<'LIVE'

== LIVE checklist (Mini only — cannot be asserted here) ==
  [ ] ~/librarian staged: CLAUDE.md + .claude/settings.json + recall-gate reachable (hook path rewritten)
  [ ] `mxr` resolves to ~/.local/bin/mxr on the Mini — recall-gate allows bare `mxr ask`
  [ ] claude.ai OAuth present (~/.claude/.credentials.json, loggedIn:true) — RC rejects tokens
  [ ] log/socket dirs pre-created: mkdir -p ~/.myndaix/orchestrator/librarian ~/.local/state
  [ ] plist installed + loaded: launchctl load ~/Library/LaunchAgents/ai.myndaix.librarian-rc.plist
  [ ] tmux session 'librarian' alive: tmux -S ~/.local/state/librarian.tmux has-session -t librarian
  [ ] phone paired (Claude app) -> ask a research question -> cited answer
  [ ] out-of-scope probe (e.g. "read ~/.myndaix/.secrets") -> gate denies
LIVE

[[ "$FAIL" -eq 0 ]]
