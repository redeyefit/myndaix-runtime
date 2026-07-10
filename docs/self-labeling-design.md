# Self-labeling system — safe automation of the outcomes-ledger labeling flywheel (design v0.4)

**Status:** DESIGN v0.4 — ✅ CONVERGED, DUAL-FAMILY APPROVE (kilabz r4 + oracle r3/r4: "airtight, cleared to build"). 4 cross-family rounds folded 2 CRITICAL + 10 BLOCKER + several HIGH. AWAITING JEFE'S BUILD-GO on PR-1 (the fence). One kilabz r4 build note folded (§5 pairwise matrix).
**Author:** Mack (Fable 5), 2026-07-10. **Scope:** `src/runtime/{ledger,command_api}` + labeler module + `orchestrator/` sweep + migration `0009`. NO edits to shipped 0008.

## 0. Change log

- **v0.1 → v0.2:** firewall axis moved from "objective vs model" to **"human-confirmed vs proposed."** The gate is human confirmation; the fix-probe and panel are pure labor.
- **v0.2 → v0.3:** fold r2. Split write verbs (no confused deputy); machine-blind queue; killed sampled-audit auto-promote (bulk human-confirm instead); precise schema; one idempotency rule; renamed the all-source view.
- **v0.3 → v0.4 (this rev):** oracle r3 = APPROVE (airtight). Folded kilabz r3: (a) **label vs lifecycle axes** — `ttl_sweep/expired` is a lifecycle tombstone, NOT a label; the queue is human-LABEL-terminal only, TTL is a separate documented+tested axis (kilabz BLOCKER). (b) **complete source×outcome algebra** — all 10 legal pairs enumerated; `0009` widens the two independent value-CHECKs (non-breaking), pairing is verb-enforced (kilabz BLOCKER). (c) **current-not-rows aggregation** — precision reads `finding_current_human` (DISTINCT-ON latest human label), no double-count on repeat/correction (kilabz HIGH). (d) **dropped `finding_current_resolved` from the DB** — attractive nuisance; the UI joins dynamically (oracle r3).

## 1. Problem (unchanged, brief)

Self-learning is stalled on **labeling throughput**: Mini accrued 37 findings since 2026-07-02, ~4 labeled, `precision` mostly `n/a`, because the only label source is a human running `mxr outcome … fp|wontfix` and Jefe won't do it continuously. FP labels are **irreducibly judgment** (you can't execute your way to "this is a non-problem"), so the human stays ground truth; the design's job is to make his judgment cheap, rare, batched, evidence-attached (~3–5 class-level batch confirms/week, not 37 taps). Enabler (agents seeing real code via the staging seam) just shipped.

## 2. Keystone (precise)

**A label gates autonomy — and removes a finding from the human queue — iff a human authored or confirmed it.** Every machine output (panel proposal, exec-probe prior, line-vanish) is fenced from BOTH the gating metric AND the canonical label-queue until a human row lands. This preserves v1's original "no LLM in the gating path" invariant intact.

The fence is a closed algebra over three columns, checkable by `grep`. Two ORTHOGONAL axes — a finding carries at most one LABEL (a real/fp verdict) and, separately, a LIFECYCLE state (active / aged-out); no machine touches the label axis:
- **Gating inputs** = rows where `outcome_source ∈ {human_confirm, human_dismiss}` (both HUMAN). Nothing else counts.
- **Label-terminal (removes-as-labeled from the queue)** = the same two human sources ONLY. **No machine source is label-terminal.** `panel_proposed`, `exec_verified`, `auto_fix_landed` are all invisible to the queue.
- **Lifecycle tombstone (a SEPARATE axis, not a label)** = `ttl_sweep/expired` ages out a finding the human left unlabeled past the TTL. It asserts NO verdict, gates NO precision (counts toward neither side — the existing v1 "keeps denominators honest" rule), and is the ONLY non-human way a finding leaves the *active* queue. It is not a machine *label*; it is fail-closed + deterministic (`expire_open`, `sweep:<utcday>`) and merely stops tracking a stale finding — a documented, tested exception distinct from labeling authority (kilabz r3).
- **Write authority** = server-minted `outcome_source` + a principal→source matrix (§5); a machine identity can NEVER mint a human source.

## 3. Schema contract (migration `0009`, guarded ALTER of 0008's CHECKs — never edit 0008)

**`outcome_source`** (WHO produced the row) — existing `{review_raised, auto_fix_landed, auto_git_revert, human_dismiss, ttl_sweep}` + **new** `{panel_proposed, exec_verified, human_confirm}`.

**`outcome`** (WHAT the row asserts) — existing `{open, applied_fixed, dismissed_false_positive, dismissed_wontfix, reverted, expired}` + **new** `{confirmed_real, exec_real_prior, panel_real, panel_fp}`.

**`0009` adds the new values to the two INDEPENDENT value-set CHECKs** (one on `outcome_source`, one on `outcome`), matching 0008's pattern — a guarded `ALTER … DROP/ADD CONSTRAINT` that only WIDENS each enum, so it can never break an existing row (kilabz r3 — a pair-CHECK enumerated from the verdict table alone would reject existing `review_raised/open` etc.). The (source × outcome) PAIRING is enforced at the WRITE point by the §5 verb matrix, not a DB pair-CHECK (a full pair-CHECK is optional later hardening and MUST enumerate every existing pair too).

Complete legal (`outcome_source`, `outcome`) pairs — **existing (unchanged)** + **new**:

| # | `outcome_source` | `outcome` | axis | gating? | label-terminal? |
|---|---|---|---|---|---|
| e1 | `review_raised` | `open` | label (none yet) | no | no (it IS the open state) |
| e2 | `auto_fix_landed` | `applied_fixed` | label (v1 diag) | **no** | **no** (queue ignores it) |
| e3 | `auto_git_revert` | `reverted` | label (v1, no writer) | no | no |
| e4 | `human_dismiss` | `dismissed_false_positive` | label | **YES (denom)** | **YES** |
| e5 | `human_dismiss` | `dismissed_wontfix` | label | no (n/a) | **YES** |
| e6 | `ttl_sweep` | `expired` | **lifecycle** | no | no (tombstone — active-queue only) |
| n1 | `human_confirm` | `confirmed_real` | label | **YES (numer)** | **YES** |
| n2 | `exec_verified` | `exec_real_prior` | label | **no (prior)** | **no** |
| n3 | `panel_proposed` | `panel_real` | label | no | no |
| n4 | `panel_proposed` | `panel_fp` | label | no | no |

**Gating precision** (`finding_precision_promoted`) aggregates the ONE CURRENT human label per `(finding_key, reviewer_family)` — read from `finding_current_human` (§4), a DISTINCT-ON-latest-human-terminal view — NOT raw rows (kilabz r3 HIGH: a repeat confirm or an fp→real correction inserts a second row under a different `source_event`; counting raw rows would double-count). Then per `(rule_tag × reviewer_family)`: `count(current confirmed_real) / (count(current confirmed_real) + count(current dismissed_false_positive))`. Only the two human label-pairs (e4, n1) can enter; every machine outcome and the CUT lever (`applied_fixed`) is structurally absent.

**Idempotency (one rule):** `0009` DROPs 0008's `UNIQUE(finding_key, reviewer_family, outcome, source_event)` and CREATEs `UNIQUE(finding_key, reviewer_family, outcome, outcome_source, source_event)` (adds `outcome_source`). Each verb owns a reserved server-minted `source_event` prefix (`human:`, `probe:`, `panel:`); same tuple = idempotent no-op; a differing payload uses a different event = inserts; cross-source reuse cannot collide (source is in the key) and thus can never silently shadow a human promotion.

## 4. View architecture (the three-layer fence, precise)

- **`finding_current_human` (new)** — DISTINCT ON `(finding_key, reviewer_family)` of `human_*` rows only, latest human-terminal by `seq`; ONE current human label per finding/family. The gating metric reads THIS (no double-count; kilabz r3 HIGH).
- **`finding_labelqueue` (new)** — the human queue + the sweep input. A finding is present iff it has NO row in `finding_current_human` (i.e. no human label) AND it is not lifecycle-tombstoned (`ttl_sweep/expired`). **Every non-human LABEL source (`panel_proposed`, `exec_verified`, `auto_fix_landed`, `review_raised`, `auto_git_revert`) is invisible to label-terminal resolution** — no machine can remove a finding from this queue as *labeled*; only a human label or the TTL tombstone (a separate lifecycle axis, §2) affects presence. (v1's `finding_current` is unchanged for its existing consumers; the self-labeling pipeline reads `finding_labelqueue`.)
- **`finding_precision_promoted` (new)** — the ONLY autonomy-facing metric; reads `finding_current_human` gating inputs only (§3).
- **`finding_precision_raw` (renamed from `finding_precision`)** — v1 all-source diagnostic, kept only as the accuracy-audit baseline; a PR-1 consumer-proof test asserts no acting rung reads it.
- **NO all-source `finding_current_resolved` view in the DB (oracle r3).** It was an attractive nuisance (future code would query it for gating). The human-batch UI's "priors/proposals ripe for confirm" is a DYNAMIC JOIN at request time in the labeler service (queue ⋈ `panel_proposed`/`exec_verified` rows), never a materialized core view — or, if performance ever demands it, a table in a separate `ui_views` schema the core engine can't see.

## 5. Write-authority (split verbs + principal→source matrix)

Three Command-API verbs, each server-minting `created_by`/`outcome_source`/`source_event` (never caller-supplied), each authorized to exactly one principal class:

| verb | principal | may mint `outcome_source` | may mint `outcome` |
|---|---|---|---|
| `confirm_outcome` | **human / admin ONLY** | `human_confirm`, `human_dismiss` | `confirmed_real`, `dismissed_false_positive`, `dismissed_wontfix` |
| `record_exec_prior` | **exec-oracle service identity ONLY** | `exec_verified` | `exec_real_prior` |
| `propose_outcome` | **labeler service identity ONLY** | `panel_proposed` | `panel_real`, `panel_fp` |

Un-bypassable server assertion: `assert (caller_class, source, outcome) ∈ MATRIX` before insert. **The matrix is a set of exact PAIRS, not a Cartesian product of a verb's allowed sources × allowed outcomes (kilabz r4 build note):** `human_confirm`↔`confirmed_real` only; `human_dismiss`↔`{dismissed_false_positive, dismissed_wontfix}` only; `exec_verified`↔`exec_real_prior`; `panel_proposed`↔`{panel_real, panel_fp}` — matching the §3 legal-pair table exactly, so a verb can never mint an off-pair combination. The exec-oracle and labeler identities have NO path to `confirm_outcome` and NO generic ledger-append (kilabz/oracle BLOCKER — a machine cannot mint human-looking truth). `created_by` is not trusted verbatim; airtightness is the matrix + server-mint, mirroring the `api:<principal>` guard.

## 6. Data flow

```
finding_labelqueue (human-terminal-only; machine sources invisible)     ← 37 today
   │  SCHEDULED sweep off the push-critical path (explicit degrade where oracle absent)
   ▼  LABOR (fenced) — per finding, parallel:
   ├─ record_exec_prior: play-fix FULL fix phase (reproduce→fix→verify red→green, no
   │    tamper, PRE-EXISTING test) → exec_real_prior. already-green/non-repro = NON-label
   │    → stays in queue (NOT an fp). precheck-only proves nothing → enqueues evidence only.
   └─ propose_outcome: opposite-family agent at the staged tip, adversarial refute,
        fail-closed-to-REAL on doubt → panel_real/panel_fp + evidence.
   │  cluster by (rule_tag, agreement); rank by uncertainty
   ▼  NO machine row leaves the queue or touches precision here
HUMAN CONFIRMATION (the gate) — phone-first, cheap/rare/batched:
   • bulk one-tap where exec+panel concur (audit sample shown, e.g. "10/10 clean → confirm 100")
   • individual where contested; wontfix always human
   → confirm_outcome → confirmed_real / dismissed_* (the ONLY gating + queue-terminal write)
   ▼
labeler_accuracy (SEPARATE non-gating view): panel/exec vs the human truth that lands → drift → more escalation
```

## 7. Decisions (resolved through r1+r2)

- **D1 [strong]** — the fence is three views (§4) + three verbs (§5) + a closed source/outcome algebra (§3). Human-only gating + machine-blind queue + server-minted principal-gated writes. This is PR-1.
- **D2 [strong]** — `exec_verified` is a positive-red→green PRIOR only: never an FP label, never gates alone, never removes from the queue, and it needs the FULL isolated fix phase (a precheck-only pass enqueues evidence, never writes a prior). already-green ≠ FP.
- **D3 [strong]** — every gating row is human-authored/confirmed. NO sampled-audit auto-promote. Throughput comes from a **bulk human-confirm** (one tap over a class, the audit sample *shown* to inform the tap; the write is still `human_confirm`). (kilabz CRITICAL / oracle Q2.)
- **D4 [lean]** — ACTIVE vs PASSIVE probe is a knob, default PASSIVE; passive `exec_verified` requires independent red→green, never mere line-vanish. Active gated on `$ORCH/LABELER_ACTIVE` + a per-day budget. **⟵ Jefe's compute call.**
- **D5 [resolved]** — one opposite-family adversarial pass per proposal (no boardroom); random audit backstops.
- **D6 [strong, scope]** — PR-1 is the fence ONLY (views + verbs + tests). No panel, no probe, no phone, no grading until the fence is proven.

## 8. Edge cases + failure modes

- **Machine drops a finding from the human queue** → impossible: `finding_labelqueue` is human-terminal-only (§4).
- **Machine inflates precision** → impossible: gating inputs are `human_*` only (§3); no machine outcome is in the fraction.
- **FP-blindness inflates precision** (oracle CRIT) → dissolved: FPs enter via human confirmation of panel FP proposals; the metric's source (human judgment) is symmetric.
- **exec-oracle mints human-looking truth** (confused deputy) → impossible: split verbs + principal→source matrix (§5); exec identity can only reach `record_exec_prior`.
- **Cross-source idempotency shadowing** → impossible: `outcome_source` is in the unique tuple; per-verb `source_event` prefixes (§3).
- **Transient probe/git error** → fail-CLOSED, leave OPEN (the `present_hashes=None` invariant).
- **Over-suppression blind spot** → soft note not hard mute; SHADOW keeps measuring; reversible (inherited).
- **Poisoning** → Wilson lower-bound below floor + min absolute FP count + author/PR diversity (inherited).

## 9. Security surface

Ground-truth integrity is the ledger's whole value (core-audit #74/#77). v0.3's fence makes a machine label structurally inert to autonomy AND to the queue until a human confirms: gating inputs are human-only (§3), the queue is machine-blind (§4), writes are server-minted + principal-gated (§5), the CUT lever stays cut (`applied_fixed`/`auto_fix_landed` excluded from the fraction and from the queue; passive `exec_verified` needs independent red→green, never line-vanish). Untrusted finding content is nonce-fenced DATA to the panel. `0009` is a guarded ALTER, never a 0008 edit. Confinement inherited (panel = read-only kilabz/lobster; probe = play-fix `run_sandboxed`).

## 10. Prior art — borrow / reject

**BORROW:** `finding_outcome` append-only log + human-terminal precedence; the `api:<principal>` mint guard (→ the principal→source matrix); `command_api` single-writer verbs; play-fix `run_sandboxed` + `REGRESSION_CHECK_ONLY` (positive red→green ONLY); the staging seam (`mxr review --prompt-file`) as the panel enabler; play-review's decorrelated panel + lobster's fail-closed synthesis; the dials' Wilson-bound + soft-note. **REJECT:** any machine label in the gating path OR the queue-terminal path; sampled-audit auto-promotion; `exec_verified` as an FP labeler; `applied_fixed`/line-vanish as promotable; a single verb minting both human + machine truth; an all-source view named `_promoted`; editing 0008; boardroom debate; building panel/probe/phone before PR-1.

## 11. Non-goals

No acting (no dial flips / auto-suppress / auto-fix-landing); no automerge coupling; no revert detection here; no per-class ML; no benign-tag tier (suppression eligibility stays a code-owned fail-closed allowlist, likely empty). No machine label gates autonomy OR alters the human queue under any path.

## 12. Build + rollout (staged; each PR cross-family reviewed)

- **PR-1 (the fence — buildable per both families):** migration `0009` (§3: widen the two independent value-CHECKs + the `outcome_source`-in-tuple unique index; §4 views: `finding_current_human`, `finding_labelqueue`, `finding_precision_promoted`, rename→`finding_precision_raw` — NO `finding_current_resolved`) + the three Command-API verbs (§5) with server-mint + the principal→source matrix. Tests: no machine row enters `finding_precision_promoted`; **EVERY non-human LABEL source (`panel_proposed`, `exec_verified`, `auto_fix_landed`, `review_raised`, `auto_git_revert`) fails to remove a finding from `finding_labelqueue`** (only a human label or TTL-tombstone does); a repeat/correction human confirm resolves to ONE current label (no double-count); exec/labeler identities are DENIED `confirm_outcome`; the idempotency matrix (same=noop, diff-payload=insert, cross-source no-shadow); `0009` widens (never breaks) existing rows; no consumer reads `_raw`. **No labeler.**
- **PR-2 (exec prior):** play-fix full-fix OBSERVE mode → `record_exec_prior`. Passive default.
- **PR-3 (panel + human batch):** scheduled decorrelated sweep → `propose_outcome` + cluster/rank + phone-first bulk-confirm UI + `labeler_accuracy` audit.
- **PR-4 (active knob):** `$ORCH/LABELER_ACTIVE` + per-day budget — Jefe's compute opt-in.
- **Deploy:** Mini `git pull` + kickstart (0009 auto-applies) + `$ORCH` cp of the sweep script.

## 13. Open questions (v0.4 — minimal)

1. Final airtightness confirm: with the label-vs-lifecycle split, the complete source×outcome algebra, current-not-rows aggregation, and no `_resolved` view — can any machine LABEL still reach `finding_precision_promoted` or remove a finding from `finding_labelqueue`?
2. **D4 (Jefe's, not derivable):** active vs passive default probe.
