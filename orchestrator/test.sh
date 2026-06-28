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
printf '%s\t%s\t%s\n' "$agent" "${MXR_TIMEOUT_S:-unset}" "$*" >> "$HOME/.myndaix/mxr-argv.log" 2>/dev/null || true   # PR-0a: argv (+ MXR_TIMEOUT_S) so tests can assert scope flags + review-call timeout
case "$prompt" in
  *READY*) [[ "${STUB_CANARY_FAIL:-}" == "$agent" ]] && exit 1; echo READY; exit 0 ;;
esac
case "$agent" in
  kilabz)  [[ -n "${STUB_KILABZ_FAIL:-}" ]] && exit 1; echo "${STUB_REVIEW:-bug: line 1 returns a-b}" ;;
  lobster) [[ -n "${STUB_LOBSTER_FAIL:-}" ]] && exit 1; echo "${STUB_TRIAGE:-1. fix the subtraction}" ;;
  # `mxr skillselect ...` (+learning Step 4): default-OFF emits empty (models SKILLS_ENABLED
  # absent). STUB_ARMED lets a test inject a canned (already-fenced) hint region.
  skillselect) printf '%s' "${STUB_ARMED:-}" ;;
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
REPOS_JSON="$FAKE/.myndaix/orchestrator/repos.json"   # PLAY_AUTOFIX gate reads this
RUNS="$FAKE/.myndaix/orchestrator/runs"

# --- recording stub fixer, OUTSIDE the repo (the auto path rejects in-repo fixers). Records its
#     argv (overwrite) AND appends a per-call marker so we can assert fire-count. ---
FIXER="$ROOT/fake-play-fix.sh"
printf '%s\n' '#!/usr/bin/env bash' 'mkdir -p "$HOME/.myndaix" 2>/dev/null' \
  'printf "%s\n" "$#" "$@" > "$HOME/.myndaix/fixer-argv"' \
  'printf x >> "$HOME/.myndaix/fixer-calls"' 'exit 0' > "$FIXER"
chmod +x "$FIXER"
NULLCFG="$(printf '{"%s":{"path":"%s","fail_to_pass":null}}' "$(basename "$REPO")" "$REPO")"

reset(){ rm -rf "$FAKE/.myndaix"; }
run(){ env HOME="$FAKE" bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$TIP" refs/heads/main "${1:-}" 2>"$ROOT/stderr"; }
# armed run: PLAY_AUTOFIX on, test-seam fixer wired
run_af(){ env HOME="$FAKE" PLAY_AUTOFIX=1 PLAY_AUTOFIX_TEST_MODE=1 PLAY_FIX_SELF="$FIXER" \
            bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$TIP" refs/heads/main "${1:-}" 2>"$ROOT/stderr"; }
af_repos(){ mkdir -p "$(dirname "$REPOS_JSON")"; printf '%s' "$1" > "$REPOS_JSON"; }
wait_fixer(){ local i; for i in $(seq 1 40); do [[ -f "$FAKE/.myndaix/fixer-argv" ]] && return 0; sleep 0.1; done; return 1; }
settle(){ sleep 0.6; }   # let a (possible) detached fire either land or prove absent
latest(){ ls -t "$INBOX"/*.md 2>/dev/null | head -1; }
ck(){ # ck <label> <substr> <file-or-empty>
  local f="${3:-$(latest)}"
  if [[ -n "$f" && -f "$f" ]] && grep -q "$2" "$f"; then echo "  ok: $1"; PASS=$((PASS+1));
  else echo "  FAIL: $1 (want '$2')"; FAIL=$((FAIL+1)); fi
}
ckfile(){ if [[ -e "$1" ]]; then echo "  ok: $2"; PASS=$((PASS+1)); else echo "  FAIL: $2 (missing $1)"; FAIL=$((FAIL+1)); fi; }
cknofile(){ if [[ ! -e "$1" ]]; then echo "  ok: $2"; PASS=$((PASS+1)); else echo "  FAIL: $2 (exists $1)"; FAIL=$((FAIL+1)); fi; }
ckexit(){ if [[ "$1" == "$2" ]]; then echo "  ok: $3"; PASS=$((PASS+1)); else echo "  FAIL: $3 (rc $1 != $2)"; FAIL=$((FAIL+1)); fi; }
# gate-mode run: synchronous --worker with PLAY_GATE on; writes a structured verdict, no detach
gate_run(){ env HOME="$FAKE" PLAY_GATE=1 PLAY_GATE_VERDICT="$ROOT/verdict.json" PLAY_GATE_RUN_ID=run123 \
              bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$TIP" refs/heads/main "" 2>"$ROOT/stderr"; }

echo "1. NEEDS-FIX path";    reset; STUB_TRIAGE="1. fix it" run; ck "delivers NEEDS-FIX" "review NEEDS-FIX"
echo "2. clean PASS gate";   reset; STUB_TRIAGE="PLAY_PASS" run; ck "delivers PASS" "review PASS"
echo "3. canary failure";    reset; STUB_CANARY_FAIL=kilabz run; ck "aborts on canary" "ABORTED — canary"
echo "4. dedupe (2nd no-op)"; reset; STUB_TRIAGE="PLAY_PASS" run; before="$(ls "$INBOX" | wc -l)"; STUB_TRIAGE="PLAY_PASS" run; after="$(ls "$INBOX" | wc -l)"
  if [[ "$before" == "$after" ]]; then echo "  ok: 2nd run produced no new delivery"; PASS=$((PASS+1)); else echo "  FAIL: dedupe ($before -> $after)"; FAIL=$((FAIL+1)); fi
echo "5. daily cap";         reset; mkdir -p "$STATE"; printf 9999 > "$STATE/count-$(date +%Y%m%d)"; STUB_TRIAGE="PLAY_PASS" run; ck "aborts on cap" "ABORTED — cap"
echo "6. corrupt counter (numeric guard)"; reset; mkdir -p "$STATE"; printf 'garbage' > "$STATE/count-$(date +%Y%m%d)"; STUB_TRIAGE="PLAY_PASS" run; ck "survives corrupt counter" "review PASS"
echo "7. oversize diff FAILs fast (over the 256KB default cap)"; reset; head -c 300000 /dev/zero | tr '\0' 'x' > "$REPO/big.txt"; git -C "$REPO" add -A; git -C "$REPO" commit -qm big; BIGTIP="$(git -C "$REPO" rev-parse HEAD)"
  env HOME="$FAKE" bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$BIGTIP" refs/heads/main 2>/dev/null; ck "aborts oversize" "ABORTED — diff"
  git -C "$REPO" reset -q --hard "$TIP"   # restore
echo "7b. a ~100KB diff (over the OLD 64KB cap, under the new) now REVIEWS"; reset; head -c 100000 /dev/zero | tr '\0' 'y' > "$REPO/mid.txt"; git -C "$REPO" add -A; git -C "$REPO" commit -qm mid; MIDTIP="$(git -C "$REPO" rev-parse HEAD)"
  env HOME="$FAKE" STUB_TRIAGE="PLAY_PASS" bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$MIDTIP" refs/heads/main 2>/dev/null; ck "100KB diff reviews (not aborted)" "review PASS"
  git -C "$REPO" reset -q --hard "$TIP"   # restore
echo "7c. PLAY_MAX_DIFF knob still caps (env override)"; reset; head -c 5000 /dev/zero | tr '\0' 'z' > "$REPO/small.txt"; git -C "$REPO" add -A; git -C "$REPO" commit -qm small; SMTIP="$(git -C "$REPO" rev-parse HEAD)"
  env HOME="$FAKE" PLAY_MAX_DIFF=1000 bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$SMTIP" refs/heads/main 2>/dev/null; ck "PLAY_MAX_DIFF=1000 caps a 5KB diff" "ABORTED — diff"
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
  if [[ "$nscoped" -eq 3 ]]; then echo "  ok: kilabz+oracle reviews + triage carry --repo + --base-ref"; PASS=$((PASS+1)); else echo "  FAIL: want 3 scoped mxr calls (kilabz+oracle+lobster), got $nscoped"; FAIL=$((FAIL+1)); fi
  if grep -q "READY.*--repo" "$log" 2>/dev/null; then echo "  FAIL: canary must stay cap-exempt"; FAIL=$((FAIL+1)); else echo "  ok: canary cap-exempt (no --repo)"; PASS=$((PASS+1)); fi

echo "16b. +learning Step 4: hint injected into BOTH reviews only (not triage); skipped under gate"; reset
  STUB_ARMED="$(printf '===BEGIN UNTRUSTED armed-skill nonce=z===\nARMEDHINT review-skill body\n===END UNTRUSTED nonce=z===')" STUB_TRIAGE="PLAY_PASS" run
  alog="$FAKE/.myndaix/mxr-argv.log"
  nhit="$(grep -c "ARMEDHINT" "$alog" 2>/dev/null || true)"; [[ "$nhit" =~ ^[0-9]+$ ]] || nhit=0
  if [[ "$nhit" -eq 2 ]]; then echo "  ok: hint reaches exactly the kilabz + oracle prompts (not triage)"; PASS=$((PASS+1)); else echo "  FAIL: want hint in 2 review prompts, got $nhit (triage leak = 3)"; FAIL=$((FAIL+1)); fi
  reset; STUB_ARMED="ARMEDGATE" gate_run >/dev/null 2>&1 || true
  if grep -q "ARMEDGATE" "$FAKE/.myndaix/mxr-argv.log" 2>/dev/null; then echo "  FAIL: hint injected into the MERGE GATE (v0.3 §2 violation)"; FAIL=$((FAIL+1)); else echo "  ok: gate mode injects NO hint (the ! gate skip holds)"; PASS=$((PASS+1)); fi

echo "16c. review-call mxr timeout: push reviews wait 600, canary stays fast, gate stays 180"; reset; STUB_TRIAGE="PLAY_PASS" run
  tlog="$FAKE/.myndaix/mxr-argv.log"
  if grep -q $'^kilabz\t600\t' "$tlog" && grep -q $'^oracle\t600\t' "$tlog" && grep -q $'^lobster\t600\t' "$tlog"; then
    echo "  ok: all 3 push review calls wait 600s"; PASS=$((PASS+1)); else echo "  FAIL: push review calls not bumped to 600"; FAIL=$((FAIL+1)); fi
  if grep -q $'^kilabz\t180\t' "$tlog" && ! grep -q $'^kilabz\tunset\t' "$tlog"; then echo "  ok: canary EXPLICITLY clamped to 180 (no ambient MXR_TIMEOUT_S inherit)"; PASS=$((PASS+1)); else echo "  FAIL: canary not clamped to 180"; FAIL=$((FAIL+1)); fi
  reset; gate_run >/dev/null 2>&1 || true; glog="$FAKE/.myndaix/mxr-argv.log"
  if grep -q $'^kilabz\t180\t' "$glog" && ! grep -q $'^kilabz\t600\t' "$glog"; then
    echo "  ok: gate review calls stay 180 (fit automerge total budget)"; PASS=$((PASS+1)); else echo "  FAIL: gate review-call timeout wrong"; FAIL=$((FAIL+1)); fi

echo "17. PR-1a: front re-execs the FIXED installed worker, not the worktree copy"; reset
  mkdir -p "$FAKE/.myndaix/orchestrator"
  fixed="$FAKE/.myndaix/orchestrator/play-review.sh"
  printf '%s\n' '#!/usr/bin/env bash' 'mkdir -p "$HOME/.myndaix" 2>/dev/null' \
    'printf "%s" "$0" > "$HOME/.myndaix/which-self"' 'exit 0' > "$fixed"
  chmod +x "$fixed"
  ( cd "$REPO" && printf '%s %s %s %s\n' refs/heads/main "$TIP" refs/heads/main \
      0000000000000000000000000000000000000000 | env HOME="$FAKE" bash "$SCRIPT" origin "" ) >/dev/null 2>&1
  for _ in $(seq 1 30); do [[ -f "$FAKE/.myndaix/which-self" ]] && break; sleep 0.1; done
  if [[ -f "$FAKE/.myndaix/which-self" ]] && grep -q "/.myndaix/orchestrator/play-review.sh" "$FAKE/.myndaix/which-self"; then
    echo "  ok: worker re-exec'd the fixed install path"; PASS=$((PASS+1))
  else echo "  FAIL: worker did not re-exec the fixed path"; FAIL=$((FAIL+1)); fi

# ====================== PLAY_AUTOFIX flip (autonomous-fix trigger) ======================
echo "18. autofix fires with base_sha=TIP and exactly 3 args"; reset; af_repos "$NULLCFG"; STUB_TRIAGE="1. fix it" run_af
  if wait_fixer; then
    nargs="$(sed -n 1p "$FAKE/.myndaix/fixer-argv")"; a2="$(sed -n 3p "$FAKE/.myndaix/fixer-argv")"
    [[ "$nargs" == "3" ]] && { echo "  ok: exactly 3 args"; PASS=$((PASS+1)); } || { echo "  FAIL: argc=$nargs"; FAIL=$((FAIL+1)); }
    [[ "$a2" == "$TIP" ]] && { echo "  ok: arg2 == tip"; PASS=$((PASS+1)); } || { echo "  FAIL: arg2=$a2 want $TIP"; FAIL=$((FAIL+1)); }
    [[ "$a2" != "$EMPTY" ]] && { echo "  ok: arg2 != base"; PASS=$((PASS+1)); } || { echo "  FAIL: arg2 is base"; FAIL=$((FAIL+1)); }
  else echo "  FAIL: fixer never fired"; FAIL=$((FAIL+3)); fi
echo "19. autofix fires at most once per tip"; reset; af_repos "$NULLCFG"; STUB_TRIAGE="1. fix it" run_af; wait_fixer; STUB_TRIAGE="1. fix it" run_af; settle
  ncalls="$(wc -c < "$FAKE/.myndaix/fixer-calls" 2>/dev/null | tr -d ' ')"; [[ "$ncalls" =~ ^[0-9]+$ ]] || ncalls=0
  [[ "$ncalls" == "1" ]] && { echo "  ok: fired once across 2 same-tip runs"; PASS=$((PASS+1)); } || { echo "  FAIL: fired $ncalls times"; FAIL=$((FAIL+1)); }
echo "20. no fire when push unconfirmed"; reset; af_repos "$NULLCFG"; STUB_TRIAGE="1. fix it" run_af "/tmp/no-such-remote-$$"; settle
  cknofile "$FAKE/.myndaix/fixer-argv" "unconfirmed push -> no auto-fire"
echo "21. no fire when fail_to_pass non-null"; reset; af_repos "$(printf '{"%s":{"path":"%s","fail_to_pass":["x"]}}' "$(basename "$REPO")" "$REPO")"; STUB_TRIAGE="1. fix it" run_af; settle
  cknofile "$FAKE/.myndaix/fixer-argv" "static fail_to_pass -> no auto-fire"
echo "22. no fire when repo key missing"; reset; af_repos '{}'; STUB_TRIAGE="1. fix it" run_af; settle
  cknofile "$FAKE/.myndaix/fixer-argv" "untracked repo -> no auto-fire"
echo "23. no fire with no trusted install"; reset; af_repos "$NULLCFG"
  env HOME="$FAKE" PLAY_AUTOFIX=1 STUB_TRIAGE="1. fix it" bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$TIP" refs/heads/main 2>/dev/null; settle
  cknofile "$FAKE/.myndaix/fixer-argv" "no \$ORCH/play-fix.sh -> no auto-fire"
echo "24. reject a fixer resolving under the repo"; reset; af_repos "$NULLCFG"
  inrepo="$REPO/evil-play-fix.sh"; printf '%s\n' '#!/usr/bin/env bash' 'printf x >> "$HOME/.myndaix/fixer-calls"' 'exit 0' > "$inrepo"; chmod +x "$inrepo"
  env HOME="$FAKE" PLAY_AUTOFIX=1 PLAY_AUTOFIX_TEST_MODE=1 PLAY_FIX_SELF="$inrepo" STUB_TRIAGE="1. fix it" bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$TIP" refs/heads/main 2>/dev/null; settle
  cknofile "$FAKE/.myndaix/fixer-calls" "in-repo fixer rejected -> no fire"; rm -f "$inrepo"
echo "25. PASS branch never auto-fires nor writes fixlist"; reset; af_repos "$NULLCFG"; STUB_TRIAGE="PLAY_PASS" run_af; settle
  cknofile "$FAKE/.myndaix/fixer-argv" "PASS -> no auto-fire"
  if ls "$RUNS"/*/fixlist.txt >/dev/null 2>&1; then echo "  FAIL: PASS wrote fixlist.txt"; FAIL=$((FAIL+1)); else echo "  ok: PASS wrote no fixlist"; PASS=$((PASS+1)); fi
echo "26. armed-but-suppressed still delivers the manual hint"; reset; af_repos "$(printf '{"%s":{"path":"%s","fail_to_pass":["x"]}}' "$(basename "$REPO")" "$REPO")"; STUB_TRIAGE="1. fix it" run_af; settle
  ck "manual hint present despite suppressed fire" "to fix: play-fix.sh"
  cknofile "$FAKE/.myndaix/fixer-argv" "suppressed -> no fire"
echo "27. PLAY_AUTOFIX unset -> no fire, hint present"; reset; af_repos "$NULLCFG"; STUB_TRIAGE="1. fix it" run; settle
  ck "manual hint present" "to fix: play-fix.sh"; cknofile "$FAKE/.myndaix/fixer-argv" "not armed -> no fire"
echo "29. reject a SYMLINKED fixer (codex BLOCKER: symlink -> in-repo copy)"; reset; af_repos "$NULLCFG"
  ln -sf "$FIXER" "$ROOT/link-fixer.sh"
  env HOME="$FAKE" PLAY_AUTOFIX=1 PLAY_AUTOFIX_TEST_MODE=1 PLAY_FIX_SELF="$ROOT/link-fixer.sh" STUB_TRIAGE="1. fix it" bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$TIP" refs/heads/main 2>/dev/null; settle
  cknofile "$FAKE/.myndaix/fixer-argv" "symlinked fixer rejected -> no fire"; rm -f "$ROOT/link-fixer.sh"
echo "28. play-fix.sh is byte-identical to origin/main (frozen)"; reset
  rr="$(cd "$(dirname "$SCRIPT")/.." && pwd)"
  if git -C "$rr" rev-parse --verify -q origin/main >/dev/null 2>&1; then
    if git -C "$rr" diff --quiet origin/main -- orchestrator/play-fix.sh; then echo "  ok: play-fix.sh unchanged vs origin/main"; PASS=$((PASS+1)); else echo "  FAIL: play-fix.sh modified by this branch"; FAIL=$((FAIL+1)); fi
  else echo "  skip: no origin/main to compare"; fi

echo "30. durable flag-file enables auto-fire WITHOUT PLAY_AUTOFIX env"; reset; af_repos "$NULLCFG"
  mkdir -p "$FAKE/.myndaix/orchestrator"; : > "$FAKE/.myndaix/orchestrator/AUTOFIX_ENABLED"
  env HOME="$FAKE" PLAY_AUTOFIX_TEST_MODE=1 PLAY_FIX_SELF="$FIXER" STUB_TRIAGE="1. fix it" bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$TIP" refs/heads/main 2>/dev/null
  if wait_fixer; then echo "  ok: flag-file armed -> fired"; PASS=$((PASS+1)); else echo "  FAIL: flag-file did not arm"; FAIL=$((FAIL+1)); fi
  a2="$(sed -n 3p "$FAKE/.myndaix/fixer-argv" 2>/dev/null)"; [[ "$a2" == "$TIP" ]] && { echo "  ok: flag-file fire uses base=tip"; PASS=$((PASS+1)); } || { echo "  FAIL: flag-file fire arg2=$a2"; FAIL=$((FAIL+1)); }

echo "31. PLAY_DISABLE_AUTOFIX=1 HARD-overrides the durable flag (controller-loop, codex BLOCKER)"; reset; af_repos "$NULLCFG"
  mkdir -p "$FAKE/.myndaix/orchestrator"; : > "$FAKE/.myndaix/orchestrator/AUTOFIX_ENABLED"
  env HOME="$FAKE" PLAY_DISABLE_AUTOFIX=1 PLAY_AUTOFIX=1 PLAY_AUTOFIX_TEST_MODE=1 PLAY_FIX_SELF="$FIXER" STUB_TRIAGE="1. fix it" bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$TIP" refs/heads/main 2>/dev/null; settle
  cknofile "$FAKE/.myndaix/fixer-argv" "disable flag suppresses fire even with durable flag + PLAY_AUTOFIX"

echo "32. GATE PASS -> structured verdict PASS, exit 0, no inbox/done (automerge gate)"; reset; rm -f "$ROOT/verdict.json"
  STUB_TRIAGE="PLAY_PASS" gate_run; ckexit $? 0 "gate PASS exits 0"
  ck "verdict says PASS" '"verdict":"PASS"' "$ROOT/verdict.json"
  ck "run_id+head threaded into verdict" '"run_id":"run123"' "$ROOT/verdict.json"
  cknofile "$STATE/done-$TIP" "gate writes NO done marker"
  if [[ -z "$(ls "$INBOX" 2>/dev/null)" ]]; then echo "  ok: gate delivers nothing to inbox"; PASS=$((PASS+1)); else echo "  FAIL: gate delivered to inbox"; FAIL=$((FAIL+1)); fi
echo "33. GATE NEEDS-FIX -> verdict NEEDS-FIX, exit 1"; reset; rm -f "$ROOT/verdict.json"
  STUB_TRIAGE="1. fix the subtraction" gate_run; ckexit $? 1 "gate NEEDS-FIX exits 1"
  ck "verdict says NEEDS-FIX" '"verdict":"NEEDS-FIX"' "$ROOT/verdict.json"
echo "34. GATE requires Oracle -> oracle-down is TRANSIENT (verdict ABORTED, exit 2 -> retry, NOT a permanent NEEDS-FIX)"; reset; rm -f "$ROOT/verdict.json"
  STUB_CANARY_FAIL=oracle STUB_TRIAGE="PLAY_PASS" gate_run; ckexit $? 2 "oracle-down under gate exits 2 (transient)"
  ck "transient verdict (distinct from a real NEEDS-FIX)" '"verdict":"ABORTED"' "$ROOT/verdict.json"

echo; echo "=== $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]]
