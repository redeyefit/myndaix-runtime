#!/usr/bin/env bash
# test-deploy-sync.sh — smoke test for deploy-sync.sh. Uses a scratch DEST (never touches the real
# ~/.myndaix) and deploys from HEAD (the committed copies of the two surface files).
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYNC="$DIR/deploy-sync.sh"
REF="HEAD"                                            # committed content, branch-independent
SCRATCH="$(mktemp -d)"
export DEPLOY_SYNC_DEST="$SCRATCH"
trap 'rm -rf "$SCRATCH"' EXIT

PASS=0; FAIL=0
ok(){ PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad(){ FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$1"; }

echo "== --apply into an empty dest =="
"$SYNC" --apply "$REF" >/dev/null 2>&1 && ok "apply exits 0" || bad "apply should succeed"
[[ -f "$SCRATCH/play-fix.sh" && -f "$SCRATCH/play-review.sh" ]] && ok "both files deployed" || bad "files missing"
[[ -x "$SCRATCH/play-fix.sh" ]] && ok "deployed file is executable" || bad "not executable"
[[ ! -L "$SCRATCH/play-fix.sh" ]] && ok "play-fix.sh is a REGULAR file (not symlink)" || bad "symlink!"
[[ -s "$SCRATCH/.deployed-sha" ]] && ok "stamp .deployed-sha written" || bad "no stamp"

echo "== deployed content == committed blob =="
want="$(git -C "$DIR/.." rev-parse "$REF:orchestrator/play-fix.sh")"
have="$(git -C "$DIR/.." hash-object "$SCRATCH/play-fix.sh")"
[[ "$want" == "$have" ]] && ok "play-fix.sh hash matches HEAD blob" || bad "hash mismatch ($have != $want)"

echo "== --check on a clean deploy =="
"$SYNC" --check "$REF" >/dev/null 2>&1 && ok "check exits 0 when synced" || bad "check should pass"

echo "== --check DETECTS drift (exit 1) =="
printf '\n# tampered\n' >> "$SCRATCH/play-fix.sh"
if "$SYNC" --check "$REF" >/dev/null 2>&1; then bad "check must fail on drift"; else ok "check exits non-zero on drift"; fi

echo "== --apply HEALS drift + backs up =="
"$SYNC" --apply "$REF" >/dev/null 2>&1 && ok "re-apply exits 0" || bad "re-apply failed"
"$SYNC" --check "$REF" >/dev/null 2>&1 && ok "check clean after heal" || bad "still drifted after heal"
ls "$SCRATCH"/play-fix.sh.bak-* >/dev/null 2>&1 && ok "prior copy backed up" || bad "no backup made"

echo "== --apply HEALS a symlinked dest into a regular file (security invariant) =="
rm -f "$SCRATCH/play-fix.sh"; ln -s /etc/hosts "$SCRATCH/play-fix.sh"
"$SYNC" --apply "$REF" >/dev/null 2>&1 && ok "apply over a symlink exits 0" || bad "apply over symlink failed"
[[ ! -L "$SCRATCH/play-fix.sh" ]] && ok "symlinked dest replaced by a regular file" || bad "still a symlink"

echo "== --preflight is ADVISORY (always exit 0), even on drift =="
printf '\n# tampered again\n' >> "$SCRATCH/play-review.sh"
"$SYNC" --preflight "$REF" >/dev/null 2>&1 && ok "preflight exits 0 despite drift (cannot brick pool)" || bad "preflight must not fail"

echo ""
echo "== RESULT: $PASS passed, $FAIL failed =="
[[ "$FAIL" -eq 0 ]]
