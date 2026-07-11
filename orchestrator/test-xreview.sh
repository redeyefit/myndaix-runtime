#!/usr/bin/env bash
# test-xreview.sh — smoke test for xreview.sh (phase-routed manual cross-family review) with a STUBBED
# mxr. Proves the routing AND the security invariants folded from two rounds of dogfooding:
#   CODE mode gates on kilabz via `mxr review` (staged snapshot); a kilabz failure is a HARD stop;
#   oracle is a degradable weak backup that gets the REAL fenced diff; a staging DEGRADATION is
#   surfaced (not swallowed); raw reviews print before synthesis; the upstream-input fence nonce and
#   the downstream-synthesis fence nonce DIFFER (no fence escape into lobster); reviewer output is
#   control-char sanitized; a missing objective-file does not abort the gate.
#   DESIGN mode leads with oracle, degrades if one is down, but FAILS CLOSED if BOTH are down.
# Run: bash orchestrator/test-xreview.sh
set -uo pipefail
SCRIPT="$(cd "$(dirname "$0")" && pwd)/xreview.sh"
ROOT="$(mktemp -d /tmp/xreview-test.XXXXXX)"; FAKE="$ROOT/home"
PASS=0; FAIL=0
trap 'rm -rf "$ROOT"' EXIT

mkdir -p "$FAKE/.local/bin"
cat > "$FAKE/.local/bin/mxr" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$HOME/mxr-argv.log" 2>/dev/null || true
if [[ "${1:-}" == "review" ]]; then            # `mxr review <agent> --repo .. --range ..` = code gate
  [[ -n "${STUB_KILABZ_FAIL:-}" ]] && exit 1
  [[ -n "${STUB_DEGRADE:-}" ]] && printf 'kilabz review ran WITHOUT snapshot (staging failed, inline-only)\n' >&2
  printf '%s\n' "${STUB_KILABZ:-KILABZ code review: real bug at line 1}"; exit 0
fi
# `mxr [--repo VAL].. <agent> <prompt>` — find the agent = first non-flag token (skip flag+value pairs)
argv=("$@"); i=0; agent=""
while [[ $i -lt ${#argv[@]} ]]; do
  case "${argv[$i]}" in
    --repo|--base-ref|--staged-workdir|--tip|--range|--prompt-file) i=$((i+2)); continue ;;
    --*) i=$((i+1)); continue ;;
    *) agent="${argv[$i]}"; break ;;
  esac
done
case "$agent" in
  oracle)  [[ -n "${STUB_ORACLE_FAIL:-}" ]] && exit 1; printf '%s\n' "${STUB_ORACLE:-ORACLE review: reframe the thesis}"; exit 0 ;;
  kilabz)  [[ -n "${STUB_KILABZ_FAIL:-}" ]] && exit 1; printf '%s\n' "${STUB_KILABZ:-KILABZ review: complete}"; exit 0 ;;
  lobster) printf '%s\n' "${STUB_TRIAGE:-1. fix the bug}"; exit 0 ;;
  *) printf 'stub:%s\n' "$agent"; exit 0 ;;
esac
STUB
chmod +x "$FAKE/.local/bin/mxr"
# openssl stub returns a DISTINCT value per call (via a counter) so the two nonces the script mints
# (nonce_in for upstream fences, nonce_syn for the lobster synthesis fence) are observably different.
cat > "$FAKE/.local/bin/openssl" <<'OSTUB'
#!/usr/bin/env bash
c="$HOME/.oc"; n=$(( $(cat "$c" 2>/dev/null || echo 0) + 1 )); printf '%s' "$n" > "$c"; printf 'nonce%08d\n' "$n"
OSTUB
chmod +x "$FAKE/.local/bin/openssl"

# a REAL throwaway git repo so code mode's _repo_path resolves (abs path -> has .git) and the
# oracle-backup `git diff <base> <head>` produces a real, non-empty diff to fence.
REPO="$ROOT/repo"; mkdir -p "$REPO"
git -C "$REPO" init -q
printf 'base-line\n' > "$REPO/f.txt"; git -C "$REPO" add f.txt
git -C "$REPO" -c user.email=t@t -c user.name=t commit -q -m base
printf 'head-line\n' > "$REPO/f.txt"
git -C "$REPO" -c user.email=t@t -c user.name=t commit -q -am head
RANGE="HEAD~1..HEAD"

DOC="$ROOT/design.md"; printf '# a design\n\nthesis: X\n' > "$DOC"
run(){ env HOME="$FAKE" PATH="$FAKE/.local/bin:$PATH" bash "$SCRIPT" "$@" 2>"$ROOT/err"; }
log(){ echo "$FAKE/mxr-argv.log"; }
ck(){ if grep -q "$2" "$1" 2>/dev/null; then echo "  ok: $3"; PASS=$((PASS+1)); else echo "  FAIL: $3"; FAIL=$((FAIL+1)); fi; }
ckx(){ if [[ "$1" == "$2" ]]; then echo "  ok: $3"; PASS=$((PASS+1)); else echo "  FAIL: $3 (rc $1 != $2)"; FAIL=$((FAIL+1)); fi; }
okv(){ if eval "$1"; then echo "  ok: $2"; PASS=$((PASS+1)); else echo "  FAIL: $2"; FAIL=$((FAIL+1)); fi; }

echo "1. code mode: kilabz GATE runs via 'mxr review' (staged snapshot), oracle gets the REAL fenced diff, verdict printed"; rm -f "$FAKE/mxr-argv.log" "$FAKE/.oc"
  out="$(run code "$REPO" "$RANGE")"; ckx $? 0 "code review exits 0"
  ck "$(log)" "review kilabz --repo $REPO --range $RANGE" "kilabz gate dispatched via mxr review (snapshot)"
  ck "$(log)" "BEGIN UNTRUSTED pushed-diff" "oracle backup gets a nonce-fenced diff (finding #3, r1)"
  ck "$(log)" "head-line" "the fenced diff carries the real change (base->head)"
  echo "$out" | grep -q "XREVIEW VERDICT (code)" && { echo "  ok: prints a synthesized code verdict"; PASS=$((PASS+1)); } || { echo "  FAIL: no code verdict"; FAIL=$((FAIL+1)); }
  # raw reviews printed to stdout BEFORE the synthesized verdict (finding #4, r1) — survive a lobster failure
  echo "$out" | grep -q "kilabz-review (authoritative" && { echo "  ok: raw kilabz review printed to stdout"; PASS=$((PASS+1)); } || { echo "  FAIL: raw review not printed"; FAIL=$((FAIL+1)); }

echo "2. code mode: the UPSTREAM-input nonce and the SYNTHESIS nonce DIFFER (finding #1, r2 — no fence escape into lobster)"; rm -f "$FAKE/mxr-argv.log" "$FAKE/.oc"
  run code "$REPO" "$RANGE" >/dev/null
  distinct="$(grep -o 'nonce=nonce[0-9]*' "$(log)" | sort -u | wc -l | tr -d ' ')"
  okv '[[ "$distinct" -ge 2 ]]' "at least 2 distinct fence nonces in play (got $distinct)"

echo "3. code mode: reviewer output is control-char sanitized on print (finding #3, r2)"; rm -f "$FAKE/mxr-argv.log" "$FAKE/.oc"
  esc="$(printf '\033')"
  out="$(env HOME="$FAKE" PATH="$FAKE/.local/bin:$PATH" STUB_KILABZ="$(printf '\033[31mANSI-HACK\033[0m real bug')" bash "$SCRIPT" code "$REPO" "$RANGE" 2>/dev/null)"
  echo "$out" | grep -q "ANSI-HACK" && { echo "  ok: review text content survives"; PASS=$((PASS+1)); } || { echo "  FAIL: review content lost"; FAIL=$((FAIL+1)); }
  if printf '%s' "$out" | LC_ALL=C grep -q "$esc"; then echo "  FAIL: raw ESC/ANSI reached stdout"; FAIL=$((FAIL+1)); else echo "  ok: ESC/ANSI stripped from printed output"; PASS=$((PASS+1)); fi

echo "4. code mode: a kilabz failure is a HARD stop (the gate)"; rm -f "$FAKE/mxr-argv.log" "$FAKE/.oc"
  env HOME="$FAKE" PATH="$FAKE/.local/bin:$PATH" STUB_KILABZ_FAIL=1 bash "$SCRIPT" code "$REPO" "$RANGE" >/dev/null 2>"$ROOT/err"; ckx $? 2 "kilabz gate failure -> exit 2"
  ck "$ROOT/err" "code gate" "error names the code gate"

echo "5. code mode: oracle down still produces a verdict (weak backup, review proceeds)"; rm -f "$FAKE/mxr-argv.log" "$FAKE/.oc"
  out="$(env HOME="$FAKE" PATH="$FAKE/.local/bin:$PATH" STUB_ORACLE_FAIL=1 bash "$SCRIPT" code "$REPO" "$RANGE" 2>/dev/null)"; ckx $? 0 "oracle-down code review still exits 0"
  echo "$out" | grep -q "XREVIEW VERDICT" && { echo "  ok: verdict produced on the kilabz gate alone"; PASS=$((PASS+1)); } || { echo "  FAIL: no verdict when oracle down"; FAIL=$((FAIL+1)); }

echo "6. code mode: a staging DEGRADATION is surfaced LOUDLY, not swallowed (finding #1, r1)"; rm -f "$FAKE/mxr-argv.log" "$FAKE/.oc"
  env HOME="$FAKE" PATH="$FAKE/.local/bin:$PATH" STUB_DEGRADE=1 bash "$SCRIPT" code "$REPO" "$RANGE" >/dev/null 2>"$ROOT/err"; ckx $? 0 "degraded gate still exits 0"
  ck "$ROOT/err" "DEGRADED" "staging degradation warned on stderr"

echo "7. code mode: a MISSING objective-file does NOT abort the gate (finding #4, r2)"; rm -f "$FAKE/mxr-argv.log" "$FAKE/.oc"
  out="$(run code "$REPO" "$RANGE" /no/such/obj.txt)"; ckx $? 0 "missing objf still exits 0"
  if grep -q -- "--prompt-file" "$(log)"; then echo "  FAIL: passed --prompt-file for a missing objf"; FAIL=$((FAIL+1)); else echo "  ok: no --prompt-file for a missing objf"; PASS=$((PASS+1)); fi

echo "8. code mode: an unresolvable repo is a usage error (exit 2)"; rm -f "$FAKE/mxr-argv.log" "$FAKE/.oc"
  run code /no/such/repo "$RANGE" >/dev/null 2>&1; ckx $? 2 "unresolvable repo -> exit 2"

echo "9. design mode: oracle LEADS (dispatched), verdict printed, no snapshot verb"; rm -f "$FAKE/mxr-argv.log" "$FAKE/.oc"
  out="$(run design "$DOC")"; ckx $? 0 "design review exits 0"
  if grep -q "oracle" "$(log)" && grep -q "kilabz" "$(log)"; then echo "  ok: both oracle (lead) + kilabz dispatched"; PASS=$((PASS+1)); else echo "  FAIL: design routing missing a family"; FAIL=$((FAIL+1)); fi
  if grep -q "^review " "$(log)"; then echo "  FAIL: design mode used the code snapshot verb"; FAIL=$((FAIL+1)); else echo "  ok: design mode embeds the doc (no snapshot verb)"; PASS=$((PASS+1)); fi

echo "10. design mode: ONE reviewer down degrades (not a hard stop) — still exits 0 with a verdict"; rm -f "$FAKE/mxr-argv.log" "$FAKE/.oc"
  out="$(env HOME="$FAKE" PATH="$FAKE/.local/bin:$PATH" STUB_ORACLE_FAIL=1 bash "$SCRIPT" design "$DOC" 2>"$ROOT/err")"; ckx $? 0 "oracle-down design review degrades, exits 0"
  ck "$ROOT/err" "DEGRADED" "degradation warned loudly"

echo "11. design mode: BOTH reviewers down -> FAIL CLOSED (finding #2, r2), no verdict"; rm -f "$FAKE/mxr-argv.log" "$FAKE/.oc"
  env HOME="$FAKE" PATH="$FAKE/.local/bin:$PATH" STUB_ORACLE_FAIL=1 STUB_KILABZ_FAIL=1 bash "$SCRIPT" design "$DOC" >/dev/null 2>"$ROOT/err"; ckx $? 2 "both design reviewers down -> exit 2"
  ck "$ROOT/err" "both design reviewers" "error names the both-reviewer failure"

echo "12. usage errors exit 2"; run bogus >/dev/null 2>&1; ckx $? 2 "bad mode -> exit 2"
  run code "$REPO" not-a-range >/dev/null 2>&1; ckx $? 2 "code without a range -> exit 2"
  run design /no/such/doc >/dev/null 2>&1; ckx $? 2 "design without a real doc -> exit 2"

echo; echo "=== $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]]
