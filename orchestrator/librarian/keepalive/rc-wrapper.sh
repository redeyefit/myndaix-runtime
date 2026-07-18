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
# (allows ONLY `mxr ask --scope research|fitness "<safe q>"`). This wrapper's job is liveness +
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
# runtime; `fitness` needs its root here. (A future sensitive scope must be added deliberately.)
export MYNDAIX_KNOWLEDGE_SCOPES="${MYNDAIX_KNOWLEDGE_SCOPES:-fitness=$HOME/fitness}"

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
  lib_log "parked — sleeping; recovery = claude auth login -> rm $PARK_MARKER -> kickstart"
  # keep the pane (and thus the tmux session) alive so the operator can attach and the bootstrap
  # does not thrash. Recovery is manual (see the header / README runbook).
  while true; do sleep 3600; done
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

# startup assertion: the fence must be present, else the session would run unconfined. Park loud
# rather than serve an unfenced RC session.
if [[ ! -f "$LIB_WORKSPACE/.claude/settings.json" ]]; then
  park "workspace-fence-missing ($LIB_WORKSPACE/.claude/settings.json)"
fi
# and `mxr` must resolve, else every ask is command-not-found (deaf librarian).
if ! command -v mxr >/dev/null 2>&1; then
  park "mxr-not-on-PATH (expected \$HOME/.local/bin/mxr)"
fi

fast_exits=0
backoff="$BACKOFF_MIN"
lib_log "wrapper start (remote-control, capacity 1, workspace=$LIB_WORKSPACE)"

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

  start="$(date +%s)"
  lib_log "launch claude remote-control --capacity 1 --spawn same-dir --permission-mode dontAsk"
  # cd belt (tmux new-session -c already sets cwd, but ensure identity+fence load from here).
  # --spawn same-dir: NOT worktree (LIB_WORKSPACE is not a git repo; worktree mode would fail).
  # --permission-mode dontAsk: spawned sessions are non-interactive (no human to answer prompts);
  #   the deny-list + recall-gate fully fence them. --name for a clear label in claude.ai/mobile.
  ( cd "$LIB_WORKSPACE" && env -u CLAUDE_CODE_OAUTH_TOKEN -u ANTHROPIC_API_KEY \
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
