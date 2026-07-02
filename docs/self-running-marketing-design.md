# Self-Running Marketing — Design (v0.1, Phase 0)

**Status:** DRAFT — awaiting cross-family review (KilaBz + Oracle) before any code.
**Greenlit:** 2026-07-01 by Jefe. **Owner:** Mack.

## What it does & why
An autonomous loop that keeps @MyndAIX's Instagram fed **without Jefe touching the tools** —
*except* the publish trigger, which stays human. MyndAIX marketing itself is both the funnel
*and* a live proof of the product (the AI lab runs its own growth). This is ON the north-star
(autonomous brain applied to our own channel), not the rach.ugc content-farm play.

The generator is fully autonomous. **Publish is a hard human gate.** "Self-running" = self-running
*up to the drop*; Jefe pulls the trigger.

## Data flow (input → process → output)
1. **Trigger** (cadence) — cron on the Mini fires the loop (e.g. daily). No agent decides *when* on its own beyond the schedule.
2. **Generate** — pick a brand pillar + script formula (hook ≤1.5s → claim → mechanism → soft CTA) → render a candidate via the existing Higgsfield runner. Formats: talking-head (**new Speak/Avatar adapter**), plus existing Soul/Cinema + Vibe Motion.
3. **Stage** — write the rendered artifact + proposed caption + hashtags to a **review drop** (reuse the jefe-inbox verdict-drop pattern). Candidate is inert here — nothing is live.
4. **GATE (human)** — Jefe reviews the staged candidate and issues an **explicit, unforgeable approval signal** (see Security). Nothing publishes without it.
5. **Publish** — on verified approval only, the publish step posts to IG via the Graph API and records to the mxq ledger. Token stays fail-closed otherwise.

## Edge cases & failure modes
- **Agent confabulates approval** (the ORIGINAL fear) → publish requires a signal an agent cannot author (below). Fail-closed if the signal is absent/malformed.
- **Double-publish / retry storm** → idempotency key per candidate; ledger dedupe guard before any Graph call.
- **Render fails / times out** → candidate never reaches the drop; loop logs and skips, no partial publish.
- **Bad/off-brand candidate** → Jefe simply doesn't approve; staged candidates expire and are swept.
- **Token expiry** → publish step verifies token validity first; on failure it stages an alert, does not silently drop.

## Security surface
- **Untrusted:** none external — but treat *agent-authored approval claims as untrusted*. An agent
  saying "Jefe approved" is NOT approval. The gate reads a signal only Jefe can produce:
  a file Jefe writes / a reply he sends / a one-tap action — never an LLM's self-report.
- **Injected:** caption/hashtag text is data, not instructions — wrapped, never eval'd.
- **Stored:** IG token stays `chmod 600`; publish path fail-closed by default.
- **Auditability:** every publish confirmed via Graph API + mxq ledger, NOT the agent's word
  (this is how we caught the real post count = 3, team-era = 2).

## Borrows vs builds (anti-over-engineering)
- **BORROW:** the controller/play-review cadence loop, the jefe-inbox drop pattern, the mxq ledger, the existing generic Higgsfield runner.
- **BUILD (small):** one Speak/Avatar adapter (endpoint + payload shape) for the runner; the stage→gate→publish glue.
- **DO NOT BUILD:** a new orchestrator, an approval UI, an autonomous publisher, a scheduling engine (cron exists), or any path where an agent can publish on its own authority.

## Decisions
- **Approval channel: `mxq approve <id>` (CLI) — LOCKED.** Explicit, unforgeable (agent can't
  invoke mxr/mxq in auto mode — see [[autonomous-dispatch-classifier]]), auditable via the ledger.
  File-drop reply is the fallback. iMessage is out (comms prefs).

### Still open (tunable, not blocking)
- Cadence: daily vs a few/week?
- Face: MX character vs a "creator" face fronting talking-heads?

## Correction to prior record
The mx-engine "rogue subagent published unauthorized" incident writeup is **wrong on its central
claim.** Ground truth (Graph API, 2026-07-01): 3 posts total, **2 team-era — both pushed by Jefe.**
No unauthorized autonomous publish occurred. The gate below is the design regardless, but it is
NOT justified by a rogue-post incident. Fix that record when back in mx-engine.
