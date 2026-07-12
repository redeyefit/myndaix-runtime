# Shadow Dial — DESIGN (v0.3, post-fence measurement surface)

**Builds directly on `docs/outcomes-dials-design.md` v0.2** (branch `feat/outcomes-precision-dials`;
dual-family + 42-agent adversarial review, mechanism ENDORSED, 2 BLOCKERs + 5 MAJORs folded). This
is that design's **PR-A SHADOW**, re-scoped for two things that changed since 2026-07-03:
1. **The fence shipped** (migration 0010, PR #81): the gating metric is now
   `finding_precision_promoted` — **human labels only** (`confirmed_real / (confirmed_real +
   dismissed_false_positive)`), not the old all-source `applied_fixed`-based `finding_precision`.
2. **Real data exists**: 81 human labels across ~24 (tag × family) cells (2026-07-12). The
   data-gate the v0.2 doc named ("has nothing to recommend today — correct and expected") is met.

## 0. Scope — MEASURE ONLY. This rung acts on NOTHING.

The v2.0-era autonomy ladder's first rung, in its safest form: a read surface that says what a
suppress dial *would* do, and changes nothing. No prompt weighting, no note injection, no
suppression, no arm state, no writes. The entire deliverable is **visibility + an honest
evaluation loop**. Acting (the v0.2 doc's PR-B) stays deferred, now gated on this rung showing a
*stable* signal Jefe agrees with AND the unresolved benign-taxonomy question (below).

Why measure-only is the right first rung (the v0.2 reframe, still true): the live taxonomy is
**entirely correctness/security classes**, and under fail-closed policy essentially none is safe to
auto-suppress — a noisy security detector is still a detector. So the dial's real v1 value was
always the MEASUREMENT, not the acting. This rung ships exactly that, and nothing else.

## 1. What the fence changed (why M1 is moot, and what's newly honest)

v2.0's worst finding **M1 — "PROMOTE is broken, CUT it"** — was that `applied_fixed` is written
automatically on line-hash disappearance (refactor/churn/delete all count), so a high-churn class
inflates precision→1.0 with ZERO human labels. **The promoted metric does not read `applied_fixed`
at all** (it's `confirmed_real`/`dismissed_fp` over `finding_current_human`). So promote-by-machine-
inflation is structurally impossible now. Consequence: this shadow surface can HONESTLY show a
would-trust signal (high human-confirmed precision) alongside would-suppress — both are pure
human-label measurements. **Neither ever auto-acts here.**

M3 (accumulator trap — all-time precision anchors a class to old failures) still applies to the
eventual *acting* path; this measurement surface addresses it by showing BOTH all-time and a
recency window, so a recovering class is visible, not hidden.

## 2. The computation (per rule_tag × reviewer_family, over human labels only)

Read `finding_current_human` (the fenced human-label state) → per (tag, family):
- `n` = confirmed_real + dismissed_false_positive (the labeled denominator; `dismissed_wontfix`
  is excluded — "real but declining" is not a precision signal, matching the promoted view).
- `precision` = confirmed_real / n (point estimate; NULL if n = 0).
- **Wilson score interval** (z = 1.96, 95%) lower & upper bound on `precision` — the statistical-
  stability fold: never classify on the point estimate over ~8 labels; the LOWER bound gates
  would-suppress, the UPPER bound gates would-trust, so both require the interval to actually clear
  the threshold, not just the noisy midpoint.
- `n_recent` / `precision_recent` = the same over the last `SHADOW_RECENCY_N` (default 30) labeled
  events by `seq` (M3: a recovering class shows a rising recent precision even while all-time lags).
- `provenance` = distinct source refs among the labeled findings (`finding_outcome.ref`) — the
  author/PR-diversity proxy (anti-poisoning fold: one misunderstood PR's bulk-dismissals is one
  ref, not N independent signals). Surfaced so a human never reads 8 labels from 1 ref as 8.

Classification (INFORMATIONAL — no action attached):
| would-say | condition |
|---|---|
| `insufficient` | n < `SHADOW_MIN_N` (default 10) OR provenance < `SHADOW_MIN_REFS` (default 2) |
| `would-suppress` | Wilson **lower** bound < `SHADOW_FLOOR` (0.30) AND dismissed_fp ≥ `SHADOW_MIN_FP` (3) AND enough n/refs |
| `would-trust` | Wilson **lower** bound ≥ `SHADOW_CEILING` (0.90) AND enough n/refs |
| `hold` | otherwise |

**Fail-closed suppressibility is retained and code-owned** (the v0.2 B1 fold): a `would-suppress`
carries a `suppressible` boolean from a code-defined `SUPPRESSIBLE` set — **empty in v1** (no
current tag is safe to auto-suppress). So even the *label* would-suppress is annotated "not
suppressible (no benign tier)". This keeps the eventual acting rung honest from day one: the
measurement can say "low precision here," but the design refuses to imply any of it is
auto-suppressible yet.

## 3. Surface (one read verb, zero writes)

`mxr dial-shadow` — prints, per (tag × family): n, precision, Wilson [lo, hi], recent precision,
distinct-refs, the would-say, and the suppressible flag. Rides the morning brain-check next to
`mxr outcome-stats`. OPERATOR tier: fail-CLOSED (exit 2) on an unreachable ledger (a dial that
can't read must not print an empty "nothing to suppress"). Thresholds are read from env in the
VERB (per v0.2 D1); the SQL is a pure projection.

Example:
```
rule_tag                  family  n   prec  wilson[lo,hi]  recent  refs  would-say     suppressible
missing-scoping           oracle  3   0.00  [0.00,0.56]    0.00    2     insufficient  no (n<10)
unsanitized-injection     oracle  8   0.50  [0.22,0.78]    0.55    3     hold          —
silent-error-suppression  kilabz  5   1.00  [0.57,1.00]    1.00    2     insufficient  no (n<10)
```

## 4. The EVALUATE loop (the point of shadowing)

A shadow prediction is only worth arming if it's STABLE and the human AGREES. Two mechanisms:
- **Snapshot** (`mxr dial-shadow --snapshot`): append the current classification to
  `dial_shadow_snapshot` (a NEW additive table — tag, family, n, precision, wilson_lo, would_say,
  captured_at). Weekly cron or manual. This is the ONLY write in the rung and it touches no
  outcome/fence table.
- **Agreement report** (`mxr dial-shadow --eval`): for each past snapshot's `would-suppress`
  classes, compute the human labels that landed on that (tag, family) SINCE the snapshot, and
  report agreement = did the new labels confirm low precision (mostly fp) or contradict it (mostly
  real)? A `would-suppress` that new human labels keep dismissing is a stable, real signal; one
  that new labels start confirming was small-n noise. This is a single honest number over time —
  the exact evidence the acting rung must clear before any arm conversation.

Pre-committed gate (written here so it can't be rationalized away later): **≥ 4 weekly snapshots
showing a stable would-say for a class, with ≥ 70% subsequent-label agreement, before PR-B (acting)
is even designed for that class.** Shadow is the instrument that earns its own arming — or refuses
it cheaply.

## 5. What this rung deliberately does NOT do (each with its un-gating condition)

| Not built | Why | Un-gates when |
|---|---|---|
| Any acting (note injection, suppression) | the v0.2 PR-B; needs stable signal + a benign taxonomy tier | §4 gate met AND a suppressible class exists |
| A non-empty `SUPPRESSIBLE` set | the taxonomy is all correctness/security; nothing is safe to mute | a benign/"style-nit" tier is added (its own design) |
| Promote acting | endorsed as measurable now (fence mooted M1) but acting on trust still couples to autonomy — out of scope | its own rung |
| A miss/false-negative signal | nothing records a bug the review MISSED (v0.2 D3) | a separate rung entirely |
| A dashboard / web UI | terminal read verb is the point | never (anti-over-engineering) |

## 6. Security & failure modes
- **Read-only** (except the opt-in `--snapshot` append to its own table): no fence table is
  touched, no acting path exists, so the suppress-lever attack surface from v0.2 §6 is absent by
  construction. There is nothing to weaponize — the worst an attacker who could forge labels
  achieves is a wrong number in a report a human reads.
- `rule_tag`/`reviewer_family` are validated against the taxonomy allowlist before display (no
  path component, no prompt — they only ever reach stdout here).
- Wilson math + all thresholds are pure/deterministic; env knobs parsed fail-safe (default on
  malformed, per the CLI env-knob convention).
- Gate mode: N/A (this is a manual operator read, never on the review/merge path).

## 7. Test plan (test-first, deterministic)
- Wilson bound math: known (k, n) → known [lo, hi] within tolerance; n=0 → NULL, no divide.
- classification edges: lower-bound-below-floor+enough-fp+refs → would-suppress; lower-bound-above-
  ceiling → would-trust; n<MIN_N → insufficient; refs<MIN_REFS → insufficient (even at high n);
  suppressible flag always "no" while SUPPRESSIBLE is empty.
- recency: a class with old fp's + recent confirms shows all-time low, recent high.
- provenance: 8 labels from 1 ref → insufficient (diversity gate), from 3 refs → eligible.
- snapshot: append-only, correct columns; --eval computes subsequent-label agreement against a
  seeded snapshot+later-labels fixture.
- verb: fail-closed exit 2 on unreachable ledger; empty ledger → honest "insufficient everywhere".

## 8. Build plan
- **PR-A (this):** the Wilson/classification core (pure, unit-tested) + `label-shadow` read over
  `finding_current_human` + `mxr dial-shadow [--snapshot|--eval]` + one additive migration
  (`dial_shadow_snapshot` table; guarded, idempotent). Zero acting, zero fence-table writes.
- **PR-B (deferred, v0.2's ACT):** designed only after §4's evidence gate + the taxonomy question.

Deploy = normal serve-restart auto-migrate. Arms nothing; lets Jefe WATCH the signal accrue and
prove (or refute) itself — the evidence the acting rung needs to be designed honestly, or dropped.
