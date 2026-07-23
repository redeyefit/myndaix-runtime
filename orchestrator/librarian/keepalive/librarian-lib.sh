#!/usr/bin/env bash
# librarian-lib.sh — shared helpers for the recall-librarian keepalive (graduate piece C to the
# always-on Mini). Sourced by rc-bootstrap.sh and rc-wrapper.sh. NOT executed directly.
#
# This is the STRIPPED sibling of orchestrator/watch/watch-lib.sh. The librarian has NO read fence
# (no sanitize_untrusted / watch-scan.py) because it does not read untrusted files — the recall-gate
# (orchestrator/librarian/hooks/recall-gate.py) is its sole tool gate and it allows ONLY
# `mxr ask --scope research|fitness|company "<safe q>"`. So the only shared pieces the keepalive needs are:
#   - lib_log():   one structured, size-bounded log line (best-effort, never fails the caller).
#   - lib_alert(): a narrow, deterministic, PARK-ONLY iMessage ping (default recipient EMPTY =>
#                  log-only, honoring the house no-auto-texts posture). Body is reason+timestamp
#                  ONLY — never any runtime/corpus content.
#
# House rules: bash-scripts.md (set -euo pipefail in the caller; quote all; no eval; 10# numerics).

# This is a SOURCED library — it must NOT set -e or -u, which would pollute the sourcing shell
# (rc-wrapper.sh deliberately runs `set -uo pipefail` WITHOUT -e, because its child `claude` exits
# non-zero as normal control flow; forcing -e here would kill the supervisor loop). We enable ONLY
# pipefail (bash-check's safety-header requirement) — every caller already sets it, so this is a no-op
# for them, and it never turns on the caller-hostile options.
set -o pipefail

# ---- config (env-overridable, all fail-safe defaults) ----
# WORKSPACE = the confined RC cwd (holds CLAUDE.md + .claude/settings.json + the recall-gate fence).
LIB_WORKSPACE="${LIB_WORKSPACE:-$HOME/librarian}"
# HOME = runtime STATE (log + park marker), kept OUT of the workspace so the confined dir stays
# pristine (the session can't read these anyway — Read is deny-listed — but keep them separate).
LIB_HOME="${LIB_HOME:-$HOME/.myndaix/orchestrator/librarian}"
LIB_LOG="${LIB_LOG:-$LIB_HOME/librarian.log}"
LIB_LOG_MAX_BYTES="${LIB_LOG_MAX_BYTES:-1048576}"          # rotate at ~1MB, keep 1 .old
# Narrow park-alert recipient. EMPTY by default (house no-auto-texts posture — logs instead of
# texting). Its OWN var, never PLAY_IMESSAGE_TO — this fires ONLY from the wrapper's park branch.
LIB_ALERT_IMESSAGE_TO="${LIB_ALERT_IMESSAGE_TO-}"

lib_log() {
  # one structured line; best-effort; never fails the caller.
  local msg="$1" ts sz
  ts="$(date '+%Y-%m-%d %H:%M:%S')"
  mkdir -p "$(dirname "$LIB_LOG")" 2>/dev/null || true
  # crude size-bounded rotate before append.
  if [[ -f "$LIB_LOG" ]]; then
    sz="$(wc -c <"$LIB_LOG" 2>/dev/null || echo 0)"; sz="$((10#${sz//[^0-9]/}))"
    if (( sz > LIB_LOG_MAX_BYTES )); then mv -f "$LIB_LOG" "$LIB_LOG.old" 2>/dev/null || true; fi
  fi
  printf '[%s] [librarian] %s\n' "$ts" "$msg" >>"$LIB_LOG" 2>/dev/null || true
}

lib_alert() {
  # ONE deterministic, wrapper-generated park ping. Body is reason+timestamp ONLY — no runtime or
  # corpus content ever (redaction satisfied by construction). Best-effort; logs its own outcome.
  local reason="$1" msg to rc
  to="$LIB_ALERT_IMESSAGE_TO"
  if [[ -z "$to" ]]; then
    lib_log "ALERT (unsent, LIB_ALERT_IMESSAGE_TO empty): $reason"
    return 0
  fi
  msg="Recall librarian parked on the Mini: ${reason} @ $(date '+%Y-%m-%d %H:%M:%S'). SSH runbook required."
  msg="${msg:0:1500}"
  # House injection-safe argv osascript form (play-review.sh) — message + recipient travel as argv
  # into AppleScript `on run {m,t}`, never string-interpolated.
  osascript -e 'on run {m, t}' \
            -e 'tell application "Messages" to send m to buddy t of (service 1 whose service type is iMessage)' \
            -e 'end run' -- "$msg" "$to" >/dev/null 2>&1
  rc=$?
  if (( rc == 0 )); then
    lib_log "ALERT sent: $reason"
  else
    # never silently suppress on a notification path — leave a visible marker.
    lib_log "ALERT FAILED-PING rc=$rc: $reason"
    printf 'FAILED-PING rc=%s reason=%s ts=%s\n' "$rc" "$reason" "$(date '+%FT%T')" \
      >>"$LIB_HOME/.parked" 2>/dev/null || true
  fi
  return 0
}

lib_validate_fence() {
  # Fail-CLOSED fence validation (review r1 CRITICAL). Existence alone is NOT enough: under
  # `defaultMode: dontAsk` with Bash intentionally un-denied, a present-but-broken gate (malformed
  # settings.json, missing/wrong hook path, a hook that emits no decision) falls THROUGH to ALLOW —
  # i.e. an UNCONFINED Bash session. So actually PROVE the fence before launch:
  #   1. settings.json is valid JSON,
  #   2. its deny-list covers the non-Bash surface (Read+Write as a proxy),
  #   3. it wires a Bash PreToolUse hook whose command is an ABSOLUTE, EXECUTABLE path,
  #   4. that hook actually DENIES a disallowed command AND enforces the EXACT scope policy
  #      (smoke-run: every phone-queryable scope allowed, a sensitive scope denied).
  # Returns 0 iff all hold; logs the specific failure and returns 1 otherwise. Side-effect-free
  # (the recall-gate is a pure decision function).
  local ws="${1:-$LIB_WORKSPACE}" settings hook prc
  settings="$ws/.claude/settings.json"
  if [[ ! -f "$settings" ]]; then lib_log "fence: settings.json missing ($settings)"; return 1; fi
  # r7 HIGH: Claude MERGES $ws/.claude/settings.local.json over the certified file, so an
  # uncertified local override (a SessionStart hook, a 2nd PreToolUse entry, disableAllHooks)
  # would silently void the shape we prove. We validate only settings.json, so a sibling
  # settings.local.json MUST NOT exist. (The ~/.claude global file is the operator's own,
  # unwritable by the confined session — same trust boundary as the accepted r3 inherited-hooks
  # residual; documented, not phone-reachable.)
  if [[ -e "$ws/.claude/settings.local.json" ]]; then
    lib_log "fence: an uncertified settings.local.json exists beside the fence — refusing"; return 1
  fi

  # parse + structural asserts in one python pass. exit: 0 ok (prints hook cmd), 2 bad-json,
  # 3 no-universal-hook, 4 weak-deny, 5 unrecognized entry/handler fields (r4 HIGH + r5 HIGH).
  hook="$(python3 - "$settings" <<'PY'
import json, sys
def _no_dup(pairs):
    # r7 HIGH: json.load collapses duplicate keys last-wins; if Claude's parser is first-wins, a
    # doubled "PreToolUse"/"hooks"/"command" lets us validate the benign copy while Claude runs the
    # evil one. Reject ANY duplicate key so the two parsers can never diverge.
    d = {}
    for k, v in pairs:
        if k in d:
            raise ValueError(f"duplicate key {k!r}")
        d[k] = v
    return d
try:
    d = json.loads(open(sys.argv[1]).read(), object_pairs_hook=_no_dup)
except Exception:
    sys.exit(2)
deny = set(((d.get("permissions") or {}).get("deny")) or d.get("deny") or [])
if not {"Read", "Write"} <= deny:
    sys.exit(4)
# accept ONLY a universal matcher ("*", "", or an ABSENT/null matcher = all tools — the allowlist
# model). "Bash" is REJECTED (PR#111 r2 HIGH): the smoke probes below exec the hook binary
# DIRECTLY, so they cannot prove live Claude routes non-Bash tools through a Bash-scoped hook —
# that would ride on the deny-list enumeration alone (the model that missed DesignSync). TYPE-
# aware check (r3 HIGH): str() made the literal JSON string "None" pass as universal — it is a
# narrow matcher (matches a tool literally named None), so it must fail like any other.
# EXACTLY-ONE-OF-EVERYTHING shape (r6 HIGH-1/2 + r4/r5): the certifiable settings shape is ONE
# PreToolUse entry (universal matcher) holding ONE {type,command} handler, and NOTHING else —
# no other hook EVENT types (a SessionStart/UserPromptSubmit entry is arbitrary command exec
# outside the Bash allowlist), no extra entries/handlers (Claude runs ALL matching hooks; a
# second command is smuggled execution the early-exit scan never inspected), no narrowing keys
# at either level (r4 "if", r5 "platforms"), no "timeout" (r5: timeout:0 cancels the gate
# pre-decision = no-decision fall-through under dontAsk).
hooks_obj = d.get("hooks") or {}
if set(hooks_obj.keys()) - {"PreToolUse"}:
    sys.exit(5)
entries = hooks_obj.get("PreToolUse") or []
if len(entries) == 0:
    sys.exit(3)   # r7 INFO: missing PreToolUse is "no hook" (3), not "extra fields" (5)
if len(entries) > 1:
    sys.exit(5)
h = entries[0] if isinstance(entries[0], dict) else {}
m = h.get("matcher")
if not (m is None or m in ("*", "")):
    sys.exit(3)
if set(h.keys()) - {"matcher", "hooks"}:
    sys.exit(5)
handlers = h.get("hooks") or []
if len(handlers) != 1:
    sys.exit(5)
hh = handlers[0] if isinstance(handlers[0], dict) else {}
if hh.get("type") != "command" or not hh.get("command"):
    sys.exit(3)
if set(hh.keys()) - {"type", "command"}:
    sys.exit(5)
cmd = hh["command"]
if "\n" in cmd or "\r" in cmd:   # r7 LOW: a newline in the path breaks the absolute-path check's
    sys.exit(5)                  # first-line-only match (fail-closed already, but no garbled log)
print(cmd); sys.exit(0)
PY
)"
  prc=$?
  case "$prc" in
    0) : ;;
    2) lib_log "fence: settings.json is not valid JSON"; return 1 ;;
    3) lib_log "fence: no universal-matcher PreToolUse hook in settings.json (matcher must be \"*\")"; return 1 ;;
    4) lib_log "fence: deny-list does not cover the non-Bash surface (Read/Write)"; return 1 ;;
    5) lib_log "fence: settings shape not certifiable — extra hook events/entries/handlers or unrecognized fields; refusing"; return 1 ;;
    *) lib_log "fence: settings.json validation failed (rc=$prc)"; return 1 ;;
  esac

  case "$hook" in /*) : ;; *) lib_log "fence: hook path not absolute: $hook"; return 1 ;; esac
  if [[ ! -x "$hook" ]]; then lib_log "fence: hook not executable: $hook"; return 1; fi

  # smoke: a disallowed Bash command MUST be denied.
  if [[ "$(_lib_gate_decision "$hook" '{"tool_name":"Bash","tool_input":{"command":"ls -la"}}')" != "deny" ]]; then
    lib_log "fence: hook did NOT deny a disallowed command (ls) — refusing to launch"; return 1
  fi
  # smoke: a NON-Bash tool MUST be denied — proves the allowlist model is in effect (the gate fires
  # for every tool, not just Bash), i.e. settings.json wired the hook with matcher "*" (r2 HIGH-2).
  if [[ "$(_lib_gate_decision "$hook" '{"tool_name":"Read","tool_input":{"file_path":"/x"}}')" != "deny" ]]; then
    lib_log "fence: hook did NOT deny a non-Bash tool (Read) — allowlist not in effect"; return 1
  fi
  # smoke: the EXACT scope policy must hold (kilabz PR#110 MEDIUM: a stale deployed gate lacking a
  # scope, or a drifted one allowing an extra scope, must NOT pass launch preflight). Every
  # phone-queryable scope MUST be allowed (else dead-but-fenced); a sensitive/unlisted scope MUST be
  # explicitly denied — including a synthetic NEVER-allowlisted canary, so a blacklist-style gate
  # (denies the known two, allows the rest) cannot pass (PR#111 review HIGH). MUST stay in sync with
  # SCOPES in hooks/recall-gate.py.
  local s
  for s in research fitness company; do
    if [[ "$(_lib_gate_decision "$hook" "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"mxr ask --scope $s \\\"smoke\\\"\"}}")" != "allow" ]]; then
      lib_log "fence: hook did NOT allow a valid mxr ask ($s) — stale/misconfigured gate"; return 1
    fi
  done
  for s in personal runtime zz-canary-unlisted; do
    if [[ "$(_lib_gate_decision "$hook" "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"mxr ask --scope $s \\\"smoke\\\"\"}}")" != "deny" ]]; then
      lib_log "fence: hook did NOT explicitly deny a non-allowlisted scope ($s) — drifted gate"; return 1
    fi
  done
  return 0
}

_lib_gate_decision() {
  # $1=hook $2=probe-payload-json → prints the gate's permissionDecision, or "" (⇒ caller fails
  # closed). STRUCTURAL parse, not substring grep (PR#111 r1 CRITICAL: a malformed gate whose
  # output contains both "allow" and "deny" strings could dual-match grep and pass every check).
  # r2 folds:
  #   - hook must EXIT 0 (FIX-1): live Claude processes hook JSON only on exit 0 — a gate that
  #     prints a decision but exits non-zero is IGNORED at runtime, so certify nothing.
  #   - decision must live at hookSpecificOutput.permissionDecision, the documented PreToolUse
  #     shape — the flat top-level fallback is DROPPED (FIX-4: live Claude may ignore a flat-only
  #     gate) and membership-tested, never truthiness-chained (FIX-3: a falsy "" must not fall
  #     through to another key).
  #   - probe scratch is a private mktemp -d with a template (FIX-5/8: no reopen-by-path TOCTOU
  #     on a predictable file).
  # Gate stderr is captured and logged, never discarded (r1 MEDIUM); parse rejects are printed to
  # stderr, not swallowed (FIX-7). r3 HIGH: stdout is captured to a FILE in the private scratch
  # and decoded STRICTLY — bash command substitution silently strips NUL bytes, and a lenient
  # errors="replace" decode would normalize invalid UTF-8 before parsing, so the preflight could
  # certify byte streams that are not the gate's valid JSON. Any non-UTF-8 byte → no decision →
  # caller fails closed.
  local hook="$1" payload="$2" hook_rc scratch
  if ! command -v python3 >/dev/null 2>&1; then
    # FIX-6: without python3 the parse below can never certify — say so instead of a misleading
    # scope-mismatch log. (The real gate is itself python3, so this host could never pass anyway.)
    lib_log "fence: python3 not on PATH — cannot parse gate output, refusing to certify"; return 0
  fi
  scratch="$(mktemp -d "${TMPDIR:-/tmp}/lib-gate.XXXXXX")" || { lib_log "fence: mktemp failed for gate probe"; return 0; }
  hook_rc=0
  printf '%s' "$payload" | "$hook" >"$scratch/out" 2>"$scratch/err" || hook_rc=$?
  if [[ -s "$scratch/err" ]]; then
    lib_log "fence: gate stderr during probe: $(head -c 300 "$scratch/err" | tr -d '\n\r')"
  fi
  if (( hook_rc != 0 )); then
    lib_log "fence: gate probe exited rc=$hook_rc — live Claude would IGNORE its decision"
    rm -rf "$scratch"; return 0
  fi
  python3 - "$scratch/out" <<'PY' || true
import json, sys
try:
    raw = open(sys.argv[1], "rb").read().decode("utf-8", errors="strict")
except Exception as e:
    print(f"gate output is not strict UTF-8: {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(0)  # no decision -> caller fails closed
try:
    d = json.loads(raw)
except Exception as e:
    print(f"gate output is not a single JSON doc: {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(0)  # no decision -> caller fails closed
hso = d.get("hookSpecificOutput") if isinstance(d, dict) else None
v = hso.get("permissionDecision") if isinstance(hso, dict) else None
if v in ("allow", "deny", "ask"):
    print(v)
PY
  rm -rf "$scratch"
}
