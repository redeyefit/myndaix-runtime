# Learning-Loop Measurement Arc — DESIGN.md

**Status:** design (v0.1, design-only pass — NOT built). Source brief:
`~/company/close-the-learning-loop-inputs.md`. Scope: the **measurement arc** only. The
**feedback arc** (auto rule/skill proposal) is a separate, hard-gated follow-on — it already
mostly exists as the auto-capture proposer (`docs/auto-capture-design.md`); its remaining piece
is the S7 driver, designed elsewhere.

## What
A read-only, time-bucketed **recurrence-rate metric** over the existing `finding_outcome`
event log: does the same class/finding recur *less* over time? Surfaced as a new
`mxr outcome-recurrence` read verb (sibling of `outcome-stats`) + a weekly number the
`audit-reviews` cadence can cite. **Collect + REPORT only — no dial acts on it, no mutation, no
PR, no LLM** (matches the ledger's stated "v1 COLLECTS ONLY").

## Why
"It's learning" must be a NUMBER, not a story — otherwise you can't tell accumulation from
improvement. This metric is the **instrument that later proves the feedback arc works**: after
the proposer lands a skill for recurring class X, X's recurrence-rate should drop. Without it,
arming the proposer is flying blind (a bad auto-learned rule poisons every future run). PR #140
(recorder stderr visibility, merged+deployed 2026-09-12) is a **just-closed prerequisite** —
before it, findings could silently fail to record, so any recurrence number would be a lie.

## Build vs Adopt (verified against the code this pass)
| Piece | Verdict | Basis |
|---|---|---|
| Repeat-detection storage | **ADOPT** | `finding_outcome` is append-only; `finding_key = sha256(repo\0tag\0path\0line_hash)` already gives content identity — a repeat = same key across ≥2 `source_event='review:%'` rows |
| Outcome classification | **ADOPT** | `finding_current` view (human-terminal precedence, else latest-by-seq) already resolves real-vs-fp per key |
| Recurrence-rate view (time-bucketed trend) | **BUILD (small)** | `finding_precision` has NO time window (all-history); no recurrence-rate view exists |
| Read verb | **BUILD (small)** | mirror `outcome_stats()` → `outcome_recurrence()` + a `mxr` printer |
| External tools (SonarQube new-vs-existing recurrence) | **BORROW-THE-PATTERN only** | our `finding_key` already mirrors issue-identity-across-scans (our code even references SonarQube); adopting a tool violates local-first |

## Data flow
```
review raises finding  --(existing)-->  finding_outcome append (source_event='review:<play>')
                                             |
   [NEW] recurrence views aggregate DISTINCT review-events per key / per rule_tag, by week
                                             |
        mxr outcome-recurrence  --reads views (read-only)-->  weekly trend table
                                             |
             audit-reviews (weekly)  --cites the number-->  is the repeat-rate dropping?
```

## The metric — three distinct signals (do NOT conflate)
1. **Class recurrence (primary):** per `rule_tag` per week — distinct review-events raising it,
   split NEW-key vs RECURRING-key (a key first-seen in a prior week). "Are we making the same
   *kind* of mistake, and is that trending down?" This is what the proposer acts on.
2. **Regression (sharpest):** a `finding_key` that reached `applied_fixed` (by event `seq`) and
   later reappears as a `review_raised` `open` — the fix came back. Ordered by `seq`, never
   `created_at`.
3. **Persistently-open:** same key raised across consecutive reviews, never fixed — an
   *unaddressed* finding, not a regression. Reported separately.

**Real vs FP split (via `finding_current`):** recurrence of `applied_fixed` / human-confirmed
findings = the SYSTEM repeating a real mistake (the loop's target). Recurrence of
`dismissed_false_positive` = the REVIEWER repeating a wrong call (reviewer-quality —
`audit-reviews`' domain, reported separately, NOT the learning-loop mistake-rate).

## Edge cases
- **Sparse/zero data:** empty view → surface prints "no recurrence data yet" (like `outcome-stats`
  on empty precision). All-zero must stay VISIBLE — it's the #140-class starvation signal, not
  "we're perfect."
- **Multiple rows per key per review** (open + later applied_fixed + human label): count
  **DISTINCT `source_event LIKE 'review:%'`**, never raw rows; classify outcome via
  `finding_current`, never the raw per-event `outcome` (else a later human correction is ignored).
- **Non-review events:** `source_event` also holds `'human:*'` and `'sweep:<utcday>'` — a label
  or a TTL sweep is NOT a re-raise; recurrence counts filter to `review:%`.
- **Small-N honesty:** never print a confident rate on a handful of events — show counts + N, and
  suppress/annotate the rate below a minimum sample (avoid a "learning!" story from noise).
- **Cross-family:** `applied_fixed` is per (key, family); regression = ANY family re-raising a
  previously-fixed key.

## Security surface
Read-only views + read-only CLI. No untrusted input (no user-supplied query fragments), no
mutation, no PR/skill creation (that's the deferred proposer), no LLM. Blast radius = a
*misleading number* on a report — mitigated by the precise metric definition above, the small-N
honesty rule, and the acceptance gate below. Nothing here can act on or poison a future run.

## Files (build phase — NOT this pass)
- `src/runtime/ledger/migrations/NNNN_finding_recurrence.sql` — the view(s), idempotent
  (`CREATE OR REPLACE VIEW`); rebuildable, zero data migration.
- `src/runtime/ledger/schema.sql` — same view(s) on the fresh-DB path (lockstep w/ migration).
- `src/runtime/ledger/postgres_store.py` — `outcome_recurrence()` read method (mirrors
  `outcome_stats()`).
- `src/runtime/outcomerecord.py` (or `cli.py`) — `mxr outcome-recurrence` printer + routing.
- `tests/test_postgres_ledger.py` — seed events → assert class-recurrence, regression, and
  real-vs-fp split; assert `review:%`-only filtering and `finding_current` classification.
- *(later, separate)* `audit-reviews` skill — cite the weekly number.

## Dependencies
`finding_outcome` accrual working (PR #140 ✓). Postgres. No new libraries.

## Deliberately NOT built (scope fence)
The S7 proposer / any rule or skill mutation (deferred, hard-gated — its own design + attack
pass); any ML/embedding; any new storage table; any dial that ACTS on the metric. Stays
report-only.

## Acceptance / prove-it-green
- **This slice:** the views compute correct recurrence/regression/real-vs-fp on seeded fixture
  events (test), and produce a sane baseline number on the real ledger.
- **Loop-closing (later, gated, after the proposer exists):** the repeat-rate of an intervened
  class measurably DROPS on a real before/after. Until that number moves, the loop is a wish.

## Concurrency boundary
Design + this doc's review change zero runtime code and restart nothing — parallel-safe. The
BUILD (a migration `serve` auto-applies on boot) must land only when no other build depends on
the live runtime ("renovate the engine only when it isn't pulling a train").
