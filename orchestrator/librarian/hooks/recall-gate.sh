#!/usr/bin/env bash
# recall-gate.sh — PreToolUse Bash hook shim for the recall librarian session. Sets an explicit PATH
# then execs the python grammar logic so the hook's stdin (the tool JSON) flows straight through.
# All grammar lives in recall-gate.py — see its header.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$DIR/recall-gate.py"
