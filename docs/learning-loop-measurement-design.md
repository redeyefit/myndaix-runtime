# Learning-Loop Measurement Arc — DESIGN.md

**Status:** design v0.2 (design-only — NOT built). v0.1 taken to cross-family review 2026-09-13;
review found the primary metric inverted + a censoring confound + stale schema assumptions. v0.2
folds every blocker/P1. **Fork chosen: (a) an HONEST DIAGNOSTIC, explicitly NOT a "learning rate"**
— ledger-only, read-only, small. Source: `~/company/close-the-learning-loop-inputs.md`. Feedback
arc (auto-capture proposer) stays fenced out (`docs/auto-capture-design.md`).

## What (reframed)
A read-only diagnostic over the `finding_outcome` event log. **Primary signal: the weekly arrival
count of NEW instances (first-ever-seen `finding_key`) per `rule_tag` class** — "are we still
introducing new instances of this mistake-class, and is that trending down?" This — NOT recurring
keys — is the learning signal. Surfaced as `mxr outcome-recurrence` (read-only) + a number for the
`audit-reviews` cadence. **Report-only: no dial, no mutation, no PR, no LLM.**

## Why the v0.1 reframe (review blockers B1–B3, all verified in code)
- **B1 — `finding_key` is a LINE, not a CLASS.** A recurring key = the same line still flagged =
  *technical debt*, not learning. Making the same *kind* of mistake in a new file mints a NEW key.
  So the learning signal is **new-instance arrival per `rule_tag` trending → 0**, not recurring-key.
- **B2 — per-review counts are cadence-poisoned.** `count(DISTINCT review event)` swings with CI
  frequency, not behavior. Measure **state transitions** (first-appearance; first reopen-after-close),
  not per-review tallies. And `finding_outcome` has **no denominator** — clean/finding-free reviews
  write zero rows — so we publish **counts/transitions, never a "rate."**
- **B3 — terminal labeling CENSORS observations** (`postgres_store.py:1618`: the recorder skips
  re-raising a `(key, family)` once it carries a human label). Post-label recurrence is invisible;
  more labeling alone lowers observed recurrence. **Every number is scoped "observed, censored after
  terminal labeling,"** and labeling coverage is reported beside it.

## Signals (facets of one event history — NOT disjoint totals)
1. **New-instance arrival (primary):** per `(repo, rule_tag, UTC-week)`, count `finding_key`s whose
   FIRST-EVER raise (min `seq` over all history, before windowing) falls in that week. Trending down
   = the loop is preventing new instances of that class.
2. **Regression (secondary):** one onset per `open → applied_fixed → open` *episode* on the **same
   `ref`**, ordered by `seq` (not `created_at`); later raises in the episode = continuation, not new
   onsets. `applied_fixed` is a **lifecycle proxy** (fires on delete/rename/edit — NOT confirmed
   truth), so this is labeled "observed reappearance after recorded closure," not "a real fix returned."
3. **Repeated-raise-without-close (replaces "persistently-open"):** ≥2 recorded raises of a key with
   no intervening recorded close. ("Persistently-open across reviews" is NOT computable — the ledger
   has no record of clean reviews that inspected-and-passed.) Expiry (`ttl_sweep/expired`) ends an episode.

## Admission predicate (a "raise" is exactly this — the source_event grammar has 5 prefixes)
```sql
outcome_source = 'review_raised' AND outcome = 'open' AND source_event LIKE 'review:%'
```
Count **distinct event identifiers**, grain `(finding_key, reviewer_family, source_event)`. Closures
(`auto_fix_landed/applied_fixed`) also carry `review:<play>` — the predicate excludes them.
`human:*`, `sweep:*`, `probe:*`, `panel:*` are NOT raises. Prefix text does not authenticate an event
— rely on the `(outcome_source, outcome)` pair, not the string.

## Real-vs-machine-vs-unadjudicated (via full algebra — schema evolved past schema.sql)
`finding_current` resolves one row per **`(finding_key, reviewer_family)`** — join on that grain, never
key alone (else multiplied rows / conflicting family labels). Keep four explicit states; machine priors
must NEVER silently become confirmed truth:

| `outcome_source` | outcome(s) | treatment |
|---|---|---|
| `review_raised` | `open` | a RAISE (the observation unit) |
| `auto_fix_landed` | `applied_fixed` | closure PROXY (lifecycle, not truth) |
| `human_confirm` | `confirmed_real` | human real (ground truth) |
| `human_dismiss` | `dismissed_false_positive` / `dismissed_wontfix` | human FP vs declined (distinct) |
| `ttl_sweep` | `expired` | episode end, NOT a fix |
| `auto_git_revert` | `reverted` | reserved — no v1 writer; excluded |
| `exec_verified` | `exec_real_prior` | UNCONFIRMED machine — excluded from truth split |
| `panel_proposed` | `panel_real` / `panel_fp` | UNCONFIRMED machine — excluded from truth split |

FP recurrence (`dismissed_false_positive`) = a REVIEWER repeating a wrong call → reported under
reviewer-quality (`audit-reviews`), NOT the learning signal. Correction: the ops DB has
`finding_precision_raw` / `finding_precision_promoted` (migration 0010), NOT `finding_precision`.

## Intervention overlay (so before/after is attributable)
Join `skill_use (review_play, skill_name, body_sha, used_at)` to anchor "Day-0" = when a skill was
actually EXPOSED to reviews (more precise than PR-open time). The diagnostic can then show new-instance
arrival for a `rule_tag` before vs after its skill's first exposure. (Correlational, clearly labeled.)

## Reporting contract
UTC weeks, half-open `[start,end)`; first-seen computed over FULL history then windowed; dedup to the
admission grain BEFORE bucketing; current (partial) week marked incomplete; classification policy =
**as-of reporting-cutoff** (frozen — so before/after comparisons use consistent adjudication, unlike
current-knowledge which silently restates history); every output stamps generation-time, cutoff, and
metric-version. Small-N: show distinct reviews/tips + labeling coverage beside counts; suppress no real
observation, but never imply a rate from a handful of events.

## Runtime contract / trust boundary (precise)
`connect → read-only SELECT aggregation → print`. NO migrations, recorder/TTL/label writes, snapshots,
job dispatch, or skill/PR ops in the read path. Reviewer-derived tags are untrusted EVIDENCE — structured
filtering limits interpretation, not truthfulness; the diagnostic reports what was OBSERVED, asserts no
truth. Four distinct terminal states, none conflated: (i) observations present, zero repeats; (ii) no
finding observations; (iii) no/unknown review activity; (iv) query/connection FAILURE. **A measurement
failure MUST exit non-zero and be distinguishable from an empty successful report** — do NOT copy
`outcome_stats()`'s success-on-failure (that is the #140 silent-failure class, again).

## Files (build phase — NOT this pass)
- `migrations/NNNN_finding_recurrence.sql` — the diagnostic view(s), idempotent `CREATE OR REPLACE VIEW`.
- `schema.sql` — same view(s), fresh-DB path (lockstep).
- `postgres_store.py` — `outcome_recurrence()` read method (own read path; distinguishable failure).
- `outcomerecord.py`/`cli.py` — `mxr outcome-recurrence` printer + routing.
- `tests/test_postgres_ledger.py` — the acceptance fixtures below.
- *(later, separate)* `audit-reviews` skill — cite the weekly number.

## Acceptance fixtures (declared expected results — must pass before build is "green")
| History / boundary | Required assertion |
|---|---|
| raise A → close B | NO recurrence, NO regression |
| same key twice in one play (both families) | explicit dedup; not a second raise |
| raise → raise within one week | defined new-vs-repeat behavior (first-seen from full history) |
| raise → close → raise → raise | ONE regression onset + continuation |
| raise → expire → raise | NOT a fixed-key regression |
| raise → human real/FP → attempted re-raise | demonstrates the CENSORED (missing) observation |
| fix on ref A → raise on ref B | NO unqualified regression (ref fence) |
| new + recurring keys in one class/review | no accidental partition / double-count |
| human correction; intervening panel/probe rows | correct truth precedence; raise counts unchanged |
| first obs before window; reversed timestamps; empty week; query failure | correct lookup/ordering/visibility; failure ≠ empty |

## Deliberately NOT built (scope fence)
The S7 proposer / any rule/skill mutation (its own gated design + attack pass); a true "learning rate"
(needs a recorder change to un-censor + a denominator source — deferred unless this diagnostic proves
insufficient); any ML/embedding; any new storage table; any dial that ACTS on the output.

## Acceptance / prove-it-green
- **This slice:** views compute new-instance / regression / repeated-raise correctly on the fixtures
  above; the read path returns a sane baseline on the real ledger AND fails non-zero on a broken DSN.
- **Loop-closing (later, gated):** after the proposer lands a skill for class X, X's new-instance
  arrival measurably drops on a real before/after (overlay-anchored). Until that moves, the loop is a wish.

## Concurrency boundary
Design + review = zero runtime code, no restart — parallel-safe. The BUILD (a migration `serve`
auto-applies on boot) lands only when no other build depends on the live runtime.
