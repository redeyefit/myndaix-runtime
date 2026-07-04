# Outcomes Precision Dials — DESIGN (v0.1)

_The self-learning ACT layer: turn the outcomes ledger's per-`(rule_tag × reviewer_family)`
precision into a deterministic **suppress/promote** signal on the review pipeline. Inputs:
`docs/outcomes-ledger-design.md` (the COLLECT rung, live+armed), `docs/research/outcomes-ledger-prior-art.md`
§E (the dial recipe), the optimal-team brief §3._

_This is the rung the ledger design deferred ("§5: the suppress/promote dial ... do NOT build now").
It is now buildable **as machinery**, but it must NOT ACT until real signal accrues — see §0._

## 0. The load-bearing constraint — SHADOW first, ACT on evidence

The ledger recorded its **first key today (2026-07-03), still unlabeled**. `finding_precision`
returns `NULL` precision for every class (denominator 0). A dial that acted now would act on an
empty dataset — the exact don't-build-on-theory anti-pattern that keep-warm / Gemini-API /
worker-watchdog all correctly failed ([[fable-week-frame]]). So the rung splits, and only the
first half is built now:

- **SHADOW / RECOMMEND (BUILD NOW):** deterministic SQL policy over `finding_precision` → a
  report of what the dial WOULD suppress/promote. Inert. Testable against synthetic rows. Arms
  instantly when signal accrues. Has nothing to recommend today — correct and expected.
- **ACT (SPEC NOW, BUILD+ARM LATER):** inject the calibration signal into the review pipeline.
  Gated on (a) a per-class human arm AND (b) ≥ `VOLUME_FLOOR` labeled observations. Never auto
  for security classes. Built only once SHADOW shows real, stable recommendations Jefe agrees with.

This mirrors every prior rung: build the mechanism, arm on accrued evidence + a human flip
(automerge, capture, outcomes-collect all shipped this way).

## 1. What & why

The ledger now measures, per `(rule_tag × reviewer_family)`, `precision = applied_fixed /
(applied_fixed + dismissed_false_positive)` and `volume` (`finding_precision` view, migration
0008). Nothing consumes it. The dial closes the loop: a family that keeps mis-flagging a class
(low precision, enough volume) should have that class **down-weighted** in future reviews; a
family with proven high precision on a class should be **up-weighted**. Deterministic, per-class,
reversible, no retraining — the prior-art §E recipe (Greptile suppress-after-N + SonarQube
precision lever + GitHub dismissal-drives-suppression).

Non-goals (explicit): no LLM in the DECISION path (thresholds are SQL); no automerge-gate
coupling (promotion never feeds auto-merge in v1 — prior-art §E human-gate); no revert signal
(reserved, shared with the ladder rung); no per-class ML; no dashboard.

## 2. The acting mechanism — a calibration NOTE, not a hard filter

Reviews are PROSE (`$review`, `$oracle_review`) plus structured `finding:` sidecar lines. You
cannot cleanly delete a suppressed finding from prose without an LLM. So the dial does NOT
hard-filter findings. It injects a **deterministic, system-computed calibration note** into the
per-family review prompt OBJECTIVE — the SAME injection point the +learning skill-hints already
use (`hint_intro` in play-review.sh), proven and safe:

- **Suppress note** (precision < floor, armed, non-security): _"Calibration: your `<tag>`
  findings have confirmed precision P=0.20 over 15 labeled cases — the operator dismissed most as
  false positives. Only raise a `<tag>` finding when you are confident; do not pad."_
- **Promote note** (precision > ceiling, armed): _"Calibration: your `<tag>` findings have
  confirmed precision P=0.95 over 20 cases — these are reliable; surface them plainly."_

The note is TRUSTED text (system-generated from allowlisted `rule_tag`s + integer counts), added
ABOVE the untrusted-diff fence, never inside it — no new injection surface (unlike the diff or
skill hints, this content is not attacker-influenced). The reviewer weights it; the ledger keeps
measuring; if suppression over-corrects (precision recovers / misses rise), the human disarms.
This is calibration-in-the-prompt, NOT LLM-in-the-recording-loop (which stays deterministic).

**Why not down-rank in triage instead?** Lobster triages prose; a triage-side note would ask
lobster to drop findings, which is (a) later in the pipeline (the reviewer already spent the
tokens) and (b) same LLM-advisory nature with less signal. Calibrating at the SOURCE (the family
that raises the class) is where the precision measurement attaches. Triage-side suppression is a
possible v2 refinement, not v1.

## 3. Data flow

```
COMPUTE (SQL, always on, inert):
  finding_precision (exists) ── finding_dial_policy (NEW view) ──▶ per (tag×family):
     recommend = 'suppress' if precision < FLOOR   and labeled >= VOLUME_FLOOR and tag not security
               = 'promote'  if precision > CEILING and labeled >= VOLUME_FLOOR
               = 'hold'     otherwise (incl. NULL precision / thin volume)   ← everything, today

SHADOW (BUILD NOW):
  mxr outcome-dials            → prints the policy view: every class, its precision/volume,
                                 the recommendation, and whether it is ARMED. Zero rows act.
                                 Rides the morning brain-check next to outcome-stats.

ACT (SPEC; built + armed later, per class):
  arm:   mxr outcome-dial arm <tag> <family> <suppress|promote>   (HUMAN only, durable flag)
         refuses unless the policy view currently recommends that action for that pair
         (can't arm a suppress the data doesn't support) AND tag not in SECURITY_TAGS for suppress
  play-review, when OUTCOMES_DIALS_ENABLED and a class is BOTH recommended AND armed:
         compute the calibration note (bounded, fail-open) and append it to that family's
         review OBJECTIVE. Fail-open: any error → no note → the review is exactly as today.
  disarm: mxr outcome-dial disarm <tag> <family>   (removes the flag; next review is un-nudged)
```

Every acting class is doubly-gated: the DATA must recommend it (auto, self-revoking if precision
recovers) AND a human must have armed it (durable, explicit). Either gate closing = no action.

## 4. Policy parameters (all env-flagged, reversible)

| Param | Default | Meaning |
|---|---|---|
| `OUTCOME_DIAL_FLOOR` | 0.30 | precision below → suppress-eligible |
| `OUTCOME_DIAL_CEILING` | 0.90 | precision above → promote-eligible |
| `OUTCOME_DIAL_VOLUME_FLOOR` | 10 | min LABELED cases (applied_fixed + dismissed_fp) before ANY recommendation — the prior-art "≥N observations" gate; below it → always 'hold' |
| `OUTCOME_DIAL_SECURITY_TAGS` | unsanitized-injection, missing-scoping, missing-file-lock, toctou-race, silent-error-suppression | never auto-suppress (a noisy security detector is still a detector); promote allowed, arm still human |
| `OUTCOMES_DIALS_ENABLED` | absent (off) | the ACT master flag (`$ORCH/…`), like OUTCOMES_ENABLED |

Volume floor uses LABELED count, not total volume: precision is only meaningful over cases that
got a fix-or-dismiss label. A class with 50 open findings and 2 labels has volume 2 for gating.

## 5. Schema / surface

No new table. One additive migration 0009 (guarded, per the migration-append-only rule — NOT an
edit to 0008): `CREATE OR REPLACE VIEW finding_dial_policy` reading `finding_precision`. Parameters
are passed to the view as… — **decision (D1 for review): thresholds live in the VERB (Python),
not the view**, so a view stays parameter-free and the flags are read once per invocation; the
view exposes raw precision/labeled/volume and the verb classifies. This keeps the flags in one
place (env) and the view a pure projection. The arm state is durable flag files under
`$ORCH/dial-arm/<tag>__<family>__<suppress|promote>` (mirrors AUTOFIX_ENABLED/OUTCOMES_ENABLED —
file-based, greppable, survives restarts, no schema).

## 6. Security surface & failure modes

- **Can an attacker force a suppression?** Suppression needs `dismissed_false_positive` labels,
  which ONLY the human creates (`mxr outcome … fp`). An attacker who can't label can't drive a
  class below the floor. `applied_fixed` is auto (fix-landed) but only RAISES precision. So the
  suppress lever is human-fed by construction; the per-class human arm + security-tag exclusion
  are belt-and-suspenders.
- **Can promotion be weaponized (e.g. to auto-merge)?** No coupling in v1 — promotion is only a
  review-prompt confidence note; it never feeds the automerge gate (that stays human-gate,
  prior-art §E). Documented as the bright line for v2.
- **Calibration-note injection?** The note is system-generated from allowlisted `rule_tag`s +
  integers, placed ABOVE the fence as trusted objective text — no attacker-controlled bytes in it.
- **Over-suppression / self-reinforcing blind spot** (the real risk): suppressing a class lowers
  its raise-rate, so it accrues fewer labels, so precision stops updating — a class could stay
  suppressed on stale evidence. Mitigations: (a) suppression is a NOTE not a hard filter, so the
  reviewer still raises high-confidence cases (labels keep trickling); (b) the SHADOW report keeps
  showing the class + its (frozen) precision so the human sees a stuck dial; (c) the arm is
  trivially reversible. AIMultiple's finding (LLM reviewers' dominant failure is false NEGATIVES)
  is why v1 dampens noise with a soft note, never a hard mute — flagged for the human to watch.
- **Availability:** the ACT path is bounded + fail-open (a slow/failed dial query → no note → the
  review runs exactly as today). SHADOW is read-only. Both HARD no-op in gate mode.
- **Thin-data false signal:** the VOLUME_FLOOR gate blocks any recommendation under N labels; the
  first weeks will show 'hold' for everything, correctly.

## 7. Borrowed / built / rejected

| Piece | Verdict |
|---|---|
| `finding_dial_policy` view + `outcome-dials` shadow verb | BUILD (one view + one read verb) |
| precision floor / promote ceiling / volume floor | BORROW (Greptile/SonarQube/GitHub §E) |
| calibration note injection (reuse skill-hint seam) | BUILD (mirror `hint_intro`) |
| per-class durable human arm (shadow→armed ladder) | BUILD (flag files, mirror OUTCOMES_ENABLED) |
| revert-rate dial | DEFER (shared with the autonomy-ladder rung) |
| automerge-gate promotion coupling | REJECT in v1 (human-gate bright line) |
| ML / embeddings / per-class model / dashboard | REJECT (deterministic SQL is the point) |

## 8. Build plan

- **PR-A (BUILD NOW): SHADOW.** Migration 0009 (`finding_dial_policy` view, additive, idempotent)
  + `finding_dials()` ledger verb (reads the view, applies flag thresholds + security exclusion +
  arm-state, returns per-class recommendation + armed bool) + `mxr outcome-dials` + tests
  (synthetic finding_outcome rows: below-floor→suppress-rec, above-ceiling→promote-rec, thin-
  volume→hold, security-tag→never-suppress-rec, NULL-precision→hold). Zero acting code. Suite-green,
  cross-family reviewed, merged, deployed. Inert until signal accrues.
- **PR-B (SPEC ONLY here; build when SHADOW shows real recommendations Jefe agrees with):** the
  arm verbs + the play-review calibration-note injection behind `OUTCOMES_DIALS_ENABLED`, per-class
  armed. Its own design pass + review when the data exists.

Deploy PR-A = normal serve-restart auto-migrate. PR-A arms NOTHING — it only lets Jefe WATCH what
the dial would do as labels accrue, which is the evidence needed to design PR-B honestly.

## 9. Open questions for review

- **D1:** thresholds in the verb (Python, env-flagged) vs parameterized into the view. Proposed:
  verb. (Keeps flags in one env place; view stays a pure projection.)
- **D2:** VOLUME_FLOOR default 10 — right for solo review volume, or too high (classes may never
  reach 10 labels in a quarter)? A lower floor (5) risks acting on noise. Revisit with real accrual.
- **D3:** should SHADOW also surface a MISS proxy (per prior-art: LLM reviewers' dominant failure
  is false negatives)? v1 has no miss signal (nothing records a bug the review MISSED). Likely a
  separate rung; flagged so promotion isn't mistaken for "optimize recall."
- **D4:** calibration-note wording — does a suppress note risk the reviewer under-raising a real
  regression in that class? The note says "when confident," not "don't raise." Review the exact text.

---

## v0.2 — cross-family + adversarial review folds (2026-07-03)

Reviewed by **kilabz** (codex), **oracle** (Gemini/Mini), and a **42-agent adversarial workflow**
(4 lenses × refuter panels; 19 raw → 10 confirmed). Verdict: **NEEDS-REVISION — 2 BLOCKERs + 5
MAJORs.** The SHADOW/ACT split and the calibration-note mechanism were BOTH endorsed by all three;
the damage is in the ACT policy, and one realization reframes the whole rung.

### The reframe (before the findings): the taxonomy has almost no suppressible class

B1/B2 below expose that the live capture taxonomy (`capture.py:43-56`, the single source of truth
`outcomes.py` imports) is **entirely correctness/security classes** — `fail-open`, `toctou-race`,
`missing-file-lock`, `unsanitized-injection`, `missing-scoping`, `silent-error-suppression`,
`migration-fail-open`, `python-in-bash-interp`, `macos-incompat`, `shared-marker-contention`,
`swiftdata-thread-safety`, `swiftui-concurrency`. Under a **fail-closed** security policy (which
B1 forces), essentially NONE of these is safe to auto-suppress — a noisy security detector is
still a detector. So the honest conclusion: **the dial's real v1 value is the MEASUREMENT (per-
class precision visibility, human-useful on its own), not auto-suppression** — which likely needs
a benign/"style-nit" taxonomy TIER that does not exist yet before it has any eligible class. This
sharpens the split: build SHADOW as a measurement surface; treat auto-ACT as a later step gated on
BOTH accrued data AND a taxonomy that has something safe to suppress.

### Findings folded

**B1 — Security-tag exclusion incomplete + not code-owned (BLOCKER; workflow ×3 + kilabz).** The
§4 default list omitted `fail-open`, `migration-fail-open`, `python-in-bash-interp` — all in the
live taxonomy. `fail-open` is the ledger's FIRST recorded finding and this codebase's most
safety-critical class; my design would have let it be armed for auto-suppression. **Fold: INVERT
to a code-owned fail-closed allowlist** — a tag is suppressible ONLY if on a small `SUPPRESSIBLE`
set defined in code; every other tag (all current ones, and any future taxonomy addition) is
protected by default. Env may ADD protection, never remove it. Given the reframe above, the
default `SUPPRESSIBLE` set is likely EMPTY until a benign tier exists.

**B2 — ACT injection omits the `! gate` guard (BLOCKER; workflow).** §6 claims gate-mode is a
no-op but the §3 injection spec never wrapped the note in `! gate` (as `outcome_intro`/`cap_intro`
already are). A suppress note could leak into the automerge gate, breaking the "no automerge
coupling" bright line. **Fold: the ACT note is HARD-gated `! gate`, explicit in the spec + a test.**

**M1 — PROMOTE is broken; CUT it from v1 (MAJOR; workflow ×2 + feedback lens).** `applied_fixed`
is written AUTOMATICALLY on line-hash disappearance (`postgres_store.py:1413-1424`) — refactor,
churn, or whole-file delete all count, per the ledger design's own known-accepted noise. A
high-churn class accrues `applied_fixed` with ZERO human labels → precision→1.0 → crosses CEILING
→ promote armed on evidence no human touched. Promote has no security brake and needs no human
label to inflate. **Fold: DROP the promote lever entirely in v1.** Suppress is the human-fed,
defended, higher-value lever; promote's marginal upside isn't worth its runaway + auto-inflation
risk. v1 is suppress-only.

**M2 — the injection seam is a SINGLE shared string, not per-family (MAJOR; workflow).**
`hint_intro`/`outcome_intro`/`cap_intro` are single bash scalars interpolated IDENTICALLY into
both the kilabz (`play-review.sh:455`) and oracle (`:476`) prompts. The precision signal is
per-family (`finding_precision GROUP BY … reviewer_family`). Reusing the shared seam would inject
kilabz's calibration into oracle's prompt and vice-versa — cross-wiring the exact per-family
signal the ledger separates. **Fold: PR-B introduces TWO NEW per-family objective variables
(`kilabz_dial_intro`, `oracle_dial_intro`); the design no longer claims to "just mirror
hint_intro."**

**M3 — the recency/accumulator trap: the dial can't self-correct (MAJOR; oracle + kilabz, the
convergent HIGH).** All-time precision `applied_fixed/(applied_fixed+dismissed_fp)` anchors a class
to its historical failures: a class that racks up 10 early FPs, gets suppressed, then calibrates
to 100% still computes `3/(3+10)=23%` and stays suppressed forever, raising too rarely to dilute
the history. **Fold: the dial policy reads a RECENCY WINDOW (last N labeled events per class, or a
decay), computed from the append-only log directly — NOT the all-time `finding_precision` view.**
This reconciles the COLLECT rung's deliberate no-window choice (§5 of the ledger design: keep all
scarce history) with the control-loop need: the RAW ledger keeps everything; the DIAL reads a
window of it.

**M4 — the dial corrupts its own measurement (MAJOR; feedback lens).** Acting on a tag changes how
the reviewer raises it, so precision-under-note stops measuring reviewer skill and measures the
note's own effect. **Fold: a PROBE channel — a sampled fraction of reviews withhold the note (the
control group) so note-free precision keeps being measured; SHADOW distinguishes note-active vs
note-free rows.** Also mandate: LOG which dials were active per review (auditability). Both go in
the ACT spec.

**Statistical stability + anti-poisoning (MEDIUM; both reviewers).** Raw precision over 10 labels
is a noisy point estimate; one misunderstood PR's 12 bulk-dismissals shouldn't trigger repo-wide
suppression, and decoy-PR FP injection could bias the ledger. **Fold into the arm gate:** require a
**Wilson lower confidence bound** below FLOOR (not the point estimate), a **minimum absolute FP
count**, and **distinct-PR / author-diversity** of the labels (not raw labeled count). SHADOW
surfaces label provenance (author/PR spread) so the human arms on a longitudinal pattern.

**Arm authority + path safety (MEDIUM; kilabz + workflow).** Flag-files + env aren't a real human
boundary. **Fold:** the security classification is code-owned/fail-closed (above); an arm records
actor + timestamp + the policy snapshot it was armed against; `rule_tag`/`reviewer_family` are
validated against the allowlist + a strict slug BEFORE they become prompt text or a flag-file path
component (no `/`/`..` in a tag reaching a path).

### Meta-conclusion

The mechanism (SHADOW measurement + calibration-note ACT) is sound and endorsed; the POLICY needed
real work, now done: **suppress-only, fail-closed code-owned security, recency-windowed, Wilson-
gated, per-family, probe-instrumented, gate-guarded.** The reframe is the bigger takeaway — at the
current all-correctness taxonomy, auto-suppression likely has NO safe class, so **v1's deliverable
is the MEASUREMENT surface** (per-class precision, human-useful for un-armed judgment), and
auto-acting waits on data AND a benign-tag tier. Recommended build: **PR-A SHADOW (measurement +
recency + provenance, zero acting)** when we choose to; **PR-B ACT** only after SHADOW shows stable
signal Jefe agrees with AND the taxonomy question is resolved. Nothing regresses meanwhile — the
un-armed backstop already delivers (it found the rev-list bug on its first review).
