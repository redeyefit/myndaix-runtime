#!/usr/bin/env bash
# test.sh — smoke test for play-review.sh. Drives the WORKER against a throwaway
# git repo with a STUBBED mxr/osascript (no real dispatch, no runtime, no 300s).
# Run: bash orchestrator/test.sh   (exits non-zero if any case fails)
set -uo pipefail

SCRIPT="$(cd "$(dirname "$0")" && pwd)/play-review.sh"
ROOT="$(mktemp -d /tmp/playrev-test.XXXXXX)"
FAKE="$ROOT/home"
REPO="$ROOT/repo"
PASS=0; FAIL=0
trap 'rm -rf "$ROOT"' EXIT

# --- stub mxr + osascript on the fake HOME's PATH (script puts ~/.local/bin first) ---
mkdir -p "$FAKE/.local/bin"
cat > "$FAKE/.local/bin/mxr" <<'STUB'
#!/usr/bin/env bash
agent="$1"; prompt="$2"
printf '%s\t%s\n' "$agent" "$*" >> "$HOME/.myndaix/mxr-argv.log" 2>/dev/null || true   # PR-0a: record argv so the test can assert scope flags
case "$prompt" in
  *READY*) [[ "${STUB_CANARY_FAIL:-}" == "$agent" ]] && exit 1; echo READY; exit 0 ;;
esac
case "$agent" in
  kilabz)  [[ -n "${STUB_KILABZ_FAIL:-}" ]] && exit 1; echo "${STUB_REVIEW:-bug: line 1 returns a-b}" ;;
  lobster) [[ -n "${STUB_LOBSTER_FAIL:-}" ]] && exit 1; echo "${STUB_TRIAGE:-1. fix the subtraction}" ;;
  *) echo "stub:$agent" ;;
esac
STUB
printf '%s\n' '#!/usr/bin/env bash' 'mkdir -p "$HOME/.myndaix" 2>/dev/null' 'echo called >> "$HOME/.myndaix/osascript-calls"' 'exit 0' > "$FAKE/.local/bin/osascript"
chmod +x "$FAKE/.local/bin/mxr" "$FAKE/.local/bin/osascript"

# --- a throwaway git repo with one real commit ---
git init -q "$REPO"
git -C "$REPO" config user.email t@t; git -C "$REPO" config user.name t
printf 'def add(a,b): return a-b\n' > "$REPO/m.py"
git -C "$REPO" add -A; git -C "$REPO" commit -qm init
TIP="$(git -C "$REPO" rev-parse HEAD)"
EMPTY=4b825dc642cb6eb9a060e54bf8d69288fbee4904
INBOX="$FAKE/.myndaix/bridge/inbox/jefe"
STATE="$FAKE/.myndaix/orchestrator/state"

reset(){ rm -rf "$FAKE/.myndaix"; }
run(){ env HOME="$FAKE" bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$TIP" refs/heads/main "${1:-}" 2>"$ROOT/stderr"; }
latest(){ ls -t "$INBOX"/*.md 2>/dev/null | head -1; }
ck(){ # ck <label> <substr> <file-or-empty>
  local f="${3:-$(latest)}"
  if [[ -n "$f" && -f "$f" ]] && grep -q "$2" "$f"; then echo "  ok: $1"; PASS=$((PASS+1));
  else echo "  FAIL: $1 (want '$2')"; FAIL=$((FAIL+1)); fi
}
ckfile(){ if [[ -e "$1" ]]; then echo "  ok: $2"; PASS=$((PASS+1)); else echo "  FAIL: $2 (missing $1)"; FAIL=$((FAIL+1)); fi; }
cknofile(){ if [[ ! -e "$1" ]]; then echo "  ok: $2"; PASS=$((PASS+1)); else echo "  FAIL: $2 (exists $1)"; FAIL=$((FAIL+1)); fi; }

echo "1. NEEDS-FIX path";    reset; STUB_TRIAGE="1. fix it" run; ck "delivers NEEDS-FIX" "review NEEDS-FIX"
echo "2. clean PASS gate";   reset; STUB_TRIAGE="PLAY_PASS" run; ck "delivers PASS" "review PASS"
echo "3. canary failure";    reset; STUB_CANARY_FAIL=kilabz run; ck "aborts on canary" "ABORTED — canary"
echo "4. dedupe (2nd no-op)"; reset; STUB_TRIAGE="PLAY_PASS" run; before="$(ls "$INBOX" | wc -l)"; STUB_TRIAGE="PLAY_PASS" run; after="$(ls "$INBOX" | wc -l)"
  if [[ "$before" == "$after" ]]; then echo "  ok: 2nd run produced no new delivery"; PASS=$((PASS+1)); else echo "  FAIL: dedupe ($before -> $after)"; FAIL=$((FAIL+1)); fi
echo "5. daily cap";         reset; mkdir -p "$STATE"; printf 9999 > "$STATE/count-$(date +%Y%m%d)"; STUB_TRIAGE="PLAY_PASS" run; ck "aborts on cap" "ABORTED — cap"
echo "6. corrupt counter (numeric guard)"; reset; mkdir -p "$STATE"; printf 'garbage' > "$STATE/count-$(date +%Y%m%d)"; STUB_TRIAGE="PLAY_PASS" run; ck "survives corrupt counter" "review PASS"
echo "7. oversize diff FAILs fast"; reset; head -c 70000 /dev/zero | tr '\0' 'x' > "$REPO/big.txt"; git -C "$REPO" add -A; git -C "$REPO" commit -qm big; BIGTIP="$(git -C "$REPO" rev-parse HEAD)"
  env HOME="$FAKE" bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$BIGTIP" refs/heads/main 2>/dev/null; ck "aborts oversize" "ABORTED — diff"
  git -C "$REPO" reset -q --hard "$TIP"   # restore
echo "8. contention records a visible skip"; reset; mkdir -p "$STATE/lock"; STUB_TRIAGE="PLAY_PASS" run; ck "delivers SKIPPED" "review SKIPPED"; ckfile "$STATE/SKIPPED-$TIP" "SKIPPED sentinel written"
echo "9. stale lock reaped"; reset; mkdir -p "$STATE/lock"; touch -t 202001010000 "$STATE/lock"; STUB_TRIAGE="PLAY_PASS" run; ck "reaps stale lock + reviews" "review PASS"
echo "10. embedded-whitespace token is NOT a pass"; reset; STUB_TRIAGE="P L A Y _ P A S S" run; ck "spaced token -> NEEDS-FIX" "review NEEDS-FIX"
echo "11. unconfirmed push is NOT deduped"; reset; STUB_TRIAGE="PLAY_PASS" run "/tmp/no-such-remote-$$"; ck "still delivers PASS" "review PASS"; cknofile "$STATE/done-$TIP" "unconfirmed push not marked done"
echo "12. delivery failure is NOT deduped"; reset; mkdir -p "$INBOX"; chmod 000 "$INBOX"; STUB_TRIAGE="PLAY_PASS" run; chmod 755 "$INBOX"; cknofile "$STATE/done-$TIP" "lost delivery not marked done"
echo "13. empty PLAY_IMESSAGE_TO disables the ping"; reset; PLAY_IMESSAGE_TO="" STUB_TRIAGE="PLAY_PASS" run; ck "still delivers PASS" "review PASS"; cknofile "$FAKE/.myndaix/osascript-calls" "no iMessage send when disabled"
echo "14. same SHA on a DIFFERENT ref does not confirm"; reset; bare="$ROOT/bare.git"; git init -q --bare "$bare"; git -C "$REPO" push -q "$bare" "$TIP:refs/heads/other" 2>/dev/null; STUB_TRIAGE="PLAY_PASS" run "$bare"; cknofile "$STATE/done-$TIP" "tip on wrong ref not deduped"
echo "15. SHA on the TARGET ref confirms"; reset; bare2="$ROOT/bare2.git"; git init -q --bare "$bare2"; git -C "$REPO" push -q "$bare2" "$TIP:refs/heads/main" 2>/dev/null; STUB_TRIAGE="PLAY_PASS" run "$bare2"; ckfile "$STATE/done-$TIP" "tip on target ref deduped"

echo "16. PR-0a: scope flags forwarded to mxr (repo bucket + reviewed SHA)"; reset; STUB_TRIAGE="PLAY_PASS" run
  rid="$(basename "$REPO")"; log="$FAKE/.myndaix/mxr-argv.log"
  nscoped="$(grep -c -- "--repo $rid --base-ref $TIP" "$log" 2>/dev/null || true)"; [[ "$nscoped" =~ ^[0-9]+$ ]] || nscoped=0
  if [[ "$nscoped" -eq 2 ]]; then echo "  ok: review+triage carry --repo + --base-ref"; PASS=$((PASS+1)); else echo "  FAIL: want 2 scoped mxr calls, got $nscoped"; FAIL=$((FAIL+1)); fi
  if grep -q "READY.*--repo" "$log" 2>/dev/null; then echo "  FAIL: canary must stay cap-exempt"; FAIL=$((FAIL+1)); else echo "  ok: canary cap-exempt (no --repo)"; PASS=$((PASS+1)); fi

echo; echo "=== $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]]
