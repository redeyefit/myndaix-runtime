# DESIGN.md — MyndAIX Team Runtime (openclaw replacement)

**Status:** **v0.4 — build-ready.** Stack decided: **Python**. Closes the five gaps from the v0.3 cross-family re-review (Codex + Gemini, 2026-06-22). This is the last design pass before scaffolding — past here, the build surfaces the rest.
**Author:** Mack, with Jefe. Built from the openclaw failure-map of 2026-06-21.

---

## 1. What it does & why
A thin, deterministic orchestrator that **routes a unit of work to an agent (CLI or API), runs it in isolation, captures the result, delivers a reply, and logs every state transition to a durable ledger.** It replaces openclaw, which coupled comms + runtime + agents + config into one Node event loop so any single slow/failed part took down the command center. The agents were never coupled to openclaw — proven 2026-06-21, Codex/Gemini/Claude all answer direct local shell calls with zero openclaw in the path. This keeps the direct calls, deletes the wrapper, and puts a durable state machine where file-IPC used to be.

## 2. Non-negotiable principle
**Contracts deep + rigid; roster/roles/models/transport flexible data behind them.** Every change is ADDITIVE (a registry row), never STRUCTURAL (a patch around the spine).
**The test:** *add an agent / swap a model / change transport without editing the spine?* If no, a contract has a gap — close it first. If you're specifying more than the contracts + a thin roster, stop.

## 3. Failure-map — what NOT to build (openclaw, 2026-06-21)
Comms coupled to execution in one loop → embed froze it → killed Discord (→C3) · one slow op blocks everything (→C4) · file-IPC state corruption (→C2) · **concurrent agents mutating the same repo → silent corruption** (openclaw's single thread accidentally prevented this) (→C5) · behavior in mutable config schema (→keep config minimal) · transport semantics leaking into agent behavior — Discord "group" → lurk → NO_REPLY ghost (→C3+C0) · plaintext secrets (→§7).

---

## 4. The contracts (specified — ~80% of this design)

### C0 — Capability model (the load-bearing axis; `cli|api` alone is insufficient)
Two orthogonal descriptors per agent:
- **reach** (how invoked): `cli` | `api` — drives the adapter + auth/cost.
- **authority** (what it may do): `responder` (prompt→text, no side effects; auto-retry-safe) · `workspace-actor` (reads/writes files, runs commands; gets an isolated worktree C5; **never auto-retried** C4) · `controller` (may emit new dispatches; writes only via Command API 4b) · `composite` (multiple internal calls, declares net authority — e.g. recon).
Authority — not reach — drives retry-safety, isolation, and dispatch rights.

### C1 — Agent invocation
`invoke(agent_id, job) → result`
- **result**: `{ status: ok|error|timeout|killed|needs_human, text, exit_code?, error_class, artifacts?[], cost?, ms }`
- **error_class**: `retryable` (transient) · `terminal` (bad-auth, validation, non-zero-exit on mutation) · `needs_human` (interactive/TTY prompt detected — park, never loop on stdin the headless spine can't answer).
- **adapter** per agent (cli: argv + prompt channel + stdout + exit-code map; api: endpoint + secret-ref + shape). Reach/authority from C0; cost/concurrency/timeout in the registry `profile`.
- **Progress visibility (v1):** while a job runs, the runner emits `heartbeat` + **stdout/stderr chunks** to a side channel (`attempt_log`, append-only, NOT in the hot state machine). Long jobs are never dark; the worker never blocks on it. *(This is the cheap visibility — full structured streaming as a contract is still deferred, §8.)*

### C2 — The ledger (a state machine, not one table)
Postgres. Explicit concepts:
- **inbound_event** `{ id, transport, envelope(jsonb→C3), body, received_at, dedupe_key UNIQUE }`
- **job** `{ id, parent_id?, root_id, depth, created_by, to(agent_id), body, capability_required, priority, status(queued|leased|running|done|failed|dead), created_at, repo_id?, base_ref?, base_sha?, worktree_path?, artifact_ref? }`
- **attempt** `{ id, job_id, worker_id, lease_expires_at, started_at, ended_at, status, result(jsonb), error_class }`
- **outbound** (outbox) `{ id, job_id, transport, reply_target, body, status(pending|sent|failed), provider_msg_id?, tries }`
- **attempt_log** (append-only side channel) `{ id, attempt_id, ts, stream(stdout|stderr|heartbeat), chunk }`
- **dead_letter** — exhausted jobs/outbounds for human triage.
**Leases** → crashed-worker job reclaimed on expiry. **Dedupe** (`inbound.dedupe_key`, `outbound.provider_msg_id`) → exactly-once-ish.
**Job chaining (solves the v0.3 gap):** a child job sets `base_ref` = a prior job's `artifact_ref`, so a worktree is created from the *previous step's output*, not the live tree. Dependent multi-agent work (mack builds → codex debugs) now passes state without auto-merging to live.

### C3 — Comms ↔ execution boundary + transport envelope
- Transport adapters normalize inbound → **transport_envelope** `{ transport, account, sender_id, channel/thread_id, reply_target, provider_msg_id, dedupe_key, formatting_caps(max_len,chunking) }`. Stored on `inbound_event`; `reply_target` flows to `outbound`. **Transport semantics never leak into job/agent fields.**
- Transport is a dumb pipe: calls `ingest_inbound`, reads `outbound[pending]`, delivers, calls `mark_outbound_sent`. Never invokes an agent; never blocks on agent work.
- Worker pool: `lease_job`, (workspace-actor → assign worktree C5), `invoke()`, `complete_attempt`/`fail_attempt`, `enqueue_outbound`.

### C4 — Failure semantics (a state machine)
- **Lease + heartbeat**: lease expiry (crash) → job reclaimed.
- **Retry is authority-gated** (effect boundary): `responder`/read-only → auto-retry `retryable` up to N w/ backoff. **`workspace-actor` → NEVER auto-retry** (a half-applied git/sed isn't idempotent) → on failure `dead_letter`. `terminal` → no retry. `needs_human` → park.
- **timeout** → SIGTERM the **process group** → `killed`.
- **Admission limits (v1-essential, anti-runaway):** every `submit_job` from a controller is checked against `max_depth`, `max_children_per_job`, `cost_budget(root)`, `chain_ttl` — exceed → rejected, dead-lettered. Stops a controller spawning an infinite job tree.
- **circuit-breaker** per agent: simple open/closed (M failures/window → open → fail-fast → cooldown). Half-open deferred.
- **api agents**: cost/rate budget; exhaustion → `terminal`.

### C5 — Workspace isolation + runner enforcement
- Any `workspace-actor` job runs in an **ephemeral git worktree** from `base_ref`@`base_sha` (live ref, or a prior job's `artifact_ref` for chaining). Mutates **only** its worktree.
- **Success** → diff captured as `artifact_ref` (branch/patch), surfaced for review — **never auto-merged**. **Failure** → worktree preserved then GC'd; live repo never touched mid-flight. **Merge is a deliberate, serialized step**, never a race.
- **The runner — not the worktree — is the boundary.** The runner enforces: `cwd`=worktree, scrubbed `env` (only declared secret-refs), no writes outside the worktree (open-base sandbox where available), and process-group kill. *(A bare worktree is not a security boundary; the runner makes it one.)*

## 4b. Interfaces, orchestration & the Command API
- **Spine headless; interfaces are clients.** Deterministic dispatch (route `to(agent_id)`, lease, retry, log) = spine. Judgment (decompose a fuzzy goal) = a `controller` agent. The spine does **no** judgment routing — a fuzzy request becomes a job for `lobster`/`mack`, which decomposes and emits child dispatches **via the Command API**.
- **The Command API is the SOLE writer to the ledger.** Transports, workers, controllers, interfaces — all go through these verbs; **nobody writes raw tables.** Each verb is a single transaction.

**Command-API state-transition table:**

| verb | allowed caller | pre-state | post-state | idempotency key | retry rule |
|---|---|---|---|---|---|
| `ingest_inbound` | transport | — | inbound_event created | `dedupe_key` | safe |
| `submit_job` | controller, interface | (admission check) | job `queued` | `(parent_id, intent_hash)` | safe |
| `lease_job` | worker | job `queued` | job `leased`, attempt opened, `lease_expires_at` set | `(job_id, worker_id)` | safe |
| `heartbeat_attempt` | worker | attempt open | `lease_expires_at` extended | — | safe |
| `complete_attempt` | worker | attempt open | attempt `ok`, job `done` | `attempt_id` | safe |
| `fail_attempt` | worker | attempt open | attempt failed; job `queued` (retry, if authority-safe) or `failed`/`dead` | `attempt_id` | safe |
| `enqueue_outbound` | worker | job `done` | outbound `pending` | `job_id` | safe |
| `claim_outbound` | transport | outbound `pending` | outbound `leased` | `(outbound_id, transport)` | safe |
| `mark_outbound_sent` | transport | outbound `leased` | outbound `sent`, `provider_msg_id` set | `provider_msg_id` | safe |
| `mark_outbound_failed` | transport | outbound `leased` | outbound `pending`/`failed` (tries++) | `outbound_id` | safe |
| `reclaim_expired` | spine janitor | lease expired | job `queued` or `dead` | lease epoch | safe |
| `dead_letter` | spine | any | dead_letter row | source id | safe |
| `cancel` | controller, interface | job not-terminal | job `failed`; running attempt killed | `job_id` | safe |
| `get_status` | any | — | (read) | — | — |

Interfaces: Terminal (Claude Code / thin CLI) = primary v1 (works today). Chat pipe = dumb pipe for mobile. GUI = later read-view, not v1.

## 5. v1 roster (data — `authority` drives behavior)
| agent | reach | authority | model | role |
|---|---|---|---|---|
| lobster | cli | controller | opus | orchestration / judgment |
| mack | cli | workspace-actor | opus | hands-on builder |
| mini | cli | workspace-actor | claude | pipeline builder |
| kilabz | cli | responder | gpt-5.5 | code reviewer (read-only) |
| codex | cli | workspace-actor | gpt-5.5 | builder / debugger |
| oracle | cli | responder | gemini-3.1-pro | reviewer / vision |
| recon | api+cli | composite (read-only) | hybrid | research |
Antman retired; Harley inactive.

## 6. Data flow
`inbound → transport → ingest_inbound → submit_job → job[queued] → lease_job → (worktree) → invoke (heartbeat+log) → complete/fail_attempt → enqueue_outbound → claim_outbound → deliver → mark_outbound_sent`. Controllers emit child jobs via `submit_job`. Every transition is one Command-API transaction.

## 7. Security surface
Untrusted = inbound body; wrap before any prompt; **never auto-exec agent output as shell** (controller output → Command-API verbs only). Secrets in a store/refs, NOT plaintext config — **rotate the keys leaked 2026-06-21.** Per-agent isolated creds. Runner enforcement (C5) is the workspace security boundary.

## 8. Scope guard
**DEFERRED to v2** (resisting these *is* the inversion guard): full structured streaming (v1 has heartbeat+log chunks) · multimodal/large artifacts beyond diffs · half-open circuit-breaker · backpressure/fairness scheduling · auto-merge of worktrees · GUI.
**BANNED (openclaw sins):** plugin system · eval harness · bundled-dep staging · config schema beyond registry+secrets · embedded event loop.

## 9. Decisions
1. Scope — **internal-first, built clean to release.**
2. Transport — **terminal primary + optional chat pipe; no v1 GUI.**
3. **Stack — Python** (FastAPI Command API · Pydantic contracts · asyncpg ledger · Alembic migrations · asyncio subprocess + `start_new_session`/`killpg` for process-group kill · `pexpect` for stubborn CLIs). Node rejected (it's what failed). Go is the fallback only if packaging/runtime-drift becomes the actual pain.

## Changelog
**v0.4** — Command-API state-transition table + sole-writer enforced · job-chaining (`base_ref`/`artifact_ref`) + workspace fields · admission limits (anti-runaway) · progress visibility (heartbeat + attempt_log) · C5 runner-enforcement security boundary · stack=Python. **Build-ready.**
**v0.3** — C0 capability model · C5 worktree isolation · Command API · multi-table ledger · authority-gated retry · transport envelope.
**v0.2** — swarm-as-adapter · §4b interfaces · no GUI. **v0.1** — initial four-contract sketch.

## Next
Scaffold the fresh **Python** repo (`myndaix-runtime`) — contracts as Pydantic, ledger as SQL/Alembic, Command API as the verb interface — then implement worker + runner + one transport. Let the build surface the rest.
