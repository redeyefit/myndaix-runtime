# DESIGN.md — Phase 2 (autonomous fix) + parallel per-repo concurrency

**Status:** **v0.1 — design for review.** Research-backed (5-agent prior-art survey, web-cited). Verdict: **BUILD on the existing spine · BORROW ~6 named patterns · ADOPT nothing** (Aider-headless = one optional swappable engine).
**Author:** Steven Fernandez — designed with the AI pair-engineering team.

---

## 1. What it does & why
Two features, both **build-on-spine** (the prior-art survey found the runtime already implements the correctness-core that River / Oban / pg-boss / Graphile are famous for — `SKIP LOCKED`, leases+heartbeats, no-double-lease, authority-gated no-retry, worktree isolation; adopting one = rewrite + lose the AI refinements):
- **A. Parallel per-repo concurrency** — FieldVision / Higgsfield / myndaix reviews+builds run concurrently, capped per repo; scale the worker pool.
- **B. Autonomous fix stage (Phase 2)** — codex applies fixes in an isolated worktree, diffs back, **never auto-merges**; a bounded `localize → fix → verify`.

## 2. Non-negotiable principle
Additive where possible. Concurrency is a **contained edit to the existing `lease_job`** (not a new system); the fix stage is a **roster entry + a job chain** over the *existing* `parent_id`/`base_ref`/`artifact_ref`. **No new durable infra, no queue engine, no DAG DSL, no agent framework.** Caveat: the concurrency change touches the spine's **most correctness-critical code** (the no-double-lease CTE) — small in lines, high in stakes → mandatory adversarial review + concurrency stress tests.

## 3. What we deliberately do NOT build (research bloat list)
Temporal (cluster + replay-determinism — wrong for non-deterministic agents) · Oban Pro Smart Engine (distributed-fleet) · Redis reliability bolt-ons (Postgres gives crash-recovery free) · LangGraph/CrewAI/AutoGen **as the spine** (re-couples comms+execution — the openclaw sin) · hosted SaaS reviewers (PR-centric, ship code to a 3rd party — kills local-first) · Docker-per-task (a worktree suffices) · RAG/vector/codegraph (no corpus, solo) · DAG DSL (`parent_id` *is* the DAG). Borrow only Temporal's **vocabulary** (idempotent activity / durable plan).

## 4. Part A — Parallel per-repo concurrency
- **Per-repo cap, enforced at DEQUEUE** (Oban-Pro lesson; River/Graphile pattern): `repo_id` is already stamped on the job row at insert. In the existing `lease_job` SKIP-LOCKED CTE, **exclude candidate rows whose `repo_id` already has ≥ `MAX_PER_REPO` open attempts** + a partial index `job(repo_id) WHERE status IN ('leased','running')` so the count is cheap. ~20 lines, no new tables. → FieldVision/Higgsfield/myndaix run in parallel; no single repo hogs the pool.
- **Per-lane isolation** (Oban free tier): give **responder reviews** (fast, retry-safe) and **workspace-actor builds** (slow, never-auto-retried) independent caps — a hung codex fix must not starve all kilabz reviews. A `lane` column (or reuse `authority`) + per-lane cap in `lease_job`.
- **Scale the pool**: `serve --size 8` (config + `launchctl kickstart -k`). **Keep `heartbeat_interval ≤ lease/2`** (already enforced) — more workers makes a falsely-reclaimed long agent run *more* likely, so do NOT relax it.
- **Per-repo, not per-tip** (research-confirmed): per-repo across 3 repos is genuinely independent (separate worktrees, no shared state) = clean fan-out. **Never fan out within one repo's edit.** The bash orchestrator's global lock becomes **per-repo** (one play per repo; repos parallel).
- **LISTEN/NOTIFY (deferred perf borrow)** — Graphile pattern: Command API `NOTIFY`s on submit; idle workers `LISTEN` and wake on demand, killing the 20ms busy-poll. **Wake-up HINT only** — the SKIP-LOCKED lease + a fallback poll stay authoritative; a missed NOTIFY must never break dispatch.

## 5. Part B — Autonomous fix stage (Phase 2)
- **Shape: a bounded job CHAIN** — `review(responder) → fix(workspace-actor; worktree; base_ref = reviewed SHA) → verify(responder/runner)`. The existing `parent_id`/`base_ref`/`artifact_ref` **is** the durable plan; routing lives in **bash/SQL inspecting the ledger, never in an LLM**.
- **The two scariest invariants are FREE** (already enforced in `fail_attempt` + `reclaim_expired`): **never auto-retry a half-applied fix** (retryable + workspace-actor → dead+dead_letter) and **never auto-merge** (`capture_diff` → `artifact_ref`, human-gated). The fix stage inherits both.
- **The fix pipeline = Agentless-style, NO agentic inner loop** (~$0.70/fix, *beats* agent frameworks on SWE-bench): `localize` (cheap file→function) → `sample-N` candidate patches (parallelize across the pool) → `verify` → pick the one that passes. Bounded by sample-N + **one retry with self-review disabled** (Sweep). No unbounded loop.
- **Borrows:** architect/editor split (strong model plans, cheap model applies a search/replace diff — halves cost, natural plan gate) · **dual test gate** (the repro test now passes AND the repo suite still passes) · **read-only test files** in the worktree (agent can't fake green by editing the test) · **budget + iteration caps in the controller, not the agent** · no-progress detection (diff unchanged → abort).
- **Trigger (v1 = human-gated)** — the fix runs only on an **explicit trigger** (a Command-API verb / "fix tip X"), returns a **diff for human-gated merge**. Auto-on-NEEDS-FIX is a later config flag; the full auto-loop is deferred. *Climb the rungs.*
- **Engine = codex** (`codex exec`, workspace-actor). **Aider-headless** (`aider --message --yes-always --no-auto-commits`) is a swappable second engine behind `invoke_cli` — A/B only if codex quality disappoints. Not the architecture.
- **Promotion (merge) is a separate human/gated Command-API action** — never triggered by agent stdout. `lobster` is the dedup/filter **judge** before the fixer (don't feed raw multi-model findings in).

## 6. Two free wins the research found (real gaps, not theoretical)
1. **Bounded reclaim (poison-pill):** `reclaim_expired` currently requeues retryable jobs with **NO cap** — a crash-loop hole. Add `reclaim_count → dead_letter at N` (River JobRescuer / Sidekiq pattern).
2. **Fixed hook install path:** re-exec the pre-push hook from a **fixed path outside the repo** (not the worktree copy) — closes the v0 trust-surface debt (flagged in v0 review *and* independently here).

## 7. Security surface
Escalation-to-code-execution is the headline risk (codex *writes+runs* in a worktree): the fixer's **objective is static** (from the play); the review enters only as **nonce-fenced untrusted DATA**; **test files read-only**; worktree isolation + diff-only + **human merge gate** is the hard backstop (the fence is soft). Never let agent/LLM stdout auto-trigger a merge or shell. Secrets unchanged (worker env, chmod 600).

## 8. Scope guard
**v1:** per-repo cap + per-lane isolation + pool→8 · reclaim cap · fixed hook path · fix stage = bounded `localize→fix→verify`, human-triggered, diff-back.
**Deferred:** per-tip concurrency · auto-on-NEEDS-FIX · full auto-loop · Aider-swap · LISTEN/NOTIFY · risk-tier fan-out · architect/editor split.
**Out of scope (research bloat):** any queue/agent engine, DAG DSL, Docker-per-task, RAG/vector, hosted SaaS reviewer.

## 9. Build order (each: build → review → test → ship; spine changes get a stress test)
1. **Concurrency** — per-repo cap in `lease_job` + index + per-lane + pool→8. **Mandatory concurrency stress harness** (N parallel plays → assert no double-lease / counter race / starvation). Highest-bug-density work; stress test is the gate.
2. **Bounded reclaim** (spine, small).
3. **Fixed hook path** (orchestrator, small).
4. **Fix stage** — roster fixer + the bounded chain + dual verify gate + diff-back. Test against a known-buggy fixture repo (stubbed engine + one real codex run).

## How it was researched
5-agent prior-art workflow (Postgres queues · coding/fix agents · multi-agent frameworks · review bots → synthesis), live-web-cited. Best sources mined: River/Oban/pg-boss/Graphile (queue patterns), **Agentless** + SWE-agent + Aider + Ellipsis (fix loop), Anthropic multi-agent + Cloudflare AI-code-review (architecture mirrors).

## Changelog
**v0.1** — initial research-backed design; per-repo concurrency + bounded fix chain; build-on-spine, adopt-nothing.
