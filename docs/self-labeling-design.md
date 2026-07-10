# Self-labeling system — safe automation of the outcomes-ledger labeling flywheel (design v0.2)

**Status:** DESIGN v0.2 — folded a dual-family design review (kilabz + oracle) that dismantled the v0.1 firewall (2 CRITICAL + 4 BLOCKER, strongly convergent). NOT built.
**Author:** Mack (Fable 5), 2026-07-10. **Scope:** `src/runtime/{ledger,command_api}` + a new labeler module + `orchestrator/` sweep + one new migration. NO edits to shipped migration 0008.

## 0. What changed from v0.1 (the review verdict)

Both families agreed the direction is right and the v0.1 firewall was **not airtight**. The core error: v0.1 fenced on the wrong axis — **"objective vs model"** — and it leaked because (a) the "objective" oracle (`play-fix`) has an LLM generating the fix and selecting the proof-test, so `exec_verified` was never LLM-free (oracle CRITICAL); and (b) objective signals are structurally **FP-blind** — you can prove a bug *exists* by execution but can't prove a *non*-problem, so an objective-only metric drifts toward 100% precision and widens autonomy on garbage (oracle CRITICAL). Kilabz added that the fence covered only the precision view, leaving three more leaks: the legacy view, the **canonical open/current state** (a model `fp` could silently drop a finding from the human queue), and **`created_by` trust** (a DB CHECK can't enforce "this verb's principal").

**v0.2 corrects the axis: the gate is HUMAN CONFIRMATION, not objective-ness.** The LLM (and the fix-probe) are pure *labor* — they propose labels, cluster, attach evidence, and shrink 37 findings to a handful of class-level batches. Nothing a machine writes affects the precision metric **or** the canonical open/current state until a **human confirms it**. This keeps v1's original "no LLM in the gating path" invariant fully intact rather than relaxing it — a much stronger posture than v0.1 tried to argue for.

## 1. Problem + evidence

Unchanged from v0.1: the self-learning loop is stalled on **labeling throughput**. Live: the Mini accrued **37 findings** since 2026-07-02; `mxr outcome-stats` shows 10 of 13 `(rule_tag × family)` rows at `precision = n/a`; only ~4 labeled in eight days, because the sole label source is a human running `mxr outcome <key> fp|wontfix`, and Jefe won't do that continuously. This is the *data-accrual* rung the whole arc named as the true bottleneck ([[acting-rungs-gate-on-data]]), and its enabler (agents seeing real code via the staging seam) just shipped.

**The hard floor, now accepted (both families):** FP labels are irreducibly judgment — you can't execute your way to "this is a non-problem." So the human is unavoidable for the FP side of precision. The design's job is therefore NOT to remove the human but to make his judgment **cheap, rare, batched, and evidence-attached** — confirming ~3–5 class-level decisions a week instead of 37 individual findings.

## 2. The keystone: the gate is human confirmation; machines do labor behind a fence

Truth = human-confirmed. A label gates autonomy iff a human authored or confirmed it. Everything a machine produces is a *proposal* that (a) reduces the human's labor and (b) is measured for its own accuracy — but is fenced from both the precision metric and the canonical open/current state.

| Role | Source | Gates autonomy / affects open-state? |
|---|---|---|
| **human** | `mxr outcome … fp/wontfix`, or a one-tap batch confirm | **YES** — `human_dismiss` / `human_confirm` (new); terminal, top precedence |
| **execution prior** | fix-probe: bug reproduces on clean base → a **stable, pre-existing** proof-test goes red→green, no regression, no tamper | **NO on its own** — a strong REAL *prior* (`exec_verified`, new) that pre-fills a human batch for cheap bulk-confirm; promotable only by a human confirm or a sampled human audit of the exec-labeler's accuracy |
| **line vanished** | a flagged line disappeared (churn/refactor/delete) | **NO** — `auto_fix_landed` (exists); the CUT lever, weak corroborator only |
| **panel proposal** | decorrelated opposite-family agent read-and-reasons at the staged tip, votes REAL/FP | **NO** — `panel_proposed` (new); labor + accuracy signal only |

The fence is enforced at **three** layers (v0.1 had only the first):
1. **Precision** — the autonomy-facing metric aggregates only human-confirmed labels, from a precedence-resolved promoted-current view (§4).
2. **Canonical state** — the `finding_current` used for the open backlog / human queue / sweep input **ignores** `panel_proposed` entirely; proposals live in a separate proposal view (kilabz BLOCKER — a model must never drop a finding from the human queue).
3. **Write authority** — a labeler principal can call ONLY `propose_outcome` (server-mints `panel_proposed`); `promote`/human labels are a separate verb reachable only by a human/admin principal; the generic ledger-append path is denied to labelers (kilabz BLOCKER — `created_by` is not trusted verbatim; source is server-minted, not caller-supplied).

## 3. Data flow

```
open findings (canonical finding_current.outcome='open', T2-blind)     ← 37 today
      │  SCHEDULED sweep (off the push-critical path; explicit degrade where oracle absent)
      ▼
┌────────────────────────────────────────────────────────────────────┐
│ LABOR (proposals + priors, fenced) — parallel per finding            │
│  • execution PRIOR (fix-probe): reproduce→fix→verify red→green (no    │
│    tamper, PRE-EXISTING test) → exec_verified REAL prior.            │
│    already-green / non-repro = NON-label → human-escalate (NOT fp).  │
│  • panel PROPOSAL: opposite-family agent at the staged tip, adversar-│
│    ial "refute this — repro or exact contradiction"; fail-closed to  │
│    REAL on doubt. → panel_proposed REAL/FP + evidence.              │
│  → cluster by (rule_tag, agreement); rank by uncertainty.           │
└────────────────────────────────────────────────────────────────────┘
      │  a machine label NEVER changes precision or canonical open-state here
      ▼
┌────────────────────────────────────────────────────────────────────┐
│ HUMAN CONFIRMATION (the gate) — cheap, rare, batched, phone-first     │
│  batches: high-agreement (panel+exec concur) → bulk one-tap confirm; │
│  contested (split / panel⊥exec) → individual; wontfix → always human.│
│  each confirm → human_confirm/human_dismiss (promoted, gating).      │
└────────────────────────────────────────────────────────────────────┘
      │
      ▼  labeler-accuracy audit (SEPARATE non-gating views): panel_proposed &
         exec_verified vs the human-confirmed truth that lands → drift → more escalation
```

Convergence unchanged: the fix-probe IS the autonomous fixer in observe mode (labeling flywheel = self-fix engine at a lower trust level), and the phone confirmation batch IS the ripe first rung of remote-control (b) — a low-blast-radius scoped write.

## 4. Decisions

**D1 [strong] — one taint-separated view architecture; the raw all-source metric is deprecated so no consumer can read it.** New migration `0009` adds:
- `outcome_source` values `panel_proposed`, `exec_verified`, `human_confirm` (guarded ALTER of the CHECK; never edit 0008).
- `finding_current_promoted` — a precedence-resolved view: **human > exec_verified > auto_fix_landed(T1) > panel_proposed(T2) > open**, terminal-wins-regardless-of-recency, mirroring `finding_current`'s human-terminal precedence (kilabz BLOCKER — aggregate from resolved rows, not raw).
- `finding_precision_promoted` — aggregates **only human-confirmed** labels from `finding_current_promoted` (exec/T1/T2 excluded from the fraction; exec is a prior, not a gating count).
- the legacy `finding_precision` is **renamed `finding_precision_raw` and documented diagnostic-only**; the autonomy name is the promoted one; PR-1 ships a test asserting no acting-rung/consumer reads the raw metric (both families BLOCKER — make the unsafe path hard to use, not just add a safe one). NB: today NO consumer reads precision (v1 collect-only), so there is nothing to migrate — but the rename + test lock it before the first consumer exists.

**D2 [strong] — canonical open/current state is T2-blind; proposals live in a separate lane.** `panel_proposed` rows are written to the ledger but a new `finding_proposal` view surfaces them; the canonical `finding_current` (open backlog / sweep input / human queue) resolves open/terminal state IGNORING `panel_proposed` (kilabz BLOCKER — a model proposal must never remove a finding from the human queue or later processing).

**D3 [strong] — server-minted write authority, per-verb principal gating, no generic append for labelers.** `propose_outcome(finding_key, family, verdict, evidence)` server-mints `outcome_source='panel_proposed'` + a reserved `source_event` prefix (`panel:<play>`); the labeler principal is authorized for ONLY this verb. `promote_outcome` (writing `human_confirm`/`human_dismiss`/`exec_verified`) is a separate verb, principal-gated to human/admin/the exec-oracle service identity. The raw `record_findings`/append path is not exposed to the labeler. `created_by`/`outcome_source`/`source_event` are all server-set, never caller-supplied (kilabz BLOCKER).

**D4 [strong] — `exec_verified` is a positive-only REAL prior, never an FP label, never gates alone.** It is written ONLY on a clean **red→green with a pre-existing test + no tamper + stable test provenance** (oracle/kilabz HIGH). `already-green`/`non-reproduced` is a NON-label → human-escalate, NOT `exec_verified fp` (non-reproduction ≠ false positive — a weak/stale/wrong-base test). `TAMPERED` is discarded. `exec_verified` pre-fills a human batch for cheap bulk-confirm and is measured for accuracy; it enters the gating fraction only after a human confirm or a passing sampled audit of the exec-labeler.

**D5 [strong, resolves the v0.1 internal contradiction] — the objective oracle needs the FULL fix phase, so a "precheck-only" mode cannot mint a T0 label.** kilabz caught that "active = precheck-only, no codex fix" contradicts "exec_verified = reproduce→fix→verify". Resolution: `exec_verified` requires the full isolated reproduce→**fix**→verify+tamper phase (play-fix's real path). A precheck-only observe pass proves at most "a test currently fails" — it therefore only **enqueues evidence** for the human/panel, it never writes `exec_verified`.

**D6 [lean] — ACTIVE vs PASSIVE is a knob, default PASSIVE. ⟵ Jefe's compute call.** Passive = harvest `exec_verified` only when a fix naturally lands **with independent red→green proof** (never from mere line-vanish — that would re-enter the CUT lever under a new name; kilabz MED). Active = deliberately fix-probe open findings to fill the table fast, gated behind `$ORCH/LABELER_ACTIVE` + a per-day probe budget (play-fix is serial/rate-limited). Both families: prove the firewall + passive pipeline before spending compute. Confirmed.

**D7 [resolved] — one opposite-family adversarial pass per proposal; no boardroom debate.** Both families: a single decorrelated refutation with fail-closed-to-REAL bias + random audit is sufficient; multi-round debate is premature over-engineering. Compute goes to the fix-probe instead.

**D8 [strong, scope discipline] — PR-1 is NARROW: taint separation + write authority + consumer-proof tests ONLY.** No panel, no fix-probe, no phone flow, no labeler-grading in PR-1 (kilabz explicit over-engineering flag). PR-1 proves the fence in isolation with tests; the labelers come after the fence is proven.

**D9 — idempotency: server-minted `source_event` with per-verb reserved prefixes; conflicts across sources reject loudly, never silent-no-op.** The unique tuple omits `outcome_source`, so a proposal reusing a future promoted `source_event` could silently block a later promotion (kilabz HIGH). Fix: each verb owns a `source_event` prefix it alone mints (`panel:`, `probe:`, `human:`), and `0009` either adds `outcome_source` to the uniqueness rule or the verb rejects a cross-source conflict loudly.

**D10 — labeler accuracy uses explicit non-gating analytics views.** A separate `labeler_accuracy` view compares `panel_proposed`/`exec_verified` against the human-confirmed truth that later lands — NOT `finding_precision_raw` filtered (which would keep the raw metric load-bearing; kilabz MED). Impossible to confuse with the autonomy metric.

## 5. Edge cases + failure modes

- **A machine drops a finding from the human queue** → impossible: canonical open-state is T2-blind (D2).
- **A machine inflates precision** → impossible: the promoted metric counts only human-confirmed labels (D1/D4).
- **FP-blindness inflates precision** (oracle CRITICAL) → dissolved: FPs enter the metric via human confirmation of panel FP proposals; the metric is symmetric because its source (human judgment) sees both sides.
- **exec-oracle mints a bogus REAL** (oracle CRITICAL: LLM fix passes a bogus test) → contained: `exec_verified` is a prior, not a gate; it needs human confirm or a passing audit sample; the audit (D10) catches an overfitting exec-labeler.
- **Cross-source idempotency no-op blocks a promotion** → D9 (reserved prefixes + loud conflict).
- **Transient probe/git error** → fail-CLOSED, leave OPEN, never a terminal label on ambiguity (the `present_hashes=None` invariant).
- **Over-suppression blind spot** → soft calibration note, never a hard mute (LLM reviewers' dominant failure is false-negatives); SHADOW keeps measuring; arm reversible (inherited from the dials design).
- **Poisoning via crafted diffs** → Wilson lower-bound below floor + minimum absolute FP count + distinct-PR/author diversity before any class is trusted (inherited).

## 6. Security surface

- **The firewall is the security thesis** — ground-truth integrity is the ledger's whole value (core-audit #74/#77). v0.2's three-layer fence (precision + canonical-state + write-authority) + human-only gate means a model label is structurally inert to autonomy until a human confirms it.
- **Write-authority is server-side** — `created_by`/`outcome_source`/`source_event` server-minted; the labeler principal denied every verb but `propose_outcome`; promotion principal-gated to human/admin (D3).
- **Untrusted finding content** — a stored finding fed to the panel is nonce-fenced UNTRUSTED DATA, objective above the fence, `clean()`-stripped (review-path discipline).
- **The CUT promote lever stays cut, and can't re-enter** — passive `exec_verified` requires independent red→green, never mere line-vanish (D6); `auto_fix_landed` is non-promotable and excluded from the promoted fraction.
- **Migration discipline** — `0009` guarded ALTER, never edit 0008.
- **Confinement inherited** — panel = confined kilabz/lobster (read-only sandboxes); fix-probe = play-fix's `run_sandboxed`.

## 7. Prior art — borrow / reject

- **BORROW:** `finding_outcome` append-only log + idempotency tuple + `finding_current` human-terminal precedence (extend, precedence-resolve); the `api:<principal>` mint guard (the write-authority pattern); `command_api` single-writer verbs; play-fix `run_sandboxed` + `REGRESSION_CHECK_ONLY` (positive red→green only) + `{TEST}`-slot proof selector; the staging seam (`mxr review --prompt-file`) as the panel's real-code enabler; play-review's decorrelated panel + lobster's fail-closed synthesis rule; the dials' Wilson-bound + soft-note + recency window.
- **REJECT:** LLM (or exec-oracle) in the GATING path (all machine labels are proposals until human-confirmed); auto-`applied_fixed`/line-vanish as promotable; editing 0008; overloading `reviewer_family` for provenance (that's `outcome_source`); `exec_verified` as an FP labeler; boardroom-debate paneling; building phone/grading before PR-1 proves the fence; a hard mute.

## 8. Non-goals

Inherited (not re-litigated): no automerge coupling; no revert detection here; no per-class ML/embeddings; no benign-tag tier (until it exists, suppression eligibility is a code-owned fail-closed allowlist, likely empty). New: this system does NOT act (no dial flips, no auto-suppress, no auto-fix-landing) — it produces human-confirmed labeled data behind a three-layer fence; acting rungs stay downstream, doubly-gated. No machine label gates autonomy or alters canonical open-state under any path.

## 9. Build + rollout (staged; each PR cross-family reviewed)

- **PR-1 (the fence, narrow + load-bearing — D8):** migration `0009` (new sources, `finding_current_promoted`, `finding_precision_promoted`, `finding_proposal`, rename `finding_precision`→`_raw`, reserved `source_event` prefixes/uniqueness) + `propose_outcome`/`promote_outcome` Command-API verbs with server-minted source + per-verb principal gating + tests proving: a `panel_proposed` row never enters the promoted metric, never alters canonical open-state, never outranks a human row, and no consumer reads `_raw`. NO labeler.
- **PR-2 (execution prior):** play-fix full-fix OBSERVE mode (isolated reproduce→fix→verify+tamper, no commit/inbox/charge) + the positive-red→green→`exec_verified` bridge (per finding_key). Passive default.
- **PR-3 (panel + human batch):** the scheduled decorrelated-panel sweep → `propose_outcome` + cluster/rank + the phone-first human confirmation batch. + the `labeler_accuracy` audit.
- **PR-4 (active knob):** `$ORCH/LABELER_ACTIVE` + per-day probe budget (D6) — Jefe's compute opt-in.
- **Deploy:** Mini `git pull` + kickstart (0009 auto-applies on serve) + `$ORCH` cp of the sweep script.

## 10. Open questions for review (v0.2)

1. **Is the three-layer fence (precision + canonical-state + server-side write-authority) now airtight** — can any machine label reach the gating metric OR alter canonical open-state without a human confirm, through any path (derived views, idempotency, precedence, principal spoofing)?
2. Should `exec_verified` ever auto-promote on a *passing audit sample* (a statistical gate on the exec-labeler's accuracy), or must every gating label be individually human-confirmed forever? (throughput vs. purity)
3. Is renaming `finding_precision`→`_raw` + a consumer-proof test sufficient, or should the raw view be dropped entirely once nothing reads it?
