#!/usr/bin/env bash
# rc-bootstrap.sh — launchd entrypoint for the recall-librarian keepalive. Idempotent: ensure
# exactly ONE tmux session named `librarian` running rc-wrapper.sh on a PINNED socket, then exit.
# launchd fires it at load + on a StartInterval timer (ai.myndaix.librarian-rc.plist.example).
# NOT a daemon — it creates the session only if it has died, and refuses if parked.
#
# All liveness checks are scoped to OUR socket + session name. The Mini has foreign `claude`
# processes resident (the runtime pool, other agents); a global pgrep would false-match and a
# global cleanup could kill them.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$DIR/librarian-lib.sh"

SOCK="${LIB_TMUX_SOCK:-$HOME/.local/state/librarian.tmux}"   # pinned OUTSIDE $TMPDIR (reaped ~3d)
SESSION="librarian"
PARK_MARKER="$LIB_HOME/.parked"
# Pane command = the supervisor wrapper. Overridable ONLY for the test harness (a fake sleeper, so
# session-creation/idempotency can be exercised without launching claude); defaults to the real one.
WRAPPER="${LIB_WRAPPER_CMD:-$DIR/rc-wrapper.sh}"

mkdir -p "$(dirname "$SOCK")" "$LIB_HOME" 2>/dev/null || true

# (a) parked? refuse to (re)start — recovery is manual (README runbook). Don't thrash the park.
if [[ -e "$PARK_MARKER" ]]; then
  lib_log "bootstrap: park marker present, not starting ($PARK_MARKER)"
  exit 0
fi

# (b) workspace must be staged — the RC session's fence (CLAUDE.md + .claude/settings.json + the
# recall-gate) lives in LIB_WORKSPACE. Starting a session in an unstaged dir would run WITHOUT the
# deny-list/gate. Fail-closed: refuse to start if the fence is not present.
if [[ ! -f "$LIB_WORKSPACE/.claude/settings.json" ]]; then
  lib_log "bootstrap: workspace fence missing ($LIB_WORKSPACE/.claude/settings.json), not starting (fail-closed)"
  exit 0
fi

# (c) disk-free floor — RC JSONL transcripts share the disk with the Postgres ledger; the house has
# a logged no-space incident class, and the Mini runs tight on free space.
avail_kb="$(df -k "$LIB_HOME" 2>/dev/null | awk 'NR==2{print $4}' | tr -dc '0-9' || true)"
avail_kb="$((10#${avail_kb:-0}))"
# fail-closed: < 512MB (INCLUDING 0 = disk full or df failed/unparseable) -> do not start.
if (( avail_kb < 524288 )); then
  lib_log "bootstrap: LOW/UNKNOWN DISK ${avail_kb}KB free, not starting (fail-closed)"
  exit 0
fi

# (d) tmux protocol mismatch (brew upgraded tmux under a running server) — park-and-alert, never
# create a second session on a fresh socket.
probe="$(tmux -S "$SOCK" has-session -t "$SESSION" 2>&1 || true)"
if printf '%s' "$probe" | grep -qi 'protocol version mismatch'; then
  lib_log "bootstrap: tmux protocol mismatch on $SOCK"
  printf 'PARKED reason=tmux-protocol-mismatch ts=%s\n' "$(date '+%FT%T')" >"$PARK_MARKER" 2>/dev/null || true
  lib_alert "tmux-protocol-mismatch"
  exit 0
fi

# (e) already alive on OUR socket+name? done (idempotent).
if tmux -S "$SOCK" has-session -t "$SESSION" 2>/dev/null; then
  lib_log "bootstrap: session '$SESSION' already alive"
  exit 0
fi

# (f) create it. The wrapper is the pane command (so pane death == wrapper death). history-limit
# bounds scrollback. cd into LIB_WORKSPACE so the confined-dir identity + fence load (plist also
# sets WorkingDirectory, but the session cwd is what matters for RC).
lib_log "bootstrap: creating session '$SESSION' on $SOCK (cwd=$LIB_WORKSPACE)"
tmux -S "$SOCK" set-option -g history-limit 5000 \; \
     new-session -d -s "$SESSION" -c "$LIB_WORKSPACE" "$WRAPPER"
lib_log "bootstrap: session created"
