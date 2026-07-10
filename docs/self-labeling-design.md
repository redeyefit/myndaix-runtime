# Self-labeling system — safe automation of the outcomes-ledger labeling flywheel (design v0.3)

**Status:** DESIGN v0.3 — folded dual-family r2 (both: axis is right, close the remaining leaks → then PR-1 is buildable). Precise schema/view/authority contracts below. NOT built.
**Author:** Mack (Fable 5), 2026-07-10. **Scope:** `src/runtime/{ledger,command_api}` + labeler module + `orchestrator/` sweep + migration `0009`. NO edits to shipped 0008.

## 0. Change log

- **v0.1 → v0.2:** firewall axis moved from "objective vs model" to **"human-confirmed vs proposed."** The gate is human confirmation; the fix-probe and panel are pure labor.
- **v0.2 → v0.3 (this rev):** fold r2. (a) **Split the write verbs** — no single verb can mint both human and machine truth (confused-deputy BLOCKER). (b) Canonical label-queue is **machine-blind** — panel_proposed, exec_verified AND auto_fix_landed all leave a finding in the human queue until a human row lands (kilabz BLOCKER). (c) **Killed the sampled-audit auto-promote path** — every gating row is human-authored/confirmed; the audit only informs a cheap *bulk* human-confirm (kilabz CRITICAL / oracle Q2). (d) **Precise schema** — exact `outcome_source`/`outcome` values + which rows are gating inputs (kilabz HIGH). (e) **One idempotency rule** — `outcome_source` added to the unique tuple (kilabz HIGH). (f) Renamed the all-source current view `_resolved` (not `_promoted`) and made it private/diagnostic (oracle MODERATE / kilabz BLOCKER).

## 1. Problem (unchanged, brief)

Self-learning is stalled on **labeling throughput**: Mini accrued 37 findings since 2026-07-02, ~4 labeled, `precision` mostly `n/a`, because the only label source is a human running `mxr outcome … fp|wontfix` and Jefe won't do it continuously. FP labels are **irreducibly judgment** (you can't execute your way to "this is a non-problem"), so the human stays ground truth; the design's job is to make his judgment cheap, rare, batched, evidence-attached (~3–5 class-level batch confirms/week, not 37 taps). Enabler (agents seeing real code via the staging seam) just shipped.

## 2. Keystone (precise)

**A label gates autonomy — and removes a finding from the human queue — iff a human authored or confirmed it.** Every machine output (panel proposal, exec-probe prior, line-vanish) is fenced from BOTH the gating metric AND the canonical label-queue until a human row lands. This preserves v1's original "no LLM in the gating path" invariant intact.

The fence is a closed algebra over three columns, checkable by `grep`:
- **Gating inputs** = rows where `outcome_source ∈ {human_confirm, human_dismiss}` (both HUMAN). Nothing else counts.
- **Queue-terminal** = the same two human sources (+ `expired` TTL). No machine source is queue-terminal.
- **Write authority** = server-minted `outcome_source` + a principal→source matrix (§5); a machine identity can NEVER mint a human source.

## 3. Schema contract (migration `0009`, guarded ALTER of 0008's CHECKs — never edit 0008)

**`outcome_source`** (WHO produced the row) — existing `{review_raised, auto_fix_landed, auto_git_revert, human_dismiss, ttl_sweep}` + **new** `{panel_proposed, exec_verified, human_confirm}`.

**`outcome`** (WHAT the row asserts) — existing `{open, applied_fixed, dismissed_false_positive, dismissed_wontfix, reverted, expired}` + **new** `{confirmed_real, exec_real_prior, panel_real, panel_fp}`. Mapping (source × outcome, the only legal pairs):

| verdict | `outcome_source` | `outcome` | gating? | queue-terminal? |
|---|---|---|---|---|
| human says REAL | `human_confirm` | `confirmed_real` | **YES (numerator)** | YES |
| human says FP | `human_dismiss` | `dismissed_false_positive` | **YES (denominator)** | YES |
| human says wontfix | `human_dismiss` | `dismissed_wontfix` | no (n/a) | YES |
| exec-probe REAL prior | `exec_verified` | `exec_real_prior` | **no (prior only)** | **no** |
| panel proposes REAL | `panel_proposed` | `panel_real` | no | no |
| panel proposes FP | `panel_proposed` | `panel_fp` | no | no |
| line vanished (v1) | `auto_fix_landed` | `applied_fixed` | **no** | **no** (was queue-terminal in v1 finding_current; the new queue view ignores it) |

**Gating precision** (`finding_precision_promoted`) = `count(confirmed_real) / (count(confirmed_real) + count(dismissed_false_positive))` per `(rule_tag × reviewer_family)`, both from `human_*` sources only — the CUT lever (`applied_fixed`) and all machine outcomes are structurally absent from the fraction. There is no path for a machine outcome to enter it.

**Idempotency (one rule):** `0009` DROPs 0008's `UNIQUE(finding_key, reviewer_family, outcome, source_event)` and CREATEs `UNIQUE(finding_key, reviewer_family, outcome, outcome_source, source_event)` (adds `outcome_source`). Each verb owns a reserved server-minted `source_event` prefix (`human:`, `probe:`, `panel:`); same tuple = idempotent no-op; a differing payload uses a different event = inserts; cross-source reuse cannot collide (source is in the key) and thus can never silently shadow a human promotion.

## 4. View architecture (the three-layer fence, precise)

- **`finding_labelqueue` (new)** — the human queue + the sweep input. A finding is present iff NO `human_*` terminal row exists for it AND it is not `expired`. **All machine sources (`panel_proposed`, `exec_verified`, `auto_fix_landed`) are invisible to terminal resolution** — a machine can never remove a finding from this queue. (v1's `finding_current` is left unchanged for its existing consumers — record_findings' sticky-dismiss + the current sweep; the self-labeling pipeline reads `finding_labelqueue`, not `finding_current`.)
- **`finding_precision_promoted` (new)** — the ONLY autonomy-facing metric; reads gating inputs only (§3).
- **`finding_current_resolved` (new, private/diagnostic)** — the all-source precedence-ordered view (`human > exec_verified > auto_fix_landed > panel_proposed > open`), for the human-batch UI (JOIN the queue against priors/proposals to see what's ripe for bulk-confirm) and the accuracy audit. Explicitly NOT `_promoted`; a PR-1 test asserts no backlog/queue/acting-rung/sweep reads it.
- **`finding_precision_raw` (renamed from `finding_precision`)** — v1 all-source diagnostic, kept only as the accuracy-audit baseline; a PR-1 consumer-proof test asserts no acting rung reads it.

## 5. Write-authority (split verbs + principal→source matrix)

Three Command-API verbs, each server-minting `created_by`/`outcome_source`/`source_event` (never caller-supplied), each authorized to exactly one principal class:

| verb | principal | may mint `outcome_source` | may mint `outcome` |
|---|---|---|---|
| `confirm_outcome` | **human / admin ONLY** | `human_confirm`, `human_dismiss` | `confirmed_real`, `dismissed_false_positive`, `dismissed_wontfix` |
| `record_exec_prior` | **exec-oracle service identity ONLY** | `exec_verified` | `exec_real_prior` |
| `propose_outcome` | **labeler service identity ONLY** | `panel_proposed` | `panel_real`, `panel_fp` |

Un-bypassable server assertion: `assert (caller_class, source, outcome) ∈ MATRIX` before insert. The exec-oracle and labeler identities have NO path to `confirm_outcome` and NO generic ledger-append (kilabz/oracle BLOCKER — a machine cannot mint human-looking truth). `created_by` is not trusted verbatim; airtightness is the matrix + server-mint, mirroring the `api:<principal>` guard.

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

- **PR-1 (the fence — buildable per both families):** migration `0009` (§3 sources/outcomes + the `outcome_source`-in-tuple unique index; §4 views: `finding_labelqueue`, `finding_precision_promoted`, `finding_current_resolved`, rename→`finding_precision_raw`) + the three Command-API verbs (§5) with server-mint + the principal→source matrix. Tests: no machine row enters `finding_precision_promoted`; no machine row removes a finding from `finding_labelqueue`; exec/labeler identities are DENIED `confirm_outcome`; the idempotency matrix (same=noop, diff-payload=insert, cross-source no-shadow); no consumer reads `_raw`/`_resolved`. **No labeler.**
- **PR-2 (exec prior):** play-fix full-fix OBSERVE mode → `record_exec_prior`. Passive default.
- **PR-3 (panel + human batch):** scheduled decorrelated sweep → `propose_outcome` + cluster/rank + phone-first bulk-confirm UI + `labeler_accuracy` audit.
- **PR-4 (active knob):** `$ORCH/LABELER_ACTIVE` + per-day budget — Jefe's compute opt-in.
- **Deploy:** Mini `git pull` + kickstart (0009 auto-applies) + `$ORCH` cp of the sweep script.

## 13. Open questions (v0.3 — minimal)

1. Confirm the fence is now airtight: can any machine row reach `finding_precision_promoted` OR remove a finding from `finding_labelqueue`, through any path (views, idempotency, principal matrix, source/outcome algebra)?
2. `finding_current_resolved` is private/diagnostic — is a consumer-proof test enough, or should the human-batch UI read the queue + a proposals view directly and drop `_resolved` entirely?
3. **D4 (Jefe's, not derivable):** active vs passive default probe.
