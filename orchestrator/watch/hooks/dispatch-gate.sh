#!/usr/bin/env bash
# dispatch-gate.sh — PreToolUse hook (matcher: "Bash") for the Watch session (§3.2, HIGH-3).
# Thin shim: sets an explicit PATH, then execs the python logic so the hook's stdin (the tool
# JSON) flows straight through. All grammar lives in dispatch-gate.py — see its header.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$DIR/dispatch-gate.py"
