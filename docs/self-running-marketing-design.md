# Self-Running Marketing — Design (v0.2, Phase 0)

**Status:** REVISED per cross-family review (KilaBz REVISE + Oracle REVISE, both folded). Ready for Plan phase.
**Greenlit:** 2026-07-01 by Jefe. **Owner:** Mack.
**v0.2 change:** structural separation-of-duties gate; reuse mx-engine `mxq` publish spine instead of building glue.

## What it does & why
An autonomous loop that keeps @MyndAIX's Instagram fed **without Jefe touching the tools** —
*except* the publish trigger, which stays human. MyndAIX marketing itself is both the funnel
*and* a live proof of the product (the AI lab runs its own growth). This is ON the north-star
(autonomous brain applied to our own channel), not the rach.ugc content-farm play.

The generator is fully autonomous. **Publish is a hard human gate — enforced structurally, not by prompt.**
"Self-running" = self-running *up to the queue*; Jefe pulls the trigger with one CLI action.

## Data flow (input → process → output)
1. **Trigger** (cadence) — cron on the Mini fires the loop (e.g. daily). No agent decides *when* beyond the schedule.
2. **Generate** — pick a brand pillar + script formula (hook ≤1.5s → claim → mechanism → soft CTA) → render a candidate via the existing Higgsfield runner. Formats: talking-head (**new Speak/Avatar adapter**), plus existing Soul/Cinema + Vibe Motion. Runs in a process that **holds no IG token and no publish capability** (see Separation of Duties).
3. **Ingest into `mxq`** — the rendered artifact + proposed caption + hashtags are enqueued into the existing mx-engine `mxq` queue as a candidate in a `ready`-but-**unscheduled** state. Unscheduled items never post. Enqueue binds an **asset manifest / content hash** so the candidate is immutable from here.
4. **GATE (human)** — Jefe reviews the candidate and runs **`mxq schedule <id> "<when>"`** — the real, existing human gate. This is the *only* thing that authorizes a post. (Not a new "approve = post" verb — reuse mxq's actual semantics.)
5. **Publish** — mxq's existing deterministic publisher claims the scheduled/due item and posts via the Graph API. It is the *only* component holding the token. It inherits mxq's atomic due-claims, attempt IDs, `publish_unknown` handling, token-refresh locking, and anti-double-publish.

## Security surface — Separation of Duties is the gate
The gate is **structural, not a prompt.** A prompt-enforced "only publish if approved" is a self-report an LLM can forge (both reviewers flagged this). Instead:

- **Generator process:** no IG token, no publish tool, no Graph credentials. It can only enqueue candidates. Even a fully hallucinating agent physically cannot publish.
- **Publisher process:** holds the token; only ever acts on items a human `mxq schedule` marked due. It never takes instructions from the generator or an approval *flag* — only from queue state a human transitioned.
- **Human action = the trigger.** `mxq schedule` is a deterministic CLI command; the human running it IS the authorization. No async agent "watches for approval."
- **Defense-in-depth (not load-bearing):** whether `mxq` is classifier-gated in auto mode is unverified (only `mxr` is proven). Separation-of-duties makes it moot — no token means no publish even if an agent invoked mxq. Still, add a `mxq`-specific auto-mode negative test.
- **Injected:** caption/hashtag text is data, not instructions — wrapped, never eval'd.
- **Auditability:** every publish confirmed via Graph API + mxq ledger, NOT the agent's word (how we caught the real post count = 3, team-era = 2).

## Edge cases & failure modes
- **Agent confabulates approval** (original fear) → structurally impossible; generator holds no publish path.
- **Generation runaway / spam** (Oracle) → soft error must NOT infinite-retry: bounded retries per candidate + a per-run candidate cap. Overflow logs and stops, never floods the queue or burns Higgsfield credits.
- **Approval race** (Oracle) → candidate is immutable once ingested (bound to content hash / asset manifest). No post-schedule mutation of caption or artifact; a change requires a new candidate id.
- **Post-approval Graph rejection** (Oracle) → scheduled item fails to publish (size/format/network) → mark `publish_unknown`/failed, **notify Jefe via the jefe-inbox drop, do NOT let an agent auto-fix-and-republish.** Human re-queues.
- **Double-publish / retry storm** → inherited from mxq (atomic due-claim + attempt IDs), not re-solved here.
- **Render fails / times out** → candidate never enqueued; loop logs and skips.
- **Token expiry** → publisher verifies token first (mxq token-refresh locking); on failure stages an alert, does not silently drop.

## Borrows vs builds (anti-over-engineering)
- **BORROW:** mx-engine `mxq` (queue, scheduler-gate, deterministic publisher, all publish hardening), the controller/play-review cadence loop, the jefe-inbox drop, the existing generic Higgsfield runner.
- **BUILD (small):** one Speak/Avatar adapter (endpoint + payload shape) for the runner; the generator loop that renders + enqueues into mxq; generation retry/candidate caps; a `mxq`-auto-mode negative test.
- **DO NOT BUILD:** a new orchestrator, an approval UI, an autonomous publisher, a separate publish engine, a scheduling engine, a new "approve" verb, or any path where the generator holds the token.

## Decisions
- **Gate: existing `mxq schedule <id> "<when>"` — LOCKED** (KilaBz: it's the real gate; unscheduled never posts). Supersedes the v0.1 invented `mxq approve`.
- **Enforcement: structural separation of duties — LOCKED** (both reviewers): generator holds no token/publish path; publisher is the sole token holder acting only on human-scheduled items.

### Still open (tunable, not blocking)
- Cadence: daily vs a few/week?
- Face: MX character vs a "creator" face fronting talking-heads?

## Cross-family review record (2026-07-01)
- **KilaBz (Codex): REVISE** — `mxq approve` is wrong; real gate is `mxq schedule`. Don't rebuild mxq's existing hardening. `mxq` auto-gating unverified.
- **Oracle (agy): REVISE** — gate forgeable without token/tool isolation; add separation of duties; make the human CLI action the deterministic publish trigger; cover generation runaway, approval race, post-approval Graph rejection.
- Both folded into v0.2. Net effect: **less to build** (inherit mxq) with a **structurally unforgeable** gate.

## Correction to prior record
The mx-engine "rogue subagent published unauthorized" incident writeup is **wrong on its central
claim.** Ground truth (Graph API, 2026-07-01): 3 posts total, **2 team-era — both pushed by Jefe.**
No unauthorized autonomous publish occurred. The gate here is the design regardless, but it is
NOT justified by a rogue-post incident. Fix that record when back in mx-engine.
