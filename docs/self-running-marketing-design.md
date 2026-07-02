# Self-Running Marketing — Design (v0.3, Phase 0)

**Status:** REVISED per two review rounds: interactive cross-family (KilaBz + Oracle, folded in v0.2)
plus two play-review verdicts on the pushed doc (folded here). Ready for re-review.
**Greenlit:** 2026-07-01 by Jefe. **Owner:** Mack.
**v0.3 change:** enqueue narrowed to a no-escalation contract; `mxq schedule` specified as an
ENFORCED human boundary (mechanism + test, not an assumption); OS-level token isolation;
reconciliation-before-requeue on `publish_unknown`; honest threat model.

## What it does & why
An autonomous loop that keeps @MyndAIX's Instagram fed **without Jefe touching the tools** —
*except* the publish trigger, which stays human. MyndAIX marketing itself is both the funnel
*and* a live proof of the product (the AI lab runs its own growth). This is ON the north-star
(autonomous brain applied to our own channel), not the rach.ugc content-farm play.

The generator is fully autonomous. **Publish is a hard human gate — enforced structurally, not by prompt.**
"Self-running" = self-running *up to the queue*; Jefe pulls the trigger with one CLI action.

## Data flow (input → process → output)
1. **Trigger** (cadence) — cron on the Mini fires the loop (e.g. daily). No agent decides *when* beyond the schedule.
2. **Generate** — pick a brand pillar + script formula (hook ≤1.5s → claim → mechanism → soft CTA) → render a candidate via the existing Higgsfield runner. Formats: talking-head (**Speak adapter, PR #44**), plus existing Soul/Cinema + Vibe Motion. Runs in a process that **cannot read the IG token and has no publish path** (see Separation of Duties).
3. **Ingest into `mxq` — via a NARROW enqueue contract.** The generator's only write capability is
   "create one immutable, **unscheduled** candidate": artifact + caption + hashtags, content-addressed
   by hash at insert. The enqueue path (API or DB grant) **cannot set `scheduled_at`/`due`/`status` or
   any other publishable state — ever.** A generator that could enqueue an already-due item would be
   publishing with extra steps; this contract is what makes "no publish capability" true rather than
   asserted (play-review HIGH).
4. **GATE (human)** — Jefe reviews the candidate and runs **`mxq schedule <id> "<when>"`** — the real, existing human gate. This is the *only* transition that authorizes a post. Approval binds to the candidate's **content hash over artifact + caption + hashtags** — any change after review is a different candidate.
5. **Publish** — mxq's existing deterministic publisher claims the scheduled/due item (atomic due-claim **before** the Graph call, never check-then-post) and posts via the Graph API. It is the *only* component able to read the token. It inherits mxq's attempt IDs, `publish_unknown` handling, token-refresh locking, and anti-double-publish.

## Security surface — Separation of Duties is the gate
The gate is **structural, not a prompt.** A prompt-enforced "only publish if approved" is a self-report an LLM can forge. Concretely:

- **Token isolation is OS-level, not organizational** (play-review P1): "the generator doesn't hold
  the token" is insufficient if both run as the same OS user — the agent could just read the file and
  `curl` the Graph API. The publisher runs as a **distinct OS user** (or launchd service account); the
  token file is owned by and readable ONLY by that user (`chmod 600`, different owner). The generator
  agent's user has zero read path to it.
- **`mxq schedule` is an ENFORCED boundary, with a mechanism, verified by test** (play-review HIGH):
  the schedule transition is accepted only from a surface the agent user cannot reach — the mxq
  DB/state is writable only by the publisher user, and `mxq schedule` runs under Jefe's own user with
  grants the agent user lacks. "The auto-mode classifier blocks mxq" is **defense-in-depth only** —
  unverified, and a classifier is not a security boundary. Ship with a negative test: an agent-user
  process attempting schedule or enqueue-escalation MUST fail, and the test proves it.
- **`<id>` inputs are hostile** (play-review P1): `mxq schedule <id>` validates the id as a strict UUID
  before any path/query use — ids originate from agent-authored candidates.
- **Threat model — nothing here is "none external"** (play-review P2, correcting v0.2): untrusted
  inputs include Higgsfield render outputs, generated captions/hashtags, staged metadata, Graph API
  responses, and anything arriving on an approval channel. All are data, never instructions; wrapped,
  never eval'd.
- **Auditability:** every publish confirmed via Graph API + mxq ledger, NOT the agent's word
  (this is how the real post count — 3 total, 2 team-era — was established).

## Edge cases & failure modes
- **Agent confabulates approval** → structurally impossible: no token read path, no schedule grant.
- **Generation runaway / spam** → hard caps, not vibes: bounded retries per candidate, a per-run
  candidate cap, and a **daily credit ceiling** on the generation account; breach = stop + alert, never
  flood the queue or burn credits silently.
- **Approval race (TOCTOU)** → candidates are immutable and content-addressed at enqueue; schedule
  binds the hash; publisher re-verifies the hash before the Graph call.
- **`publish_unknown` / failed publish** → **reconcile BEFORE any requeue** (play-review MEDIUM): the
  Graph call may have succeeded with the confirmation lost — re-queueing without a Graph/ledger
  reconciliation is a double-post. Notify Jefe via the jefe-inbox drop; a human re-queues only after
  reconciliation shows the post is absent. No agent auto-fix-and-republish, ever.
- **Double-publish / retry storm** → inherited from mxq (atomic due-claim + attempt IDs), not re-solved here.
- **GC / candidate expiry** → the sweep is **approval-aware** (play-review P2): it never removes a
  candidate that is scheduled or mid-review; missing-file cases degrade to a clean skip + alert.
- **Render fails / times out** → candidate never enqueued; loop logs and skips.
- **Token expiry** → publisher verifies token first (mxq token-refresh locking); on failure stages an alert, does not silently drop.

## Borrows vs builds (anti-over-engineering)
- **BORROW:** mx-engine `mxq` (queue, scheduler-gate, deterministic publisher, all publish hardening), the controller/play-review cadence loop, the jefe-inbox drop, the Higgsfield runner + Speak adapter (PR #44).
- **BUILD (small):** the narrow enqueue contract (unscheduled-immutable-only), the generator loop that renders + enqueues, generation caps + credit ceiling, the OS-user split for the publisher/token, the agent-cannot-schedule negative test.
- **DO NOT BUILD:** a new orchestrator, an approval UI, an autonomous publisher, a separate publish engine, a scheduling engine, a new "approve" verb, or any path where the generator's OS user can read the token or write publishable state.

## Decisions
- **Gate: existing `mxq schedule <id> "<when>"` — LOCKED**, now with a specified enforcement mechanism
  (OS-user separation + grants + negative test), not an assumption.
- **Enforcement: structural separation of duties — LOCKED**: generator user has no token read path and
  no publishable-state write path; publisher is the sole token holder acting only on human-scheduled items.
- **Enqueue contract: unscheduled-immutable-only — LOCKED** (v0.3): the generator's single write
  capability; anything more re-opens the escalation path.
- **No fallback approval channel — LOCKED** (v0.3): the v0.2 file-drop fallback is dropped. One gate,
  one mechanism; a weaker fallback is just the bypass (play-review P2).

### Still open (tunable, not blocking)
- Cadence: daily vs a few/week?
- Face: MX character vs a "creator" face fronting talking-heads?

## Review record (2026-07-01)
- **Interactive cross-family (v0.1 → v0.2):** KilaBz REVISE (reuse mxq's real `schedule` gate + hardening; don't rebuild) + Oracle REVISE (structural separation of duties; deterministic human-triggered publish; runaway/race/rejection coverage). Both folded in v0.2.
- **play-review on v0.1 push (NEEDS-FIX):** OS-level token isolation; unforgeable approval (hard permission boundary, not classifier); TOCTOU hash-binding; atomic claim before Graph call; id sanitization; fallback-channel parity; generation circuit breaker; approval-aware GC; honest threat model. Folded in v0.3 (hash-binding + atomic claim were already in v0.2; the rest are new here).
- **play-review on v0.2 push (NEEDS-FIX, kilabz-only — oracle leg empty):** narrow enqueue contract (HIGH); enforced schedule boundary + negative test (HIGH); reconciliation before requeue (MEDIUM). All folded in v0.3.

## Ground truth of record (2026-07-01)
Graph API readout: @MyndAIX has 3 posts total — 2 team-era (2026-06-29 reel, 2026-06-30 carousel), both
pushed by Jefe himself, plus the 2025-04 founding image. No unauthorized autonomous publish has occurred;
the earlier mx-engine incident writeup does not match this readout. The gate in this design stands on its
own merits, not on that incident.
