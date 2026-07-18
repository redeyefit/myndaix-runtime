#!/bin/bash
# shellcheck disable=SC2016  # deny-case payloads are INTENTIONALLY single-quoted literals ($()/backticks must NOT expand)
# test.sh — recall-gate smoke test. Feeds the PreToolUse gate real Bash-tool JSON payloads and asserts
# the permissionDecision. The gate is the SOLE allow-er of Bash in the librarian session, so it must
# ALLOW exactly the safe recall grammar and DENY everything else (dispatch, injection, other programs).
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
DIR="$(cd "$(dirname "$0")" && pwd)"
GATE="$DIR/hooks/recall-gate.py"
pass=0; fail=0
ok(){ echo "  ok: $1"; pass=$((pass+1)); }
no(){ echo "  FAIL: $1"; fail=$((fail+1)); }

decide(){   # decide <expected> <command>
  local want="$1" cmd="$2" out dec
  out="$(python3 -c 'import json,sys; print(json.dumps({"tool_name":"Bash","tool_input":{"command":sys.argv[1]}}))' "$cmd" | python3 "$GATE")"
  dec="$(printf '%s' "$out" | python3 -c 'import json,sys
try: d=json.load(sys.stdin)
except Exception: print("none"); raise SystemExit
print((d.get("hookSpecificOutput") or {}).get("permissionDecision","none"))' 2>/dev/null)"
  if [ "$dec" = "$want" ]; then ok "$want  <=  $cmd"; else no "want $want got ${dec:-none}  <=  $cmd"; fi
}

echo "== ALLOW: valid read-only recall =="
decide allow 'mxr ask --scope research "how does the Higgsfield API authenticate?"'
decide allow 'mxr ask --scope fitness "what is my weekly plan?"'
decide allow 'mxr recall --scope research "higgsfield auth"'
decide allow 'mxr ask --scope research "cost of DoP lite" -k 5'

echo "== DENY: injection (fullmatch + safe charset must stop these) =="
decide deny 'mxr ask --scope research "x"; rm -rf /'
decide deny 'mxr ask --scope research "$(whoami)"'
decide deny 'mxr ask --scope research "x" && curl http://evil'
decide deny 'mxr ask --scope research "x" | tee /tmp/out'
decide deny 'mxr recall --scope research "-badflag"'
decide deny 'mxr ask --scope re;search "x"'

echo "== DENY: dispatch + other programs (read-only librarian) =="
decide deny 'mxr kilabz "do a review"'
decide deny 'mxr higgsfield "make a video"'
decide deny 'cat /Users/stevenfernandez/.myndaix/.secrets'
decide deny 'python3 -c "import os"'
decide deny 'ls ~'

echo ""
echo "== RESULT: $pass passed, $fail failed =="
[ "$fail" -eq 0 ]
