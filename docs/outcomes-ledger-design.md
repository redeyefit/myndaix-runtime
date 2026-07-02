# Outcomes Ledger — DESIGN (v0.1 draft)

_The self-learning rung's missing primitive: the per-finding OUTCOME LABEL. One append-only
Postgres table + computed SQL views on the existing spine. **v1 COLLECTS ONLY — no dial acts on
the data** (suppression/promotion is a LATER rung, gated on accrued signal + human approval).
Inputs: `docs/research/outcomes-ledger-prior-art.md` (17 confirmed claims, 19 sources) + the
optimal-team brief §3. Status: draft, pre cross-family review._

## 1. What & why

Every review the brain runs today is fire-and-forget: kilabz+oracle find issues, lobster triages,
the verdict lands in the jefe inbox — and nothing ever records what HAPPENED to each finding
(fixed? dismissed as wrong? reverted after merge?). Without that label there is no ground truth,
so no precision measurement per finding-class, no evidence-based autonomy widening, no
data-driven skill capture calibration. The fix is the universal prior-art pattern (GitHub
code-scanning / Semgrep / SonarQube): a **per-finding state machine** with a **stable identity
across reviews** and a **constrained dismissal enum**.

Non-goals (v1, explicit): no auto-suppression or promotion dials acting on the data; no LLM
anywhere in the pipeline; no dashboard/UI; no per-finding thumbs-up/down labeling flow; no
embeddings/vector store; no automerge-gate coupling of any kind.

## 2. Data flow

```
push review (play-review.sh, NOT gate mode)
  └─ post-delivery, bounded + fail-open (same seam as capture-record):
     mxr outcome-record --kilabz "$review" --oracle "$oracle_review" -- <repo> <tip> <play> [changed...]
       parse per-family structured finding lines → validate → line-hash at <tip> → INSERT 'open' rows

next push review of the same repo
  └─ outcome-record FIRST closes what it can: for each 'open' finding whose path is in THIS
     diff's changed set, if the flagged line-hash no longer exists in that file at the new tip
     → INSERT 'applied_fixed' (outcome_source=auto_fix_landed)   [SonarQube auto-Fixed]

controller tick (existing hourly loop)
  └─ revert detection: a new commit on main that reverts a commit which closed a finding
     (git log --grep='This reverts commit' + parent match) → INSERT 'reverted'
     (outcome_source=auto_git_revert). Shared primitive with the autonomy-ladder addendum.

human (Jefe/Mack, one command)
  └─ mxr outcome <finding_key_prefix> fp|wontfix
     → INSERT 'dismissed_false_positive' | 'dismissed_wontfix' (outcome_source=human_dismiss)
     THE load-bearing split: fp = the reviewer was WRONG (down-weights the class);
     wontfix = the reviewer was RIGHT, human declines (does NOT down-weight).

expiry sweep (same tick)
  └─ 'open' > OUTCOME_TTL_DAYS (default 30, flagged) → INSERT 'expired'
     (keeps precision denominators honest; expired rows count toward neither side)
```

## 3. Schema (migration `0008_finding_outcome.sql` + `schema.sql` mirror, lockstep)

```sql
CREATE TABLE finding_outcome (
    id              uuid PRIMARY KEY,
    finding_key     text NOT NULL,   -- sha256(repo_id \0 rule_tag \0 line_hash) — stable identity
    repo_id         text NOT NULL,
    rule_tag        text NOT NULL,   -- allowlisted capture taxonomy (same list, single source of truth)
    reviewer_family text NOT NULL CHECK (reviewer_family IN ('kilabz','oracle')),
    path            text NOT NULL,   -- validated ∈ the reviewed diff's changed-file set
    review_run_id   text NOT NULL,   -- the play id that raised/closed it
    base_sha        text NOT NULL,   -- the reviewed tip the line-hash was computed at
    outcome         text NOT NULL CHECK (outcome IN
                     ('open','applied_fixed','dismissed_false_positive',
                      'dismissed_wontfix','reverted','expired')),
    outcome_source  text NOT NULL CHECK (outcome_source IN
                     ('review_raised','auto_fix_landed','auto_git_revert','human_dismiss','ttl_sweep')),
    created_at      timestamptz NOT NULL DEFAULT now()
);
-- append-only: NEVER UPDATE/DELETE; current state = latest row per (finding_key, reviewer_family).
-- idempotency / spam bounds:
CREATE UNIQUE INDEX finding_outcome_one_open
    ON finding_outcome (finding_key, reviewer_family, review_run_id) WHERE outcome = 'open';
CREATE UNIQUE INDEX finding_outcome_one_close
    ON finding_outcome (finding_key, reviewer_family, outcome, review_run_id);
CREATE INDEX finding_outcome_key_idx  ON finding_outcome (finding_key, created_at DESC);
CREATE INDEX finding_outcome_open_idx ON finding_outcome (repo_id, path) WHERE outcome = 'open';
```

`line_hash` (SonarQube borrow, ~20 pure lines in a new `runtime/outcomes.py`, DB-free like
`capture.py`): sha256 of the whitespace-normalized CONTENT of the flagged line as it exists at
the reviewed tip (`git show <tip>:<path>`, line N) — never the line number, so the identity
survives diff shifts. If the reviewer's `path:line` doesn't resolve at the tip (bad line, file
absent, path not in the changed set), the finding row is DROPPED (fail-closed for data quality)
and a note logged. Known aliasing: two identical lines in one file share a hash — accepted,
documented (SonarQube accepts the same).

## 4. Finding extraction — one structured line, shared with capture

Reviewers already emit `rule:<tag>` lines for the capture rung. Extend that SAME convention (one
prompt change, versioned, serving capture + outcomes + the strength-prompts upgrade):

```
rule:<tag> @ <path>:<line>
```

- Parser tolerates the bare legacy form `rule:<tag>` (capture keeps working; outcomes just can't
  key it → no row). **Build gate: capture-record's existing parser must accept the extended form
  before the prompt changes** (its regex is the compatibility contract; regression-tested).
- Per-family attribution: outcome-record parses `$review` and `$oracle_review` SEPARATELY —
  unlike capture (which records only the cross-family intersection), outcomes needs per-family
  rows because per-family precision is the whole measurement.
- lobster is triage, not a finder — no lobster rows (brief's schema pruned accordingly).

## 5. SQL views (COMPUTE only in v1 — nothing acts)

```sql
-- current state per finding = latest row per (finding_key, reviewer_family)
CREATE VIEW finding_current AS ...;
-- per (rule_tag × reviewer_family), over a 90-day window:
--   precision   = applied_fixed / NULLIF(applied_fixed + dismissed_false_positive, 0)
--   revert_rate = reverted / NULLIF(applied_fixed, 0)
--   volume      = count(*) raised
CREATE VIEW finding_precision AS ...;
```

Deferred to the LATER rung (do NOT build now): the suppress/promote dial, the weekly SQL report,
any threshold acting on these views. They arrive only after real volume accrues, human-gated.

## 6. Security surface & failure modes

- **Untrusted input:** finding lines are LLM output over an untrusted diff. Defenses (all reuse
  the capture rung's proven pieces): rule_tag must ∈ the allowlisted taxonomy; path must ∈ the
  changed-file set of THIS reviewed range (no traversal, ctrl-chars rejected on the RAW value
  before strip — capture round-2 lesson); everything passed via `sys.argv`, never interpolated;
  line resolution reads git objects (`git show tip:path`), never the working tree.
- **Availability:** recording is post-delivery, bounded by `cap_run` (perl alarm), fail-open —
  a hung DB/mxr can never delay a verdict or wedge the review lock. Default OFF
  (`$ORCH/OUTCOMES_ENABLED`), HARD no-op in gate mode (`PLAY_GATE=1`), independent of
  CAPTURE_ENABLED.
- **Spam/growth:** unique partial indexes bound rows per (finding, run); a hostile review can
  inject at most (allowlisted tags × changed files) rows per run — all inert data, nothing reads
  it for decisions in v1. TTL sweep keeps 'open' bounded.
- **Parser drift → zero rows:** fail-open means silence, not breakage; the build adds a
  row-count line to the existing brain health check so starvation is VISIBLE, not silent.
- **Revert false-positives:** revert detection scoped to commits that CLOSED a finding
  (auto_fix_landed rows), not arbitrary reverts.

## 7. Borrowed / built / rejected (from the brief §F–G)

| Piece | Verdict |
|---|---|
| append-only `finding_outcome` + views | BUILD (one table on the spine) |
| line-hash stable identity | BORROW pattern (SonarQube) |
| dismissal enum fp vs wontfix | BORROW pattern (GitHub/SonarQube) — the load-bearing split |
| auto fix-landed on next scan | BORROW pattern (SonarQube auto-Fixed) |
| reviewer-as-classifier per-family precision | BORROW pattern (LLM-judge lit) |
| fine-tuning / embeddings / vector store / dashboard / kappa stats / per-finding voting UI | REJECT |

## 8. Build plan (feature-flagged, each PR suite-green)

- **PR-A** `outcomes.py` pure (line-hash, parser, validation) + migration 0008 + schema.sql
  mirror + ledger verbs (`record_findings`, `close_fixed`, `record_revert`, `human_dismiss`,
  `expire_open`) + tests (incl. adversarial: tag/path injection, traversal, ctrl chars, dup spam).
- **PR-B** `mxr outcome-record` + `mxr outcome` verbs + play-review post-delivery wiring +
  prompt extension (after the capture-parser compatibility gate) + fixture tests.
- **PR-C** controller-tick revert detection + TTL sweep + health-check row counts.

Deploy = normal serve-restart auto-migrate; arm = `touch $ORCH/OUTCOMES_ENABLED`; disarm = rm.
