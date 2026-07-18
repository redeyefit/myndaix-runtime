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
ok '[[ "$(python3 "$MF" build "$TMP/mf.env" 2>/dev/null | python3 -c "import json,sys;print(json.load(sys.stdin)[\"migration_head\"])")" == "inbox_cursor" ]]' "manifest records migration head object"
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
printf 'CREATE TABLE IF NOT EXISTS t (id int);\nALTER TABLE t ADD COLUMN IF NOT EXISTS c int NOT NULL DEFAULT 0;\nCREATE INDEX IF NOT EXISTS t_i ON t(id);\n' > "$TMP/goodM.sql"
ok '! python3 "$SUB/migration_lint.py" "$TMP/badA.sql" >/dev/null 2>&1' "lint rejects DROP COLUMN"
ok '! python3 "$SUB/migration_lint.py" "$TMP/badB.sql" >/dev/null 2>&1' "lint rejects SET NOT NULL"
ok '! python3 "$SUB/migration_lint.py" "$TMP/badC.sql" >/dev/null 2>&1' "lint rejects RENAME COLUMN"
ok 'python3 "$SUB/migration_lint.py" "$TMP/goodM.sql" >/dev/null 2>&1' "lint passes additive (ADD COLUMN NOT NULL DEFAULT / CREATE TABLE+INDEX IF NOT EXISTS)"
printf 'DROP INDEX IF EXISTS old_i;\n' > "$TMP/dropidx.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/dropidx.sql" >/dev/null 2>&1' "allowlist: a bare DROP INDEX now REJECTS fail-closed (a unique-backing index drop can't be told from a perf-index drop)"
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
ok 'python3 "$SUB/migration_lint.py" "$REPO/src/runtime/ledger/migrations/0013_dial_shadow_snapshot.sql" >/dev/null 2>&1' "lint clean on a real ADDITIVE migration (0013: CREATE TABLE/INDEX IF NOT EXISTS) — no false positive"
ok '! python3 "$SUB/migration_lint.py" "$REPO/src/runtime/ledger/migrations/0006_skill_pk.sql" >/dev/null 2>&1' "lint REJECTS the real 0006 (DROP CONSTRAINT inside a DO/EXECUTE block) — dynamic DDL is fail-closed (r3 #1)"

echo "== PR-1c review folds: M1 diff-filter, M2 quarantine, M3 old-pid, M4 disarm =="
ok 'grep -q -- "--diff-filter=AMR" "$SUB/reconcile.sh"' "M1: reconcile lints ADDED+MODIFIED+RENAMED migrations (not added-only)"
ok 'grep -q "QUARANTINED_SHA" "$SUB/reconcile.sh" && grep -q "QUARANTINED_SHA" "$SUB/bootstrap-fetch.sh"' "M2: quarantine-SHA written by reconcile + honored by bootstrap-fetch (no revert thrash)"
ok 'grep -q "is QUARANTINED" "$SUB/bootstrap-fetch.sh"' "M2: bootstrap-fetch HOLDS when origin == quarantined SHA"
ok 'grep -q "old_pid" "$SUB/reconcile.sh" && grep -q "pid\" != \"\$old_pid" "$SUB/reconcile.sh"' "M3: health_gate requires serve pid to CHANGE (no false-green on old serve)"
ok 'grep -q "DISARMED (sentinel" "$SUB/reconcile.sh" && ! grep -q "sleep 3; launchctl bootout" "$SUB/reconcile.sh"' "M4: disarmed poll bootout is SYNCHRONOUS final action (not a detached subshell)"

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
printf 'ALTER TABLE t ADD CONSTRAINT ck CHECK (x>0) NOT VALID;\n' > "$TMP/addcnv.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/addcnv.sql" >/dev/null 2>&1' "lint REJECTS ADD CONSTRAINT ... NOT VALID — PG enforces it on new writes; not an additive escape (r3 #2)"
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
ok 'grep -B12 "reset --hard \"\$PREV_GOOD\"" "$SUB/reconcile.sh" | grep -q "la_bootout"' "r2 HIGH#3: mutating ticks quiesced before the auto-revert reset"
ok 'grep -q "quarantine NOT set" "$SUB/reconcile.sh"' "r2 HIGH#4: QUARANTINED_SHA write is fail-closed (die on failure)"

echo "== PR-1c cross-family r3 folds: linter fail-closed + revert/disarm hardening =="
# r3 #1 — dynamic DDL (DO/EXECUTE/CALL) is fail-closed (a DROP can hide in the stripped body).
printf 'DO $$ BEGIN EXECUTE '\''ALTER TABLE job DROP COLUMN context'\''; END $$;\n' > "$TMP/r3do.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/r3do.sql" >/dev/null 2>&1' "r3 #1: DO/EXECUTE block hiding a DROP is rejected (dynamic DDL fail-closed)"
printf 'CALL do_destructive();\n' > "$TMP/r3call.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/r3call.sql" >/dev/null 2>&1' "r3 #1: CALL <proc> is rejected (can run arbitrary DDL)"
# r3 #5 — DROP DEFAULT breaks old INSERTs on a NOT NULL column after a revert.
printf 'ALTER TABLE t ALTER COLUMN c DROP DEFAULT;\n' > "$TMP/r3dd.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/r3dd.sql" >/dev/null 2>&1' "r3 #5: DROP DEFAULT is rejected (old INSERTs omitting the col fail post-revert)"
# r3 #6 — quoted identifier containing a SPACE no longer defeats the \\S+ identifier match.
printf 'ALTER TABLE t RENAME COLUMN "my col" TO x;\n' > "$TMP/r3rn.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/r3rn.sql" >/dev/null 2>&1' "r3 #6: RENAME of a space-containing quoted column is caught (quoted-ident neutralized)"
printf 'ALTER TABLE t ALTER COLUMN "my col" TYPE text;\n' > "$TMP/r3ty.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/r3ty.sql" >/dev/null 2>&1' "r3 #6: ALTER TYPE of a space-containing quoted column is caught"
# r3 #7 — DIGIT-leading quoted identifier no longer bypasses the [A-Za-z_] anchor.
printf 'ALTER TABLE t DROP COLUMN "1st_col";\n' > "$TMP/r3dg.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/r3dg.sql" >/dev/null 2>&1' "r3 #7: DROP COLUMN of a digit-leading quoted name is caught"
# r3 #8 — a column literally named as an unreserved keyword (constraint) is a real column drop.
printf 'ALTER TABLE t DROP COLUMN constraint;\n' > "$TMP/r3kw.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/r3kw.sql" >/dev/null 2>&1' "r3 #8: DROP COLUMN of a keyword-named column (constraint) is caught (was a lookahead bypass)"
# r3 #9 — a double-quoted "DEFAULT" constraint name no longer spoofs the ADD COLUMN NOT NULL check.
printf 'ALTER TABLE t ADD COLUMN age INT NOT NULL CONSTRAINT "DEFAULT" CHECK (age > 0);\n' > "$TMP/r3sp.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/r3sp.sql" >/dev/null 2>&1' "r3 #9: quoted \"DEFAULT\" constraint name no longer spoofs a real DEFAULT"
# no-false-positive: a legit ADDITIVE add of a digit-quoted column still passes.
printf 'ALTER TABLE t ADD COLUMN "1st_col" INT;\n' > "$TMP/r3ok.sql"; ok 'python3 "$SUB/migration_lint.py" "$TMP/r3ok.sql" >/dev/null 2>&1' "r3: ADD COLUMN of a quoted-digit name still PASSES (neutralization doesn'\''t over-flag)"
# r3 #3 — auto-revert quiesce is FAIL-CLOSED (SIGKILL-escalate then die) + abandoned-worker guard.
ok 'grep -q "la_ensure_gone" "$SUB/lib.sh"' "r3 #3: lib.sh has the SIGKILL-escalation helper la_ensure_gone"
ok 'grep -q "la_ensure_gone .* || die \"auto-revert:" "$SUB/reconcile.sh"' "r3 #3: auto-revert quiesce dies (not WARN) if a tick survives SIGKILL before the reset"
ok 'grep -q "auto-revert: a worker is still running from the deploy clone" "$SUB/reconcile.sh"' "r3 #3: auto-revert refuses the reset if a play-* worker still runs from the deploy clone"
# r3 #4 — disarm re-reads the sentinel ON DISK (not the stale ROLE_LABELS snapshot) + confirm-gone die.
ok 'grep -q "! -e \"\$MYNDAIX_HOME/\$RECONCILE_SENTINEL\"" "$SUB/reconcile.sh"' "r3 #4: disarm decision re-reads RECONCILE_ARMED on disk (not the install_artifacts snapshot)"
ok 'grep -q "DISARM FAILED" "$SUB/reconcile.sh"' "r3 #4: a bootout that leaves the poll loaded fails LOUD (die), not silent || true"
# r3 #4c — the poll injects RECONCILE_POLL=1 and bootstrap HOLDS a disarmed poll-fire (manual runs proceed).
ok 'grep -q "RECONCILE_POLL" "$SUB/plists/ai.myndaix.reconcile.json"' "r3 #4c: reconcile poll descriptor injects RECONCILE_POLL=1 (env_literal)"
ok 'grep -q "RECONCILE_POLL:-0" "$SUB/bootstrap-fetch.sh" && grep -q "RECONCILE_ARMED" "$SUB/bootstrap-fetch.sh"' "r3 #4c: bootstrap-fetch holds a poll-fired invocation when disarmed (belt behind the bootout)"
# sentinel-name SYNC: reconcile.sh RECONCILE_SENTINEL == descriptor requires_sentinel == bootstrap literal.
SENT_DESC="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["requires_sentinel"])' "$SUB/plists/ai.myndaix.reconcile.json")"
ok '[[ "'"$SENT_DESC"'" == "RECONCILE_ARMED" ]] && grep -q "RECONCILE_SENTINEL=\"RECONCILE_ARMED\"" "$SUB/reconcile.sh" && grep -q "MYNDAIX_HOME/RECONCILE_ARMED" "$SUB/bootstrap-fetch.sh"' "r3 #4: the arm-sentinel name agrees across descriptor + reconcile.sh + bootstrap-fetch"

echo "== PR-1c cross-family r4 folds: single-pass lexer + routine/SET-DEFAULT-NULL + poll env reload =="
# r4 CRIT-1 — a comment delimiter INSIDE a string no longer swallows the DDL between it and a later quote.
printf "SELECT '/*';\nALTER TABLE job DROP COLUMN context;\nSELECT '*/';\n" > "$TMP/r4c1a.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/r4c1a.sql" >/dev/null 2>&1' "r4 CRIT-1: block-comment tokens inside strings don'\''t swallow a DROP COLUMN"
printf "ALTER TABLE t ADD COLUMN c TEXT DEFAULT 'http://u/--/p';\nDROP TABLE victims;\nSELECT 'x';\n" > "$TMP/r4c1b.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/r4c1b.sql" >/dev/null 2>&1' "r4 CRIT-1: a -- inside a string doesn'\''t swallow a following DROP TABLE"
# r4 CRIT-2 — dynamic DDL in a CREATE FUNCTION body invoked by SELECT is caught (routine rejected).
printf 'CREATE OR REPLACE FUNCTION f() RETURNS void LANGUAGE plpgsql AS $x$ BEGIN EXECUTE '\''ALTER TABLE job DROP COLUMN context'\''; END $x$;\nSELECT f();\n' > "$TMP/r4c2.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/r4c2.sql" >/dev/null 2>&1' "r4 CRIT-2: CREATE FUNCTION with a DDL body (+SELECT invoke) is rejected (opaque body fail-closed)"
printf 'DROP FUNCTION old_fn();\n' > "$TMP/r4df.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/r4df.sql" >/dev/null 2>&1' "r4 CRIT-2: DROP FUNCTION is rejected (old code may depend on it)"
# r4 CRIT-3 — a single-quote inside a double-quoted identifier no longer unbalances the string pass.
printf 'SELECT "dummy_'\''x"; DROP TABLE victims; SELECT '\''z'\'';\n' > "$TMP/r4c3.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/r4c3.sql" >/dev/null 2>&1' "r4 CRIT-3: a quoted-ident containing a '\'' doesn'\''t swallow a DROP TABLE (single-pass lexer)"
# r4 HIGH-1 — SET DEFAULT NULL is the DROP DEFAULT contraction in disguise; SET DEFAULT <val> is additive.
printf 'ALTER TABLE t ALTER COLUMN c SET DEFAULT NULL;\n' > "$TMP/r4h1.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/r4h1.sql" >/dev/null 2>&1' "r4 HIGH-1: SET DEFAULT NULL rejected (functional DROP DEFAULT)"
printf 'ALTER TABLE t ALTER COLUMN c SET DEFAULT 5;\n' > "$TMP/r4h1b.sql"; ok 'python3 "$SUB/migration_lint.py" "$TMP/r4h1b.sql" >/dev/null 2>&1' "r4 HIGH-1: SET DEFAULT <value> still PASSES (only NULL is the contraction)"
# r4 HIGH-3 — a column literally named expression/identity dropped bare is a real column drop.
printf 'ALTER TABLE t DROP expression;\n' > "$TMP/r4h3.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/r4h3.sql" >/dev/null 2>&1' "r4 HIGH-3: DROP expression (bare, keyword-named column) is caught"
# no-false-positive: DROP NOT NULL relaxation still PASSES (the one genuinely-additive property drop).
printf 'ALTER TABLE t ALTER COLUMN c DROP NOT NULL;\n' > "$TMP/r4nn.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/r4nn.sql" >/dev/null 2>&1' "allowlist: DROP NOT NULL now REJECTS fail-closed (a null written pre-revert could surface to old code)"
# r4 HIGH-2 — a manual converge reloads an already-loaded poll so a new plist/env (RECONCILE_POLL) lands.
ok 'grep -q "RECONCILE_POLL:-0.* != .1." "$SUB/reconcile.sh"' "r4 HIGH-2: a manual converge (RECONCILE_POLL unset) reloads the poll so a changed env takes effect"

echo "== PR-1c cross-family r5 folds: dollar-tag lexer + SET DEFAULT (NULL) + DROP ROUTINE + FP fixes =="
# r5 CRIT-1 — a digit-bearing dollar tag ($a1$) is a real string; a -- inside it must not swallow the DROP.
printf 'SELECT $a1$--$a1$; DROP TABLE victims;\n' > "$TMP/r5c1.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/r5c1.sql" >/dev/null 2>&1' "r5 CRIT-1: digit dollar-tag \$a1\$ tokenized; a -- inside doesn'\''t swallow a DROP TABLE"
# r5 CRIT-2 — $$ embedded in an identifier (a\$\$) must NOT open a spurious dollar-quote that eats the DROP.
printf 'ALTER TABLE t ADD COLUMN a$$ int; DROP TABLE users; SELECT b$$;\n' > "$TMP/r5c2.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/r5c2.sql" >/dev/null 2>&1' "r5 CRIT-2: \$\$ inside an identifier doesn'\''t open a spurious dollar-quote (lookbehind anchor)"
# r5 HIGH-3 — parenthesized SET DEFAULT (NULL) is the same contraction as SET DEFAULT NULL.
printf 'ALTER TABLE t ALTER COLUMN c SET DEFAULT (NULL);\n' > "$TMP/r5h3.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/r5h3.sql" >/dev/null 2>&1' "r5 HIGH-3: SET DEFAULT (NULL) parenthesized form is caught"
# r5 HIGH-4 — DROP ROUTINE drops a function/procedure dependency.
printf 'DROP ROUTINE IF EXISTS public.old_fn();\n' > "$TMP/r5h4.sql"; ok '! python3 "$SUB/migration_lint.py" "$TMP/r5h4.sql" >/dev/null 2>&1' "r5 HIGH-4: DROP ROUTINE is rejected"
# r5 FP-6 — a GENERATED NOT NULL column is additive (Postgres supplies its value); must PASS.
printf 'ALTER TABLE t ADD COLUMN g int GENERATED ALWAYS AS (a + b) STORED NOT NULL;\n' > "$TMP/r5fp6.sql"; ok 'python3 "$SUB/migration_lint.py" "$TMP/r5fp6.sql" >/dev/null 2>&1' "r5 FP-6: GENERATED ... NOT NULL column PASSES (additive, not a bare NOT NULL add)"
# regression: a legit dollar-quoted string default still parses fine and PASSES.
printf 'ALTER TABLE t ADD COLUMN note text DEFAULT $tag$ hi $tag$;\n' > "$TMP/r5reg.sql"; ok 'python3 "$SUB/migration_lint.py" "$TMP/r5reg.sql" >/dev/null 2>&1' "r5: a legit \$tag\$ string default still PASSES (lexer regression)"
# r5 FP-7 — --allow-routine is a NARROW escape: it lets CREATE FUNCTION through but STILL rejects a DROP TABLE.
printf 'CREATE FUNCTION f() RETURNS trigger AS $$ BEGIN RETURN NULL; END $$ LANGUAGE plpgsql;\n' > "$TMP/r5rt.sql"; ok 'python3 "$SUB/migration_lint.py" --allow-routine "$TMP/r5rt.sql" >/dev/null 2>&1' "r5 FP-7: --allow-routine permits a blessed CREATE FUNCTION"
printf 'CREATE FUNCTION f() RETURNS void AS $$ x $$ LANGUAGE sql;\nDROP TABLE victims;\n' > "$TMP/r5rt2.sql"; ok '! python3 "$SUB/migration_lint.py" --allow-routine "$TMP/r5rt2.sql" >/dev/null 2>&1' "r5 FP-7: --allow-routine is NARROW — a DROP TABLE alongside still REJECTS"
ok 'grep -q "RECONCILE_ALLOW_ROUTINE" "$SUB/reconcile.sh" && grep -q -- "--allow-routine" "$SUB/reconcile.sh"' "r5 FP-7: reconcile passes --allow-routine from the operator-gated RECONCILE_ALLOW_ROUTINE"
# r5 HIGH-5 / #8 — the poll bootstrap has the EBUSY retry, and the manual reload is fail-closed (ensure_gone).
ok 'grep -A6 "manual converge" "$SUB/reconcile.sh" | grep -q "la_ensure_gone \"\$label\" 10 5"' "r5 #8: manual poll reload fail-closes via la_ensure_gone (no silent bootstrap skip)"
ok 'grep -A8 "RECONCILE_POLL:-0" "$SUB/reconcile.sh" | grep -q "sleep 2; la_bootstrap"' "r5 HIGH-5: poll bootstrap retries a transient launchd EBUSY like every other tick"

echo "== PR-1c cross-family r6 folds: hand-written Postgres-lexer scanner (E''/nested comments/Unicode/CR) =="
# These fixtures need a literal backslash-quote, a CR, a nested comment, and a UTF-8 identifier char —
# written via python so the exact bytes are unambiguous (printf escaping would mangle them).
python3 - "$TMP" <<'PYEOF'
import os, sys
d = sys.argv[1]
cases = {
    "r6a":  "SELECT E'prefix\\'--not a comment'; DROP TABLE victims;\n",       # E'' \' escape (CRIT)
    "r6b":  "/* outer /* inner */ -- still inside outer */ DROP TABLE victims;\n",  # nested comment (CRIT)
    "r6c":  "SELECT 1 AS ä$tag$; DROP TABLE victims; SELECT $tag$ $tag$;\n",   # unicode ident (CRIT)
    "r6d":  "-- innocuous\r DROP TABLE victims;\n",                            # CR ends PG comment (HIGH)
    "r6ok": "ALTER TABLE t ADD COLUMN c text DEFAULT E'a\\nb';\n",             # legit E'' -> PASS
}
for k, v in cases.items():
    open(os.path.join(d, k + ".sql"), "w", encoding="utf-8").write(v)
PYEOF
ok '! python3 "$SUB/migration_lint.py" "$TMP/r6a.sql" >/dev/null 2>&1' "r6 CRIT: E'' backslash-escaped quote doesn'\''t close the string early (a -- inside can'\''t swallow a DROP)"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r6b.sql" >/dev/null 2>&1' "r6 CRIT: nested block comment tracked by depth (a -- in the still-open outer can'\''t swallow a DROP)"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r6c.sql" >/dev/null 2>&1' "r6 CRIT: a Unicode identifier char before \$tag\$ doesn'\''t open a spurious dollar-quote"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r6d.sql" >/dev/null 2>&1' "r6 HIGH: a CR terminates a Postgres -- comment (a DROP after the CR is seen)"
ok 'python3 "$SUB/migration_lint.py" "$TMP/r6ok.sql" >/dev/null 2>&1' "r6: a legit E'' string default still PASSES (scanner regression)"
ok 'grep -q "_scan_quoted" "$SUB/migration_lint.py" && ! grep -q "_TOKEN_RE" "$SUB/migration_lint.py"' "r6: _normalize is the hand-written lexer scanner (regex _TOKEN_RE retired)"

echo "== PR-1c cross-family r7 folds: Postgres Unicode identifier rules (ident_cont = high-bit bytes) =="
# UTF-8 / combining-mark fixtures written via python for exact bytes.
python3 - "$TMP" <<'PYEOF'
import os, sys
d = sys.argv[1]
cases = {
    # combining acute (e + U+0301) before $tag$ must NOT open a fake dollar-quote that hides the DROP
    "r7a":  "CREATE TABLE IF NOT EXISTS é$tag$(id int);\nDROP TABLE IF EXISTS victims;\nSELECT $tag$ok$tag$;\n",
    "r7b":  "ALTER TABLE users DROP é;\n",                                  # unquoted Unicode column drop
    "r7c":  "ALTER TABLE t ADD COLUMN c text DEFAULT $café$ DROP TABLE is text $café$;\n",  # $café$ -> PASS
    "r7ok": "ALTER TABLE t ADD COLUMN é int;\n",                            # additive unicode col -> PASS
}
for k, v in cases.items():
    open(os.path.join(d, k + ".sql"), "w", encoding="utf-8").write(v)
PYEOF
ok '! python3 "$SUB/migration_lint.py" "$TMP/r7a.sql" >/dev/null 2>&1' "r7 CRIT: a combining-mark identifier char before \$tag\$ doesn'\''t open a fake dollar-quote (PG high-bit ident_cont)"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r7b.sql" >/dev/null 2>&1' "r7 CRIT: an UNQUOTED Unicode column name drop is caught (\\\\S anchor, not ASCII-only)"
ok 'python3 "$SUB/migration_lint.py" "$TMP/r7c.sql" >/dev/null 2>&1' "r7 HIGH-FP: a \$café\$ Unicode dollar tag is recognized as a string (no false positive)"
ok 'python3 "$SUB/migration_lint.py" "$TMP/r7ok.sql" >/dev/null 2>&1' "r7: ADD COLUMN with a Unicode name still PASSES (additive, not over-flagged)"

echo "== PR-1c cross-family r8 + attack-fleet folds: rule-set semantic coverage (live-PG confirmed) =="
python3 - "$TMP" <<'PYEOF'
import os, sys
d = sys.argv[1]
cases = {
    "r8crit": "CREATE RULE r AS ON DELETE TO job DO INSTEAD NOTHING;\n",           # fleet CRITICAL: silent DML rewrite
    "r8trunc": "TRUNCATE TABLE job;\n",
    "r8detach": "ALTER TABLE parent DETACH PARTITION p2026;\n",
    "r8noinh": "ALTER TABLE child NO INHERIT parent;\n",
    "r8dropattr": "ALTER TYPE address DROP ATTRIBUTE zip;\n",
    "r8renval": "ALTER TYPE status RENAME VALUE 'active' TO 'enabled';\n",         # enum contraction; operands strip to ''
    "r8gen": "ALTER TABLE job ALTER COLUMN id SET GENERATED ALWAYS;\n",
    "r8uidx": "CREATE UNIQUE INDEX u ON t(c);\n",
    "r8seq": "ALTER SEQUENCE job_id_seq RESTART WITH 1;\n",
    "r8addval": "ALTER TYPE status ADD VALUE 'archived';\n",                        # additive enum add -> PASS
    "r8attach": "CREATE TABLE parent (id int) PARTITION BY RANGE (id);\nCREATE TABLE p (id int);\nALTER TABLE parent ATTACH PARTITION p FOR VALUES FROM (1) TO (2);\n",  # both new -> additive -> PASS (r6)
    "r8cidx": "CREATE INDEX idx ON t(c);\n",                                        # non-unique index -> PASS
    "r8dollar": "INSERT INTO cfg(k,v) VALUES('h', $§$ has DROP TABLE text $§$);\n", # high-bit dollar tag -> PASS
    "r8trig": "CREATE TRIGGER t AFTER INSERT ON job EXECUTE FUNCTION f();\n",
    "r8trigdrop": "CREATE TRIGGER t AFTER INSERT ON job EXECUTE FUNCTION f();\nDROP TABLE x;\n",
}
for k, v in cases.items():
    open(os.path.join(d, k + ".sql"), "w", encoding="utf-8").write(v)
PYEOF
ok '! python3 "$SUB/migration_lint.py" "$TMP/r8crit.sql" >/dev/null 2>&1' "r8 CRIT (fleet, live-PG): CREATE RULE ... DO INSTEAD is rejected (silent DML rewrite survives a code-revert)"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r8trunc.sql" >/dev/null 2>&1' "r8: TRUNCATE rejected (data loss a code-revert can'\''t undo)"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r8detach.sql" >/dev/null 2>&1' "r8: DETACH PARTITION rejected"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r8noinh.sql" >/dev/null 2>&1' "r8: NO INHERIT rejected"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r8dropattr.sql" >/dev/null 2>&1' "r8: ALTER TYPE DROP ATTRIBUTE rejected (composite-type contraction)"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r8renval.sql" >/dev/null 2>&1' "r8: ALTER TYPE RENAME VALUE rejected (enum label gone; matched on the keyword)"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r8gen.sql" >/dev/null 2>&1' "r8: SET GENERATED ALWAYS rejected (rejects old explicit-value INSERTs)"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r8uidx.sql" >/dev/null 2>&1' "r8: CREATE UNIQUE INDEX rejected (uniqueness tightening)"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r8seq.sql" >/dev/null 2>&1' "r8: ALTER SEQUENCE RESTART rejected (ID reissue -> PK collision)"
ok 'python3 "$SUB/migration_lint.py" "$TMP/r8addval.sql" >/dev/null 2>&1' "r8: ALTER TYPE ADD VALUE PASSES (additive enum add, not over-flagged)"
ok 'python3 "$SUB/migration_lint.py" "$TMP/r8attach.sql" >/dev/null 2>&1' "r8/r6: ATTACH PARTITION of a NEW child into a NEW parent PASSES (additive — no old code touches either)"
ok 'python3 "$SUB/migration_lint.py" "$TMP/r8cidx.sql" >/dev/null 2>&1' "r8: non-unique CREATE INDEX PASSES (performance-only)"
ok 'python3 "$SUB/migration_lint.py" "$TMP/r8dollar.sql" >/dev/null 2>&1' "r8 FP (fleet): a high-bit dollar tag is recognized as a string (no false positive)"
ok 'python3 "$SUB/migration_lint.py" --allow-routine "$TMP/r8trig.sql" >/dev/null 2>&1' "r8: --allow-routine permits a blessed CREATE TRIGGER"
ok '! python3 "$SUB/migration_lint.py" --allow-routine "$TMP/r8trigdrop.sql" >/dev/null 2>&1' "r8: --allow-routine stays NARROW — a DROP TABLE alongside a trigger still REJECTS"

echo "== PR-1c cross-family r9: ALLOWLIST inversion (fail-closed by default; no unbounded denylist) =="
python3 - "$TMP" <<'PYEOF'
import os, sys
d = sys.argv[1]
cases = {
    # r9 gaps a denylist missed — now rejected by DEFAULT (not on the allowlist), no new rule needed
    "r9setschema": "ALTER TABLE job SET SCHEMA archive;\n",
    "r9rls": "ALTER TABLE job FORCE ROW LEVEL SECURITY;\n",
    "r9dropext": "DROP EXTENSION pgcrypto CASCADE;\n",
    "r9droppolicy": "DROP POLICY p ON job;\n",
    "r9addgen": "ALTER TABLE t ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY;\n",
    "r9addcolpk": "ALTER TABLE audit_log ADD COLUMN g int PRIMARY KEY;\n",
    "r9alterdomain": "ALTER DOMAIN d ADD CONSTRAINT ck CHECK (VALUE > 0);\n",
    "r9replica": "ALTER TABLE t REPLICA IDENTITY NOTHING;\n",   # never enumerated — allowlist rejects it FREE
    "r9selfn": "SELECT drop_it();\n",                            # bare function call (no FROM) -> reject
    # corrected-additive shapes the allowlist must PASS
    "r9corview": "CREATE OR REPLACE VIEW v AS SELECT 1;\n",      # re-derivable view -> additive
    "r9uidxnew": "CREATE TABLE fresh (a int);\nCREATE UNIQUE INDEX u ON fresh(a);\n",  # unique idx on same-migration table
    "r9backfill": "SELECT id FROM t ORDER BY id FOR UPDATE;\nUPDATE t SET x=0 WHERE x<>0;\n",  # idempotent backfill DML
    "r9setdef": "ALTER TABLE t ALTER COLUMN c SET DEFAULT 5;\n",  # SET DEFAULT non-null -> additive
}
for k, v in cases.items():
    open(os.path.join(d, k + ".sql"), "w", encoding="utf-8").write(v)
PYEOF
ok '! python3 "$SUB/migration_lint.py" "$TMP/r9setschema.sql" >/dev/null 2>&1' "r9: ALTER TABLE SET SCHEMA rejected (not on allowlist)"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r9rls.sql" >/dev/null 2>&1' "r9: FORCE ROW LEVEL SECURITY rejected"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r9dropext.sql" >/dev/null 2>&1' "r9: DROP EXTENSION rejected"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r9droppolicy.sql" >/dev/null 2>&1' "r9: DROP POLICY rejected"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r9addgen.sql" >/dev/null 2>&1' "r9: ALTER COLUMN ADD GENERATED ALWAYS rejected"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r9addcolpk.sql" >/dev/null 2>&1' "r9 HIGH-2: ADD COLUMN ... PRIMARY KEY (inline tightening) rejected"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r9alterdomain.sql" >/dev/null 2>&1' "r9: ALTER DOMAIN ADD CONSTRAINT rejected"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r9replica.sql" >/dev/null 2>&1' "r9: REPLICA IDENTITY NOTHING rejected FREE (never enumerated — allowlist default catches it)"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r9selfn.sql" >/dev/null 2>&1' "r9: a bare SELECT f() (no FROM, an opaque call) rejected"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r9corview.sql" >/dev/null 2>&1' "r10: CREATE OR REPLACE VIEW now REJECTS (a view narrow isn'\''t revertible — PG forbids it, serve bricks; live-PG confirmed)"
ok 'python3 "$SUB/migration_lint.py" "$TMP/r9uidxnew.sql" >/dev/null 2>&1' "r9: CREATE UNIQUE INDEX on a SAME-MIGRATION new table PASSES (old code doesn'\''t know the table)"
ok 'python3 "$SUB/migration_lint.py" "$TMP/r9backfill.sql" >/dev/null 2>&1' "r9: idempotent backfill (SELECT FOR UPDATE + UPDATE) PASSES (schema-neutral data DML)"
ok 'python3 "$SUB/migration_lint.py" "$TMP/r9setdef.sql" >/dev/null 2>&1' "r9: SET DEFAULT <value> PASSES (additive; only SET DEFAULT NULL is the contraction)"
ok 'grep -q "_is_additive" "$SUB/migration_lint.py" && grep -q "ALLOWLIST" "$SUB/migration_lint.py"' "r9: the gate is an allowlist (_is_additive), not an unbounded denylist"

echo "== PR-1c cross-family r10 + attack-fleet: allowlist HOLES (same-migration tracking, dblink, view) =="
python3 - "$TMP" <<'PYEOF'
import os, sys
d = sys.argv[1]
cases = {
    # r10 CRITICAL shape A: a LATER create must not retroactively bless an EARLIER destructive op
    "r10rev": "ALTER TABLE job DROP COLUMN context;\nCREATE TABLE IF NOT EXISTS job (id uuid);\n",
    # r10 CRITICAL shape B: CREATE TABLE IF NOT EXISTS is a no-op on an existing table -> can't launder a DROP
    "r10ifne": "CREATE TABLE IF NOT EXISTS users (id int);\nALTER TABLE users DROP COLUMN password_hash;\n",
    # r10 HIGH-1: a DDL-capable function via SELECT ... FROM (dblink_exec) must be rejected
    "r10dblink": "SELECT dblink_exec('db', 'ALTER TABLE job DROP COLUMN context') FROM generate_series(1,1);\n",
    # r10 fleet HIGH: CREATE OR REPLACE VIEW is not safely revertible
    "r10corview": "CREATE OR REPLACE VIEW w AS SELECT a, b, c, d FROM tw;\n",
    # PASS: an UNCONDITIONAL create genuinely proves newness -> a unique index / constraint on it is additive
    "r10uncond": "CREATE TABLE fresh (a int);\nCREATE UNIQUE INDEX u ON fresh (a);\nALTER TABLE fresh ADD CONSTRAINT pk PRIMARY KEY (a);\n",
    # PASS: a plain (new) CREATE VIEW is additive; a locking SELECT is the accepted migration use
    "r10newview": "CREATE VIEW v AS SELECT 1;\n",
    "r10lock": "SELECT id FROM t ORDER BY id FOR UPDATE;\n",
}
for k, v in cases.items():
    open(os.path.join(d, k + ".sql"), "w", encoding="utf-8").write(v)
PYEOF
ok '! python3 "$SUB/migration_lint.py" "$TMP/r10rev.sql" >/dev/null 2>&1' "r10 CRIT (both signals): a later CREATE TABLE can'\''t retroactively bless an earlier DROP COLUMN (order-aware created set)"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r10ifne.sql" >/dev/null 2>&1' "r10 CRIT (live-PG): CREATE TABLE IF NOT EXISTS can'\''t launder a DROP COLUMN onto an existing table (unconditional-create only)"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r10dblink.sql" >/dev/null 2>&1' "r10 HIGH-1: a SELECT invoking dblink_exec (DDL via function) is rejected (SELECT needs FOR UPDATE)"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r10corview.sql" >/dev/null 2>&1' "r10 (fleet, live-PG): CREATE OR REPLACE VIEW rejected (a view narrow isn'\''t revertible)"
ok 'python3 "$SUB/migration_lint.py" "$TMP/r10uncond.sql" >/dev/null 2>&1' "r10: an UNCONDITIONAL new table + its UNIQUE INDEX/CONSTRAINT PASSES (newness provable; a create fails if it existed)"
ok 'python3 "$SUB/migration_lint.py" "$TMP/r10newview.sql" >/dev/null 2>&1' "r10: a plain new CREATE VIEW still PASSES (additive)"
ok 'python3 "$SUB/migration_lint.py" "$TMP/r10lock.sql" >/dev/null 2>&1' "r10: a locking SELECT ... FOR UPDATE still PASSES (accepted migration use)"

echo "== PR-1c cross-family r11: expression-injection (dblink DDL-via-DML, NULL default) =="
python3 - "$TMP" <<'PYEOF'
import os, sys
d = sys.argv[1]
cases = {
    "r11ret": "INSERT INTO rc(id,active) VALUES ('x',0) ON CONFLICT (id) DO UPDATE SET active=rc.active RETURNING dblink_exec('db','ALTER TABLE job DROP COLUMN body');\n",
    "r11sel": "SELECT dblink_exec('db','ALTER TABLE job DROP COLUMN body') FROM rc WHERE id='x' FOR UPDATE;\n",
    "r11ext": "CREATE EXTENSION IF NOT EXISTS dblink;\n",
    "r11nn": "ALTER TABLE users ADD COLUMN age INT NOT NULL DEFAULT NULL;\n",
    "r11cast": "ALTER TABLE users ALTER COLUMN age SET DEFAULT CAST(NULL AS int);\n",
    "r11nncast": "ALTER TABLE users ADD COLUMN age INT NOT NULL DEFAULT CAST(NULL AS int);\n",
    "r11okdef": "ALTER TABLE t ADD COLUMN c int NOT NULL DEFAULT 0;\n",           # non-null default -> PASS
    "r11oklock": "SELECT repo_id FROM repo_concurrency ORDER BY repo_id FOR UPDATE;\n",  # 0002 lock -> PASS
    "r11okins": "INSERT INTO rc(id,active) SELECT r, count(*) FROM t GROUP BY r ON CONFLICT (id) DO UPDATE SET active=EXCLUDED.active;\n",  # 0002 backfill -> PASS
}
for k, v in cases.items():
    open(os.path.join(d, k + ".sql"), "w", encoding="utf-8").write(v)
PYEOF
ok '! python3 "$SUB/migration_lint.py" "$TMP/r11ret.sql" >/dev/null 2>&1' "r11 HIGH: dblink_exec via INSERT ... RETURNING rejected (dblink hard-rejected + RETURNING rejected)"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r11sel.sql" >/dev/null 2>&1' "r11 HIGH: dblink_exec via a locking SELECT rejected (dblink + function in select-list)"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r11ext.sql" >/dev/null 2>&1' "r11: CREATE EXTENSION dblink rejected (the DDL-from-DML mechanism)"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r11nn.sql" >/dev/null 2>&1' "r11 HIGH: ADD COLUMN NOT NULL DEFAULT NULL rejected (default evaluates to null)"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r11cast.sql" >/dev/null 2>&1' "r11: SET DEFAULT CAST(NULL AS int) rejected (NULL-default variant)"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r11nncast.sql" >/dev/null 2>&1' "r11: ADD COLUMN NOT NULL DEFAULT CAST(NULL...) rejected"
ok 'python3 "$SUB/migration_lint.py" "$TMP/r11okdef.sql" >/dev/null 2>&1' "r11: ADD COLUMN NOT NULL DEFAULT <non-null> still PASSES"
ok 'python3 "$SUB/migration_lint.py" "$TMP/r11oklock.sql" >/dev/null 2>&1' "r11: 0002's locking SELECT (plain column list) still PASSES"
ok 'python3 "$SUB/migration_lint.py" "$TMP/r11okins.sql" >/dev/null 2>&1' "r11: 0002's INSERT ... SELECT count(*) backfill still PASSES (function in INSERT-SELECT is fine)"

echo "== PR-1c cross-family r12: quoted-dblink bypass + ALTER TYPE multi-action =="
python3 - "$TMP" <<'PYEOF'
import os, sys
d = sys.argv[1]
cases = {
    "r12qdb": 'INSERT INTO rs(status) SELECT public."dblink_exec"(\'db\',\'ALTER TABLE job DROP COLUMN context\');\n',
    "r12qext": 'CREATE EXTENSION IF NOT EXISTS "dblink" WITH SCHEMA public;\n',
    "r12atmulti": "ALTER TYPE my_composite ADD ATTRIBUTE new_attr int, DROP ATTRIBUTE old_attr;\n",
    "r12atren": "ALTER TYPE t ADD ATTRIBUTE a int, RENAME VALUE 'x' TO 'y';\n",
    "r12dbcomment": "-- dblink is deliberately NOT used here\nCREATE TABLE t (id int);\n",  # comment mention -> PASS
    "r12dbstring": "INSERT INTO notes(msg) VALUES ('dblink is banned') ON CONFLICT DO NOTHING;\n",  # string -> PASS
    "r12atmultiok": "ALTER TYPE addr ADD ATTRIBUTE city text, ADD ATTRIBUTE zip text;\n",  # all ADD -> PASS
}
for k, v in cases.items():
    open(os.path.join(d, k + ".sql"), "w", encoding="utf-8").write(v)
PYEOF
ok '! python3 "$SUB/migration_lint.py" "$TMP/r12qdb.sql" >/dev/null 2>&1' "r12 HIGH: a QUOTED \"dblink_exec\" is rejected (de-identified file-level check, not defeated by qi-neutralization)"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r12qext.sql" >/dev/null 2>&1' "r12: CREATE EXTENSION \"dblink\" (quoted) rejected"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r12atmulti.sql" >/dev/null 2>&1' "r12 HIGH: ALTER TYPE ADD ATTRIBUTE, DROP ATTRIBUTE rejected (per-clause, not prefix-only)"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r12atren.sql" >/dev/null 2>&1' "r12: ALTER TYPE ADD ATTRIBUTE, RENAME VALUE rejected (a trailing contraction clause is caught)"
ok 'python3 "$SUB/migration_lint.py" "$TMP/r12dbcomment.sql" >/dev/null 2>&1' "r12: dblink mentioned only in a COMMENT does NOT false-positive (stripped before the check)"
ok 'python3 "$SUB/migration_lint.py" "$TMP/r12dbstring.sql" >/dev/null 2>&1' "r12: dblink mentioned only in a STRING does NOT false-positive"
ok 'python3 "$SUB/migration_lint.py" "$TMP/r12atmultiok.sql" >/dev/null 2>&1' "r12: ALTER TYPE with ALL ADD ATTRIBUTE clauses still PASSES (additive multi-action)"

echo "== PR-1c cross-family r13: U&-escaped dblink + generated-name + nested-paren NULL default =="
python3 - "$TMP" <<'PYEOF'
import os, sys
d = sys.argv[1]
cases = {
    # r13: U&"\0064blink..." decodes to dblink -> caught (the 3rd PG identifier syntax)
    "r13uesc": 'INSERT INTO sink SELECT public.U&"\\0064blink_exec"(\'db\',\'DROP TABLE users\');\n',
    "r13uext": 'CREATE EXTENSION IF NOT EXISTS U&"\\0064blink";\n',
    # r13: column literally named `generated` (unreserved kw) no longer exempts the NOT-NULL-no-default check
    "r13gen": "ALTER TABLE t ADD COLUMN generated int NOT NULL;\n",
    # r13: nested-paren / wrapped-cast NULL defaults
    "r13ndbl": "ALTER TABLE t ADD COLUMN age int NOT NULL DEFAULT ((NULL));\n",
    "r13ncast": "ALTER TABLE t ADD COLUMN age int NOT NULL DEFAULT (CAST(NULL AS int));\n",
    "r13nset": "ALTER TABLE t ALTER COLUMN c SET DEFAULT ((NULL));\n",
    # must still PASS: real generated columns, DEFAULT (0), a legit U& identifier that isn't dblink
    "r13okgen": "ALTER TABLE t ADD COLUMN g int NOT NULL GENERATED ALWAYS AS IDENTITY;\n",
    "r13okdef": "ALTER TABLE t ADD COLUMN c int NOT NULL DEFAULT (0);\n",
    "r13okuid": 'ALTER TABLE t ADD COLUMN U&"caf\\00E9" int;\n',
}
for k, v in cases.items():
    open(os.path.join(d, k + ".sql"), "w", encoding="utf-8").write(v)
PYEOF
ok '! python3 "$SUB/migration_lint.py" "$TMP/r13uesc.sql" >/dev/null 2>&1' "r13 HIGH: a U&-escaped dblink identifier (U&\"\\0064blink_exec\") is decoded and rejected"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r13uext.sql" >/dev/null 2>&1' "r13: CREATE EXTENSION U&\"\\0064blink\" (unicode-escaped) rejected"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r13gen.sql" >/dev/null 2>&1' "r13 HIGH: ADD COLUMN generated (a column named as the keyword) NOT NULL no-default is rejected (exemption anchored to GENERATED ALWAYS/BY DEFAULT)"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r13ndbl.sql" >/dev/null 2>&1' "r13 HIGH: ADD COLUMN NOT NULL DEFAULT ((NULL)) (nested parens) rejected"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r13ncast.sql" >/dev/null 2>&1' "r13: NOT NULL DEFAULT (CAST(NULL AS int)) (wrapped cast) rejected"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r13nset.sql" >/dev/null 2>&1' "r13: SET DEFAULT ((NULL)) rejected"
ok 'python3 "$SUB/migration_lint.py" "$TMP/r13okgen.sql" >/dev/null 2>&1' "r13: a real GENERATED ALWAYS AS IDENTITY column still PASSES (exemption intact)"
ok 'python3 "$SUB/migration_lint.py" "$TMP/r13okdef.sql" >/dev/null 2>&1' "r13: DEFAULT (0) (parenthesized non-null) still PASSES"
ok 'python3 "$SUB/migration_lint.py" "$TMP/r13okuid.sql" >/dev/null 2>&1' "r13: a legit non-dblink U& identifier column still PASSES (decoded, additive)"

echo "== PR-1c cross-family r14: schema-qualified quoted-identifier name extraction =="
python3 - "$TMP" <<'PYEOF'
import os, sys
d = sys.argv[1]
cases = {
    # r14 launder: a schema-qualified quoted CREATE must NOT bless a DROP on a DIFFERENT existing table
    "r14launder": 'CREATE TABLE public."new_table" (id int);\nALTER TABLE public."existing_table" DROP COLUMN important_data;\n',
    "r14launder2": 'CREATE TABLE "new" (id int);\nALTER TABLE "existing" DROP COLUMN x;\n',
    "r14luidx": 'CREATE TABLE public."new" (id int);\nCREATE UNIQUE INDEX u ON public."existing"(email);\n',
    # r14 false-positive fix: a legit ADD COLUMN on a schema-qualified quoted table must PASS
    "r14fp": 'ALTER TABLE public."users" ADD COLUMN new_col int;\n',
    # unquoted schema-qualified same-migration still works
    "r14unq": 'CREATE TABLE public.orders (id int);\nCREATE UNIQUE INDEX u ON public.orders(id);\n',
}
for k, v in cases.items():
    open(os.path.join(d, k + ".sql"), "w", encoding="utf-8").write(v)
PYEOF
ok '! python3 "$SUB/migration_lint.py" "$TMP/r14launder.sql" >/dev/null 2>&1' "r14 BLOCKER: schema-qualified quoted CREATE doesn'\''t launder a DROP COLUMN on a different existing table"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r14launder2.sql" >/dev/null 2>&1' "r14: a bare quoted CREATE doesn'\''t launder a DROP COLUMN on a different quoted table"
ok '! python3 "$SUB/migration_lint.py" "$TMP/r14luidx.sql" >/dev/null 2>&1' "r14: a schema-qualified quoted CREATE doesn'\''t bless a UNIQUE INDEX on a different existing table"
ok 'python3 "$SUB/migration_lint.py" "$TMP/r14fp.sql" >/dev/null 2>&1' "r14: a legit ADD COLUMN on a schema-qualified quoted table PASSES (false-positive fixed)"
ok 'python3 "$SUB/migration_lint.py" "$TMP/r14unq.sql" >/dev/null 2>&1' "r14: unquoted schema-qualified same-migration (CREATE public.orders + UNIQUE INDEX) still PASSES"

echo "== PR-1d: pg_catalog pre-existing-relation context (--existing) =="
printf 'job\nusers\nfinding_current\nexisting_v\n' > "$TMP/prev.txt"   # the live-DB relation set (prev_good schema)
printf '' > "$TMP/prev_empty.txt"                                      # DB with no relations (every object new)
python3 - "$TMP" <<'PYEOF'
import os, sys
d = sys.argv[1]
cases = {
    "d_newuidx": "CREATE TABLE IF NOT EXISTS finding_outcome (id int);\nCREATE UNIQUE INDEX u ON finding_outcome(id);\n",
    "d_newview": "CREATE OR REPLACE VIEW v AS SELECT 1;\n",
    "d_launder": "CREATE TABLE IF NOT EXISTS users (id int);\nALTER TABLE users DROP COLUMN pw;\n",
    "d_replexist": "CREATE OR REPLACE VIEW existing_v AS SELECT a, b FROM t;\n",
    "d_uidxexist": "CREATE UNIQUE INDEX u ON job (x);\n",
    "d_dropexist": "ALTER TABLE public.\"job\" DROP COLUMN context;\n",
}
for k, v in cases.items():
    open(os.path.join(d, k + ".sql"), "w", encoding="utf-8").write(v)
PYEOF
ok 'python3 "$SUB/migration_lint.py" --existing "$TMP/prev.txt" "$TMP/d_newuidx.sql" >/dev/null 2>&1' "PR-1d: with --existing, a NEW table + its UNIQUE INDEX PASSES (finding_outcome not in pg_catalog = born this deploy)"
ok 'python3 "$SUB/migration_lint.py" --existing "$TMP/prev.txt" "$TMP/d_newview.sql" >/dev/null 2>&1' "PR-1d: a NEW view (CREATE OR REPLACE VIEW, not in pg_catalog) PASSES"
ok '! python3 "$SUB/migration_lint.py" --existing "$TMP/prev.txt" "$TMP/d_launder.sql" >/dev/null 2>&1' "PR-1d: launder STILL closed — DROP COLUMN on a pre-existing table (users in pg_catalog) REJECTS even with --existing"
ok '! python3 "$SUB/migration_lint.py" --existing "$TMP/prev.txt" "$TMP/d_replexist.sql" >/dev/null 2>&1' "PR-1d: CREATE OR REPLACE of a PRE-EXISTING view (existing_v in pg_catalog) REJECTS (a redef could narrow un-revertibly)"
ok '! python3 "$SUB/migration_lint.py" --existing "$TMP/prev.txt" "$TMP/d_uidxexist.sql" >/dev/null 2>&1' "PR-1d: UNIQUE INDEX on a PRE-EXISTING table (job) REJECTS (tightening)"
ok '! python3 "$SUB/migration_lint.py" --existing "$TMP/prev.txt" "$TMP/d_dropexist.sql" >/dev/null 2>&1' "PR-1d: schema-qualified DROP on a pre-existing table (public.\"job\", bare job in pg_catalog) REJECTS"
ok '! python3 "$SUB/migration_lint.py" "$TMP/d_newuidx.sql" >/dev/null 2>&1' "PR-1d fallback: WITHOUT --existing the new-table UNIQUE INDEX stays conservative-REJECT (current behavior preserved)"
for m in 0008 0009 0011 0012; do
  f=$(ls "$REPO"/src/runtime/ledger/migrations/${m}_*.sql)
  ok 'python3 "$SUB/migration_lint.py" --existing "$TMP/prev_empty.txt" "'"$f"'" >/dev/null 2>&1' "PR-1d: real $m PASSES when its objects are new (empty pg_catalog) — the idempotent create+index/view false-positive is fixed"
done
ok 'grep -q "pg_catalog for pre-existing" "$SUB/reconcile.sh" && grep -q -- "--existing" "$SUB/reconcile.sh"' "PR-1d: reconcile queries pg_catalog (fail-closed) and passes --existing to the lint"
# PR-1d review folds (r-pr1d): identifier truncation + foreign tables; + codify the whitespace-dot non-hole.
L63="aaaaaaaaaa_bbbbbbbbbb_cccccccccc_dddddddddd_eeeeeeeeee_ffffffff"   # exactly 63 bytes
printf 'job\nexisting_v\n%s\n' "$L63" > "$TMP/prevd.txt"
printf 'ALTER TABLE %sg DROP COLUMN context;\n' "$L63" > "$TMP/d_trunc.sql"          # 64-byte -> PG 63-byte existing
printf 'ALTER TABLE public . job DROP COLUMN x;\n' > "$TMP/d_wsalter.sql"            # whitespace-around-dot
printf 'CREATE OR REPLACE VIEW public . existing_v AS SELECT 1;\n' > "$TMP/d_wsview.sql"
ok '! python3 "$SUB/migration_lint.py" --existing "$TMP/prevd.txt" "$TMP/d_trunc.sql" >/dev/null 2>&1' "PR-1d HIGH: a 64-byte name PG truncates to a pre-existing 63-byte relation REJECTS (NAMEDATALEN truncation)"
ok '! python3 "$SUB/migration_lint.py" --existing "$TMP/prevd.txt" "$TMP/d_wsalter.sql" >/dev/null 2>&1' "PR-1d: whitespace-around-dot ALTER (public . job) REJECTS (r14 dot-collapse; not a denylist bypass)"
ok '! python3 "$SUB/migration_lint.py" --existing "$TMP/prevd.txt" "$TMP/d_wsview.sql" >/dev/null 2>&1' "PR-1d: whitespace-around-dot CREATE OR REPLACE VIEW (public . existing_v) REJECTS"
ok 'relk="$(grep -oE "relkind IN \(([^)]*)\)" "$SUB/reconcile.sh")"; for k in r i I S v m c f p; do echo "$relk" | grep -q "'\''$k'\''" || exit 1; done' "PR-1d r2 HIGH: pg_catalog query covers ALL user relkinds (r/i/I/S/v/m/c/f/p) so no pre-existing object (sequence, index, composite) is seen as new"
# PR-1d r2 folds: identifier folding parity (ASCII-only downcase + 63-byte truncate on BOTH sides).
python3 - "$TMP" <<'PYEOF'
import os, sys
d = sys.argv[1]
n63 = "a" * 61 + "İ"     # 61 'a' + İ (U+0130, 2 bytes) = exactly 63 bytes; Python .lower() would expand it
prev = ["job", "job_id_seq", n63, "a" * 61 + "K"]   # + Kelvin-K name
open(os.path.join(d, "prev_r2.txt"), "w", encoding="utf-8").write("\n".join(prev) + "\n")
open(os.path.join(d, "d_seqrename.sql"), "w", encoding="utf-8").write("ALTER TABLE job_id_seq RENAME TO job_id_seq_old;\n")
open(os.path.join(d, "d_unifold.sql"), "w", encoding="utf-8").write(f"ALTER TABLE {n63} DROP COLUMN x;\n")
open(os.path.join(d, "d_kelvin.sql"), "w", encoding="utf-8").write(f"ALTER TABLE {'a'*61}K DROP COLUMN x;\n")
PYEOF
ok '! python3 "$SUB/migration_lint.py" --existing "$TMP/prev_r2.txt" "$TMP/d_seqrename.sql" >/dev/null 2>&1' "PR-1d r2: ALTER TABLE <pre-existing sequence> RENAME REJECTS (sequence now in the pg_catalog set)"
ok '! python3 "$SUB/migration_lint.py" --existing "$TMP/prev_r2.txt" "$TMP/d_unifold.sql" >/dev/null 2>&1' "PR-1d r2: a 63-byte non-ASCII (İ) pre-existing name REJECTS — ASCII-fold parity, no Unicode-.lower() truncation miss"
ok '! python3 "$SUB/migration_lint.py" --existing "$TMP/prev_r2.txt" "$TMP/d_kelvin.sql" >/dev/null 2>&1' "PR-1d r2: a Kelvin-sign (U+212A) pre-existing name REJECTS (ASCII-fold doesn'\''t shrink it like Python .lower())"
ok 'grep -q "_pg_fold" "$SUB/migration_lint.py" && grep -q "ident.translate" "$SUB/migration_lint.py"' "PR-1d r2: identifier folding mirrors Postgres (ASCII downcase + 63-byte truncate) on both sides via _pg_fold"
# PR-1d r3 fold: Postgres inheritance-wildcard (name*) must not read as a new relation.
printf 'job\nusers\n' > "$TMP/prevw.txt"
printf 'ALTER TABLE job* DROP COLUMN context;\n' > "$TMP/d_w1.sql"
printf 'ALTER TABLE public.job* DROP COLUMN context;\n' > "$TMP/d_w2.sql"
printf 'ALTER TABLE job * DROP COLUMN context;\n' > "$TMP/d_w3.sql"
printf 'CREATE TABLE IF NOT EXISTS newt (id int);\nALTER TABLE newt* ADD COLUMN c int;\n' > "$TMP/d_w4.sql"
ok '! python3 "$SUB/migration_lint.py" --existing "$TMP/prevw.txt" "$TMP/d_w1.sql" >/dev/null 2>&1' "PR-1d r3: ALTER TABLE job* (inheritance wildcard) DROP COLUMN REJECTS (targets pre-existing job, not a new relation)"
ok '! python3 "$SUB/migration_lint.py" --existing "$TMP/prevw.txt" "$TMP/d_w2.sql" >/dev/null 2>&1' "PR-1d r3: schema-qualified public.job* wildcard DROP REJECTS"
ok '! python3 "$SUB/migration_lint.py" --existing "$TMP/prevw.txt" "$TMP/d_w3.sql" >/dev/null 2>&1' "PR-1d r3: spaced wildcard 'job *' DROP REJECTS"
ok 'python3 "$SUB/migration_lint.py" --existing "$TMP/prevw.txt" "$TMP/d_w4.sql" >/dev/null 2>&1' "PR-1d r3: newt* ADD COLUMN on a genuinely-new table still PASSES (wildcard doesn'\''t block additive)"
ok '! grep -qE "\(\[\^..s\(\]\+\)" "$SUB/migration_lint.py"' "PR-1d r3: no name-capture regex uses the *-including class ([^\\s(]+) — all exclude the inheritance wildcard"
# PR-1d r4 fold: inheritance/partition target-set expansion — a recursing op on a "new" parent that gained
# a PRE-EXISTING descendant must not skip the clause checks.
printf 'child\nexisting_part\n' > "$TMP/previ.txt"
python3 - "$TMP" <<'PYEOF'
import os, sys
d = sys.argv[1]
cases = {
    "d_inhren":  "CREATE TABLE parent (c int);\nALTER TABLE child INHERIT parent;\nALTER TABLE parent* RENAME COLUMN c TO d;\n",
    "d_inhdrop": "CREATE TABLE parent (c int);\nALTER TABLE child INHERIT parent;\nALTER TABLE parent DROP COLUMN c;\n",
    "d_inhck":   "CREATE TABLE parent (c int);\nALTER TABLE child INHERIT parent;\nALTER TABLE parent ADD CONSTRAINT ck CHECK (c>0);\n",
    "d_partui":  "CREATE TABLE p (id int) PARTITION BY RANGE (id);\nALTER TABLE p ATTACH PARTITION existing_part FOR VALUES FROM (1) TO (100);\nCREATE UNIQUE INDEX u ON p (id);\n",
    "d_newfull": "CREATE TABLE fresh (a int);\nCREATE UNIQUE INDEX u ON fresh (a);\nALTER TABLE fresh ADD CONSTRAINT pk PRIMARY KEY (a);\n",
    "d_inhadd":  "CREATE TABLE parent (c int);\nALTER TABLE child INHERIT parent;\nALTER TABLE parent ADD COLUMN e int;\n",
}
for k, v in cases.items():
    open(os.path.join(d, k + ".sql"), "w", encoding="utf-8").write(v)
PYEOF
ok '! python3 "$SUB/migration_lint.py" --existing "$TMP/previ.txt" "$TMP/d_inhren.sql" >/dev/null 2>&1' "PR-1d r4: RENAME on a new parent that INHERITs a pre-existing child REJECTS (recurses to the pre-existing child)"
ok '! python3 "$SUB/migration_lint.py" --existing "$TMP/previ.txt" "$TMP/d_inhdrop.sql" >/dev/null 2>&1' "PR-1d r4: DROP COLUMN on a new INHERIT-parent REJECTS (recurses)"
ok '! python3 "$SUB/migration_lint.py" --existing "$TMP/previ.txt" "$TMP/d_inhck.sql" >/dev/null 2>&1' "PR-1d r4: ADD CONSTRAINT CHECK on a new INHERIT-parent REJECTS (CHECK recurses to the pre-existing child)"
ok '! python3 "$SUB/migration_lint.py" --existing "$TMP/previ.txt" "$TMP/d_partui.sql" >/dev/null 2>&1' "PR-1d r4: UNIQUE INDEX on a new partitioned table with a PRE-EXISTING attached partition REJECTS"
ok 'python3 "$SUB/migration_lint.py" --existing "$TMP/previ.txt" "$TMP/d_newfull.sql" >/dev/null 2>&1' "PR-1d r4: a genuinely-new table (no INHERIT/ATTACH) + UNIQUE INDEX + ADD CONSTRAINT PK still PASSES"
ok 'python3 "$SUB/migration_lint.py" --existing "$TMP/previ.txt" "$TMP/d_inhadd.sql" >/dev/null 2>&1' "PR-1d r4: ADD COLUMN on a new INHERIT-parent still PASSES (additive even when recursed to the child)"

# PR-1d r5 fold: PARTITION OF is a linkage too (a new partition catches the pre-existing parent's routed
# inserts), and the linkage guard must be DEPLOY-global (linkage in file N, tightening in file N+1).
printf 'events\njob\n' > "$TMP/prevp.txt"          # events + job pre-exist
python3 - "$TMP" <<'PYEOF'
import os, sys
d = sys.argv[1]
cases = {
    # Path A: a CONSTRAINED partition of a PRE-EXISTING parent tightens the parent's routed inserts -> REJECT
    "d_paconstr": "CREATE TABLE events_us PARTITION OF events (CONSTRAINT ck CHECK (amount > 0)) FOR VALUES IN ('US');\n",
    "d_padefck":  "CREATE TABLE events_def PARTITION OF events (CHECK (amount > 0)) DEFAULT;\n",
    # r7: a BARE partition of a PRE-EXISTING parent also tightens (shrinks a pre-existing DEFAULT partition's
    # implicit bound), so it REJECTS too; a partition (bare or constrained) of a NEW parent still PASSES.
    "d_pabare":   "CREATE TABLE events_us PARTITION OF events FOR VALUES IN ('US');\n",
    "d_pabarenew": "CREATE TABLE ev0 (region text) PARTITION BY LIST (region);\nCREATE TABLE ev0_us PARTITION OF ev0 FOR VALUES IN ('US');\n",
    "d_panew":    "CREATE TABLE ev (region text, amount int) PARTITION BY LIST (region);\nCREATE TABLE ev_us PARTITION OF ev (CONSTRAINT ck CHECK (amount > 0)) FOR VALUES IN ('US');\n",
    # Path B: split-form partition tightening in ONE file -> REJECT (PARTITION OF now sets `linked`)
    "d_pbsplit":  "CREATE TABLE events_us PARTITION OF events FOR VALUES IN ('US');\nALTER TABLE events_us ADD CONSTRAINT ck CHECK (amount > 0);\nCREATE UNIQUE INDEX uq ON events_us (email);\n",
}
for k, v in cases.items():
    open(os.path.join(d, k + ".sql"), "w", encoding="utf-8").write(v)
# BLOCKER 2 cross-file: linkage in 001, tightening in 002 (deploy-global `linked`)
open(os.path.join(d, "d_x001.sql"), "w", encoding="utf-8").write(
    "CREATE TABLE new_parent (id int) PARTITION BY RANGE (id);\nALTER TABLE new_parent ATTACH PARTITION pre_existing FOR VALUES FROM (1) TO (100);\n")
open(os.path.join(d, "d_x002.sql"), "w", encoding="utf-8").write(
    "CREATE UNIQUE INDEX u ON new_parent (id);\nALTER TABLE new_parent DROP COLUMN context;\n")
PYEOF
printf 'pre_existing\n' > "$TMP/prevx.txt"
ok '! python3 "$SUB/migration_lint.py" --existing "$TMP/prevp.txt" "$TMP/d_paconstr.sql" >/dev/null 2>&1' "PR-1d r5: constrained PARTITION OF a pre-existing parent REJECTS (partition-local CHECK tightens routed parent inserts)"
ok '! python3 "$SUB/migration_lint.py" --existing "$TMP/prevp.txt" "$TMP/d_padefck.sql" >/dev/null 2>&1' "PR-1d r5: constrained DEFAULT partition of a pre-existing parent REJECTS"
ok '! python3 "$SUB/migration_lint.py" --existing "$TMP/prevp.txt" "$TMP/d_pabare.sql" >/dev/null 2>&1' "PR-1d r7: BARE partition of a PRE-EXISTING parent REJECTS (shrinks a pre-existing DEFAULT partition'\''s implicit bound — fail-closed)"
ok 'python3 "$SUB/migration_lint.py" --existing "$TMP/prevp.txt" "$TMP/d_pabarenew.sql" >/dev/null 2>&1' "PR-1d r7: BARE partition of a NEW parent (same deploy) PASSES (no old code touches it)"
ok 'python3 "$SUB/migration_lint.py" --existing "$TMP/prevp.txt" "$TMP/d_panew.sql" >/dev/null 2>&1' "PR-1d r5: constrained partition of a NEW parent (same deploy) PASSES (no old code depends on it)"
ok '! python3 "$SUB/migration_lint.py" --existing "$TMP/prevp.txt" "$TMP/d_pbsplit.sql" >/dev/null 2>&1' "PR-1d r5: split-form ADD CONSTRAINT / UNIQUE INDEX on a partition of a pre-existing parent REJECTS (PARTITION OF sets linked)"
ok '! python3 "$SUB/migration_lint.py" --existing "$TMP/prevx.txt" "$TMP/d_x001.sql" "$TMP/d_x002.sql" >/dev/null 2>&1' "PR-1d r5 BLOCKER 2: cross-file linkage (001 ATTACH) + tightening (002 DROP/UNIQUE) REJECTS (deploy-global linked)"
ok 'python3 "$SUB/migration_lint.py" --existing "$TMP/prevx.txt" "$TMP/d_x002.sql" >/dev/null 2>&1' "PR-1d r5 BLOCKER 2 control: 002 ALONE (no linkage file) PASSES — proving the deploy-global carry is what closes it"
ok 'grep -q "deploy_linked" "$SUB/migration_lint.py" && grep -q "PARTITION\\\\s+OF" "$SUB/migration_lint.py"' "PR-1d r5: linkage guard is deploy-global (deploy_linked) and includes PARTITION OF"

# PR-1d r6 fold: ATTACH PARTITION tightens on BOTH sides — a pre-existing PARENT re-routes its inserts into
# the attached partition (its CHECK/NOT NULL/UNIQUE can reject rows the parent used to accept), and a
# pre-existing CHILD gains the implicit partition-bound CHECK. Additive ONLY when parent AND child are both
# new this deploy. (kilabz found the parent side; the child side is the same class, closed together.)
printf 'events\nexisting_child\n' > "$TMP/prevat.txt"    # events + existing_child pre-exist
python3 - "$TMP" <<'PYEOF'
import os, sys
d = sys.argv[1]
cases = {
    # parent-side (kilabz r6): a NEW constrained child attached to a PRE-EXISTING parent -> REJECT
    "d_atpar":  "CREATE TABLE events_us (region text, amount integer, CONSTRAINT ck CHECK (amount > 0));\nALTER TABLE events ATTACH PARTITION events_us FOR VALUES IN ('US');\n",
    # parent-side, bare new child, still a PRE-EXISTING parent -> REJECT fail-closed (both-new rule)
    "d_atparb": "CREATE TABLE events_us (region text, amount integer);\nALTER TABLE events ATTACH PARTITION events_us FOR VALUES IN ('US');\n",
    # child-side: a PRE-EXISTING child attached to a NEW parent -> the child gains a partition bound -> REJECT
    "d_atchild": "CREATE TABLE p (id int) PARTITION BY RANGE (id);\nALTER TABLE p ATTACH PARTITION existing_child FOR VALUES FROM (1) TO (100);\n",
    # both new (even a constrained child) -> PASS (no old code touches either relation)
    "d_atboth":  "CREATE TABLE p (region text, amount integer) PARTITION BY LIST (region);\nCREATE TABLE p_us (region text, amount integer, CONSTRAINT ck CHECK (amount > 0));\nALTER TABLE p ATTACH PARTITION p_us FOR VALUES IN ('US');\n",
}
for k, v in cases.items():
    open(os.path.join(d, k + ".sql"), "w", encoding="utf-8").write(v)
PYEOF
ok '! python3 "$SUB/migration_lint.py" --existing "$TMP/prevat.txt" "$TMP/d_atpar.sql" >/dev/null 2>&1' "PR-1d r6: constrained partition ATTACHed to a PRE-EXISTING parent REJECTS (partition-local CHECK tightens routed parent inserts)"
ok '! python3 "$SUB/migration_lint.py" --existing "$TMP/prevat.txt" "$TMP/d_atparb.sql" >/dev/null 2>&1' "PR-1d r6: ATTACH to a PRE-EXISTING parent REJECTS fail-closed (both parent and child must be new)"
ok '! python3 "$SUB/migration_lint.py" --existing "$TMP/prevat.txt" "$TMP/d_atchild.sql" >/dev/null 2>&1' "PR-1d r6: ATTACH a PRE-EXISTING child to a new parent REJECTS (child gains the partition-bound CHECK)"
ok 'python3 "$SUB/migration_lint.py" --existing "$TMP/prevat.txt" "$TMP/d_atboth.sql" >/dev/null 2>&1' "PR-1d r6: ATTACH where parent AND child are both new PASSES (no old code touches either)"
ok '! grep -q "ATTACH..s.PARTITION..b., c" "$SUB/migration_lint.py"' "PR-1d r6: _additive_alter_table_clause no longer unconditionally blesses an ATTACH clause"

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

echo "== liveness-canary: declared-set extraction (liveness_targets.py) =="
LV="$TMP/lv"; LVH="$LV/home"
mkdir -p "$LVH/inbox" "$LVH/state" "$LVH/orchestrator" "$LV/fakehome/Library/LaunchAgents" "$LV/plists"
printf 'MACHINE_ROLE=factory\nMYNDAIX_HOME=%s\nMYNDAIX_DSN=postgresql://127.0.0.1/runtime\nOPERATOR_INBOX=%s/inbox\nAUTHOR_ALLOWLIST=bot\nDEPLOY_CLONE=%s\n' "$LVH" "$LVH" "$REPO" > "$LVH/config.env"
LT="$SUB/liveness_targets.py"
printf '{"label":"ai.myndaix.t1","roles":["factory"],"stdout":"{MYNDAIX_HOME}/orchestrator/t1.out","liveness_max_gap_seconds":100}' > "$LV/plists/t1.json"
printf '{"label":"ai.myndaix.t2","roles":["lab"],"stdout":"{MYNDAIX_HOME}/orchestrator/t2.out","liveness_max_gap_seconds":100}' > "$LV/plists/t2.json"
printf '{"label":"ai.myndaix.t3","roles":["factory"],"requires_sentinel":"T3_ARMED","stdout":"{MYNDAIX_HOME}/orchestrator/t3.out","liveness_max_gap_seconds":50}' > "$LV/plists/t3.json"
printf '{broken' > "$LV/plists/t4.json"
printf '{"label":"ai.myndaix.t5","roles":["factory"],"stdout":"{MYNDAIX_HOME}/orchestrator/t5.out"}' > "$LV/plists/t5.json"
lt_out="$(python3 "$LT" "$LVH/config.env" "$LV"/plists/*.json)"; lt_rc=$?
ok '[[ "$lt_rc" -eq 0 ]]' "targets: a corrupt descriptor does NOT sink the batch (exit 0)"
ok '[[ "$(printf "%s\n" "$lt_out" | awk -F"\t" "\$1==\"ai.myndaix.t1\"{print \$2\":\"\$3\":\"\$4}")" == "100:-:$LVH/orchestrator/t1.out" ]]' "targets: watched job emits label/max_gap/-/resolved .out path"
ok '! printf "%s\n" "$lt_out" | grep -q "ai.myndaix.t2"' "targets: other-role descriptor skipped (excluded, not missing)"
ok '[[ "$(printf "%s\n" "$lt_out" | awk -F"\t" "\$1==\"ai.myndaix.t3\"{print \$3}")" == "T3_ARMED" ]]' "targets: requires_sentinel carried through"
ok 'printf "%s\n" "$lt_out" | grep -q "^ERR	t4.json	"' "targets: corrupt JSON -> per-file ERR line (fail-closed, batch continues)"
ok 'printf "%s\n" "$lt_out" | grep -q "^ERR	t5.json	.*liveness_max_gap_seconds"' "targets: watched-role descriptor MISSING liveness_max_gap_seconds -> ERR (unwatchable = the omission class)"

echo "== liveness-canary: build gates — every descriptor watchable + every-tick-logs invariant =="
for d in "$SUB"/plists/*.json; do
  ok 'python3 -c "import json,sys; g=json.load(open(sys.argv[1])).get(\"liveness_max_gap_seconds\"); sys.exit(0 if isinstance(g,int) and not isinstance(g,bool) and g>0 else 1)" "'"$d"'"' "descriptor $(basename "$d") declares a positive liveness_max_gap_seconds"
done
# every descriptor's PROGRAM must write >=1 stdout line per fire (the .out mtime IS the
# execution evidence) — asserted via the liveness-fire marker each program carries.
cat > "$TMP/lvfire.py" <<'PYEOF'
import json, os, sys
repo, desc = sys.argv[1], sys.argv[2]
prog = json.load(open(desc))["program"][-1]
prog = prog.replace("{DEPLOY_CLONE}", repo)
# the reconcile poll runs the INSTALLED bootstrap-fetch copy; its source is in substrate/
prog = prog.replace("{MYNDAIX_HOME}/bin/bootstrap-fetch", os.path.join(repo, "substrate", "bootstrap-fetch.sh"))
txt = open(prog).read()
# The marker documents the invariant; it must be BACKED by real fire plumbing on a
# NON-COMMENT line (KilaBz re-review: comment-embedded strings must not pass): either the
# wrappers' unconditional `printf ... tick fire`, or an every-exit-path log() (defined
# locally, or lib.sh sourced — whose log the canaries call on every path; behaviorally
# proven for the canaries by the stub runs below; the wrappers can't be behaviorally run
# here without ticking live systems).
marker = "liveness-fire" in txt
# FIRST-TOKEN-anchored shapes (KilaBz r3: `: # printf ... tick fire` style inline-comment
# lines must not pass) — the fire plumbing must BE the command, not appear after one.
import re as _re
fire = _re.compile(r"^\s*printf\s.*tick fire")
logdef = _re.compile(r"^\s*log\s*\(\)\s*\{")
libsrc = _re.compile(r"^\s*(source|\.)\s+\S*lib\.sh")
lines = txt.splitlines()
plumbing = any(fire.match(l) or logdef.match(l) or libsrc.match(l) for l in lines)
sys.exit(0 if marker and plumbing else 1)
PYEOF
for d in "$SUB"/plists/*.json; do
  ok 'python3 "$TMP/lvfire.py" "$REPO" "'"$d"'"' "every-tick-logs: $(basename "$d" .json) program carries the liveness-fire stdout invariant"
done
ok '[[ "$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))[\"schedule\"][\"StartInterval\"])" "$SUB/plists/ai.myndaix.liveness.json")" == "$(grep -m1 "^INTERVAL=" "$SUB/liveness-canary.sh" | cut -d= -f2 | cut -d" " -f1)" ]]' "canary INTERVAL matches its descriptor StartInterval"

echo "== liveness-canary: runtime divergence paths (stub launchctl — live launchd untouched) =="
cat > "$LV/launchctl" <<'EOF'
#!/bin/bash
# stub: LV_MODE=notloaded|clean, LV_EXIT_CODE=<n>, LV_LIST="<extra loaded labels>"
case "$1" in
  print)
    [[ "${LV_MODE:-clean}" == "notloaded" ]] && exit 113
    printf '\tstate = waiting\n\tpid = 4242\n\tlast exit code = %s\n' "${LV_EXIT_CODE:-0}"
    ;;
  list)
    printf 'PID\tStatus\tLabel\n'
    for l in ${LV_LIST:-}; do printf -- '-\t0\t%s\n' "$l"; done
    ;;
esac
exit 0
EOF
chmod +x "$LV/launchctl"
lvrun() { MYNDAIX_HOME="$LVH" HOME="$LV/fakehome" LIVENESS_LAUNCHCTL="$LV/launchctl" /bin/bash "$SUB/liveness-canary.sh" 2>&1; }
# run 1 — nothing loaded: divergences, streak=1, NO alert yet; sentinel-gated job skipped
lv1="$(LV_MODE=notloaded lvrun)"; lv1rc=$?
ok '[[ "$lv1rc" -eq 0 ]]' "canary exits 0 even when divergent (no launchd failure state)"
ok 'printf "%s" "$lv1" | grep -q "ai.myndaix.controller: NOT LOADED"' "not-loaded declared job -> divergence with bootstrap remedy"
ok '! printf "%s" "$lv1" | grep -q "ai.myndaix.reconcile"' "sentinel-gated job (unarmed) skipped — not a divergence"
ok 'printf "%s" "$lv1" | grep -q "ai.myndaix.runtime: daemon NOT LOADED"' "static daemon (ai.myndaix.runtime) covered — pid liveness"
ok '[[ "$(cat "$LVH/state/liveness-streak")" == "1" ]]' "streak=1 after first divergent run"
ok '[[ "$(ls "$LVH/inbox" 2>/dev/null | wc -l | tr -d " ")" == "0" ]]' "below threshold: no alert dropped"
ok '[[ -e "$LVH/state/liveness-last-run" ]]' ".last_run touched on a normal run (Oracle build note 1)"
# run 2 — still divergent: threshold 2 -> alert + latch
lv2="$(LV_MODE=notloaded lvrun)"
ok 'ls "$LVH/inbox"/liveness-alert-*.md >/dev/null 2>&1' "streak threshold (2) -> alert dropped to operator inbox"
ok '[[ -e "$LVH/state/liveness-alerted" ]]' "latch set only after a successful alert write"
ok 'grep -q "NOT LOADED" "$LVH"/inbox/liveness-alert-*.md && grep -q "remedy" "$LVH"/inbox/liveness-alert-*.md' "alert lists each divergent label + which check failed + the remedy"
# run 3 — latched: no duplicate alert
lv3="$(LV_MODE=notloaded lvrun)"
ok '[[ "$(ls "$LVH/inbox" | wc -l | tr -d " ")" == "1" ]]' "latched: no duplicate alert on run 3"
# run 4 — clean via UNCONDITIONAL reconcile-grace (fresh plists) -> streak + latch reset
for d in "$SUB"/plists/*.json; do
  l="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["label"])' "$d")"
  touch "$LV/fakehome/Library/LaunchAgents/$l.plist"
done
lv4="$(LV_MODE=clean lvrun)"
ok 'printf "%s" "$lv4" | grep -q "all declared jobs alive"' "fresh plist mtimes -> unconditional reconcile-grace PASS"
ok '[[ ! -e "$LVH/state/liveness-streak" && ! -e "$LVH/state/liveness-alerted" ]]' "clean run resets streak + latch"
# run 5 — grace expired, loaded, but NO .out ever -> never-ran divergence
find "$LV/fakehome/Library/LaunchAgents" -name "*.plist" -exec touch -t 202601010000 {} +
lv5="$(LV_MODE=clean lvrun)"
ok 'printf "%s" "$lv5" | grep -q "NEVER RAN"' "past grace with no execution evidence -> never-ran divergence"
# run 6 — fresh evidence everywhere EXCEPT controller (stale) -> only controller flags
python3 "$LT" "$LVH/config.env" "$SUB"/plists/*.json | while IFS="$(printf '\t')" read -r l g s o; do
  [[ -n "$o" ]] || continue; mkdir -p "$(dirname "$o")"; touch "$o"
done
touch -t 202601010000 "$LVH/orchestrator/controller.out"
lv6="$(LV_MODE=clean lvrun)"
ok 'printf "%s" "$lv6" | grep -q "ai.myndaix.controller: STALE"' "stale .out past max gap -> divergence naming the .out"
ok '! printf "%s" "$lv6" | grep -q "ai.myndaix.automerge:"' "fresh .out passes (no false stale)"
# run 7 — healthy freshness but last exit code nonzero -> divergence
touch "$LVH/orchestrator/controller.out"
lv7="$(LV_MODE=clean LV_EXIT_CODE=78 lvrun)"
ok 'printf "%s" "$lv7" | grep -q "last exit code = 78"' "nonzero last exit code -> divergence"
# run 8 — rogue sweep: an undeclared loaded ai.myndaix.* label flags; declared ones don't
lv8="$(LV_MODE=clean LV_LIST="ai.myndaix.rogue ai.myndaix.controller" lvrun)"
ok 'printf "%s" "$lv8" | grep -q "ai.myndaix.rogue: ROGUE"' "reverse sweep: undeclared loaded label -> rogue divergence"
ok '! printf "%s" "$lv8" | grep -q "ai.myndaix.controller: ROGUE"' "reverse sweep: declared label never flagged rogue"
# run 9 — ARMED sentinel-gated job IS watched (missing evidence -> divergence)
touch "$LVH/RECONCILE_ARMED"
rm -f "$LVH/state/reconcile.out"   # run 6 pre-touched it (targets ignore arming; bash gates it)
lv9="$(LV_MODE=clean lvrun)"
ok 'printf "%s" "$lv9" | grep -q "ai.myndaix.reconcile: NEVER RAN"' "armed sentinel job IS watched (no evidence -> divergence)"
rm -f "$LVH/RECONCILE_ARMED"
# run 10 — sleep/wake self-grace: own last-run stale -> touch + skip one tick, check nothing
touch -t 202601010000 "$LVH/state/liveness-last-run"
lv10="$(LV_MODE=notloaded lvrun)"
ok 'printf "%s" "$lv10" | grep -q "sleep/wake grace"' "stale own .last_run -> sleep-guard grace tick"
ok '! printf "%s" "$lv10" | grep -q "DIVERGENT"' "grace tick checks nothing (no wake-up alert storm)"
# run 11 — a NEGATIVE last exit code (signal-class) is caught too (KilaBz P3 fold)
lv11="$(LV_MODE=clean LV_EXIT_CODE=-1 lvrun)"
ok 'printf "%s" "$lv11" | grep -q "last exit code = -1"' "negative last exit code -> divergence (signal-class parse)"
# review folds: rogue sweep is GATED on a populated declared set (Oracle P3 — an empty set
# must not flag every legit job ROGUE with a bootout remedy); exit-0-always covers state
# writes too (KilaBz P2 — no die/set-e death on streak/latch/mkdir failures)
ok 'grep -B8 "ROGUE" "$SUB/liveness-canary.sh" | grep -q "targets_ok" || grep -q "targets_ok\" -eq 1" "$SUB/liveness-canary.sh"' "rogue sweep skipped when the declared set failed to populate"
# run 12 — BEHAVIORAL exit-0-always proof (KilaBz re-review: a textual grep can't prove it).
# Why chmod 555 works here (KilaBz r3): 555 blocks directory-ENTRY creation, not writes to
# existing files — so both failing paths are made CREATIONS: .last_run is removed first
# (its touch becomes a create), and the streak write always creates "$STREAK_FILE.tmp"
# (the tmp never persists — it is mv'd or rm'd every run). Both deterministically fail.
rm -f "$LVH/state/liveness-last-run"
chmod 555 "$LVH/state"
lv12="$(LV_MODE=notloaded lvrun)"; lv12rc=$?
chmod 755 "$LVH/state"
ok '[[ "$lv12rc" -eq 0 ]]' "read-only state dir mid-divergence -> canary STILL exits 0 (behavioral exit-0-always)"
ok 'printf "%s" "$lv12" | grep -q "ALARM could not write streak"' "streak-write failure ALARM-logged (not die, not silent)"
ok 'printf "%s" "$lv12" | grep -q "WARN cannot touch .last_run"' ".last_run create-failure WARN-logged, run continues"
ok '! grep -qE "^[[:space:]]*die " "$SUB/liveness-canary.sh"' "canary body never calls die (config-load die inside lib.sh is the accepted pre-check)"
# mutual watch: drift-canary reverse-watches liveness-canary.out through its existing latch
ok 'grep -q "liveness-canary.out" "$SUB/drift-canary.sh" && grep -A3 "liveness-canary.out stale" "$SUB/drift-canary.sh" | grep -q "rc=1"' "drift-canary folds a stale liveness-canary.out into its own streak+latch path"
ok 'grep -qE "print|list" "$SUB/liveness-canary.sh" && ! grep -qE "\"\\\$LCTL\" (bootstrap|bootout|kickstart)" "$SUB/liveness-canary.sh"' "liveness-canary is READ-ONLY against launchd (print/list only)"

echo "== shell hygiene: bash -n + shellcheck clean on the production substrate scripts =="
# The production scripts must be pristine. test.sh itself is exempt from the strict SC2034 gate
# (its ok '<cmd>' harness uses vars only inside single-quoted eval strings shellcheck can't see
# into) — it is bash -n checked below. shellcheck is skipped gracefully if absent (Linux CI).
HAVE_SC=1; command -v shellcheck >/dev/null 2>&1 || { HAVE_SC=0; echo "  --: SKIP shellcheck (not installed)"; }
for s in lib.sh bootstrap-fetch.sh reconcile.sh drift-canary.sh liveness-canary.sh; do
  ok 'bash -n "'"$SUB/$s"'"' "bash -n $s"
  [[ "$HAVE_SC" == 1 ]] && ok 'shellcheck -x -S warning "'"$SUB/$s"'" >/dev/null 2>&1' "shellcheck $s"
done
ok 'bash -n "'"$SUB/test.sh"'"' "bash -n test.sh"

echo "=================================================="
echo "  substrate test.sh: $pass ok, $fail fail"
echo "=================================================="
[[ "$fail" -eq 0 ]]
