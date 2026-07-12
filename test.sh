#!/bin/bash
# test.sh — NO-SPEND smoke test for the MX Quality Orchestrator + supplier gateway.
# Runs every gateway/orchestrator suite against httpx.MockTransport (no network, no dollars)
# plus a CLI dry-run proving the human cost gate renders and ABORTS pre-spend.
# Run BEFORE every deploy: ./test.sh
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
cd "$(dirname "$0")"

PY=".venv/bin/python3"
[ -x "$PY" ] || PY="python3"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [test.sh] $*"; }

# hand-rolled standalone suites (each exits non-zero on any failure)
for t in test_supplier test_orchestrator test_orchestrator_supplier test_critic test_runner; do
  log "running tests/$t.py"
  PYTHONPATH=src "$PY" "tests/$t.py"
done

log "pytest suite: stitcher (mock-transport, no spend)"
PYTHONPATH=src "$PY" -m pytest tests/test_stitch.py -q

log "CLI dry-run: cost gate must render and ABORT on EOF (never auto-spend)"
# the CLI exits 1 on a non-ok manifest (the abort IS the expected outcome) -> || true
out="$(PYTHONPATH=src "$PY" -m runtime.orchestrator "smoke brief" \
       --image-url https://res.cloudinary.com/demo/seed.png < /dev/null 2>&1 || true)"
echo "$out" | grep -q 'COST GATE' || { log "FAIL: cost gate text missing"; exit 1; }
echo "$out" | grep -q '"status": "aborted"' || { log "FAIL: unapproved gate did not abort"; exit 1; }
echo "$out" | grep -q '"cost_units": "USD"' || { log "FAIL: gateway backend not default"; exit 1; }

log "ALL PASS"
