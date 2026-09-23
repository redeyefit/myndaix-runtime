# Review dedup — stop paying twice for the same review (design, 2026-09-23)

**Status:** DRAFT for cross-family review. Scope approved by Jefe 2026-09-23 (items 1–3 + doc rule).
**Goal:** cut redundant agent calls without cutting review coverage. Every change fails TOWARD
reviewing: any lookup/post error = behave exactly as today.

## Evidence (ledgers + inbox, 7 days to 2026-09-23)

| Source | Volume | Redundant because |
|---|---|---|
| Pre-flight canaries (`reply with exactly: READY`) | MacBook **482 of 991** kilabz+lobster+oracle calls; Mini 90 of 184 | fires seconds before a real call that surfaces the same failure; lobster canaries are Claude-budget calls |
| Post-merge `main` backstop reviews | **33** (runtime 20, FV 13) + 13 autofix-branch reviews spawned off them | both repos enforce PR-only main (`enforce_admins: true`); the merged code was already push-reviewed on its branch. Sampled re-finds: 78418 re-flagged `test-deploy-sync.sh:117` from branch review 30517 |
| Workflow-worktree pushes keyed `agent-<id>` | **24** reviews (29 FV inbox files) | not redundant — MIS-KEYED: `repo_id = basename(worktree dir)`, so per-repo markers/fold/outcomes miss them |
| Manual re-reviews of already push-reviewed branches | ~8 | the pre-push hook already reviews every push |

## 1. Lazy canary (push mode only)

- Stamp `$STATE/agent-ok-<agent>` (plain `touch`, mtime = signal) after ANY successful `call()`
  for that agent — canary or real call.
- Before the canary loop: skip an agent's canary when its stamp is younger than
  `PLAY_CANARY_TTL` (default 600s; `0` = always canary = today's behavior).
- **Failure semantics preserved:** if a REAL call (review / triage) fails for an agent whose
  canary was skipped, run that canary THEN. Canary fails → `abort canary` (the existing
  transient/refund path); canary passes → the original abort class stands. So a dead agent is
  still classified transient, never a poison-head ceiling hit.
- Gate mode (automerge) unchanged — keeps its always-canary fail-closed contract.
- Mtime read: `stat -f %m` (macOS) — both machines are macOS; a stat failure = stamp absent.

## 2. Backstop skips PRs already covered by push review

**The coverage fact that shapes this:** push review is INCREMENTAL (`play-review.sh:247`,
existing branch → `remotesha..localsha`; fold_walk only extends over SKIPPED ranges, and an
ABORTED range is lost — backlog "range-from-last-REVIEWED-sha"). So "the PR head was reviewed"
does NOT imply "the whole PR was reviewed". Coverage must be a CHAIN.

**Signal — a GitHub commit status** (boring shared substrate: the two ledgers are
machine-local, GitHub is the one record both machines see). Context `myndaix/reviewed`,
state `success`, description `<VERDICT> play <id>`. Commit statuses are a different API from
check-runs, which is all automerge's `_ci_green` reads (`automerge.py:355`) — no interference.

**Post rule (play-review worker, push mode, verdict ∈ {PASS, NEEDS-FIX} only):** post on `tip`
iff the reviewed range's `base` is itself covered — `base` is an ancestor of the remote trunk
(new branch reviewed from its merge-base), OR `base` already carries `myndaix/reviewed`.
Inductive: a status on X ⇒ every commit from trunk to X sat inside some delivered review.
ABORTED / SKIPPED / over-cap-fallback / EMPTY_TREE-base runs never post → chain breaks → the
backstop reviews that PR on main. Post failure is logged, never fatal (no status = review later).

**Skip rule (controller tick, after the empty-diff short-circuit, `controller.py:1032`):** walk
first-parent commits of `reviewed_sha..head` oldest-first. A commit M is COVERED iff all hold:
1. `gh api repos/<nwo>/commits/<M>/pulls` → a merged PR whose `merge_commit_sha == M`;
2. that PR's `head.sha` carries `myndaix/reviewed` (queried by exact sha);
3. `git merge-tree --write-tree M^1 <head.sha>` == `M^{tree}` — the merge added exactly the
   PR's reviewed content, no hand-resolved conflict edits (true for merge AND squash commits).
Advance the cursor (`skip_to`) across the longest covered PREFIX; the first uncovered commit
onward dispatches normally, same tick. Log each skip with the PR number. Any API/git error on a
commit = uncovered (fail toward review). Rebase-merge PRs (N rewritten commits) fail rule 1 for
all but one commit → reviewed normally (conservative, acceptable).

## 3. `repo_id` from the git common dir (plan slice 6)

`repo_id = basename(dirname(git rev-parse --path-format=absolute --git-common-dir))` at both
`play-review.sh:258` and `:311`. Identical for every normal clone and for the named worktrees
(`~/code/worktrees/*/myndaix-runtime`); only `agent-<id>` worktrees change — to the real repo.
Same root cause, same PR: `xreview.sh:98` rejects worktrees (`-d "$rp/.git"`) → test with
`git -C "$rp" rev-parse --git-dir`. (`deploy-sync` + its test have the same bug — separate, stays in backlog.)

## Doc rule (FV CLAUDE.md, plan slice 5)

Don't manually re-dispatch a CODE review for a pushed branch — read the push verdict. Design
reviews and explicit second opinions unaffected.

## Deliberately NOT built

- No change to incremental push-review ranges or reviewer effort (kilabz stays xhigh).
- No ledger/Command-API change, no new table: stamps are orchestrator state, the chain lives on GitHub.
- No backstop removal: unreviewed PRs (aborts, Dependabot, web edits, un-hooked clones) still get it.

## Failure modes & residuals (for the attack pass)

- **Status spoofing:** anyone holding a repo-write token can post `myndaix/reviewed`. Declared
  residual: the backstop is a redundancy catcher, not the security boundary (that is PR-required
  main + human merge). Builder confinement gap (memory) widens who holds the token on the Mini.
- **Semantic merge interactions:** a PR reviewed against old main, merged clean onto new main,
  is no longer re-reviewed in combination. Declared residual; the sampled main reviews re-found
  branch findings rather than interaction bugs.
- **Force-push:** new tip reviewed as `oldtip..newtip`; chain holds if `oldtip` had status.
- **Stamp staleness:** an agent dies inside the TTL → first real call fails → lazy canary
  classifies it exactly as today. Cost: one wasted real call per outage, not per review.
- **Race:** status posted after the controller tick read it → the tick reviews (extra review, not a gap).

## Test plan (the ≤15s loop: `orchestrator/test.sh` cases + a controller unit test)

Canary: fresh stamp skips; stale stamp canaries; TTL=0 canaries; skipped-canary + real-call
fail + canary fail → `transient-*` marker written. Chain: base-on-trunk posts; base-with-status
posts; base-without-status does NOT post; ABORTED never posts. Skip: covered merge advances;
missing status reviews; tree mismatch reviews; gh failure reviews; covered prefix + uncovered
tail dispatches only the tail. repo_id: a worktree resolves to the main clone's name.
