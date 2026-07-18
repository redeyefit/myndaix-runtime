#!/usr/bin/env bash
# librarian-lib.sh — shared helpers for the recall-librarian keepalive (graduate piece C to the
# always-on Mini). Sourced by rc-bootstrap.sh and rc-wrapper.sh. NOT executed directly.
#
# This is the STRIPPED sibling of orchestrator/watch/watch-lib.sh. The librarian has NO read fence
# (no sanitize_untrusted / watch-scan.py) because it does not read untrusted files — the recall-gate
# (orchestrator/librarian/hooks/recall-gate.py) is its sole tool gate and it allows ONLY
# `mxr ask --scope research|fitness "<safe q>"`. So the only shared pieces the keepalive needs are:
#   - lib_log():   one structured, size-bounded log line (best-effort, never fails the caller).
#   - lib_alert(): a narrow, deterministic, PARK-ONLY iMessage ping (default recipient EMPTY =>
#                  log-only, honoring the house no-auto-texts posture). Body is reason+timestamp
#                  ONLY — never any runtime/corpus content.
#
# House rules: bash-scripts.md (set -euo pipefail in the caller; quote all; no eval; 10# numerics).

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
