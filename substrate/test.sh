#!/usr/bin/env bash
# test.sh — substrate smoke + security harness. Runs against throwaway fixtures only;
# never touches live ~/.myndaix, the live ledger, or launchd. The launchd-bootstrap /
# serve-restart / live psql migration probe are LIVE-verified at deploy (LAB first).
#
# Run: substrate/test.sh
set -uo pipefail   # NOT -e: every check runs; we tally pass/fail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUB="$REPO/substrate"
export PYTHONPATH="$SUB"
CP="$SUB/config_parse.py"; RP="$SUB/render_plist.py"; MF="$SUB/manifest.py"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0
ok(){ if eval "$1"; then echo "  ok: $2"; pass=$((pass+1)); else echo "  XX: $2"; fail=$((fail+1)); fi; }

# plget.py — cross-platform plist validate/extract (macOS `plutil` is unavailable in Linux CI).
# `plget.py <file>` validates it parses (exit nonzero on malformed XML); `<file> <dot.path>` prints
# a value. This is a STRONGER check than `plutil -lint` — it round-trips through the same plistlib.
cat > "$TMP/plget.py" <<'PYEOF'
import plistlib, sys
with open(sys.argv[1], "rb") as fh:
    d = plistlib.load(fh)
if len(sys.argv) == 3:
    cur = d
    for k in sys.argv[2].split("."):
        cur = cur[k]
    sys.stdout.write(str(cur))
PYEOF
PG="$TMP/plget.py"

good_factory(){ cat > "$1" <<EOF
MACHINE_ROLE=factory
MYNDAIX_HOME=$TMP/home
MYNDAIX_DSN=postgresql://127.0.0.1/runtime
MYNDAIX_WORK_DSN=postgresql://127.0.0.1/runtime_work
OPERATOR_INBOX=$TMP/home/inbox
AUTHOR_ALLOWLIST=bot-one,bot-two
DEPLOY_CLONE=$REPO
EOF
}

echo "== config_parse: happy + fail-closed =="
good_factory "$TMP/good.env"
ok 'python3 "$CP" "$TMP/good.env" >/dev/null' "valid factory config -> exit 0"
ok '[[ "$(python3 "$CP" "$TMP/good.env" --get DEPLOY_CLONE)" == "$REPO" ]]' "--get DEPLOY_CLONE resolves"
printf 'MACHINE_ROLE=lab\nMYNDAIX_HOME=%s\nMYNDAIX_DSN=x; touch %s/pwned\n' "$TMP" "$TMP" > "$TMP/inj.env"
python3 "$CP" "$TMP/inj.env" >/dev/null 2>&1
ok '[[ ! -e "$TMP/pwned" ]]' "S3: config value NEVER executed (no source/eval)"
printf 'MACHINE_ROLE=lab\nMYNDAIX_HOME=/x\nMYNDAIX_DSN=postgresql://127.0.0.1/r\nEVIL=1\n' > "$TMP/unk.env"
ok '! python3 "$CP" "$TMP/unk.env" >/dev/null 2>&1' "unknown key -> fail-closed"
printf 'MACHINE_ROLE=lab\nMYNDAIX_HOME=/a/../../etc\nMYNDAIX_DSN=postgresql://127.0.0.1/r\n' > "$TMP/trav.env"
ok '! python3 "$CP" "$TMP/trav.env" >/dev/null 2>&1' "S1: path traversal .. -> fail-closed"
printf 'MACHINE_ROLE=banana\nMYNDAIX_HOME=/x\nMYNDAIX_DSN=postgresql://127.0.0.1/r\n' > "$TMP/role.env"
ok '! python3 "$CP" "$TMP/role.env" >/dev/null 2>&1' "invalid MACHINE_ROLE -> fail-closed"
printf 'MACHINE_ROLE=factory\nMYNDAIX_HOME=/x\nMYNDAIX_DSN=postgresql://127.0.0.1/r\nOPERATOR_INBOX=/i\nAUTHOR_ALLOWLIST=\n' > "$TMP/empty.env"
ok '! python3 "$CP" "$TMP/empty.env" >/dev/null 2>&1' "empty AUTHOR_ALLOWLIST on factory -> fail-OPEN guard rejects"
printf 'MACHINE_ROLE=lab\nMYNDAIX_HOME=/x\nMYNDAIX_DSN=postgresql://127.0.0.1/r\n' > "$TMP/lab.env"
ok 'python3 "$CP" "$TMP/lab.env" >/dev/null 2>&1' "lab w/o inbox+allowlist -> exit 0"

echo "== render_plist: all descriptors lint-valid + XML-injection safe =="
for d in "$SUB"/plists/*.json; do
  out="$TMP/$(basename "$d" .json).plist"
  python3 "$RP" render "$d" "$TMP/good.env" > "$out" 2>/dev/null
  ok 'python3 "$PG" "$out" >/dev/null 2>&1' "render+parse $(basename "$d")"
done
ok 'python3 "$RP" role-check "$SUB/plists/ai.myndaix.controller.json" factory' "role-check controller applies to factory"
ok '! python3 "$RP" role-check "$SUB/plists/ai.myndaix.controller.json" lab' "role-check controller NOT on lab"
# PLAY_SELF env_literal injection points at the deploy clone (Option A)
python3 "$RP" render "$SUB/plists/ai.myndaix.controller.json" "$TMP/good.env" > "$TMP/ctl.plist"
ok '[[ "$(python3 "$PG" "$TMP/ctl.plist" EnvironmentVariables.PLAY_SELF)" == "$REPO/orchestrator/play-review.sh" ]]' "Option A: PLAY_SELF injected = deploy-clone play-review.sh"
# reconcile poll interval placeholder
python3 "$RP" render "$SUB/plists/ai.myndaix.reconcile.json" "$TMP/good.env" > "$TMP/rec.plist"
ok '[[ "$(python3 "$PG" "$TMP/rec.plist" StartInterval)" == "900" ]]' "reconcile StartInterval = POLL default 900"
# S2: XML injection — a DSN with & < > ]]> parses valid + round-trips byte-exact
printf 'MACHINE_ROLE=factory\nMYNDAIX_HOME=/x/.myndaix\nMYNDAIX_DSN=postgresql://u:p&a<b>c]]>@127.0.0.1/r\nOPERATOR_INBOX=/i\nAUTHOR_ALLOWLIST=bot\nDEPLOY_CLONE=%s\n' "$REPO" > "$TMP/evil.env"
python3 "$RP" render "$SUB/plists/ai.myndaix.controller.json" "$TMP/evil.env" > "$TMP/evil.plist" 2>/dev/null
ok 'python3 "$PG" "$TMP/evil.plist" >/dev/null 2>&1' "S2: &<>]]> in DSN -> plist still parses valid"
ok '[[ "$(python3 "$PG" "$TMP/evil.plist" EnvironmentVariables.MYNDAIX_DSN)" == "postgresql://u:p&a<b>c]]>@127.0.0.1/r" ]]' "S2: DSN round-trips byte-exact (plistlib escaped, no wedged bootstrap)"

echo "== manifest: build + check =="
good_factory "$TMP/mf.env"    # good_factory already sets DEPLOY_CLONE=$REPO
ok 'python3 "$MF" build "$TMP/mf.env" >/dev/null 2>&1' "manifest build -> exit 0"
ok '[[ "$(python3 "$MF" build "$TMP/mf.env" 2>/dev/null | python3 -c "import json,sys;print(json.load(sys.stdin)[\"migration_head\"])")" == "dial_shadow_snapshot" ]]' "manifest records migration head object"
# config_hash strips DSN userinfo (no secrets in the receipt)
ok 'python3 "$MF" build "$TMP/evil.env" 2>/dev/null | grep -q config_hash && ! python3 "$MF" build "$TMP/evil.env" 2>/dev/null | grep -q "u:p&a"' "manifest config_hash carries no DSN userinfo"

echo "== reconcile: arg + update-bootstrap + dry-run non-destructive + converge-guard =="
ok '! MYNDAIX_HOME="$TMP/home" /bin/bash "$SUB/reconcile.sh" --bogus >/dev/null 2>&1' "unknown arg -> nonzero"
mkdir -p "$TMP/home"; cp "$TMP/good.env" "$TMP/home/config.env"
MYNDAIX_HOME="$TMP/home" /bin/bash "$SUB/reconcile.sh" --update-bootstrap >/dev/null 2>&1
ok '[[ -x "$TMP/home/bin/bootstrap-fetch" ]]' "--update-bootstrap installs static fetcher"
ok 'diff -q "$SUB/bootstrap-fetch.sh" "$TMP/home/bin/bootstrap-fetch" >/dev/null' "installed fetcher == repo source"
# dry-run against a LAB config so no factory converge; DEPLOY_CLONE=repo (read-only fetch)
printf 'MACHINE_ROLE=lab\nMYNDAIX_HOME=%s/home\nMYNDAIX_DSN=postgresql://127.0.0.1/runtime\nDEPLOY_CLONE=%s\n' "$TMP" "$REPO" > "$TMP/home/config.env"
sha_before="$(git -C "$REPO" rev-parse HEAD)"
MYNDAIX_HOME="$TMP/home" /bin/bash "$SUB/reconcile.sh" --dry-run > "$TMP/dry.out" 2>&1; drc=$?
ok '[[ "$sha_before" == "$(git -C "$REPO" rev-parse HEAD)" ]]' "E6: dry-run did NOT change HEAD"
ok '[[ ! -e "$TMP/home/state/RUNNING_SHA" ]]' "E6: dry-run wrote no receipt"
ok '! grep -q "dispatching Stage-0" "$TMP/dry.out"' "E6: dry-run never triggered Stage-0 reset"
ok '[[ "$drc" -eq 1 ]]' "dry-run reports drift (exit 1) vs un-converged fixture"
ok '! RECONCILE_BOOTSTRAPPED=1 MYNDAIX_HOME="$TMP/home" /bin/bash "$SUB/reconcile.sh" >/dev/null 2>&1' "converge on lab role -> nonzero (factory-only)"
# E3/E4: missing / invalid config fail-closed with no restart
rm -f "$TMP/home/config.env"
ok '! RECONCILE_BOOTSTRAPPED=1 MYNDAIX_HOME="$TMP/home" /bin/bash "$SUB/reconcile.sh" --dry-run >/dev/null 2>&1' "E3: missing config.env -> fail-closed"

echo "== M4 automerge denylist (S4) — substrate self-deploy guard =="
# Helper: check a path's classification (paths via argv to dodge shell-quoting of the eval).
cat > "$TMP/dc.py" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])                       # <repo>/src
from runtime.automerge import is_denylisted, _doc_path
path, expect = sys.argv[2], sys.argv[3]
r = {"doc": _doc_path(path), "nondoc": not _doc_path(path), "deny": is_denylisted(path)}[expect]
sys.exit(0 if r else 1)
PYEOF
DC="$TMP/dc.py"; SRC="$REPO/src"
# automerge.py imports the ledger (asyncpg) at module load -> use the venv python if present.
PY="$REPO/.venv/bin/python"; [[ -x "$PY" ]] || PY="python3"
if "$PY" -c 'import asyncpg' >/dev/null 2>&1; then
  ok '"$PY" "$DC" "$SRC" substrate/reconcile.sh nondoc' "substrate/reconcile.sh NOT docs-only (blocked as non-.md)"
  ok '"$PY" "$DC" "$SRC" substrate/plists/ai.myndaix.reconcile.json nondoc' "substrate config json NOT docs-only"
  ok '"$PY" "$DC" "$SRC" substrate/runbook.md deny' "substrate/runbook.md denylisted (docs under substrate)"
  ok '"$PY" "$DC" "$SRC" substrate/runbook.md nondoc' "substrate/runbook.md NOT auto-mergeable"
  ok '"$PY" "$DC" "$SRC" docs/notes.md doc' "plain docs/notes.md STILL auto-mergeable (no over-block)"
else
  echo "  --: SKIP M4 denylist checks (no asyncpg-capable python; run CI or the venv)"
fi

echo "== migration head pin matches the highest migration (risk #3 CI guard) =="
HEAD_OBJ="$(cat "$SUB/migration_head.txt")"
LATEST_MIG="$(ls "$REPO"/src/runtime/ledger/migrations/*.sql | sort | tail -1)"
# ANCHOR HEAD_OBJ to the create-TARGET position (right after CREATE <type> [IF NOT EXISTS]) so a
# stale pin can't false-pass on `CREATE INDEX foo ON <stale_obj>` (adversarial review MED).
ok 'grep -qiE "^[[:space:]]*CREATE (TABLE|VIEW|MATERIALIZED VIEW|INDEX)( IF NOT EXISTS)?[[:space:]]+\"?'"$HEAD_OBJ"'\"?([[:space:]]|\(|$)" "$LATEST_MIG"' "migration_head.txt ($HEAD_OBJ) is the object DEFINED by the newest migration ($(basename "$LATEST_MIG"))"
# a stale-index false-positive must NOT pass: a CREATE INDEX whose ON-target is HEAD_OBJ is not a match
ok '! grep -qiE "^[[:space:]]*CREATE INDEX( IF NOT EXISTS)?[[:space:]]+'"$HEAD_OBJ"'[[:space:]]+ON" "$LATEST_MIG" || true' "head-pin not satisfied by a CREATE INDEX ... ON <obj> (anchor sanity)"

echo "== QUIESCE_LABELS (bootstrap-fetch) == MUTATING_TICKS (reconcile) == mutating descriptors =="
cat > "$TMP/qcheck.py" <<'PYEOF'
import json, re, sys, glob, os
sub = sys.argv[1]
want = sorted(json.load(open(d))["label"] for d in glob.glob(os.path.join(sub, "plists", "*.json"))
              if json.load(open(d)).get("mutating"))
def arr(path, name):
    txt = open(path).read()
    m = re.search(name + r"=\(([^)]*)\)", txt)
    return sorted(m.group(1).split()) if m else []
q = arr(os.path.join(sub, "bootstrap-fetch.sh"), "QUIESCE_LABELS")
r = arr(os.path.join(sub, "reconcile.sh"), "MUTATING_TICKS")
if want == q == r:
    sys.exit(0)
sys.stderr.write(f"MISMATCH want={want} quiesce={q} reconcile={r}\n"); sys.exit(1)
PYEOF
ok 'python3 "$TMP/qcheck.py" "$SUB"' "the two hardcoded quiesce lists match the mutating:true descriptors"

echo "== manifest drift-list fails TOWARD drift on an unresolvable SHA =="
cat > "$TMP/drcheck.py" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])   # substrate dir
import manifest
base = {"origin_sha": None, "deploy_sha": None, "plists_expected": {}, "plists_installed": {}, "labels_loaded": {}}
assert manifest.drift_list(base), "unresolvable SHA must be drift"
ok = dict(base, origin_sha="a"*40, deploy_sha="a"*40)
assert not manifest.drift_list(ok), "resolved-equal SHA must be clean"
print("ok")
PYEOF
ok 'python3 "$TMP/drcheck.py" "$SUB" >/dev/null 2>&1' "origin_sha=None -> drift; equal SHAs -> clean"

echo "== cross-family folds: SQL guard, orphan-prune scoping, RUNNING_SHA-last, orphan drift =="
# HEAD_OBJ interpolated into SQL is guarded by a strict-identifier regex + die (MAJOR)
ok 'grep -qE "migration_head.txt not a plain identifier" "$SUB/reconcile.sh"' "reconcile validates HEAD_OBJ as an identifier before SQL"
# orphan prune is SCOPED to state/managed_labels — NEVER a wildcard over ai.myndaix.* (risk #1)
ok 'grep -q "MANAGED_REC" "$SUB/reconcile.sh"' "orphan prune reads the recorded managed-label set"
ok '! grep -qE "for .* in .*(LA_DIR|LaunchAgents).*/ai\.myndaix\.\*\.plist" "$SUB/reconcile.sh"' "orphan prune does NOT iterate a bare ai.myndaix.* plist glob"
# RUNNING_SHA is written AFTER manifest.json (commit marker last) — MAJOR
ok 'python3 - "$SUB/reconcile.sh" <<PYEOF
import sys
t = open(sys.argv[1]).read()
sys.exit(0 if t.index("manifest.json.tmp") < t.index("RUNNING_SHA.tmp") else 1)
PYEOF' "manifest.json built before RUNNING_SHA (RUNNING_SHA is the final commit marker)"
# manifest flags an orphaned managed label as drift (CRITICAL)
cat > "$TMP/orphan.py" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
import manifest
m = {"origin_sha": "a"*40, "deploy_sha": "a"*40, "plists_expected": {}, "plists_installed": {},
     "labels_loaded": {}, "orphans": {"ai.myndaix.gone": {"installed": True, "loaded": True}}}
assert any("orphaned" in d for d in manifest.drift_list(m)), "orphan must be drift"
m2 = dict(m, orphans={})
assert not manifest.drift_list(m2), "no orphan + equal SHA = clean"
print("ok")
PYEOF
ok 'python3 "$TMP/orphan.py" "$SUB" >/dev/null 2>&1' "manifest.drift_list flags an orphaned managed label"

echo "== bootstrap-fetch --only-if-changed short-circuit is DRIFT-GATED (scratch git, SAFE) =="
# The skip now requires SHA-unchanged AND a clean reconcile --dry-run (BLOCKER fix). Build a fully
# self-contained scratch deploy clone with the substrate deps but NO plist descriptors, so the
# dry-run's manifest check finds NO drift (empty expected set, SHA match, clean tree) -> skip -> exit
# BEFORE any launchctl. This is why it's safe to run against a machine with real ai.myndaix.* jobs.
SC="$TMP/sc"; mkdir -p "$SC"
git init -q --bare "$SC/origin.git"
git clone -q "$SC/origin.git" "$SC/clone" 2>/dev/null
mkdir -p "$SC/clone/substrate/plists"
for f in reconcile.sh lib.sh bootstrap-fetch.sh config_parse.py render_plist.py manifest.py migration_head.txt; do
  cp "$SUB/$f" "$SC/clone/substrate/$f"
done
# Mirror the real repo: __pycache__/.venv are gitignored, so running the substrate python in the
# deploy clone does NOT dirty the tree (a bare fixture without this would false-drift on the .pyc).
printf '__pycache__/\n*.pyc\n.venv/\n' > "$SC/clone/.gitignore"
( cd "$SC/clone" && git config user.email t@t && git config user.name t && \
  git add -A && git commit -qm init && git branch -M main && git push -q origin main ) >/dev/null 2>&1
CHEAD="$(git -C "$SC/clone" rev-parse HEAD)"
mkdir -p "$SC/home/state"
printf '%s\n' "$CHEAD" > "$SC/home/state/RUNNING_SHA"
printf 'MACHINE_ROLE=factory\nMYNDAIX_HOME=%s/home\nMYNDAIX_DSN=postgresql://127.0.0.1/runtime\nOPERATOR_INBOX=%s/home/i\nAUTHOR_ALLOWLIST=bot\nDEPLOY_CLONE=%s/clone\n' "$SC" "$SC" "$SC" > "$SC/home/config.env"
MYNDAIX_HOME="$SC/home" /bin/bash "$SUB/bootstrap-fetch.sh" > "$SC/bf.out" 2>&1; bfrc=$?
ok '[[ "$bfrc" -eq 0 ]] && grep -q "no drift — skip" "$SC/bf.out"' "unchanged + no drift -> skip (exit 0)"
ok '[[ "$CHEAD" == "$(git -C "$SC/clone" rev-parse HEAD)" ]]' "short-circuit did not reset the clone"
ok '! grep -qi "bootout\|reset+clean to" "$SC/bf.out"' "short-circuit never quiesced or reset (no launchctl)"
# code-structure: the skip is GATED on a dry-run (not a bare SHA-equality exit) — BLOCKER fix
ok 'grep -q -- "reconcile.sh\" --dry-run" "$SUB/bootstrap-fetch.sh"' "skip is gated on reconcile --dry-run (drift falls through to converge)"

echo "== work-isolation: play-fix verify sandbox has NO live DSN (r2-C1 lock) =="
ok '! grep -nE "env -i" "$REPO/orchestrator/play-fix.sh" | grep -q "MYNDAIX_DSN"' "play-fix verify sandbox (env -i) injects no MYNDAIX_DSN"
ok 'grep -q "deny network" "$REPO/orchestrator/play-fix.sh"' "play-fix sandbox denies network (no live-ledger reach)"

echo "== shell hygiene: bash -n + shellcheck clean on the production substrate scripts =="
# The production scripts must be pristine. test.sh itself is exempt from the strict SC2034 gate
# (its ok '<cmd>' harness uses vars only inside single-quoted eval strings shellcheck can't see
# into) — it is bash -n checked below. shellcheck is skipped gracefully if absent (Linux CI).
HAVE_SC=1; command -v shellcheck >/dev/null 2>&1 || { HAVE_SC=0; echo "  --: SKIP shellcheck (not installed)"; }
for s in lib.sh bootstrap-fetch.sh reconcile.sh drift-canary.sh; do
  ok 'bash -n "'"$SUB/$s"'"' "bash -n $s"
  [[ "$HAVE_SC" == 1 ]] && ok 'shellcheck -x -S warning "'"$SUB/$s"'" >/dev/null 2>&1' "shellcheck $s"
done
ok 'bash -n "'"$SUB/test.sh"'"' "bash -n test.sh"

echo "=================================================="
echo "  substrate test.sh: $pass ok, $fail fail"
echo "=================================================="
[[ "$fail" -eq 0 ]]
