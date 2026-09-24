# Review on GitHub — make the AI verdict a required check, delete the scaffolding (design, 2026-09-24)

**Status:** DRAFT v3, for round 3 of cross-family design review. Rounds 1 and 2 are folded; see
"Review log". Direction approved by Jefe 2026-09-24.
**Supersedes** `review-subtraction-design.md` (NOT PASS, 2 blockers). Its blocker 1 was "a human
can click Merge on a PR the AI rejected; the backstop is the only catch". This design closes that
gap with a GitHub primitive instead of keeping the backstop.

## Threat model (read first; round 2 showed v2 claimed more than it could back)

- **What the gate IS for: ACCIDENTS.** Code merges unreviewed because the reviewer was down, or
  a verdict went unread. This happened on 2026-09-23:
  - Codex ran out of credits twice (~23:00–08:00 and ~16:00–22:30).
  - FV #151 and #145 (both security) merged at 07:51/07:56 while every kilabz canary failed.
  - The MacBook's Codex login file was lost when the disk filled, so push reviews ran with no gate.
  - Today every one of these fails OPEN. Under this design each one holds the merge.
- **What the gate is NOT for: a hostile actor holding Jefe's GitHub identity.**
  - Agents run `gh` as Jefe (admin). Anything "only Jefe can do" is a convention for them: the
    admin bypass, enabling auto-merge, or adding a workflow that reports a `review` check.
    That is true today too, and this design does not make it worse.
  - The real fix is a separate non-admin GitHub account for agents. That is a later rung, named
    here and not built.
- **Authors are same-repo only** (Jefe plus his agents). Injected content can reach a reviewer
  only through something an agent committed.

## Why

Most of the review loop re-derives things GitHub already knows or already enforces:
- which range to review (push-hook range math, cursor, chunker);
- whether reviewed code landed (backstop);
- how to merge on green (automerge tick);
- how to show a verdict (inbox relay).

Evidence:
- **kilabz on PR branches is the load-bearing part:** 481 findings confirmed or fixed in 7 days,
  3 labeled fp.
- **The post-merge backstop re-reviewed merged code 33×/week** (runtime 20, FV 13).
- **Push-mode lobster synthesis kept 17 of 18 oracle findings later labeled fp.**
- **Labels (MacBook ledger, lifetime): 53% of 1,702 findings never got a human label**
  (expired or still open).
  - oracle raised 60% of all findings, and 33% of its human-labeled ones were dismissed vs
    kilabz 11%.
  - The backstop's 229 findings sit in a SECOND ledger (the Mini), a separate queue for
    already-reviewed code.
  - Noisy volume plus split ledgers is why labels lag.

## Verified substrate (2026-09-24)

- **`main` protection:** PR required and `enforce_admins: true` on both repos. Required checks are
  `test` (runtime) and `web` + `security-test` (FV). Direct pushes to `main` are blocked.
- **Self-hosted runners on the Mini, running as user `jefe`:** `mini-fieldvision{,-2}` (FV) and
  `jefes-mac-mini` (runtime). `codex` 0.154 (logged in), the pool (`mxr`) and the oracle (agy)
  host are all on that machine.
- **Visibility:** FieldVision-iOS is PRIVATE. myndaix-runtime is PUBLIC today; Jefe will make it
  private later (0 stars, forks or users). Fork-PR approval is `all_external_contributors`.
- **GitHub docs:**
  - `pull_request_target` runs regardless of fork-approval settings.
  - **A skipped job reports success to a required check.**
  - Public-repo Actions logs are public.
  - Merge queue is org-only (not needed).

## Scope: FieldVision first; runtime after it goes private

The public-log exfil channel (round 2, CRITICAL 1) and every fork finding exist only because
runtime is public. **Pilot on FV** (private): it is where the unreviewed security merges happened
and where most review spend goes. **Runtime cuts over only after it is private.** Until then it
keeps today's push review, and nothing is deleted that runtime still uses.

## Design

One workflow per repo, `.github/workflows/review.yml`, on that repo's Mini runner.

```
pull_request_target [opened, synchronize, reopened, ready_for_review, edited]  # workflow comes from MAIN
  job runs only if: head.repo == this repo AND not draft          # never schedules fork code on the Mini
  concurrency: review-<PR#>, cancel-in-progress                   # only the latest head is reviewed
  permissions: contents: read, statuses: write, pull-requests: write
  steps (every script from the BASE checkout; PR content is data, never executed):
    1. H = event head sha. git fetch <H> into a private ref; fetched sha must == H, else FAIL
    2. diff = git -c core.hooksPath=/dev/null -c core.fsmonitor=false
              diff --no-ext-diff --no-textconv <merge-base>...H     # rc checked -> FAIL on error
       no checkout, no worktree, no git archive, no submodule/LFS recursion
       binary / submodule-pointer / unsupported content in the diff -> FAIL "needs human review"
       over the size budget -> FAIL "split the PR"
    3. submit to the pool bound to H: mxr review (kilabz gate) + oracle (advisory)
    4. RESULT CONTRACT (anything unrecognized = FAIL):
         PASS  iff the pool job status = done AND kilabz returns a VALID structured verdict (#117)
               with zero findings AND the verdict's sha == H
         FAIL  on findings, timeout, cancel, failed or empty or malformed or truncated verdict,
               or a sha mismatch
         oracle: advisory; its failure or findings never veto (body carries them for Jefe)
    5. post commit STATUS `ai-review` on H (success | failure) — THIS is the required context,
       NOT the job. A skipped job posts nothing, so the status stays absent and blocks the merge.
       Forks and drafts therefore can never satisfy the gate.
    6. private repo: post one PR comment (verdict body + H), gitleaks'd first (belt).
       The job log prints NOTHING from reviewers: all reviewer I/O goes to the run dir + ledger.
required context `ai-review` in a ruleset; bypass = repository admin, "pull requests only"
  (verify live on a throwaway PR at build). Classic protection is unchanged.
```

- **Why a commit status, not the job, is the required check** (round 2, HIGH): a skipped job
  counts as success. With the status, absent means blocking, and it binds to the exact sha reviewed.
- **Full PR diff, never incremental.** Fold pushes change callers of earlier pushes, and full-diff
  needs no stored state. Cost is held by `cancel-in-progress` plus draft skip.
- **Late or cancelled pool jobs** can only post a status on THEIR sha. The required check binds to
  the PR's current head, so a late result cannot satisfy a newer head. (Pool cancellation is not
  guaranteed, so a late job wastes cost but cannot break correctness.)
- **One ledger, one label queue:** every review runs on the Mini, so every finding lands in the
  Mini ledger. That ends today's MacBook/Mini split.
- **Reviewer reuse, not a rewrite:** the pool, profiles, canary and timeouts stay as they are
  (invariants 1 and 4). **#117 (schema-enforced kilabz verdict) becomes a prerequisite**, taken
  off hold.
- **No lobster synthesis**, carried over from the subtraction design (item 2).

## Deleted after BOTH repos cut over (shared code goes when its last consumer does)

| Removed | Replaced by |
|---|---|
| pre-push hook + `play-review.sh` FRONT (trunk_ref/lag/fold_walk, lock/contention/STALE) | `pull_request_target` + concurrency |
| controller backstop + `review_cursor` + chunker | required `ai-review` status (the skills indexer stays) |
| `automerge-tick/preflight` + PLAY_GATE | GitHub auto-merge (2 merges ever: `automerge_seen` 2026-09-24) |
| `deploy-sync.sh` + trusted-install verification | main-branch workflow — ONLY after an audit shows what still supplies installed `mxr`/profiles |
| inbox relay of routine verdicts | status + PR comment; Mack reads via `gh`/`mxr get` |
| autofix: `autofix-arm.sh`, `play-fix.sh`, `fix-sweep.sh`, `autofix_fire()` + their launchd registrations | nothing; its only trigger dies with the backstop |

Per-repo cutover removes only that repo's hook, backstop watch and registrations. **Build
checklist:** every launchd job, installed hook, queued job and caller of each deleted piece,
retired together with it.

**Kept:** the pool/ledger/Command API, outcome labeling, substrate deploy, the doorbell for
non-review alerts.

## Security surface

- **Untrusted:** the PR diff and head blobs, and the PR title, body and branch name.
- **Never interpolate `${{ github.event.* }}` into `run:`**; pass values via `env:`. Pin
  third-party actions by SHA.
- **No execution of PR content by git:** no checkout, worktree or archive of the head; hooks,
  fsmonitor, external diff and textconv are disabled; no submodule or LFS recursion.
  The reviewer's snapshot is the existing `mxr review` staging seam, and the build must specify
  and test its exact command, symlink handling (no dereference outside the snapshot) and config
  discovery (AGENTS.md / project config in the snapshot).
- **Accepted residuals under the threat model:**
  - A reviewer in codex's read-only sandbox can still RUN readable scripts and READ what `jefe`
    can read. On FV (private) the outputs reach only Jefe.
  - The pass/fail bit is a 1-bit channel.
  - A same-repo workflow could forge `ai-review`.
  - Confinement (curator pattern) and the agent bot account are the later fixes.
- **Stale comment:** `ci.yml`'s ACCEPTED RISK note says runtime is "private". Fix it in the build
  PR, or it becomes true when Jefe flips visibility.

## Failure modes

- **Reviewer unavailable** (credits, pool, canary, auth, Mini offline): no `success` status, so the
  merge waits (fail-closed). Escape hatch: the ruleset's admin bypass on that one PR.
  - The bypass must also clear FV's classic-protection checks if those are red. Verify how the
    layering behaves live (round 2).
- **Dependabot PRs:** reviewed like any other. Test token/secret behavior under
  `pull_request_target` in the pilot; never exempt them blindly.
- **Base retarget** (`edited`), force-push, rebase: a new run for the current head. Draft→ready:
  `ready_for_review` runs it.

## Rollout (one repo at a time)

1. FV: land `review.yml` NOT required. Shadow it on the next 3 PRs next to the pre-push review;
   compare verdicts, costs and the status-binding tests.
   - Adversarial tests: fork-like skip, draft→ready, retarget, force-push mid-run, late result,
     malformed verdict, a same-name job from a PR workflow.
2. FV: require `ai-review` via the ruleset; verify the bypass on a throwaway PR; turn on repo
   auto-merge.
3. FV: remove its pre-push hook and backstop watch.
4. Runtime: only after it is private, repeat 1–3. Then delete the shared code.

## Deliberately NOT built

No chunking, cursor, backstop, override label, merge queue, new table, or Command-API change.
**Not Codex's native `@codex review`:** comment-only, separately billed, and it can't write our
ledger. **Not an agent bot account** yet (a named later rung).
**Removed from v2:**
- The `destructive-blocker` deny of `gh pr merge --admin` (round 2: theater; the API is
  reachable other ways).
- The claims "verdict body never leaves the ledger" and "human-only opt-in".

## Review log

- **Round 1 (oracle only; kilabz was logged out):**
  - obfuscated exfil → don't publish;
  - fork PRs → intended;
  - no override → ruleset bypass;
  - automerge → refuted by data;
  - full diff;
  - autofix → delete.
- **Round 2 (both families), NOT PASS:**
  - CRIT public-log exfil → scope to private repos (FV now, runtime after the flip) + silent logs.
  - CRIT shared identity → explicit threat model; bypass hook removed; bot account named as
    later.
  - HIGH skipped-job success → commit status `ai-review` is the required context.
  - HIGH fork approval → fact recorded.
  - HIGH git execution → hardened diff, no checkout, seam to be specified.
  - HIGH result contract → PASS only on a valid #117 verdict bound to H.
  - HIGH coverage/revision → sha binding, `edited`, unsupported content = FAIL.
  - MED human-only auto-merge → claim removed.
  - MED Dependabot → pilot test.
  - MED deletion audit → build checklist; shared code last.
  - Oracle's round-1 "confirmed closed" list was overstated; those cases are now pilot tests.

## Still next, independent of this design

The "honest checks" PR: `orchestrator/test.sh` has 9 failures on main and isn't in CI. Also sweep
the `! grep -q … && ok || bad` assertions.
