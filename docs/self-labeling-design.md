# Self-labeling system — safe automation of the outcomes-ledger labeling flywheel (design v0.1)

**Status:** DESIGN v0.1 — grounded in a 6-reader code survey; NOT reviewed, NOT built.
**Author:** Mack (Fable 5), 2026-07-10. **Scope:** `src/runtime/{ledger,outcomerecord,command_api}` + a new labeler module + `orchestrator/` (a scheduled sweep) + one new migration. NO edits to shipped migration 0008.

## 1. Problem + evidence

The self-learning loop is stalled on **labeling throughput, not machinery**. The Mini has been armed since 2026-07-02 and accrued **37 findings**, but `mxr outcome-stats` shows 10 of 13 `(rule_tag × family)` rows at `precision = n/a`: findings are *collected* but not *labeled*. Only ~4 got a real fp/fixed verdict in eight days. Per-class precision is the exact signal every acting rung (suppress dials, autofix-widening, the shadow→armed→wide ladder) is gated on — and it stays empty because the ONLY label source today is a human running `mxr outcome <key> fp|wontfix` (`human_dismiss`), and Jefe won't do that efficiently or continuously.

Two facts make this the ripe next step rather than premature: (a) this is the *data-accrual* rung — the thing the strategic conviction ([[acting-rungs-gate-on-data]]) named as the actual bottleneck, not another acting rung gated on data that doesn't exist; and (b) its enabling capability shipped hours ago — the review-context staging seam (PR-1/2, `mxr review`) means an agent can now SEE the real code at a reviewed tip, which it could not a week ago (empty-workspace reviewer).

**The design tension, stated up front (survey `panel-audit` gotcha #1 / `authority-firewall` gotcha #4):** the shipped v1 outcomes ledger has an EXPLICIT non-goal — *"no LLM anywhere in the pipeline; v1 COLLECTS ONLY."* That rule exists to protect ground-truth integrity (core-audit #74/#77: a transient error must never fabricate an `applied_fixed`). A labeling system made of LLMs directly contradicts it. **This design does not discard that invariant — it replaces the blunt mechanism (blanket LLM exclusion) with a precise one (a firewall that structurally prevents an LLM label from ever reaching the gating metric).** Whether the firewall is airtight enough to relax the blanket rule is THE question for cross-family review.

## 2. The keystone: separate the *labor* of labeling from the *authority* of truth

An LLM can do the labor — re-examine a finding against real code, propose a verdict, cluster, rank, deliver to a phone. It must never be the authority — the thing recorded as "this finding was real" that feeds autonomy. If a model's label counts as truth, precision measures *model-agreeing-with-model* (correlated blind spots) → a class the family systematically gets wrong scores high precision → autonomy widens on garbage. That is the exact "LLM scoring its own reviews" trap [[optimal-team-brief]] §4/§6 already declined.

So truth flows from a **provenance hierarchy** of label sources, and the autonomy-gating metric reads ONLY the top tiers:

| Tier | Source | `outcome_source` | Gates autonomy? |
|---|---|---|---|
| **T0 — human** | Jefe confirms/overrides | `human_dismiss` (exists) | YES (terminal, top precedence) |
| **T0 — execution** | fix-probe: bug reproduces on clean base → a proof-test goes red→green, no regression, no tamper | `exec_verified` (**new**) | YES |
| **T1 — corroborator** | a flagged line simply vanished from the file (churn/refactor/delete) | `auto_fix_landed` (exists) | **NO** (the CUT promote lever — §6) |
| **T2 — proposal** | LLM panel read-and-reasons against real code, votes | `panel_proposed` (**new**) | **NO — proposals only** |

The firewall is one rule: **the promoted-precision view reads only T0.** T1/T2 rows are recorded, visible, and useful for *prioritization* and for *measuring the labelers' own accuracy* — but invisible to the metric that unlocks action until a T0 event promotes them.

## 3. Data flow

```
open findings (finding_current.outcome='open')                     ← the backlog (37 today)
      │
      ▼   a SCHEDULED sweep (off the push-critical path; NOT inline in play-review)
┌─────────────────────────────────────────────────────────────────┐
│ TIER-0 OBJECTIVE ORACLE (promotable) — the fix-probe               │
│  passive: harvest exec_verified when a fix naturally lands + human │
│  active (gated knob): per-finding OBSERVE run of play-fix's        │
│    sandbox — precheck "does the finding's proof-test fail on clean │
│    base?" (REAL) / "already passes?" (FP) — NO codex fix, NO commit│
│  → writes exec_verified (T0) ONLY on a clean red→green+no-tamper   │
│  → adjudicable findings resolve here without a human               │
└─────────────────────────────────────────────────────────────────┘
      │ residue: findings not execution-adjudicable (no proof-test / not reproducible by test)
      ▼
┌─────────────────────────────────────────────────────────────────┐
│ TIER-2 PANEL (proposal only) — read-and-reason at the staged tip   │
│  a DECORRELATED cross-family adjudicator (the raiser's OPPOSITE    │
│  family) gets the real code (mxr review --prompt-file), prompted   │
│  ADVERSARIALLY: "refute this finding — show the repro or the exact │
│  contradiction." Votes CONFIRMED/REFUTED + evidence.               │
│  → writes panel_proposed (T2), NEVER promotes; default REFUTED-not │
│    (fail-closed toward "valid finding" on any doubt — lobster rule)│
│  → CLUSTERS + RANKS the residue by (class, uncertainty)           │
└─────────────────────────────────────────────────────────────────┘
      │ escalation set = split votes + all wontfix candidates + a RANDOM AUDIT sample
      ▼
┌─────────────────────────────────────────────────────────────────┐
│ TIER-0 HUMAN — scarce, escalation-only, phone-first                │
│  Jefe sees a COMPRESSED batch (~3-5/week, not 37): a class         │
│  decision ("these 8 fail-open are the reviewer being right — one   │
│  tap"), the contested residue, wontfix calls (roadmap = his), and  │
│  an audit sample. Each tap → human_dismiss/exec-confirm (T0).      │
└─────────────────────────────────────────────────────────────────┘
      │
      ▼  the labelers get GRADED: panel_proposed vs the T0 truth that later lands
   labeler-accuracy audit → panels that drift get MORE human escalation
```

The self-heal convergence (why this is the right rung, not a chore before it): **the fix-probe is the autonomous fixer running in OBSERVE mode.** Every finding it adjudicates by reproducing-then-fixing is both a training label AND a harmless dry-run of the self-fix. The identical machinery, once the accrued T0 data proves a class high-precision, flips from "shadow-probe to learn" to "armed-fix to act" — the shadow→armed→wide ladder, fueled by data the shadow phase itself produced.

## 4. Decisions

**D1 [strong] — the firewall is the FIRST thing built, before any labeler.** A new migration `0009` adds `exec_verified` + `panel_proposed` to the `outcome_source` CHECK, a precedence LADDER in a promoted-only current view, and a `finding_precision_promoted` view that reads ONLY `{human_dismiss, exec_verified}`. The future autonomy dials read `finding_precision_promoted`, never `finding_precision`. Rationale (survey `authority-firewall` missing #6): build the fence BEFORE the first consumer, so a proposal can never reach a dial through an unfenced view. Nothing else is safe to add until this exists.

**D2 [strong] — the LLM writes `panel_proposed` ONLY, through a new Command-API verb, and the write-point forbids it from minting a promoted source.** Mirror the `api:<principal>` reserved-namespace guard (`registry._reserve_api_namespace`): a `propose_outcome` verb can write ONLY `panel_proposed`; `promote_outcome` (writing `exec_verified`/`human_dismiss`) is a separate verb reachable only by the objective oracle or a human principal. `created_by` is trusted verbatim, so airtightness comes from the upstream mint guard, exactly as `api:` gets its airtightness (survey gotcha).

**D3 [strong] — the objective oracle is the fix-probe, and it labels REPRODUCIBILITY, not landed-ness.** `REGRESSION_CHECK_ONLY` (bug reproduces on clean base → proof-test red→green → no regression → no tamper) = `exec_verified` REAL. "proof-test already passes on the clean base" = `exec_verified` FP. `TAMPERED` = NON-label (discard). play-fix never commits, so `exec_verified` means *the finding reproduced and a fix made a real test pass* — a strong-but-noisy REAL signal, treated as high-precision-not-perfect (survey `fix-verify` gotchas). It requires a per-finding OBSERVE mode that runs only the sandbox precheck, no codex fix, no daily-cap charge, no inbox write — **which does not exist and must be built** (survey `fix-verify` missing #1/#2).

**D4 [lean] — ACTIVE vs PASSIVE oracle is a KNOB, default PASSIVE, active is a gated graduation. ⟵ THE decision that is Jefe's, not derivable.**
- *Passive* (default): harvest `exec_verified` only when a fix naturally lands + human labels. Free, slow — but proves the firewall + panel safely.
- *Active*: deliberately fix-probe every open finding to fill the table fast. Costs codev runs (serial, rate-limited by play-fix's single-flight lock + daily cap — survey gotcha), so it's gated behind an `$ORCH/LABELER_ACTIVE` flag + a per-day probe budget.
Recommendation: passive-first through the firewall+panel proving; flip active once T0 accrual is trusted AND Jefe opts into the compute. This sets whether the system is a compute-spending flywheel or a patient accumulator.

**D5 — the panel runs OFF the push-critical path.** A scheduled sweep over stored open findings, NOT inline in play-review (iron law: a hung panel call must never delay a verdict or wedge the review lock — survey gotcha). Decorrelation is load-bearing: the adjudicator is the raiser's OPPOSITE family; where oracle is absent (agy = Mini-only) the panel degrades EXPLICITLY (no single-family labels that lose decorrelation), it does not fail-open.

**D6 — human stays terminal, escalation-only, phone-first.** `finding_current`'s human-terminal precedence is preserved (a `panel_proposed`/`exec_verified` row can never outrank `human_dismiss`). The escalation set (split votes + wontfix candidates + audit sample) is the SAME build as the phone-READ remote-control rung (labeling is a low-blast-radius scoped write — worst case a recoverable mislabel, never touches the spine), so it sidesteps the hard phone-DISPATCH auth gate.

**D7 — the labelers are themselves graded.** A `labeler-accuracy` audit compares `panel_proposed` labels to the T0 truth that later lands (human/exec) — computed from `finding_precision` filtered by `outcome_source='panel_proposed'` vs promoted. A panel that drifts gets MORE human escalation. Grader-gets-graded (audit-reviews pattern), built fresh against the ledger (the skill reads the retired bridge corpus — survey gotcha).

## 5. Edge cases + failure modes

- **A model tries to promote itself** → structurally impossible: `propose_outcome` can only write `panel_proposed`; the CHECK enum rejects any other value from that verb's principal; `finding_precision_promoted` excludes it.
- **Mislabel-as-fp poisons a real class** (a false `fp` down-weights → sticky suppression) → the panel is biased toward "valid finding" on any doubt (lobster synthesis rule), a `panel_proposed` fp NEVER suppresses (only T0 can), and the audit catches a drifting panel. Symmetric to the ledger's obsessive fail-closed-against-fabricated-`applied_fixed`.
- **Over-suppression blind spot** (suppress a class → fewer raises → fewer labels → stale precision) → inherit the dials' mitigations: a SOFT calibration note, never a hard mute (LLM reviewers' dominant failure is false-negatives); SHADOW keeps measuring the stuck class; arm trivially reversible.
- **Transient probe error** (git/sandbox failure mid-probe) → fail-CLOSED, leave OPEN, never write a T0 label on ambiguity (the `present_hashes=None` invariant, survey gotcha).
- **Idempotency collision** — the unique tuple is `(finding_key, reviewer_family, outcome, source_event)`, NOT including `outcome_source`; a promotion that reuses `(outcome, source_event)` changing only source will silently NOT insert. Every new source uses a NEW deterministic `source_event` (e.g. `probe:<utcday>:<tip12>`, `panel:<play>`), mirroring `human:<key12>:<kind>`.
- **Snapshot can't execute** — the staging snapshot is read-only, no exec (files 0400, no exec bits). So the PANEL (read-and-reason) and the OBJECTIVE oracle (play-fix sandbox, which runs tests in a throwaway worktree) are DIFFERENT paths; a finding needing execution to confirm goes to the fix-probe, not the panel.
- **Poisoning via crafted diffs** — anti-poisoning gates (inherited from dials): a Wilson lower-confidence-bound below floor (not point estimate), a minimum absolute label count, and distinct-PR/author diversity before any class is trusted.

## 6. Security surface

- **Untrusted finding content** — a stored finding's path/line/code context is attacker-influenced diff content; fed to the panel it is nonce-fenced as UNTRUSTED DATA with the objective ABOVE the fence, `clean()`-stripped (exactly the review-path discipline). A stored finding is not more trustworthy than the diff it came from.
- **The firewall is the whole security thesis** — ground-truth integrity is the ledger's entire value (core-audit #74/#77). A model-proposed label is a deliberate fabrication risk of the same shape; the firewall's fail-closed default is OPEN/unpromoted, never a proposed terminal state that gates.
- **The CUT promote lever stays cut** — `applied_fixed` auto-writes on line-hash disappearance (churn/refactor/delete inflate precision→1.0 with ZERO human touch). It is a T1 corroborator, NOT promotable; the promoted view excludes it. Do not re-open this (survey gotcha, dials M1).
- **Migration discipline** — 0008 is append-only once on main; the new sources/views land in a NEW `0009` via guarded `ALTER ... DROP/ADD CONSTRAINT`, never by editing 0008 (editing a shipped migration is itself the `migration-fail-open` tag).
- **Confinement inherited** — panel adjudicators are the already-confined kilabz (`codex --sandbox read-only`) / lobster (`--tools Read Glob Grep --strict-mcp-config --safe-mode`); the fix-probe reuses play-fix's `run_sandboxed` (net-deny, write-deny-except-worktree, secret-read-deny). No new confinement surface.

## 7. Prior art — borrow / reject

- **BORROW:** `finding_outcome` append-only log + idempotency tuple + `finding_current` human-terminal precedence + `finding_precision` metric (extend, don't replace); the `api:<principal>` reserved-namespace mint guard; `command_api` single-writer verbs; play-fix's `run_sandboxed` + `REGRESSION_CHECK_ONLY` verdict + `{TEST}`-slot `fail_to_pass_template` (injection-free proof selector); the review-context staging seam (`mxr review --prompt-file`) as the panel's real-code enabler; play-review's decorrelated panel + lobster synthesis rule; capture's cross-family-agreement recurrence; audit-reviews' grader-gets-graded pattern; the dials' Wilson-bound + soft-note-not-hard-mute + recency window.
- **REJECT:** LLM in the DECISION/gating path (proposals only; thresholds stay SQL/human); auto-`applied_fixed` as promotable (the CUT lever); editing migration 0008; overloading `reviewer_family` to carry proposal-vs-promotion (that's `outcome_source`); a hard mute (soft note only); per-class ML/embeddings/vector store; a dashboard/UI; inline-in-play-review paneling (off-critical-path sweep instead).

## 8. Non-goals (inherited + new)

Inherited (do NOT re-litigate): no automerge-gate coupling; no revert detection here (shared with the ladder rung); no per-class ML; no benign-tag taxonomy tier (separate decision — until it exists, suppression eligibility is a code-owned fail-closed allowlist, likely empty). New: this system does NOT act (no dial flips, no auto-suppress, no auto-fix-landing) — it produces LABELED DATA behind a firewall; the acting rungs remain downstream, doubly-gated (data recommends + human arms). It does NOT let a model write a promoted label under any path.

## 9. Build + rollout (staged; each PR cross-family reviewed)

- **PR-1 (the firewall, small + load-bearing):** migration `0009` (`exec_verified` + `panel_proposed` sources, precedence ladder, `finding_precision_promoted` view) + `propose_outcome`/`promote_outcome` Command-API verbs with the mint guard + tests proving a `panel_proposed` row never enters the promoted metric and never outranks a human row. NO labeler yet.
- **PR-2 (objective oracle):** play-fix per-finding OBSERVE mode (precheck-only, no codex, no charge, no inbox) + the verdict→`promote_outcome(exec_verified)` bridge (per-finding_key). Passive by default.
- **PR-3 (panel + escalation):** the scheduled decorrelated-panel sweep over open findings → `propose_outcome` + cluster/rank + the phone-first escalation batch. + the labeler-accuracy audit.
- **PR-4 (active knob):** `$ORCH/LABELER_ACTIVE` + per-day probe budget for active fix-probing — gated on Jefe's compute opt-in (D4).
- **Deploy:** Mini `git pull` + kickstart (new migration auto-applies on serve) + `$ORCH` cp of any new sweep script.

## 10. Open questions for review

1. **Is the firewall airtight enough to relax v1's "no LLM anywhere" blanket rule?** (the central question — does a `panel_proposed` source, fenced from `finding_precision_promoted` + a mint-guarded verb, structurally prevent a model label from EVER gating autonomy?)
2. **D4:** active vs passive default — Jefe's compute-appetite call.
3. Is `exec_verified` (reproduce-then-fix, no landing) trustworthy enough to be a promoting T0 source, or should it too require a human confirm on a sample?
4. Does the panel need >1 adversarial pass (boardroom-style debate) or is a single opposite-family refutation sufficient for a T2 proposal?
