#!/usr/bin/env bash
# test-fix.sh — fixture-driven tests for play-fix.sh (PR-4 fix stage v1).
# Uses the MYNDAIX_FIX_PATCH_OVERRIDE test seam so it exercises the patch-policy,
# clean-base precheck, sandboxed verify, and verdict logic with NO pool/codex.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
PLAY="$SELF_DIR/play-fix.sh"
PY=/usr/bin/python3
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP" 2>/dev/null || true' EXIT
ORCH="$TMP/orch"; INBOX="$TMP/inbox"; REPO="$TMP/repo"
mkdir -p "$ORCH" "$INBOX" "$REPO"
pass=0; fail=0

# ---- fixture repo: a real bug (add does subtraction), one failing + one passing test ----
git -C "$REPO" init -q
printf 'def add(a, b):\n    return a - b   # BUG: should be +\n' > "$REPO/calc.py"
printf 'from calc import add\nassert add(2, 2) == 4, "add broken"\nprint("ok")\n' > "$REPO/test_add.py"
printf 'print("ok")\n' > "$REPO/test_other.py"
git -C "$REPO" add -A
git -C "$REPO" -c user.email=t@t -c user.name=t commit -qm init
BASE="$(git -C "$REPO" rev-parse HEAD)"

printf '{ "fixture": { "path": "%s", "verify": ["%s", "test_other.py"], "fail_to_pass": ["%s", "test_add.py"] } }\n' \
  "$REPO" "$PY" "$PY" > "$ORCH/repos.json"
printf 'fix the add() bug so add(2,2)==4\n' > "$TMP/fixlist.txt"

# ---- helpers to mint patches from the fixture working tree ----
mint(){ git -C "$REPO" diff > "$1"; git -C "$REPO" checkout -q -- .; git -C "$REPO" clean -fdq; }

# good fix: a-b -> a+b  (read BEFORE opening for write — w truncates)
"$PY" - "$REPO/calc.py" <<'PY'
import sys; p=sys.argv[1]; s=open(p).read().replace("a - b","a + b"); open(p,"w").write(s)
PY
mint "$TMP/good.patch"

# tamper: neuter the failing test, do NOT fix the bug
"$PY" - "$REPO/test_add.py" <<'PY'
import sys; p=sys.argv[1]; open(p,"w").write('from calc import add\nassert True\nprint("ok")\n')
PY
mint "$TMP/tamper.patch"

# symlink: a new symlink (policy must refuse)
ln -s /etc/passwd "$REPO/evil"; git -C "$REPO" add evil
git -C "$REPO" diff --cached > "$TMP/symlink.patch"
git -C "$REPO" reset -q; rm -f "$REPO/evil"

# non-applying patch (context not present at base)
printf -- '--- a/calc.py\n+++ b/calc.py\n@@ -99,1 +99,1 @@\n-nope\n+nah\n' > "$TMP/nonapply.patch"
: > "$TMP/empty.patch"

run(){ # run <repo_id> <patch>
  rm -f "$INBOX"/*.md 2>/dev/null || true
  MYNDAIX_ORCH="$ORCH" MYNDAIX_REPOS_JSON="$ORCH/repos.json" MYNDAIX_FIX_INBOX="$INBOX" \
    MYNDAIX_FIX_PATCH_OVERRIDE="$2" bash "$PLAY" "$1" "$BASE" "$TMP/fixlist.txt" >/dev/null 2>&1 || true
}
verdict(){ grep -h '^# fix ' "$INBOX"/*.md 2>/dev/null | head -1 | awk '{print $3}'; }
check(){ # check <label> <expected>
  local got; got="$(verdict)"
  if [[ "$got" == "$2" ]]; then echo "  ok: $1 -> $2"; pass=$((pass+1));
  else echo "  FAIL: $1 -> expected $2, got '${got:-<none>}'"; fail=$((fail+1)); fi
}

echo "1. genuine fix -> REGRESSION_CHECK_ONLY"
run fixture "$TMP/good.patch";     check "good fix" REGRESSION_CHECK_ONLY
echo "2. test-tampering -> TAMPERED"
run fixture "$TMP/tamper.patch";   check "neutered test" TAMPERED
echo "3. symlink -> UNVERIFIED (policy)"
run fixture "$TMP/symlink.patch";  check "symlink" UNVERIFIED
echo "4. non-applying patch -> UNVERIFIED"
run fixture "$TMP/nonapply.patch"; check "no-apply" UNVERIFIED
echo "5. empty patch -> NO_FIX"
run fixture "$TMP/empty.patch";    check "empty" NO_FIX
echo "6. unknown repo_id -> ABORTED (fail-closed)"
run ghost "$TMP/good.patch";       check "missing config" ABORTED

echo "7. bad base_sha -> ABORTED (fail-closed)"
rm -f "$INBOX"/*.md 2>/dev/null || true
MYNDAIX_ORCH="$ORCH" MYNDAIX_REPOS_JSON="$ORCH/repos.json" MYNDAIX_FIX_INBOX="$INBOX" \
  MYNDAIX_FIX_PATCH_OVERRIDE="$TMP/good.patch" bash "$PLAY" fixture deadbeef "$TMP/fixlist.txt" >/dev/null 2>&1 || true
check "bad sha" ABORTED

echo "8. over-cap fix-list -> ABORTED (fail-closed, no truncation)"
"$PY" -c "open('$TMP/big.txt','w').write('x'*70000)"
rm -f "$INBOX"/*.md 2>/dev/null || true
MYNDAIX_ORCH="$ORCH" MYNDAIX_REPOS_JSON="$ORCH/repos.json" MYNDAIX_FIX_INBOX="$INBOX" \
  MYNDAIX_FIX_PATCH_OVERRIDE="$TMP/good.patch" bash "$PLAY" fixture "$BASE" "$TMP/big.txt" >/dev/null 2>&1 || true
check "over-cap" ABORTED

echo
echo "=== $pass passed, $fail failed ==="
[[ "$fail" -eq 0 ]]
