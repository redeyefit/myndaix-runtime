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
# writes to /dev/null inside the sandbox: passes ONLY if the sbpl /dev/null allow is present,
# else PermissionError -> verify fails -> UNVERIFIED (regression test for the sandbox hardening)
printf 'open("/dev/null","w").write("x")\nprint("ok")\n' > "$REPO/test_devnull.py"
printf 'x = 1\n' > "$REPO/dummy.py"     # a tracked file to rename in the bypass test
git -C "$REPO" add -A
git -C "$REPO" -c user.email=t@t -c user.name=t commit -qm init
BASE="$(git -C "$REPO" rev-parse HEAD)"

# 'fixture' carries a fail_to_pass_template (selector tests); 'fixture_devnull' verifies via a
# test that writes /dev/null (sandbox-hardening regression) and has NO template (missing-template test)
jq -n --arg repo "$REPO" --arg py "$PY" '{
  fixture: { path: $repo, verify: [$py, "test_other.py"], fail_to_pass: [$py, "test_add.py"], fail_to_pass_template: [$py, "{TEST}"] },
  fixture_devnull: { path: $repo, verify: [$py, "test_devnull.py"], fail_to_pass: [$py, "test_add.py"] }
}' > "$ORCH/repos.json"
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

# rename-bypass: fix the bug AND rename a dummy file to a test-harness path (must still TAMPER)
"$PY" - "$REPO/calc.py" <<'PY'
import sys; p=sys.argv[1]; s=open(p).read().replace("a - b","a + b"); open(p,"w").write(s)
PY
git -C "$REPO" add calc.py; mkdir -p "$REPO/sub"; git -C "$REPO" mv dummy.py sub/conftest.py
git -C "$REPO" diff --cached > "$TMP/renamefix.patch"; git -C "$REPO" reset -q --hard "$BASE" >/dev/null

# nested .envrc (DENY_RE regardless of fix)
ln -sf /dev/null /dev/null 2>/dev/null || true
printf -- 'diff --git a/sub/.envrc b/sub/.envrc\nnew file mode 100644\nindex 0000000..1111111\n--- /dev/null\n+++ b/sub/.envrc\n@@ -0,0 +1 @@\n+export EVIL=1\n' > "$TMP/envrc.patch"

# secrets in the produced patch: fix the bug AND embed an AWS-key signature in a comment
"$PY" - "$REPO/calc.py" <<'PY'
import sys; p=sys.argv[1]; s=open(p).read().replace("a - b","a + b")
open(p,"w").write(s + "# AKIA0000000000000000\n")
PY
mint "$TMP/secret.patch"

# timeout: patch makes the imported module hang -> target test times out -> UNVERIFIED
"$PY" - "$REPO/calc.py" <<'PY'
import sys; p=sys.argv[1]
open(p,"w").write("import time\ndef add(a, b):\n    while True:\n        time.sleep(1)\n    return a + b\n")
PY
mint "$TMP/hang.patch"

# runtime tamper: calc.py rewrites a test file when imported (static policy can't see it)
"$PY" - "$REPO/calc.py" <<'PY'
import sys; p=sys.argv[1]
open(p,"w").write('open("test_add.py","w").write("assert True\\n")\ndef add(a, b):\n    return a + b\n')
PY
mint "$TMP/runtime_tamper.patch"

run(){ # run <repo_id> <patch>
  rm -f "$INBOX"/*.md 2>/dev/null || true
  MYNDAIX_ORCH="$ORCH" MYNDAIX_REPOS_JSON="$ORCH/repos.json" MYNDAIX_FIX_INBOX="$INBOX" \
    MYNDAIX_FIX_TEST_MODE=1 MYNDAIX_FIX_PATCH_OVERRIDE="$2" bash "$PLAY" "$1" "$BASE" "$TMP/fixlist.txt" >/dev/null 2>&1 || true
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
  MYNDAIX_FIX_TEST_MODE=1 MYNDAIX_FIX_PATCH_OVERRIDE="$TMP/good.patch" bash "$PLAY" fixture deadbeef "$TMP/fixlist.txt" >/dev/null 2>&1 || true
check "bad sha" ABORTED

echo "8. over-cap fix-list -> ABORTED (fail-closed, no truncation)"
"$PY" -c "open('$TMP/big.txt','w').write('x'*70000)"
rm -f "$INBOX"/*.md 2>/dev/null || true
MYNDAIX_ORCH="$ORCH" MYNDAIX_REPOS_JSON="$ORCH/repos.json" MYNDAIX_FIX_INBOX="$INBOX" \
  MYNDAIX_FIX_TEST_MODE=1 MYNDAIX_FIX_PATCH_OVERRIDE="$TMP/good.patch" bash "$PLAY" fixture "$BASE" "$TMP/big.txt" >/dev/null 2>&1 || true
check "over-cap" ABORTED

flagcheck(){ # flagcheck <substr> <label>
  if grep -hq "$1" "$INBOX"/*.md 2>/dev/null; then echo "  ok: $2"; pass=$((pass+1));
  else echo "  FAIL: $2 (expected '$1' in delivery)"; fail=$((fail+1)); fi
}

echo "9. rename-to-test bypass -> TAMPERED (NUL-safe policy, even with a real fix)"
run fixture "$TMP/renamefix.patch"; check "rename bypass" TAMPERED
echo "10. nested .envrc -> UNVERIFIED (DENY_RE)"
run fixture "$TMP/envrc.patch";    check "nested envrc" UNVERIFIED
echo "11. secret signature in patch -> REGRESSION_CHECK_ONLY but diff withheld + flagged"
run fixture "$TMP/secret.patch";   check "secret verdict" REGRESSION_CHECK_ONLY; flagcheck "secrets-hit" "secret flagged + withheld"
echo "12. hanging verify -> UNVERIFIED (timeout fires, script does not wedge)"
rm -f "$INBOX"/*.md 2>/dev/null || true
MYNDAIX_ORCH="$ORCH" MYNDAIX_REPOS_JSON="$ORCH/repos.json" MYNDAIX_FIX_INBOX="$INBOX" \
  MYNDAIX_FIX_TEST_MODE=1 MYNDAIX_FIX_TIMEOUT=2 MYNDAIX_FIX_PATCH_OVERRIDE="$TMP/hang.patch" bash "$PLAY" fixture "$BASE" "$TMP/fixlist.txt" >/dev/null 2>&1 || true
check "timeout" UNVERIFIED

echo "13. RUNTIME harness tampering (code rewrites a test on import) -> TAMPERED"
run fixture "$TMP/runtime_tamper.patch"; check "runtime tamper" TAMPERED

echo "14. override WITHOUT test-mode -> ABORTED (no production bypass)"
rm -f "$INBOX"/*.md 2>/dev/null || true
MYNDAIX_ORCH="$ORCH" MYNDAIX_REPOS_JSON="$ORCH/repos.json" MYNDAIX_FIX_INBOX="$INBOX" \
  MYNDAIX_FIX_PATCH_OVERRIDE="$TMP/good.patch" bash "$PLAY" fixture "$BASE" "$TMP/fixlist.txt" >/dev/null 2>&1 || true
check "ungated override" ABORTED

# ---- sandbox /dev/null hardening + per-fix fail_to_pass selector ----
run_sel(){ # run_sel <repo_id> <patch> <selector>
  rm -f "$INBOX"/*.md 2>/dev/null || true
  MYNDAIX_ORCH="$ORCH" MYNDAIX_REPOS_JSON="$ORCH/repos.json" MYNDAIX_FIX_INBOX="$INBOX" \
    MYNDAIX_FIX_TEST_MODE=1 MYNDAIX_FIX_PATCH_OVERRIDE="$2" bash "$PLAY" "$1" "$BASE" "$TMP/fixlist.txt" "$3" >/dev/null 2>&1 || true
}

echo "15. /dev/null write under sandbox -> REGRESSION_CHECK_ONLY (proves the sbpl allow)"
run fixture_devnull "$TMP/good.patch"; check "devnull allow" REGRESSION_CHECK_ONLY
echo "16. per-fix selector -> REGRESSION_CHECK_ONLY (substituted into trusted template)"
run_sel fixture "$TMP/good.patch" "test_add.py"; check "selector good" REGRESSION_CHECK_ONLY
echo "17. selector with shell metachars -> ABORTED (no injection)"
run_sel fixture "$TMP/good.patch" 'test_add.py;rm -rf /'; check "selector injection" ABORTED
echo "18. selector path traversal -> ABORTED"
run_sel fixture "$TMP/good.patch" '../calc.py'; check "selector traversal" ABORTED
echo "19. selector names a non-existent file -> ABORTED"
run_sel fixture "$TMP/good.patch" "test_ghost.py"; check "selector missing file" ABORTED
echo "20. selector supplied but repo has no template -> ABORTED"
run_sel fixture_devnull "$TMP/good.patch" "test_add.py"; check "selector no template" ABORTED

echo "21. \$ORCH under a read-denied \$HOME/.myndaix -> exec relocated outside it (REGRESSION_CHECK_ONLY)"
# the live default $ORCH is $HOME/.myndaix/orchestrator, which the sbpl read-denies; if the verify
# worktree/scratch lived there, git would die traversing the read-denied ancestor. The fix relocates
# exec OUTSIDE the deny tree. Reproduce by pointing HOME + $ORCH under a .myndaix path; exec (mktemp
# in the real TMPDIR) stays readable, so verify still reaches REGRESSION_CHECK_ONLY.
DENIED_ORCH="$TMP/.myndaix/orchestrator"; mkdir -p "$DENIED_ORCH"; cp "$ORCH/repos.json" "$DENIED_ORCH/repos.json"
rm -f "$INBOX"/*.md 2>/dev/null || true
HOME="$TMP" MYNDAIX_ORCH="$DENIED_ORCH" MYNDAIX_REPOS_JSON="$DENIED_ORCH/repos.json" MYNDAIX_FIX_INBOX="$INBOX" \
  MYNDAIX_FIX_TEST_MODE=1 MYNDAIX_FIX_PATCH_OVERRIDE="$TMP/good.patch" bash "$PLAY" fixture "$BASE" "$TMP/fixlist.txt" >/dev/null 2>&1 || true
check "read-denied run dir" REGRESSION_CHECK_ONLY

echo
echo "=== $pass passed, $fail failed ==="
[[ "$fail" -eq 0 ]]
