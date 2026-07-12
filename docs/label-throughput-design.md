# Label-throughput — human bulk-confirm, terminal-first (PR-A of the self-labeling rollout)

**DESIGN v0.1 — 2026-07-12.** Child of `docs/self-labeling-design.md` v0.4 (branch
`design/self-labeling-system`, dual-family APPROVED): this is the buildable spec for the HUMAN
side of its §6 confirmation stage, re-scoped against the shipped code (fence = migration 0010,
PR #81; lifecycle fixes = 0011, PR #84) and the measured live state.

## 1. Why now, and why THIS first

The bottleneck is measured, not theoretical: the Mini's `finding_labelqueue` is **42 deep,
growing ~6/day, with 2 human labels total** — and the acting rungs gate on
`finding_precision_promoted`, which reads ONLY human labels. Two grounding facts flip the v0.4
rollout order (PR-2 exec-prior before PR-3 human batch):

1. **`confirm_outcome` is unreachable.** It is the ONLY writer of
   (`human_confirm`,`confirmed_real`) — the numerator of the gating precision — and it has NO
   CLI wiring (`postgres_store.py:1662` has zero callers). The keys-file offers only
   `fp|wontfix`. As shipped, promoted precision can never exceed 0/n: the flywheel cannot
   produce the very number the autonomy ladder gates on.
2. **PR-2's substrate does not exist.** On the auto path the regression check NEVER runs
   (autofix requires `fail_to_pass:null` in repos.json — play-review.sh:263-264 — and play-fix
   with a null proof finishes UNVERIFIED at play-fix.sh:318 BEFORE the verify suite). play-fix
   receives no finding keys (the fixlist is free-text triage — play-review.sh:603), and its
   verify signal is per-PATCH, not per-finding. Exec-prior today = heavy new plumbing for a
   non-gating prior with ~zero firings.

So PR-A = make the human's labels cheap and complete, on the substrate that is 100% ready.
Throughput target (inherited): **~3–5 batch commands/week, not 42 taps.**

## 2. What PR-A builds (4 small pieces, one PR)

### 2a. `real` — wire the confirm path
`mxr outcome <key12> real` joins `fp|wontfix`. Routes to the EXISTING
`confirm_outcome(prefix, "all", kind, principal_role="admin")`.
- **All three kinds route through `confirm_outcome`** (D-1 below): one verb, one semantics.
  `human_dismiss` stays for API/back-compat, but the CLI stops calling it. Behavioral delta
  (deliberate): `confirm_outcome` writes for a finding in ANY `finding_current` state — so a
  human can confirm-real a finding that already auto-closed (`applied_fixed`) or expired.
  That is CORRECT: post-hoc human ground truth on a closed finding is exactly what the
  promoted metric wants. `fp` on an expired finding likewise records truth (was-wrong) without
  resurrecting anything (labelqueue membership is human-label-terminal; 0011 toggle unaffected).
- Prefix resolution, ambiguity refusal, idempotent re-issue: inherited unchanged from the verb.

### 2b. Multi-key batch — same verb, N keys
`mxr outcome <kind> <key12> [<key12>...]` (kind-first when multiple keys; the existing
`<key12> <kind>` single form stays). Loops the single-key verb per key — no new ledger verb, no
new transaction shape.
- **Per-key fail-closed, batch fail-open:** an ambiguous/unknown key refuses THAT key (prints
  candidates) and continues; the summary line reports `labeled N, refused M`. Exit 0 always
  (operator-retry, per the outcome verb's tier-3 discipline).
- **Explicit keys ONLY** (D-2): every labeled key was enumerated by the human (typed/pasted).
  NO `--tag`/`--play`/`--all` selector bulk in PR-A — a selector labels rows the human never
  saw, which is exactly the authority-laundering D3 forbids without shown evidence. When the
  panel (PR-C) exists to supply concurrence evidence, selector-bulk gets its own reviewed PR.
- Batch cap 200 keys/invocation (queue is 42; the cap is a fat-finger guard, not a quota).

### 2c. `mxr labelqueue` — the evidence surface (read-only)
The terminal edition of v0.4's "cluster/rank + audit sample shown". Reads
`finding_labelqueue` ⋈ latest open row, clustered by (`rule_tag`, `reviewer_family`), ordered
by cluster size desc:

```
unsanitized-injection  oracle   6   4822…  src/runtime/curate.py  …
                                key12s: 383c0f  5d1adc  386fa1  37b10c  ab24c6  8ccd99
```

- Per cluster: count, the key12s (paste-ready for 2b), paths, and `ref`/`play` of the latest
  raise (so the human can pull the verdict file's claim text if wanted).
- Read-only SQL over existing views — no new view, no materialization (v0.4 §4: UI joins are
  request-time, never core views).
- Fail-closed on DB unreachable (exit 2, operator command tier — mirrors knowledge verbs).

### 2d. Keys-file completes the label set
`play-review.sh outcomes_record` adds the third paste-ready line per finding:
`mxr outcome <key12> real      # reviewer was RIGHT — confirmed ground truth`
plus one batch hint at the bottom: `# batch: mxr outcome real <key12> <key12> …`.
(One printf format change + the hint line; fail-open discipline unchanged.)

## 3. Deliberately NOT in PR-A (each with its un-gating condition)

| Deferred | Why | Builds when |
|---|---|---|
| PR-B exec-prior (play-fix observe → `record_exec_prior`) | no auto-path firings (fail_to_pass null), no per-finding linkage; non-gating prior | a repo gains a real `fail_to_pass` proof AND the fixlist carries finding keys (its own plumbing PR) |
| PR-C panel (`propose_outcome` sweep + cluster/rank by uncertainty) | paid decorrelated sweeps; evidence pane for selector-bulk | after PR-A ships and the queue's shape (which classes stay contested) says panels earn their spend |
| Selector-bulk (`--tag`/`--play`) | labels unseen rows; needs shown concurrence evidence (D3) | with PR-C |
| Phone-first UI | it's the Telegram transport build (docs/telegram-transport-design.md v0.2, pre-oracle, not built) | its own design gauntlet + build |
| `labeler_accuracy` view | audits machine labelers; none exist yet | with PR-B/PR-C |

## 4. Data flow

```
review (play-review) ──finding:<tag>@path:line──▶ outcome-record ──▶ finding_outcome (open)
                                                        │
                                              keys-file → jefe inbox (fp|wontfix|real ×3 + batch hint)
                                                        │
human reads (Mack relays / labelqueue browser) ──▶ mxr outcome <kind> <keys…>
                                                        │
                                          confirm_outcome (admin) ──▶ human_confirm/human_dismiss rows
                                                        │
                                      finding_current_human ──▶ finding_precision_promoted (THE gate)
                                                        │
                                            finding_labelqueue shrinks (human-label-terminal)
```

## 5. Failure modes & edges
- **Ambiguous key12 in a batch** → that key refused with candidates, batch continues (per-key
  fail-closed). Grinding a collision yields refusals, never a mislabel (inherited).
- **Same-kind re-issue / batch re-run** → idempotent no-op rows (5-col index), count reports 0.
- **Kind correction (fp→real)** → new row, higher seq, `finding_current_human` takes the latest
  (inherited correction semantics). Known residual (documented in verb): exact flip-flop BACK
  to a prior kind can't re-win — acceptable at solo scale, unchanged by PR-A.
- **`real` on never-raised key** → `_finding_fields` returns None → error surfaced, no row.
- **DB down** → labeling verb prints the error and exits 0 having written nothing (tier-3);
  `labelqueue` exits 2 (operator tier).
- **Concurrent TTL sweep during a batch** → rows are append-only + idempotent; a finding
  expiring mid-batch still takes the human label (human rows are terminal; expiry only matters
  for queue membership, and human-label wins the membership test).

## 6. Security surface
- **No new trust boundary.** Local shell = trusted operator (existing CLI contract,
  postgres_store.py:1642-1648); the CLI hard-codes `principal_role="admin"` exactly as
  `mxr outcome` does today. No new network surface, no new inputs from untrusted sources.
- Keys arrive as argv hex tokens, validated ≥12-hex per key before any DB touch; junk refuses
  per-key. The keys-file remains outbound-only (loop-immunity invariant untouched — no agent
  reads the inbox).
- The fence is untouched: PR-A writes only through the two human verbs already gated + pair-CHECKed.

## 7. Decisions for Jefe
- **D-1 [lean yes]:** route CLI `fp|wontfix` through `confirm_outcome` too (uniform semantics;
  the any-state delta is argued correct in 2a). Alternative: keep `human_dismiss` for fp/wontfix
  and add only `real` via `confirm_outcome` — zero behavioral delta, two code paths forever.
- **D-2 [strong]:** explicit-keys-only bulk in PR-A; selector-bulk waits for panel evidence.

## 8. Test plan (test-first, deterministic)
Extend `tests/test_self_labeling_fence.py` + a new CLI-level check set:
confirm-real lands in promoted numerator; real-after-applied_fixed writes; fp-after-expired
writes without resurrecting queue membership; batch = mixed valid/ambiguous/junk keys → per-key
refusals + correct count; batch re-run idempotent; labelqueue clustering matches a seeded queue;
keys-file carries all three commands (orchestrator/test.sh).
