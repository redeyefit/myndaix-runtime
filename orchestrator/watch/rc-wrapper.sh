#!/usr/bin/env bash
# rc-wrapper.sh — the in-tmux supervisor for Watch's remote-control session (§3.1).
# tmux runs THIS as the pane command, so pane death == wrapper death (has-session is a true
# supervisor proxy — L1). The wrapper runs `claude` as a CHILD and loops on its exit (it does NOT
# exec — exec would end the loop; the "wrapper is the pane command" property is preserved because
# tmux runs the wrapper, not claude).
#
# Loop: reachability gate -> clean-env launch of `claude remote-control --capacity 1` -> on exit,
# classify. 3 consecutive sub-5s exits (auth expiry / flag churn / quota refusal) -> PARK: write
# marker, fire ONE narrow iMessage alert, sleep infinity (HIGH-1 — keep the pane alive for the
# SSH runbook; exiting would destroy the session and strand recovery).
set -uo pipefail                                  # NOT -e: a child non-zero exit is normal here
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$DIR/watch-lib.sh"

# The read wrappers are invoked by BARE name (CLAUDE.md rule 3; the hook allows the bare form).
# They live symlinked in $WATCH_HOME/bin, which the default PATH above does NOT include — without
# this prepend, `claude` inherits a PATH where `read-inbox`/`mxr-read` are command-not-found and
# Watch's entire read surface is silently dead (r3 HIGH).
export PATH="$WATCH_HOME/bin:$PATH"

PARK_MARKER="$WATCH_HOME/.parked"
FAST_EXIT_SECS=5
FAST_EXIT_LIMIT=3
BACKOFF_MIN=5
BACKOFF_MAX=600

# ---- HIGH-5: clean auth-env boundary, established ONCE up front ----
# We deliberately do NOT source the Mini's secrets/load.sh here — sourcing arbitrary code to fetch
# one value is the exact re-injection vector HIGH-5 named (a re-exported token or a proxy
# ANTHROPIC_BASE_URL would break RC or false-park). Instead the alert recipient comes ONLY from a
# dedicated single-value file (or a pre-set env from the plist). No sourced code runs in this
# process at all, so the boundary is airtight by construction.
if [[ -z "${WATCH_ALERT_IMESSAGE_TO-}" && -r "$WATCH_HOME/.alert-to" ]]; then
  WATCH_ALERT_IMESSAGE_TO="$(head -c 128 "$WATCH_HOME/.alert-to" | tr -dc 'A-Za-z0-9@._+-')"
fi
export WATCH_ALERT_IMESSAGE_TO
unset CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY ANTHROPIC_BASE_URL 2>/dev/null || true
# No MCP tools in the Watch session (r2 HIGH: under dontAsk, MCP file-read tools would be
# auto-allowed and bypass the read fence). remote-control has no --strict-mcp-config flag; the
# env var disables MCP loading entirely (v2.1.150+).
export CLAUDE_CODE_DISABLE_MCP=1

park() {
  local reason="$1"
  printf 'PARKED reason=%s ts=%s\n' "$reason" "$(date '+%FT%T')" >"$PARK_MARKER" 2>/dev/null || true
  watch_log "PARK: $reason"
  watch_alert "$reason"
  watch_log "parked — sleeping; recovery = claude auth login -> rm $PARK_MARKER -> kickstart"
  # HIGH-1: keep the pane (and thus the tmux session) alive so the operator can attach and the
  # bootstrap does not thrash. Recovery is manual (see the header / README runbook).
  while true; do sleep 3600; done
}

reachable() {
  # reachable if we get ANY HTTP response from the API host (401 counts). M2: on failure, one
  # IP-literal probe splits DNS failure from routing failure for the log.
  if curl -sS -m5 -o /dev/null "https://api.anthropic.com/" 2>/dev/null; then return 0; fi
  if curl -sS -m5 -o /dev/null "https://1.1.1.1/" 2>/dev/null; then
    watch_log "unreachable: api.anthropic.com fails but 1.1.1.1 ok -> DNS problem"
  else
    watch_log "unreachable: 1.1.1.1 also fails -> routing/ISP down"
  fi
  return 1
}

# startup assertion: the read surface must actually be reachable, else Watch is deaf. Park loud
# rather than run blind (r3 HIGH).
if ! command -v read-inbox >/dev/null 2>&1 || ! command -v mxr-read >/dev/null 2>&1; then
  park "read-wrappers-not-on-PATH (expected in $WATCH_HOME/bin)"
fi

fast_exits=0
backoff="$BACKOFF_MIN"
watch_log "wrapper start (capacity 1)"

while true; do
  # a lingering park marker means a prior park; bootstrap should have refused to (re)start us, but
  # belt-and-suspenders: honor it.
  if [[ -e "$PARK_MARKER" ]]; then park "stale-park-marker-present"; fi

  if ! reachable; then
    sleep "$backoff"
    backoff=$(( backoff * 2 )); (( backoff > BACKOFF_MAX )) && backoff="$BACKOFF_MAX"
    continue                                       # network gaps do NOT count as fast-exits
  fi

  # HIGH-5: assert the clean env IMMEDIATELY before launch; refuse to hand a dirty auth env to RC
  # (which would hard-fail every relaunch). env -u is the belt.
  if [[ -n "${CLAUDE_CODE_OAUTH_TOKEN-}" || -n "${ANTHROPIC_API_KEY-}" ]]; then
    park "dirty-auth-env"
  fi
  case "${ANTHROPIC_BASE_URL-}" in
    ""|https://api.anthropic.com|https://api.anthropic.com/) : ;;
    *) park "nonstandard-ANTHROPIC_BASE_URL" ;;
  esac

  start="$(date +%s)"
  watch_log "launch claude remote-control --capacity 1"
  env -u CLAUDE_CODE_OAUTH_TOKEN -u ANTHROPIC_API_KEY \
      claude remote-control --capacity 1
  rc=$?
  end="$(date +%s)"
  dur=$(( end - start ))
  watch_log "claude exited rc=$rc after ${dur}s"

  if (( dur < FAST_EXIT_SECS )); then
    fast_exits=$(( fast_exits + 1 ))
    watch_log "fast-exit ${fast_exits}/${FAST_EXIT_LIMIT}"
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
