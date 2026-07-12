#!/bin/bash
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
cd /tmp/shadow-dial-wt
export LEDGER_TEST_DSN=postgresql://localhost/runtime_test
export PYTHONPATH=/tmp/shadow-dial-wt/src
PY=/Users/stevenfernandez/code/active/myndaix-runtime/.venv/bin/python
fails=0
for t in tests/test_*.py; do
  if grep -q '^if __name__' "$t"; then
    r=$("$PY" "$t" 2>&1 | tail -1)
  else
    r=$("$PY" -m pytest -q "$t" 2>&1 | tail -1)
  fi
  if echo "$r" | grep -qE "ALL PASS|passed|SKIP"; then
    :
  else
    fails=$((fails+1))
    echo "FAIL $t: $r"
  fi
done
echo "matrix done, $fails failing suites"
exit "$fails"
