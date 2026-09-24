# Review on GitHub — make the AI verdict a required check, delete the scaffolding (design, 2026-09-24)

**Status:** DRAFT v4. Rounds 1–3 of cross-family design review are folded; see "Review log".
Remaining open items are live tests, listed as cutover gates in Rollout. Direction approved by Jefe 2026-09-24.
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
    editing branch protection, posting a status, or enabling auto-merge. That is true today
    too, and this design does not make it worse.
  - What IS in scope: an agent's well-meaning mistakes, like reaching for `--admin` on a
    blocked merge. `enforce_admins: true` makes that fail (round 3).
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
  job runs only if: head.repo == this repo AND not draft AND base.ref == main
                                                                  # never schedules fork code on the Mini;
                                                                  # main is FV's ONLY protected branch
  concurrency: review-<PR#>, cancel-in-progress                   # only the latest head is reviewed
  permissions: contents: read, statuses: write               # no pull-requests: write — nothing is commented
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
    5. POST /repos/{repo}/statuses/{H} (the event head sha, NEVER $GITHUB_SHA), context
       `mx/ai-review`, state success|failure, description in a FIXED format built by trusted code
       ("PASS" | "<n> findings" | "reviewer unavailable" + " · job <uuid>"); no target_url.
       Check the response; a failed post = job FAIL. THIS status is the required context, NOT
       the job. A skipped job posts nothing, so the status stays absent and blocks the merge.
    6. verdict BODY → the existing private delivery (play-review's deliver() to the jefe inbox,
       synced to the MacBook, relayed by Mack) + the ledger. NOTHING reviewer-generated is posted
       to GitHub: no PR comment, no check summary, no annotation. The job log prints nothing
       from reviewers; all reviewer I/O goes to the run dir + ledger.
required context `mx/ai-review` added to FV's CLASSIC branch protection, where
  enforce_admins: true already applies. NO ruleset, NO bypass actor: `gh pr merge --admin` fails
  for everyone, agents included.
```

- **Why base == main only** (round 3, HIGH: a status binds to a sha, not a base; a success on
  `H` reviewed against one base could authorize `H` retargeted to another):
  - The only diff ever certified is `merge-base(main, H)...H`, which is a function of H.
  - As main advances, that merge-base only moves forward, so the diff can only shrink. A
    success certifies a superset of what would merge.
  - A PR against another base gets no status. When it is retargeted to main, `edited` reviews it.
- **Same-sha reviewer variance:** two runs over the SAME `H` review the SAME diff, and the last
  status posted wins. That is model nondeterminism, not a different diff. Accepted as a residual;
  `cancel-in-progress` makes it rare.

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
| controller backstop + `review_cursor` + chunker | required `mx/ai-review` status (the skills indexer stays) |
| `automerge-tick/preflight` + PLAY_GATE | GitHub auto-merge (2 merges ever: `automerge_seen` 2026-09-24) |
| `deploy-sync.sh` + trusted-install verification | main-branch workflow — ONLY after an audit shows what still supplies installed `mxr`/profiles |
| autofix: `autofix-arm.sh`, `play-fix.sh`, `fix-sweep.sh`, `autofix_fire()` + their launchd registrations | nothing; its only trigger dies with the backstop |

Per-repo cutover removes only that repo's hook, backstop watch and registrations. **Build
checklist:** every launchd job, installed hook, queued job and caller of each deleted piece,
retired together with it.

**Kept:** the pool/ledger/Command API, outcome labeling, substrate deploy, the doorbell, and
**the jefe-inbox delivery (deliver() + Syncthing)**. The inbox is the private channel for verdict
bodies (round 3: GitHub comments are not private; see Security).

## Security surface

- **Untrusted:** the PR diff and head blobs, and the PR title, body and branch name.
- **Never interpolate `${{ github.event.* }}` into `run:`**; pass values via `env:`. Pin
  third-party actions by SHA.
- **No execution of PR content by git:** no checkout, worktree or archive of the head; hooks,
  fsmonitor, external diff and textconv are disabled; no submodule or LFS recursion.
  The reviewer's snapshot is the existing `mxr review` staging seam, and the build must specify
  and test its exact command, symlink handling (no dereference outside the snapshot) and config
  discovery (AGENTS.md / project config in the snapshot).
- **Verdict bodies never go to GitHub, even on a private repo.** GitHub renders external images
  in private comments through its Camo proxy, so a reviewer induced to encode a secret into an
  image URL would get an outbound channel (round 3). The status description is plain text built
  by trusted code; the only reviewer-derived value in it is an integer.
- **Accepted residuals under the threat model:**
  - A reviewer in codex's read-only sandbox can still RUN readable scripts and READ what `jefe`
    can read. Its output goes only to the ledger and the private inbox.
  - The pass/fail bit and the finding count are low-bandwidth channels, visible only to repo
    collaborators (Jefe).
  - Name collision: a check or status named `mx/ai-review` from another producer could satisfy
    the requirement. GitHub can't pin a context to one workflow. Mitigations: an unusual name,
    plus a cutover gate test (absent status + same-name job).
  - An agent that DELIBERATELY posts a status or edits branch protection with Jefe's admin token.
    Accidental `--admin` merges are blocked by `enforce_admins`. Deliberate API misuse is the
    hostile case the bot-account rung fixes.
  - Confinement (curator pattern) and the agent bot account are the later fixes.
- **Stale comment:** `ci.yml`'s ACCEPTED RISK note says runtime is "private". Fix it in the build
  PR, or it becomes true when Jefe flips visibility.

## Failure modes

- **Reviewer unavailable** (credits, pool, canary, auth, Mini offline): no `success` status, so the
  merge waits (fail-closed).
  - **Escape hatch (Jefe only, manual, audit-logged):** remove `mx/ai-review` from FV's required
    contexts in Settings, merge what must merge, then re-add it. This drops the gate
    repo-wide for the outage window, but during an outage no review can run anyway.
  - The other classic checks (`web`, `security-test`) stay required throughout. The hatch is
    AI-review-only (round 3: a ruleset bypass could never clear classic checks, so the v3
    ruleset was contradictory and is deleted).
  - Why not a standing bypass: round 3 showed a well-meaning agent reaching for `--admin` on a
    blocked merge is an ACCIDENT under this threat model. `enforce_admins` makes that fail.
- **Dependabot PRs:** reviewed like any other. Test token/secret behavior under
  `pull_request_target` in the pilot; never exempt them blindly.
- **Base retarget** (`edited`), force-push, rebase: a new run for the current head. Draft→ready:
  `ready_for_review` runs it.

## Rollout (one repo at a time)

**Prerequisites (shared runtime work, before FV step 1):** #117 structured verdict; the `mxr
review` staging seam specified and tested (exact snapshot command, no symlink dereference outside
the snapshot, AGENTS.md / project config discovery); `mx/ai-review` posting via the event head sha.

1. FV: land `review.yml` NOT required. Shadow it on the next 3 PRs next to the pre-push review
   (during shadow both deliver to the inbox; that doubling is intended).
2. **Cutover gate tests** — every one must show its expected outcome on a throwaway PR:

   | Case | Expected |
   |---|---|
   | No `mx/ai-review` status | merge blocked |
   | Same-name JOB green or skipped, genuine status absent | blocked; if it satisfies the gate, rename or pin before cutover |
   | Status on the head sha vs a same-name result on the test-merge sha | record which GitHub evaluates |
   | Old success on H1, new head H2 | blocked until H2 is reviewed |
   | PR opened against a non-main base, then retargeted to main | no status until `edited` review |
   | draft → ready at the same head | `ready_for_review` run posts the status |
   | kilabz PASS + oracle failed / kilabz failed + oracle PASS / zero findings from a FAILED kilabz job / wrong sha / malformed / empty / truncated | PASS, FAIL, FAIL, FAIL, FAIL, FAIL, FAIL |
   | Dependabot PR | status posts (or the restriction is documented and handled) |
   | `gh pr merge --admin` on a blocked PR | refused |
   | git hooks / attributes / submodule / LFS / symlink in the head | nothing executes, nothing outside the snapshot is read |

3. FV: add `mx/ai-review` to classic required contexts; turn on repo auto-merge.
4. FV: remove its pre-push hook and its backstop watch, plus its registrations and queued work.
   Runtime's push review and all shared machinery keep running (mixed state is expected).
5. Runtime: only after it is private, repeat 1–4. Then delete the shared code (last consumer).
   "One ledger, one label queue" is this FINAL state, not the FV-only pilot state.

## Deliberately NOT built

No chunking, cursor, backstop, override label, merge queue, new table, or Command-API change.
**Not Codex's native `@codex review`:** comment-only, separately billed, and it can't write our
ledger. **Not an agent bot account** yet (a named later rung).
**Removed across rounds:**
- The `destructive-blocker` deny of `gh pr merge --admin` (round 2: theater; `enforce_admins`
  does this job for real, round 3).
- The claims "verdict body never leaves the ledger" and "human-only opt-in" (round 2).
- The ruleset and its admin bypass; PR comments and gitleaks-on-verdict (round 3).

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
- **Round 3 (both families), NOT PASS, no CRITICAL:**
  - HIGH status binds to sha, not base → review only base == main (FV's only protected branch);
    the certified diff is a function of H and only shrinks as main advances.
  - HIGH context collision → `mx/ai-review` name + a cutover gate test.
  - HIGH accidental admin use → no ruleset, no bypass; `mx/ai-review` joins classic protection
    under `enforce_admins`. Escape hatch = Jefe removes the context in Settings.
  - HIGH Camo image channel in private comments → no reviewer text on GitHub at all; bodies go
    to the existing private inbox.
  - HIGH inbox double-notify during the pilot → REFUTED: only play-review/play-fix write the
    inbox, and the workflow calls the pool directly (shadow doubling is intended).
  - HIGH legacy automerge racing native auto-merge on FV → REFUTED: `automerge_seen` on the
    Mini has 0 FV rows ever; automerge only acts on runtime.
  - MED ruleset vs classic contradiction → closed by deleting the ruleset.
  - MED deferred closures → named cutover gate tests (Rollout step 2).
  - Accepted: same-sha reviewer variance (last status wins; same diff).

## Still next, independent of this design

The "honest checks" PR: `orchestrator/test.sh` has 9 failures on main and isn't in CI. Also sweep
the `! grep -q … && ok || bad` assertions.
