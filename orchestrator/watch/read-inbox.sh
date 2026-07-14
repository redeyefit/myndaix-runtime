#!/usr/bin/env bash
# read-inbox.sh — the ONLY sanctioned way Watch reads inbox / state files (§3.2, §3.8, H2/B1).
# Path-locked: refuses anything outside the two allowed roots. Fail-closed on every edge.
#
#   read-inbox.sh                 # newest file in inbox/jefe/
#   read-inbox.sh <file>          # a specific file, MUST resolve under an allowed root
#
# Output is ALWAYS fenced by sanitize_untrusted — attacker-influenced verdict bodies never reach
# the model raw. This wrapper is pre-approved in settings.json; a bare Read/cat on these paths is
# NOT (that would bypass the fence).
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$DIR/watch-lib.sh"

INBOX="${WATCH_INBOX:-$HOME/.myndaix/bridge/inbox/jefe}"
# Allowed roots (realpath'd). Reads must resolve UNDER one of these — no traversal.
declare -a ROOTS=("$INBOX" "$WATCH_HOME")

resolve() { python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1" 2>/dev/null || true; }

under_allowed_root() {
  local target="$1" root rr
  for root in "${ROOTS[@]}"; do
    rr="$(resolve "$root")"
    [[ -n "$rr" ]] || continue
    # exact match or a path segment under the root (trailing slash guards prefix aliasing).
    [[ "$target" == "$rr" || "$target" == "$rr/"* ]] && return 0
  done
  return 1
}

target=""
if [[ $# -eq 0 ]]; then
  # newest regular file in the inbox (skip .tmp / dotfiles), fail-closed if none.
  target="$(find "$INBOX" -maxdepth 1 -type f ! -name '.*' ! -name '*.tmp' -print0 2>/dev/null \
            | xargs -0 ls -t 2>/dev/null | head -1 || true)"
  [[ -n "$target" ]] || { echo "read-inbox: inbox empty ($INBOX)" >&2; exit 1; }
else
  [[ $# -eq 1 ]] || { echo "read-inbox: exactly one argument" >&2; exit 2; }
  target="$1"
fi

real="$(resolve "$target")"
[[ -n "$real" ]] || { echo "read-inbox: cannot resolve path" >&2; exit 2; }
under_allowed_root "$real" || { echo "read-inbox: path outside allowed roots (refused)" >&2; watch_log "read-inbox REFUSE path=$real"; exit 2; }
[[ -f "$real" ]] || { echo "read-inbox: not a regular file" >&2; exit 2; }

watch_log "read-inbox ACCEPT path=$real"
# feed the file through the shared fence; label is the basename (defanged inside).
sanitize_untrusted "inbox:$(basename "$real")" < "$real"
