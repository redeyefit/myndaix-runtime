#!/usr/bin/env bash
# rc-bootstrap.sh — launchd entrypoint for Watch (§3.1). Idempotent: ensure exactly ONE tmux
# session named `watch` running rc-wrapper.sh on a PINNED socket, then exit. launchd fires it on a
# timer + at load (orchestrator/watch/ai.myndaix.rc-keepalive.plist.example). NOT a daemon.
#
# All liveness checks are scoped to OUR socket + session name — the Mini has foreign `claude`
# processes resident (a claude-max-api proxy, a bare interactive claude); a global pgrep would
# false-match and a global cleanup could kill them.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$DIR/watch-lib.sh"

SOCK="${WATCH_TMUX_SOCK:-$HOME/.local/state/watch.tmux}"   # pinned OUTSIDE $TMPDIR (reaped ~3d)
SESSION="watch"
PARK_MARKER="$WATCH_HOME/.parked"

mkdir -p "$(dirname "$SOCK")" "$WATCH_HOME" 2>/dev/null || true

# (a) parked? refuse to (re)start — recovery is manual (README runbook). Don't thrash the park.
if [[ -e "$PARK_MARKER" ]]; then
  watch_log "bootstrap: park marker present, not starting ($PARK_MARKER)"
  exit 0
fi

# (b) disk-free floor — JSONL transcripts share the disk with the Postgres ledger; the house has
# a logged no-space incident class, and the Mini shows only ~29Gi available.
avail_kb="$(df -k "$WATCH_HOME" 2>/dev/null | awk 'NR==2{print $4}' | tr -dc '0-9' || true)"
avail_kb="$((10#${avail_kb:-0}))"
# fail-closed: < 512MB (INCLUDING 0 = disk full or df failed/unparseable) -> do not start.
if (( avail_kb < 524288 )); then
  watch_log "bootstrap: LOW/UNKNOWN DISK ${avail_kb}KB free, not starting (fail-closed)"
  exit 0
fi

# (c) tmux protocol mismatch (brew upgraded tmux under a running server) — park-and-alert, never
# create a second session on a fresh socket.
probe="$(tmux -S "$SOCK" has-session -t "$SESSION" 2>&1 || true)"
if printf '%s' "$probe" | grep -qi 'protocol version mismatch'; then
  watch_log "bootstrap: tmux protocol mismatch on $SOCK"
  printf 'PARKED reason=tmux-protocol-mismatch ts=%s\n' "$(date '+%FT%T')" >"$PARK_MARKER" 2>/dev/null || true
  watch_alert "tmux-protocol-mismatch"
  exit 0
fi

# (d) already alive on OUR socket+name? done (idempotent).
if tmux -S "$SOCK" has-session -t "$SESSION" 2>/dev/null; then
  watch_log "bootstrap: session '$SESSION' already alive"
  exit 0
fi

# (e) create it. The wrapper is the pane command (so pane death == wrapper death). history-limit
# bounds scrollback (M1). cd into WATCH_HOME belt-and-suspenders (plist also sets WorkingDirectory).
watch_log "bootstrap: creating session '$SESSION' on $SOCK"
tmux -S "$SOCK" set-option -g history-limit 5000 \; \
     new-session -d -s "$SESSION" -c "$WATCH_HOME" "$DIR/rc-wrapper.sh"
watch_log "bootstrap: session created"
