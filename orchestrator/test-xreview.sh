#!/usr/bin/env bash
# test-xreview.sh — smoke test for xreview.sh (phase-routed manual cross-family review) with a STUBBED
# mxr. Proves the routing: CODE mode gates on kilabz via `mxr review` (staged snapshot); a kilabz
# failure is a HARD stop; oracle is a degradable weak backup. DESIGN mode leads with oracle and
# degrades (not stops) if oracle is down. Run: bash orchestrator/test-xreview.sh
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
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$FAKE/.local/bin/openssl"; chmod +x "$FAKE/.local/bin/openssl"

DOC="$ROOT/design.md"; printf '# a design\n\nthesis: X\n' > "$DOC"
run(){ env HOME="$FAKE" PATH="$FAKE/.local/bin:$PATH" bash "$SCRIPT" "$@" 2>"$ROOT/err"; }
log(){ echo "$FAKE/mxr-argv.log"; }
ck(){ if grep -q "$2" "$1" 2>/dev/null; then echo "  ok: $3"; PASS=$((PASS+1)); else echo "  FAIL: $3"; FAIL=$((FAIL+1)); fi; }
ckx(){ if [[ "$1" == "$2" ]]; then echo "  ok: $3"; PASS=$((PASS+1)); else echo "  FAIL: $3 (rc $1 != $2)"; FAIL=$((FAIL+1)); fi; }

echo "1. code mode: kilabz GATE runs via 'mxr review' (staged snapshot), verdict printed"; rm -f "$FAKE/mxr-argv.log"
  out="$(run code myndaix-runtime aaaa..bbbb)"; ckx $? 0 "code review exits 0"
  ck "$(log)" "^review kilabz --repo myndaix-runtime --range aaaa..bbbb" "kilabz gate dispatched via mxr review (snapshot)"
  echo "$out" | grep -q "XREVIEW VERDICT (code)" && { echo "  ok: prints a synthesized code verdict"; PASS=$((PASS+1)); } || { echo "  FAIL: no code verdict"; FAIL=$((FAIL+1)); }

echo "2. code mode: a kilabz failure is a HARD stop (the gate)"; rm -f "$FAKE/mxr-argv.log"
  env HOME="$FAKE" PATH="$FAKE/.local/bin:$PATH" STUB_KILABZ_FAIL=1 bash "$SCRIPT" code myndaix-runtime aaaa..bbbb >/dev/null 2>"$ROOT/err"; ckx $? 2 "kilabz gate failure -> exit 2"
  ck "$ROOT/err" "code gate" "error names the code gate"

echo "3. code mode: oracle down still produces a verdict (weak backup, review proceeds)"; rm -f "$FAKE/mxr-argv.log"
  out="$(env HOME="$FAKE" PATH="$FAKE/.local/bin:$PATH" STUB_ORACLE_FAIL=1 bash "$SCRIPT" code myndaix-runtime aaaa..bbbb 2>/dev/null)"; ckx $? 0 "oracle-down code review still exits 0"
  echo "$out" | grep -q "XREVIEW VERDICT" && { echo "  ok: verdict produced on the kilabz gate alone"; PASS=$((PASS+1)); } || { echo "  FAIL: no verdict when oracle down"; FAIL=$((FAIL+1)); }

echo "4. design mode: oracle LEADS (dispatched), verdict printed"; rm -f "$FAKE/mxr-argv.log"
  out="$(run design "$DOC")"; ckx $? 0 "design review exits 0"
  if grep -q "oracle" "$(log)" && grep -q "kilabz" "$(log)"; then echo "  ok: both oracle (lead) + kilabz dispatched"; PASS=$((PASS+1)); else echo "  FAIL: design routing missing a family"; FAIL=$((FAIL+1)); fi
  # oracle must be dispatched WITHOUT a staged snapshot (no 'review' verb / no --range) in design mode
  if grep -q "^review " "$(log)"; then echo "  FAIL: design mode used the code snapshot verb"; FAIL=$((FAIL+1)); else echo "  ok: design mode embeds the doc (no snapshot verb)"; PASS=$((PASS+1)); fi

echo "5. design mode: oracle DOWN degrades (not a hard stop) — still exits 0 with a verdict"; rm -f "$FAKE/mxr-argv.log"
  out="$(env HOME="$FAKE" PATH="$FAKE/.local/bin:$PATH" STUB_ORACLE_FAIL=1 bash "$SCRIPT" design "$DOC" 2>"$ROOT/err")"; ckx $? 0 "oracle-down design review degrades, exits 0"
  ck "$ROOT/err" "DEGRADED" "degradation warned loudly"

echo "6. usage errors exit 2"; run bogus >/dev/null 2>&1; ckx $? 2 "bad mode -> exit 2"
  run code myndaix-runtime not-a-range >/dev/null 2>&1; ckx $? 2 "code without a range -> exit 2"
  run design /no/such/doc >/dev/null 2>&1; ckx $? 2 "design without a real doc -> exit 2"

echo; echo "=== $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]]
