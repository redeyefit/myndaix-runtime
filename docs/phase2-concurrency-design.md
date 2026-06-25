# DESIGN.md — Phase 2 (autonomous fix) + parallel per-repo concurrency

**Status:** **v0.4 — design for review (§4 SQL rewritten).** Three-family reviewed: in-session panel (3 blockers) → codex "ship-with-changes" (5, folded in v0.3) → **Oracle/agy "rethink" (4 new blockers, folded here)**. All three converged on the cap-race headline; Oracle's new findings had near-zero overlap. Research-backed: BUILD on spine · BORROW patterns · ADOPT nothing.
**Author:** Steven Fernandez — designed with the AI pair-engineering team.

> **The §4 rethink (Oracle O2):** an advisory-lock-in-a-loop does NOT compose with `SKIP LOCKED` — a worker keeps re-selecting a hot capped repo's rows, fails the recount, retries → 100% CPU while a cold repo starves. v0.4 replaces it with a **`repo_concurrency` counter row joined + locked inside the lease CTE**, so capped repos are *naturally skipped* (no spin, no starvation, race-free). One tiny int per repo — not "new durable infra" in any meaningful sense.

---

## 1. What it does & why
- **A. Parallel per-repo concurrency** — FieldVision/Higgsfield/myndaix run concurrently, **capped per repo**; scale the pool. Per-repo (not per-tip).
- **B. Autonomous fix stage** — codex applies a fix in an isolated worktree, **diffs back, never auto-merges**; **one bounded attempt** in v1, human-triggered.

## 2. Non-negotiable principle
Additive where possible — the per-repo cap is the one true SPINE edit (the no-double-lease lease path) + one tiny counter table: its own PR, gated by a stress test, highest-risk change in the system.

## 3. Prerequisites — MUST land before the feature
- **P1 — Plumb `repo_id` (+ exact reviewed SHA as `base_ref`)** through `cli.py`/`mxr` → `submit_job`; `play-review.sh` stamps the repo. **`repo_id IS NULL` ⇒ cap-EXEMPT.**
- **P2 — Sandbox (best-effort) + env-scrub + network policy for the codex fix actor.** Explicit `env=` whitelisting `PATH`/`HOME`, dropping every unneeded `secret_ref`; restrict network egress. **macOS `sandbox-exec` is weak — don't over-trust it;** for v1 the **human merge gate** is the real backstop (Docker/chroot deferred to when auto-fix is flipped).

## 4. Part A — per-repo concurrency via a counter row (v0.4, race + starvation free)
- **New table `repo_concurrency(repo_id text PRIMARY KEY, active int NOT NULL DEFAULT 0)`** — one row per repo. This is the serialization + cap point.
- **Lease (only `WHERE repo_id IS NOT NULL`** — O1: `pg_advisory_xact_lock(NULL)` *throws*, and `'repo:'||NULL → NULL`; legacy NULL jobs must bypass all per-repo logic and lease normally): join the candidate job to its `repo_concurrency` row, `FOR UPDATE` that row, require `active < MAX_PER_REPO`; on lease `UPDATE repo_concurrency SET active = active + 1`. **Capped repos are naturally excluded by the WHERE** → the worker leases a *cold* repo's job instead of spinning on a hot one. No advisory lock, no candidate-iteration loop.
- **Decrement on EVERY terminal transition** — complete / fail / dead / **reclaim** — `active = active - 1` (guarded ≥ 0, exactly once per leased attempt). Reclaim freeing the slot is what prevents stale-lease starvation.
- **Self-healing reconciliation:** the janitor periodically re-derives `active` from the count of **non-expired** leased attempts per repo and corrects drift (covers a crash between increment and a missed decrement). The counter is the fast cap-decision; reconciliation keeps it honest.
- **Indexes DAY-1** (Oracle: not "EXPLAIN first" — 8 pollers → seq-scan contention): ship the queued-jobs index + `repo_concurrency` PK from the start.
- **Lease timeout → 300–600s** (Oracle): at pool=8 with CPU-saturating agent CLIs, 120s/30s risks delayed heartbeat → false reclaim → **double execution of a workspace-actor** (double-applied fix — the scariest outcome). Widen the margin; keep `heartbeat ≤ lease/2`.
- **git `.git/index.lock` backoff** (Oracle): concurrent worktree git ops on one repo contend on the index lock → wrap git ops in retry/backoff.
- **Per-repo, not per-tip.** Bash orchestrator lock → per-repo. **CUT from v1: per-lane** (when it returns, a dedicated `lane` column, never reuse `authority`).
- **STRESS HARNESS = build gate; assert ALL of:** no cap violation per repo under parallel hammer; cold repos lease while a hot repo floods (now structural, still assert); reclaim decrements `active` (slot freed); long heartbeat-protected jobs NOT falsely reclaimed (with the bigger lease); counter doesn't drift (reconciliation heals); planner uses the indexes (`EXPLAIN ANALYZE`).

## 5. Part B — fix stage (v1 = ONE bounded attempt)
- **v1 = a single codex attempt → verify → diff-back.** CUT from v1 (prove the single attempt first): sample-N, architect/editor split, Aider A/B, LISTEN/NOTIFY.
- **Bounded chain** over existing `parent_id`/`base_ref`/`artifact_ref`; pass the exact reviewed SHA as `base_ref`. N/retry-depth are bash-controller constants asserted vs `MAX_CHILDREN`/`MAX_DEPTH`.
- **Inherited-free invariants — only while every mutating/verifying stage stays `authority=WORKSPACE_ACTOR` (pin it):** never auto-retry a mutation, never auto-merge.
- **Verify = a SEPARATE responder job from a CLEAN `base_ref` checkout** — never the fix worktree's harness ("read-only test files" is far too narrow: runners, package scripts, mocks, lockfiles, snapshots, CI config all fake green). Run the *original* suite from the reviewed SHA.
- **Trigger v1 = human-gated.** auto-on-NEEDS-FIX BLOCKED until clean-checkout verify (and Docker sandbox) are real. Full auto-loop deferred.
- **Fence the fixer:** static objective; fix-list + localize file-list as nonce-fenced DATA, **never a shell arg / path** to git/codex. **Path-traversal:** resolve + assert every agent file write is inside the worktree (Oracle).

## 6. Three spine fixes (build FIRST — low-risk hardening)
- **Bounded reclaim (O3 fix):** count **ALL attempts** for the job (`count(*) FROM attempt WHERE job_id=…`), **not just `status='failed'`** — an instant crash leaves the attempt `leased→expired`, never `failed`, so a failed-only count never trips and reclaim pins all workers forever. ≥ N → `dead_letter`. Apply at BOTH requeue paths (`reclaim_expired` + `fail_attempt`).
- **Worktree GC (O4):** remove the worktree on every terminal transition + a janitor sweep of orphaned worktree dirs (not tied to a live job). A disk-full kills the whole pool.
- **Fixed hook path:** re-exec the pre-push hook from a fixed path outside the repo. **Its own trivial PR (~10 lines), hardening-first** — orthogonal but cheap; don't bundle.

## 7. Security surface
**codex containment (P2)** — env-scrub + network-egress restriction + best-effort seatbelt; macOS `sandbox-exec` is weak so the **human merge gate is the v1 backstop** (Docker deferred). **Verify from a clean `base_ref` checkout.** **Validate the produced diff** — reject symlink / path-traversal / permission-bit tricks before surfacing; resolve+assert agent writes inside the worktree. **Rigorous worktree reset/isolation** between jobs. Escalation-to-code-execution: static objective, nonce-fenced DATA, file-list never argv. Never let agent stdout auto-trigger merge or shell. `lobster` = dedup/judge gate.

## 8. Scope guard
**v1:** P1 · P2 · per-repo cap (counter-row join, NULL-guarded, expired-aware, self-healing) · bounded reclaim (count-all, both paths) · worktree GC · fixed hook (own PR) · lease→5–10min · indexes day-1 · git backoff · fix stage = ONE attempt + clean-checkout verify + diff-back, human-gated · pool→8 (separate, after cap proven).
**Deferred:** Docker sandbox (until auto-fix) · per-lane · sample-N · architect/editor · LISTEN/NOTIFY · auto-on-NEEDS-FIX · full loop · Aider-swap.
**Out of scope:** queue/agent engine, DAG DSL, RAG/vector, hosted SaaS reviewer.

## 9. Build order
0. **Prereqs:** P1 plumbing · P2 env-scrub/net.
1. **Hardening (low-risk, parallel-safe):** fixed hook (own PR) · bounded reclaim (count-all, both paths) · worktree GC.
2. **Per-repo cap** — its OWN PR: `repo_concurrency` counter-row join (NULL-guarded, decrement-on-all-terminals, self-healing janitor) + day-1 indexes + lease→5–10min + git backoff, at **current pool size**, behind the **full stress harness**. *The 🔴 part — re-reviewed clean before code.*
3. **Pool → 8** — separately, only after the cap is proven.
4. **Fix stage** — one attempt + clean-checkout verify + diff-back, env-scrubbed codex, against a known-buggy fixture repo.

## How it was reviewed
v0.1 → in-session panel (3 blockers) → v0.2. → codex "ship-with-changes" (5) → v0.3. → **Oracle/agy "rethink" (4 new blockers: O1 NULL-lock crash, O2 advisory-loop spin/starve, O3 poison-pill counts only `failed`, O4 worktree leak)** + refinements (index day-1, lease→5–10min, git backoff, sandbox-exec is weak, path-traversal) → **v0.4**, with §4 SQL moved advisory-lock-loop → counter-row-join. Three families, near-zero overlap, all converged on the cap race. **Next: re-run Oracle+codex on the rewritten §4 only, confirm clean, then exit Phase 0 → PLAN.**

## Changelog
**v0.4** — Oracle rethink folded: §4 advisory-lock-loop → **`repo_concurrency` counter-row join** (O2, no spin/starve); **NULL-guard** the per-repo path (O1, `advisory_lock(NULL)` throws); reclaim cap counts **ALL attempts** not just `failed` (O3); **worktree GC** on terminal + janitor sweep (O4); lease→5–10min (false-reclaim/double-exec at pool=8); partial indexes day-1; git index-lock backoff; sandbox-exec marked weak (Docker deferred, human gate is v1 backstop); path-traversal resolve+assert. Kept (vs Oracle): fixed-hook as its own hardening PR; Docker out of v1.
**v0.3** — codex changes (try-advisory-lock + CAS, exclude expired, expanded stress, build reorder, v1 fix → one attempt, clean-checkout verify).
**v0.2** — per-repo advisory lock (was TOCTOU race); P1/P2 prerequisites; per-lane cut; reclaim both paths.
**v0.1** — initial research-backed design.
