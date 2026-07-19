#!/usr/bin/env bash
# rc-wrapper.sh — the in-tmux supervisor for the recall librarian's remote-control session.
# tmux runs THIS as the pane command, so pane death == wrapper death (has-session is a true
# supervisor proxy). The wrapper runs `claude` as a CHILD and loops on its exit (it does NOT
# exec — exec would end the loop; "the wrapper is the pane command" is preserved because tmux runs
# the wrapper, not claude).
#
# Loop: reachability gate -> clean-env launch of `claude remote-control` in the confined workspace
# -> on exit, classify. 3 consecutive sub-5s exits (auth expiry / flag churn) -> PARK: write
# marker, fire ONE narrow alert, sleep (keep the pane alive for the SSH runbook; exiting would
# destroy the session and strand recovery).
#
# The fence is NOT in this wrapper — it is the confined workspace (LIB_WORKSPACE): CLAUDE.md +
# .claude/settings.json (deny-list of every non-Bash tool) + the recall-gate PreToolUse Bash hook
# (allows ONLY `mxr ask --scope research|fitness|company "<safe q>"`). This wrapper's job is liveness +
# a clean, minimal launch environment.
set -uo pipefail                                  # NOT -e: a child non-zero exit is normal here

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$DIR/librarian-lib.sh"

# ---- launch environment (self-contained; does NOT depend on ~/.zshrc) ----
# ~/.local/bin holds the `mxr` shim (the ONLY program the recall-gate allows). Prepend it so the
# session's Bash tool resolves `mxr` — WITHOUT this, `mxr ask` is command-not-found and the
# librarian is silently deaf. This is the load-bearing PATH (the review flagged operator-env-
# defined config as a weakness; baking it here is tighter than relying on the login shell).
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

# Scope roots for `mxr ask` — baked in, not sourced from ~/.zshrc. `research` is hardcoded in the
# runtime; `fitness` + `company` need their roots here. `company` = ~/company (Jefe's plan/schedule,
# non-sensitive). MUST stay in sync with the recall-gate SCOPES allowlist. (A future SENSITIVE scope
# must be added deliberately — to the gate allowlist AND here.)
export MYNDAIX_KNOWLEDGE_SCOPES="${MYNDAIX_KNOWLEDGE_SCOPES:-fitness=$HOME/fitness,company=$HOME/company}"

# No MCP tools in the session (under dontAsk, MCP tools would be auto-allowed and bypass the fence).
# remote-control has no --strict-mcp-config flag; the env var disables MCP loading entirely.
export CLAUDE_CODE_DISABLE_MCP=1

PARK_MARKER="$LIB_HOME/.parked"
FAST_EXIT_SECS=5
FAST_EXIT_LIMIT=3
BACKOFF_MIN=5
BACKOFF_MAX=600

# ---- clean auth-env boundary, established ONCE up front ----
# RC requires interactive claude.ai OAuth (keychain / ~/.claude/.credentials.json) and REJECTS
# long-lived tokens. A stray CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY / proxy ANTHROPIC_BASE_URL
# would hard-fail every relaunch (fast-exit -> false park). Strip them so RC uses the keychain login.
if [[ -z "${LIB_ALERT_IMESSAGE_TO-}" && -r "$LIB_HOME/.alert-to" ]]; then
  LIB_ALERT_IMESSAGE_TO="$(head -c 128 "$LIB_HOME/.alert-to" | tr -dc 'A-Za-z0-9@._+-')"
fi
export LIB_ALERT_IMESSAGE_TO
unset CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY ANTHROPIC_BASE_URL 2>/dev/null || true

park() {
  local reason="$1"
  printf 'PARKED reason=%s ts=%s\n' "$reason" "$(date '+%FT%T')" >"$PARK_MARKER" 2>/dev/null || true
  lib_log "PARK: $reason"
  lib_alert "$reason"
  lib_log "parked — recovery = fix the cause, then: rm $PARK_MARKER (self-restarts within ~120s)"
  # Keep the pane (and thus the tmux session) alive WHILE parked so the operator can attach and the
  # bootstrap does not thrash (bootstrap refuses to (re)start while the marker exists). But EXIT the
  # moment the marker is removed: the pane dies -> the tmux session ends -> the next bootstrap tick
  # recreates a fresh session. Without this, `rm marker` alone never restarts RC (review r1 HIGH:
  # bootstrap would see the still-sleeping parked session as 'already alive'). Recovery = fix + rm.
  while [[ -e "$PARK_MARKER" ]]; do sleep 60; done
  lib_log "park marker removed — exiting so bootstrap recreates a fresh session"
  exit 0
}

reachable() {
  # reachable if we get ANY HTTP response from the API host (401 counts). On failure, one IP-literal
  # probe splits DNS failure from routing failure for the log.
  if curl -sS -m5 -o /dev/null "https://api.anthropic.com/" 2>/dev/null; then return 0; fi
  if curl -sS -m5 -o /dev/null "https://1.1.1.1/" 2>/dev/null; then
    lib_log "unreachable: api.anthropic.com fails but 1.1.1.1 ok -> DNS problem"
  else
    lib_log "unreachable: 1.1.1.1 also fails -> routing/ISP down"
  fi
  return 1
}

fast_exits=0
backoff="$BACKOFF_MIN"
lib_log "wrapper start (remote-control, capacity 1, workspace=$LIB_WORKSPACE)"

# preflight() — validated IMMEDIATELY BEFORE EVERY launch (review r2 HIGH-3/LOW-2/MED-1), not once
# at start: the wrapper can live for weeks, so a mid-life fence break (deploy / python move / a
# `git checkout` to a branch without the hook), a wrong `mxr` on PATH, or a filled disk must be
# caught before the NEXT relaunch — never serve a broken/unconfined session. Non-transient failures
# (fence, mxr) PARK; a low disk is transient (log rotation / temp reaper) so it BACKS OFF instead.
preflight() {
  # fence must actually CONFINE (parse + smoke-run the gate: deny ls, deny a non-Bash tool, allow a
  # valid `mxr ask`), not merely exist — else RC would serve an UNCONFINED surface (r1 CRITICAL).
  if ! lib_validate_fence "$LIB_WORKSPACE"; then
    park "workspace-fence-invalid (see librarian.log for the specific failure)"
  fi
  # `mxr` must resolve to the CANONICAL shim, not just any `mxr` on PATH — the recall-gate allows the
  # literal token `mxr`, so a different `mxr` would run under the allow (r1 MED).
  local mxr_resolved
  mxr_resolved="$(command -v mxr 2>/dev/null || true)"
  if [[ "$mxr_resolved" != "$HOME/.local/bin/mxr" || ! -x "$mxr_resolved" ]]; then
    park "mxr-not-canonical (want \$HOME/.local/bin/mxr, got '${mxr_resolved:-none}')"
  fi
}

while true; do
  # a lingering park marker means a prior park; bootstrap should have refused to (re)start us, but
  # belt-and-suspenders: honor it.
  if [[ -e "$PARK_MARKER" ]]; then park "stale-park-marker-present"; fi

  if ! reachable; then
    sleep "$backoff"
    backoff=$(( backoff * 2 )); (( backoff > BACKOFF_MAX )) && backoff="$BACKOFF_MAX"
    continue                                       # network gaps do NOT count as fast-exits
  fi

  # assert the clean env IMMEDIATELY before launch; refuse to hand a dirty auth env to RC (which
  # would hard-fail every relaunch). env -u is the belt.
  if [[ -n "${CLAUDE_CODE_OAUTH_TOKEN-}" || -n "${ANTHROPIC_API_KEY-}" ]]; then
    park "dirty-auth-env"
  fi
  case "${ANTHROPIC_BASE_URL-}" in
    ""|https://api.anthropic.com|https://api.anthropic.com/) : ;;
    *) park "nonstandard-ANTHROPIC_BASE_URL" ;;
  esac

  # disk floor, re-checked every relaunch (MED-1): RC JSONL transcripts share the disk with the
  # Postgres ledger (logged no-space class). Transient -> back off + retry, don't park (self-heals).
  avail_kb="$(df -k "$LIB_HOME" 2>/dev/null | awk 'NR==2{print $4}' | tr -dc '0-9' || true)"
  avail_kb="$((10#${avail_kb:-0}))"
  if (( avail_kb < 524288 )); then
    lib_log "low/unknown disk ${avail_kb}KB — not launching this cycle, backing off"
    sleep "$backoff"; backoff=$(( backoff * 2 )); (( backoff > BACKOFF_MAX )) && backoff="$BACKOFF_MAX"
    continue
  fi

  # fence + mxr must still hold right now (non-transient failures park). See preflight() header.
  preflight

  start="$(date +%s)"
  lib_log "launch claude remote-control --capacity 1 --spawn same-dir --permission-mode dontAsk"
  # cd belt (tmux new-session -c already sets cwd, but ensure identity+fence load from here).
  # --spawn same-dir: NOT worktree (LIB_WORKSPACE is not a git repo; worktree mode would fail).
  # --permission-mode dontAsk: spawned sessions are non-interactive (no human to answer prompts);
  #   the deny-list + recall-gate fully fence them. --name for a clear label in claude.ai/mobile.
  ( cd "$LIB_WORKSPACE" && env -u CLAUDE_CODE_OAUTH_TOKEN -u ANTHROPIC_API_KEY -u ANTHROPIC_BASE_URL \
      claude remote-control --capacity 1 --spawn same-dir --permission-mode dontAsk --name librarian )
  rc=$?
  end="$(date +%s)"
  dur=$(( end - start ))
  lib_log "claude exited rc=$rc after ${dur}s"

  if (( dur < FAST_EXIT_SECS )); then
    fast_exits=$(( fast_exits + 1 ))
    lib_log "fast-exit ${fast_exits}/${FAST_EXIT_LIMIT}"
    if (( fast_exits >= FAST_EXIT_LIMIT )); then
      park "auth-or-flag-failure (${FAST_EXIT_LIMIT} sub-${FAST_EXIT_SECS}s exits)"
    fi
    sleep "$backoff"
    backoff=$(( backoff * 2 )); (( backoff > BACKOFF_MAX )) && backoff="$BACKOFF_MAX"
  else
    # a healthy long run: reset the circuit.
    fast_exits=0
    backoff="$BACKOFF_MIN"
    sleep "$BACKOFF_MIN"
  fi
done
