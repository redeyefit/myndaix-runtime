# S7 proposer — Mini arm runbook (one-time, after the PR merges)

The proposer ships OFF. Arming is deliberate, on the **Mini only** (the autonomy host — its ledger
holds the clean `ready` signal). Each step is reversible; rollback is always `rm $ORCH/PROPOSER_ENABLED`.

## 0. Prereqs (already true when the PR merges)
- The branch is on `main` and the Mini reconcile has CONVERGED past the merge commit
  (check `~/.myndaix/state/reconcile.out` for the new sha).
- `capture_candidate` on the Mini shows the 2 `ready` classes (`toctou-race`, `fail-open`).

## 1. Credentials (the gate-b step — do this FIRST)
1. Mint the **proposer's own fine-grained PAT** (dedicated bot identity — NOT the automerge token):
   repo `redeyefit/myndaix-runtime` only; permissions: Contents (r/w), Pull requests (r/w),
   Metadata (read). Nothing else (no workflow, no admin).
2. Install it on the Mini: paste into `~/.myndaix/.proposer-token`, then `chmod 600` it.
   The tick REFUSES to run live if this file is missing or empty (identity fallback guard).
3. Rotate the shared automerge `r2` (the old exposure): mint a Mini-only `r3` with the same scopes
   as `r2`, replace `~/.myndaix/.automerge-token` on the Mini, verify an automerge tick is healthy,
   then REVOKE `r2` on GitHub and delete the MacBook's `~/.myndaix/.automerge-token.decommissioned`.
   After this, each autonomous git-writer holds its own single-host credential.

## 2. Launchd job — NOTHING TO DO (substrate-managed)
`substrate/plists/ai.myndaix.proposer.json` ships the job; the Mini's reconcile installs + loads it
in its own launchd context on the first converge after merge (hourly at :45 — offset from
controller :00 / automerge :30; `launchctl load` over SSH fails on macOS, which is why this is
reconcile's job, not a hand step). Verify after converge: `launchctl list | grep proposer`.
(`orchestrator/ai.myndaix.proposer.plist.example` remains for a hand-managed/lab install only.)

## 3. Green dry-run BEFORE arming (the ≤15s check)
DRY_RUN deliberately bypasses the `PROPOSER_ENABLED` flag (a dry tick is proven side-effect-free),
so this diagnostic runs while the scheduled live job stays fully disarmed:
```bash
cd ~/code/active/myndaix-runtime
MYNDAIX_PROPOSER_DRY_RUN=1 MYNDAIX_DSN=postgresql://127.0.0.1/runtime \
  PYTHONPATH=src .venv/bin/python -m runtime.proposer tick
```
Expect: `would propose toctou-race → skill/auto/toctou-race for redeyefit/myndaix-runtime` (+
`fail-open`), and NOTHING opened. Any error → stop, do not arm.

## 4. Arm
```bash
touch ~/.myndaix/orchestrator/PROPOSER_ENABLED
```
The next :45 tick opens the first `--draft` PR. Disarm any time: `rm` the flag.

## 5. The human loop (what Jefe does per proposal)
- A draft PR `skill(auto): <tag> — recurring review finding` appears. Read the provenance commits
  in the body/diff, REPLACE the stub body with the real lesson, DELETE the `MDX-AUTOPROPOSED-STUB-
  UNAUTHORED` line, mark ready, merge. The controller indexes it next tick (an unedited stub is
  refused at index — merging without authoring cannot pollute reviews, it just no-ops with an alert).
- Close = declined (backoff applies). Un-acted for 14 days = auto-closed as stale.
