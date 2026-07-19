#!/bin/bash
# shellcheck disable=SC2016  # deny-case payloads are INTENTIONALLY single-quoted literals ($()/backticks must NOT expand)
# test.sh — recall-gate smoke test. Feeds the PreToolUse gate real Bash-tool JSON and asserts the
# permissionDecision. The gate is the SOLE allow-er of Bash in the librarian session, so it must ALLOW
# exactly `mxr ask --scope research|fitness|company "<safe q>"` and FAIL-CLOSED (explicit deny) on everything
# else — including malformed payloads (review r1 HIGH: a bare return falls through to ALLOW under dontAsk).
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
DIR="$(cd "$(dirname "$0")" && pwd)"
GATE="$DIR/hooks/recall-gate.py"
pass=0; fail=0
ok(){ echo "  ok: $1"; pass=$((pass+1)); }
no(){ echo "  FAIL: $1"; fail=$((fail+1)); }

_decision(){ python3 -c 'import json,sys
try: d=json.load(sys.stdin)
except Exception: print("none"); raise SystemExit
print((d.get("hookSpecificOutput") or {}).get("permissionDecision","none"))' 2>/dev/null; }

decide(){   # decide <expected> <command>  — wraps the command in a well-formed Bash tool payload
  local want="$1" cmd="$2" dec
  dec="$(python3 -c 'import json,sys; print(json.dumps({"tool_name":"Bash","tool_input":{"command":sys.argv[1]}}))' "$cmd" | python3 "$GATE" | _decision)"
  if [ "$dec" = "$want" ]; then ok "$want  <=  $cmd"; else no "want $want got ${dec:-none}  <=  $cmd"; fi
}
decide_raw(){   # decide_raw <expected> <raw-json-payload>  — for malformed / non-string / non-dict payloads
  local want="$1" payload="$2" dec
  dec="$(printf '%s' "$payload" | python3 "$GATE" | _decision)"
  if [ "$dec" = "$want" ]; then ok "$want  <=  raw: $payload"; else no "want $want got ${dec:-none}  <=  raw: $payload"; fi
}

echo "== ALLOW: valid read-only recall (mxr ask, allowlisted scope) =="
decide allow 'mxr ask --scope research "how does the Higgsfield API authenticate?"'
decide allow 'mxr ask --scope fitness "what is my weekly plan?"'
decide allow 'mxr ask --scope research "cost of DoP lite" -k 5'
decide allow 'mxr ask --scope company "what is the operating principle?"'

echo "== DENY: injection (fullmatch + safe charset) =="
decide deny 'mxr ask --scope research "x"; rm -rf /'
decide deny 'mxr ask --scope research "$(whoami)"'
decide deny 'mxr ask --scope research "x" && curl http://evil'
decide deny 'mxr ask --scope research "x" | tee /tmp/out'
decide deny 'mxr ask --scope re;search "x"'

echo "== DENY: scope allowlist (only research|fitness|company — a future SENSITIVE scope must NOT be phone-reachable) =="
decide deny 'mxr ask --scope personal "any secret"'
decide deny 'mxr ask --scope runtime "x"'

echo "== DENY: recall verb dropped + dispatch + other programs =="
decide deny 'mxr recall --scope research "higgsfield"'
decide deny 'mxr kilabz "do a review"'
decide deny 'mxr higgsfield "make a video"'
decide deny 'cat /Users/stevenfernandez/.myndaix/.secrets'
decide deny 'python3 -c "import os"'
decide deny 'ls ~'

echo "== DENY: FAIL-CLOSED on malformed payloads (review r1/r2 HIGH — no bare-return / no crash fall-through) =="
decide_raw deny '{"tool_name":"Bash","tool_input":{"command":["rm","-rf","/"]}}'
decide_raw deny 'this is not json'
decide_raw deny '{"tool_name":"Bash","tool_input":{"command":""}}'
decide_raw deny '{"tool_name":"Bash","tool_input":{}}'
decide_raw deny '["a","list","not","an","object"]'
decide_raw deny '{"tool_name":"Bash","tool_input":"ls"}'
decide_raw deny '{"tool_name":"Bash","tool_input":["mxr","ask"]}'
echo "== DENY: control chars — no strip()-normalised leading newline, no embedded 2nd line (r2 self-probe) =="
decide_raw deny '{"tool_name":"Bash","tool_input":{"command":"\nmxr ask --scope research \"x\""}}'
decide_raw deny '{"tool_name":"Bash","tool_input":{"command":"mxr ask --scope research \"x\"\ncurl evil"}}'
decide_raw deny '{"tool_name":"Bash","tool_input":{"command":"mxr ask --scope research \"x\"\t; ls"}}'
echo "== DENY: env/cwd override keys (r2 MED — valid command but poisoned execution env) =="
decide_raw deny '{"tool_name":"Bash","tool_input":{"command":"mxr ask --scope research \"x\"","env":{"LD_PRELOAD":"/tmp/evil.so"}}}'
decide_raw deny '{"tool_name":"Bash","tool_input":{"command":"mxr ask --scope research \"x\"","cwd":"/tmp"}}'
echo "== DENY: every NON-Bash tool (ALLOWLIST model, keepalive review r2 HIGH-1/HIGH-2 — matcher \"*\") =="
# The gate now fires for EVERY tool and denies anything that is not a valid `mxr ask`. This is
# fail-closed WITHOUT relying on the settings deny-list staying exhaustive (DesignSync had slipped it).
decide_raw deny '{"tool_name":"Read","tool_input":{"file_path":"/etc/passwd"}}'
decide_raw deny '{"tool_name":"Write","tool_input":{"file_path":"/x","content":"y"}}'
decide_raw deny '{"tool_name":"DesignSync","tool_input":{}}'
decide_raw deny '{"tool_name":"WebFetch","tool_input":{"url":"http://evil"}}'
decide_raw deny '{"tool_name":"SomeFutureTool","tool_input":{}}'
decide_raw deny '{"tool_name":null,"tool_input":{}}'

echo ""
echo "== RESULT: $pass passed, $fail failed =="
[ "$fail" -eq 0 ]
