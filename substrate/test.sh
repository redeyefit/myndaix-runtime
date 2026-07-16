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

echo "== r3 folds: descriptor type-validation (rc2), venv-health drift, tree-check-before-dry-run =="
# a malformed descriptor (roles is an int) must exit 2 (schema error), NOT 1 (=other role -> skip)
printf '{"label":"ai.myndaix.x","roles":5}' > "$TMP/bad.json"
python3 "$RP" role-check "$TMP/bad.json" factory >/dev/null 2>&1; rcbad=$?
ok '[[ "$rcbad" -eq 2 ]]' "role-check on a non-list roles -> exit 2 (fail closed, not silent skip)"
printf '{"label":"ai.myndaix.x","roles":["factory"]}' > "$TMP/good.json"
python3 "$RP" role-check "$TMP/good.json" lab >/dev/null 2>&1; rcother=$?
ok '[[ "$rcother" -eq 1 ]]' "role-check on a valid other-role descriptor -> exit 1 (skip)"
# manifest venv-health surfaces as drift
cat > "$TMP/venv.py" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
import manifest
m = {"origin_sha":"a"*40,"deploy_sha":"a"*40,"plists_expected":{},"plists_installed":{},
     "labels_loaded":{},"orphans":{},"venv_ok":False}
assert any("venv" in d for d in manifest.drift_list(m)), "missing venv must be drift"
assert not manifest.drift_list(dict(m, venv_ok=True)), "healthy venv + equal SHA = clean"
print("ok")
PYEOF
ok 'python3 "$TMP/venv.py" "$SUB" >/dev/null 2>&1' "manifest.drift_list flags a missing/invalid venv"
# BLOCKER fix: the short-circuit checks the clone tree is clean BEFORE trusting its dry-run
ok 'grep -B2 -- "reconcile.sh\" --dry-run" "$SUB/bootstrap-fetch.sh" | grep -q "status --porcelain"' "short-circuit verifies tree clean before the clone dry-run"

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
# A converged machine has a healthy venv; the manifest venv-health check must see one (else the
# dry-run would report venv drift and the short-circuit would correctly fall through to converge).
mkdir -p "$SC/clone/.venv/bin"; printf '#!/bin/sh\n' > "$SC/clone/.venv/bin/pip"; chmod +x "$SC/clone/.venv/bin/pip"
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

echo "== PR-1c: migration lint (additive-only) =="
printf 'ALTER TABLE t DROP COLUMN c;\n' > "$TMP/badA.sql"
printf 'ALTER TABLE t ALTER COLUMN c SET NOT NULL;\n' > "$TMP/badB.sql"
printf 'ALTER TABLE t RENAME COLUMN a TO b;\n' > "$TMP/badC.sql"
printf 'CREATE TABLE IF NOT EXISTS t (id int);\nALTER TABLE t ADD COLUMN IF NOT EXISTS c int NOT NULL DEFAULT 0;\nCREATE INDEX IF NOT EXISTS t_i ON t(id);\nDROP INDEX IF EXISTS old_i;\n' > "$TMP/goodM.sql"
ok '! python3 "$SUB/migration_lint.py" "$TMP/badA.sql" >/dev/null 2>&1' "lint rejects DROP COLUMN"
ok '! python3 "$SUB/migration_lint.py" "$TMP/badB.sql" >/dev/null 2>&1' "lint rejects SET NOT NULL"
ok '! python3 "$SUB/migration_lint.py" "$TMP/badC.sql" >/dev/null 2>&1' "lint rejects RENAME COLUMN"
ok 'python3 "$SUB/migration_lint.py" "$TMP/goodM.sql" >/dev/null 2>&1' "lint passes additive (ADD COLUMN NOT NULL DEFAULT / CREATE IF NOT EXISTS / CREATE+DROP INDEX)"
printf -- '-- DROP TABLE old;\n/* DROP COLUMN x */\n' > "$TMP/cmt.sql"
ok 'python3 "$SUB/migration_lint.py" "$TMP/cmt.sql" >/dev/null 2>&1' "lint ignores commented-out contractions"
# adversarial review M1 hardening: newline-split keyword, string-literal false-positive, ADD-COLUMN-NN, DROP CONSTRAINT
printf 'DROP\n  TABLE foo;\n' > "$TMP/split.sql"
ok '! python3 "$SUB/migration_lint.py" "$TMP/split.sql" >/dev/null 2>&1' "lint catches a keyword split across a newline (DROP\\n TABLE)"
printf "INSERT INTO x(sql) VALUES('please DROP TABLE nothing');\n" > "$TMP/strlit.sql"
ok 'python3 "$SUB/migration_lint.py" "$TMP/strlit.sql" >/dev/null 2>&1' "lint does NOT false-positive on DROP inside a string literal"
printf 'ALTER TABLE t ADD COLUMN c int NOT NULL;\n' > "$TMP/nndef.sql"
ok '! python3 "$SUB/migration_lint.py" "$TMP/nndef.sql" >/dev/null 2>&1' "lint rejects ADD COLUMN NOT NULL without DEFAULT"
printf 'ALTER TABLE t DROP CONSTRAINT t_pk;\n' > "$TMP/dcon.sql"
ok '! python3 "$SUB/migration_lint.py" "$TMP/dcon.sql" >/dev/null 2>&1' "lint rejects DROP CONSTRAINT"
ok 'python3 "$SUB/migration_lint.py" "$REPO/src/runtime/ledger/migrations/0006_skill_pk.sql" >/dev/null 2>&1' "lint clean on the real 0006 (DROP-in-a-string) — no false positive"

echo "== PR-1c review folds: M1 diff-filter, M2 quarantine, M3 old-pid, M4 disarm =="
ok 'grep -q -- "--diff-filter=AMR" "$SUB/reconcile.sh"' "M1: reconcile lints ADDED+MODIFIED+RENAMED migrations (not added-only)"
ok 'grep -q "QUARANTINED_SHA" "$SUB/reconcile.sh" && grep -q "QUARANTINED_SHA" "$SUB/bootstrap-fetch.sh"' "M2: quarantine-SHA written by reconcile + honored by bootstrap-fetch (no revert thrash)"
ok 'grep -q "is QUARANTINED" "$SUB/bootstrap-fetch.sh"' "M2: bootstrap-fetch HOLDS when origin == quarantined SHA"
ok 'grep -q "old_pid" "$SUB/reconcile.sh" && grep -q "pid\" != \"\$old_pid" "$SUB/reconcile.sh"' "M3: health_gate requires serve pid to CHANGE (no false-green on old serve)"
ok 'grep -q "DISARMED — bootout" "$SUB/reconcile.sh" && ! grep -q "sleep 3; launchctl bootout" "$SUB/reconcile.sh"' "M4: disarmed poll bootout is SYNCHRONOUS final action (not a detached subshell)"

echo "== cross-family folds: health-only verify, disarmed-not-orphan, lint bypasses =="
cat > "$TMP/ho.py" <<'PYEOF'
import sys; sys.path.insert(0, sys.argv[1]); import manifest
m = {"deploy_sha":"a"*40,"origin_sha":"b"*40,"plists_expected":{},"plists_installed":{},"labels_loaded":{},"orphans":{}}
assert manifest.drift_list(m), "full check: deploy!=origin must be drift"
assert not manifest.drift_list(m, health_only=True), "health_only: deploy!=origin must NOT be drift (auto-revert)"
# a disarmed sentinel-gated label is NOT an orphan
m2 = dict(m, origin_sha="a"*40, disarmed=["ai.myndaix.reconcile"], orphans={})
assert not manifest.drift_list(m2), "disarmed label excluded from orphans"
print("ok")
PYEOF
ok 'python3 "$TMP/ho.py" "$SUB" >/dev/null 2>&1' "CRIT#1 health_gate verify skips deploy-vs-origin (revert converges); CRIT#2 disarmed != orphan"
ok 'grep -q -- "check --health-only" "$SUB/reconcile.sh"' "health_gate uses manifest check --health-only"
# lint bypasses the cross-family review found (optional COLUMN kw, ADD CONSTRAINT, multi-clause DEFAULT)
printf 'ALTER TABLE t ALTER c TYPE text;\n' > "$TMP/ncol.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/ncol.sql" >/dev/null 2>&1' "lint catches ALTER c TYPE (optional COLUMN kw)"
printf 'ALTER TABLE t DROP age;\n' > "$TMP/ndrop.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/ndrop.sql" >/dev/null 2>&1' "lint catches DROP age (optional COLUMN kw)"
printf 'ALTER TABLE t ADD CONSTRAINT ck CHECK (x>0);\n' > "$TMP/addc.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/addc.sql" >/dev/null 2>&1' "lint catches ADD CONSTRAINT (tightening)"
printf 'ALTER TABLE t ADD CONSTRAINT ck CHECK (x>0) NOT VALID;\n' > "$TMP/addcnv.sql"; ok 'python3 "$SUB/migration_lint.py" "$TMP/addcnv.sql" >/dev/null 2>&1' "lint passes ADD CONSTRAINT ... NOT VALID (additive)"
printf 'ALTER TABLE t ADD COLUMN a INT NOT NULL, ADD COLUMN b INT DEFAULT 0;\n' > "$TMP/multi.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/multi.sql" >/dev/null 2>&1' "lint catches per-clause: NOT NULL col spoofed by a DEFAULT on another col"

echo "== PR-1c cross-family r2 folds: paren-aware lint + revert/disarm robustness =="
printf 'ALTER TABLE t DROP COLUMN IF EXISTS c;\n' > "$TMP/r2a.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/r2a.sql" >/dev/null 2>&1' "r2 lint: DROP COLUMN IF EXISTS caught"
printf 'ALTER TABLE t ADD CHECK (x>0);\n' > "$TMP/r2b.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/r2b.sql" >/dev/null 2>&1' "r2 lint: unnamed ADD CHECK caught"
printf 'ALTER TABLE t ADD COLUMN c numeric(10,2) NOT NULL;\n' > "$TMP/r2c.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/r2c.sql" >/dev/null 2>&1' "r2 lint: numeric(10,2) comma doesn'\''t mis-split (paren-aware)"
printf 'ALTER TABLE t ADD CONSTRAINT c1 CHECK (x>0), ADD CONSTRAINT c2 CHECK (y>0) NOT VALID;\n' > "$TMP/r2d.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/r2d.sql" >/dev/null 2>&1' "r2 lint: per-clause — a NOT VALID clause doesn'\''t exempt an earlier tightening one"
# disarmed-but-LOADED job is drift in the FULL check but NOT under --health-only (CRIT #2)
cat > "$TMP/dl.py" <<'PYEOF'
import sys; sys.path.insert(0, sys.argv[1]); import manifest
m = {"deploy_sha":"a"*40,"origin_sha":"a"*40,"plists_expected":{},"plists_installed":{},"labels_loaded":{},
     "orphans":{}, "disarmed":["ai.myndaix.reconcile"], "disarmed_loaded":["ai.myndaix.reconcile"]}
assert manifest.drift_list(m), "full check: a still-loaded disarmed job MUST be drift"
assert not manifest.drift_list(m, health_only=True), "health_only: disarmed-loaded must NOT block the converge"
print("ok")
PYEOF
ok 'python3 "$TMP/dl.py" "$SUB" >/dev/null 2>&1' "r2 CRIT#2: disarmed-but-loaded = drift (full) / ignored (--health-only)"
ok 'grep -q "migration_head.txt not a plain identifier" "$SUB/reconcile.sh" && grep -A1 "migration_head.txt not a plain identifier" "$SUB/reconcile.sh" | grep -q "return 1"' "r2 CRIT#1: health_gate returns 1 (not die) on a bad migration_head"
ok 'grep -B3 "reset --hard \"\$PREV_GOOD\"" "$SUB/reconcile.sh" | grep -q "la_bootout"' "r2 HIGH#3: mutating ticks quiesced before the auto-revert reset"
ok 'grep -q "quarantine NOT set" "$SUB/reconcile.sh"' "r2 HIGH#4: QUARANTINED_SHA write is fail-closed (die on failure)"

echo "== PR-1c: manifest sentinel-gate (reconcile poll expected ONLY when RECONCILE_ARMED) =="
cat > "$TMP/sg.py" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
import manifest
m = manifest.build(sys.argv[2])
print("armed" if "ai.myndaix.reconcile" in m["plists_expected"] else "unarmed")
PYEOF
mkdir -p "$TMP/armhome"
printf 'MACHINE_ROLE=factory\nMYNDAIX_HOME=%s/armhome\nMYNDAIX_DSN=postgresql://127.0.0.1/runtime\nOPERATOR_INBOX=%s/armhome/i\nAUTHOR_ALLOWLIST=bot\nDEPLOY_CLONE=%s\n' "$TMP" "$TMP" "$REPO" > "$TMP/armhome/config.env"
ok '[[ "$(python3 "$TMP/sg.py" "$SUB" "$TMP/armhome/config.env" 2>/dev/null)" == "unarmed" ]]' "poll NOT expected without RECONCILE_ARMED (no false drift)"
touch "$TMP/armhome/RECONCILE_ARMED"
ok '[[ "$(python3 "$TMP/sg.py" "$SUB" "$TMP/armhome/config.env" 2>/dev/null)" == "armed" ]]' "poll expected once RECONCILE_ARMED exists"

echo "== PR-1c: connect-timeout + auto-revert wiring =="
ok 'grep -q "PGCONNECT_TIMEOUT" "$SUB/lib.sh"' "lib.sh bounds psql connect (the Mini-cutover hang fix)"
ok 'grep -q "AUTO-REVERT to last-good" "$SUB/reconcile.sh"' "reconcile has the auto-revert path (§2.8)"
ok 'grep -q "migration_lint.py" "$SUB/reconcile.sh"' "reconcile lints the prev_good..HEAD migration delta"
ok 'grep -q "requires_sentinel" "$SUB/manifest.py"' "manifest honors requires_sentinel"

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
