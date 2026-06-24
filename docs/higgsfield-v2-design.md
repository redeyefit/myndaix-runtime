# DESIGN: Higgsfield runner v2 — premium models + idempotent-resume

**Status:** DRAFT for Jefe's review. Built autonomously overnight 2026-06-23 on
`feature/higgsfield-v2`. **Slice A (premium models) is safe/additive and ready to merge.
Slice B (idempotent-resume) changes the LOCKED §5-A contract — gate it behind a design
review before merge.**
**Author:** Mack. Grounded in the live-verified API facts in
`~/research/2026-06-23-higgsfield-cloud-api-image-to-video.md` (NOT guessed).
**Builds on:** v1 (`docs/higgsfield-runner-design.md`, §5-A fail-closed-after-submit).

---

## Slice A — premium models as roster rows (additive, no contract change)

### What & why
v1 pinned the roster to `dop/lite` (cheapest). v2 adds premium image→video models
(Kling 2.1 Pro, Seedance 1.0 Pro) and `dop/standard` as **pure `AgentSpec` rows** — the
"adding an agent is a new row, never a spine edit" thesis. The runner is already generic
on `adapter["application"]`, so a model whose body is `{prompt, image_url}` needs *zero*
code. The only real gap:

### The one code change: adapter/job request-params merge
Some models take extra body fields the v1 runner can't send (it hardcodes
`{"prompt", "image_url"}`):
- `dop/standard` → `{image_url, prompt, duration}` (documented)
- image models → `{prompt, aspect_ratio, resolution}` (future)

So the runner gains an **optional params merge** into the submit body, from two sources:
1. `adapter["params"]` — static per-model defaults (e.g. `{"duration": 5}`).
2. `job.context["params"]` — per-job overrides (caller-supplied).
Merge order: `{prompt, image_url}` ← `adapter.params` ← `job.context.params`
(job wins). Reserved keys (`prompt`, `image_url`) cannot be clobbered by params — they're
set last from their canonical sources. Params must be a flat dict of JSON scalars; a
non-dict or non-scalar value is dropped (never crashes the submit — same defensive posture
as the v1 knob coercion).

### Verified roster rows added (paths CONFIRMED, not inferred)
| agent_id | application | body | ~cost |
|---|---|---|---|
| `higgsfield` (v1) | `/higgsfield-ai/dop/lite` | `{prompt,image_url}` | ~$0.13 |
| `higgsfield-dop-std` | `/higgsfield-ai/dop/standard` | `+{duration}` | ~$0.13 |
| `higgsfield-kling` | `/kling-video/v2.1/pro/image-to-video` | `{prompt,image_url}` | ~$0.40 |
| `higgsfield-seedance` | `/bytedance/seedance/v1/pro/image-to-video` | `{prompt,image_url}` | premium |

`cost_budget` raised per tier. **NOT added** (would be guessing): Veo 3.1, Kling 3.0,
Seedance 2.0, WAN 2.5 — their exact `model_id` paths are not in public docs (LOW confidence
inferences only). Listed as commented candidates needing gallery verification.

### Test plan (offline, mocked transport)
- params merge: adapter-only, job-only, both (job wins), reserved keys protected, junk dropped.
- a premium roster row submits to its `application` path with the merged body.
- all v1 tests stay green (no regression).

---

## Slice B — idempotent-resume (changes §5-A; GATE before merge)

### The v1 contract we're changing
§5-A (LOCKED): once submit returns a `request_id` the job is **charged**, so EVERY later
failure is **TERMINAL** (never retried) — a structural guarantee against re-submit/
double-charge. Cost: a transient blip during a long render wastes that one generation.

### The v2 goal
A post-submit RETRYABLE failure should **resume polling the same `request_id`** on the next
attempt instead of re-submitting. Safe *only if* the `request_id` is durably persisted
**before** the worker can re-queue.

### Why `job.context` is the seam
On a RETRYABLE responder failure the ledger sets `job.status='queued'`; the job is re-leased
and `get_attempt_job` rebuilds the `Job` **including `context` (jsonb)**. So `context` is the
durable, job-scoped state that survives across attempts. The runner only *reads* it today;
v2 adds a **write-back**.

### Two re-queue paths — both must be safe
1. **Runner returned RETRYABLE** (poll blip → `fail_attempt` re-queues). Runner is alive,
   holds `request_id`.
2. **Worker crashed mid-poll** (lease expires → `reclaim_expired` re-queues). Runner is dead.

For (2) to be safe, `request_id` must be persisted the instant submit returns — a
**mid-execution ledger write**, before any polling. That's the new seam.

### Mechanism
1. **New Command-API verb** `record_job_context(job_id, delta: dict)`:
   `UPDATE job SET context = context || $delta WHERE id=$1 AND status IN ('leased','running')`
   (Postgres jsonb merge; sqlite read-merge-write). One tx, status-guarded, idempotent.
2. **Worker** passes a narrow async callback `persist(delta)` into the runner (only for the
   higgsfield path; threaded as an optional kwarg — the generic runner signature is untouched
   for everyone else). On lease-lost the callback no-ops (status guard returns 0 rows).
3. **Runner, post-submit:** immediately `await persist({"_hf_resume": {request_id, status_url,
   cancel_url}})` BEFORE the poll loop. Then post-submit failures become **RETRYABLE** again.
4. **Runner, on entry:** if `job.context["_hf_resume"].request_id` exists → **skip submit**,
   re-pin status/cancel URLs (origin-checked, same as v1), resume the poll loop.

### Safety analysis
- **No double-charge:** submit happens iff no `_hf_resume` present. Persist is status-guarded
  and happens before polling, so a crash anywhere after submit leaves a resumable token.
  Worst case the persist itself fails → fall back to v1 behavior (TERMINAL, no resume) — never
  a re-submit. (Persist-failure ⇒ fail-closed, exactly like v1.)
- **`resumable` must reflect a CONFIRMED write, not just "didn't raise"** (adversarial-review
  P0, fixed): `record_job_context` returns True only when a row was actually written (Postgres
  `RETURNING id`; sqlite the guarded SELECT hit). A status-guarded no-op (the lease was lost, so
  the merge matched 0 rows) returns **False** → `resumable=False` → post-submit failures stay
  TERMINAL. If False read as "persisted", a lost-lease requeue would re-enter submit and
  double-charge — the bool is therefore load-bearing, not cosmetic.
- **Residual window (documented limitation, accepted for v2):** the only irreducible gap is a
  worker crash in the milliseconds *between* submit returning (charged) and the persist UPDATE
  committing — the token isn't written, so a reclaim requeues with no token and the next attempt
  re-submits. It cannot be fully closed (the external submit can't share a txn with the ledger
  write). Mitigation: persist fires immediately after submit, before any polling; the 120 s lease
  means `reclaim_expired` can't fire during that window unless the worker dies in exactly those
  ms; in the only path with a janitor (the pool) the heartbeat keeps the lease alive. Same risk
  class as v1's ambiguous-submit-failure. If unacceptable, the next step is a pre-submit
  "intent" row, but that's out of v2 scope.
- **Cancel on giving up:** resume-budget exhaustion best-effort cancels the still-queued job
  (cost hygiene), matching the non-resumable timeout path.
- **Resume-loop bound:** a resumed attempt that times out re-queues and resumes — cheap (no
  re-charge), polling the same id until Higgsfield reaches a terminal status. Add a
  `_hf_resume.attempts` counter incremented on each persist; past `RESUME_MAX` (e.g. 20) →
  TERMINAL, so an externally-stuck `in_progress` can't loop forever.
- **Terminal stays terminal:** `failed`/`nsfw`/unknown-status/bad-payload on a *resumed* poll
  are still TERMINAL (Higgsfield refunds failed/nsfw) — resume only re-tries *transient* blips.
- **CancelledError** still propagates (BaseException), unchanged from v1.

### Files touched
`command_api.py` (Protocol verb), `postgres_store.py` + `sqlite_store.py` (impl),
`worker.py` (build + thread the callback for higgsfield jobs), `runner.py` (resume branch +
post-submit persist + RETRYABLE post-submit), tests.

### Test plan (offline)
- resume entry: `context._hf_resume` present → no submit POST, polls the pinned status_url.
- post-submit blip now RETRYABLE (not TERMINAL) once persist is wired.
- persist called exactly once, right after submit, with the request_id.
- persist failure → TERMINAL fall-back (no re-submit).
- resume attempts counter → TERMINAL past RESUME_MAX.
- `record_job_context` merges (both stores), status-guarded no-op when not leased.

### Open question for Jefe
Slice B is correct but it's a real change to the system's most-reviewed invariant. Options:
- **(i)** Merge Slice A now; hold Slice B for a design review (Oracle + KilaBz) + your gate.
- **(ii)** Ship both behind the review.
Recommend **(i)** — A is pure upside with no contract risk.
