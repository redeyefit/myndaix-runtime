# Review on GitHub — make the AI verdict a required check, delete the scaffolding (design, 2026-09-24)

**Status:** DRAFT for cross-family design review (oracle leads, kilabz trust boundaries).
Direction approved by Jefe 2026-09-24 ("yes"). **Supersedes** `review-subtraction-design.md`
(NOT PASS, 2 blockers). Its blocker 1 was "a human can click Merge on a PR the AI rejected;
the backstop is the only catch". This design removes that gap with a GitHub primitive instead
of keeping the backstop.

## Why

Most of the review loop re-derives things GitHub already knows or already enforces:
- which range to review (push-hook range math, cursor, chunker);
- whether reviewed code landed (backstop);
- how to merge on green (automerge tick);
- how to show a verdict (inbox relay).

Evidence (7 days to 2026-09-23, from the subtraction design + today's ledger reads):
- **kilabz on PR branches is the load-bearing part:** 481 findings confirmed or fixed, 3 labeled fp.
- **The post-merge backstop re-reviewed merged code 33×/week** (runtime 20, FV 13) that its PR
  push had already reviewed.
- **Push-mode lobster synthesis kept 17 of 18 oracle findings later labeled fp.**
- **The gate is social today.** 2026-09-23: Codex ran out of credits twice (~23:00–08:00 and
  ~16:00–22:30). FV #151 and #145 (both security) merged at 07:51/07:56 while every kilabz
  canary was failing. A required check would have held both.

## Verified substrate (2026-09-24)

- `main` protection: PR required, `enforce_admins: true`, required check `test` (runtime). FV:
  `web` + `security-test`. Direct pushes to `main` are already blocked for everyone.
- **Self-hosted runners on the Mini, running as user `jefe`:** `jefes-mac-mini` (runtime) and
  `mini-fieldvision{,-2}` (FV). `codex` 0.154 and its auth (`~/.codex/auth.json`, 600) are on
  that host, as are the pool (`mxr`) and the oracle (agy) host.
- **`redeyefit/myndaix-runtime` is PUBLIC** (intentional, showcase); FieldVision-iOS is private.
  Fork-PR workflow approval was flipped to `all_external_contributors` on 2026-09-24.
- **Available:** auto-merge, required checks, `delete_branch_on_merge` (ON since 2026-09-24).
  **Not available:** merge queue (org-only; not needed).

## Design

One workflow per repo, `.github/workflows/review.yml`, on the Mini runner.

```
pull_request_target [opened, synchronize, reopened, ready_for_review]   # workflow comes from MAIN
  if: head.repo == this repo  AND  not draft                             # forks never reach the Mini
  concurrency: review-<PR#>, cancel-in-progress                          # only the latest push is reviewed
  permissions: contents: read, pull-requests: write
  steps (all scripts from the BASE checkout; PR code is data, never executed):
    1. fetch PR head into a private ref; diff = git diff <base>...<head>   # rc checked -> fail closed
    2. size over budget -> FAIL "split the PR"                              # no chunking, ever
    3. submit to the pool: mxr review (kilabz gate, de-linked read-only snapshot of head)
                           + oracle second opinion (agy, fenced diff)       # existing staging seam
    4. verdict = NEEDS-FIX iff a kilabz `finding:` line (oracle is advisory, as in subtraction #2)
    5. gitleaks the verdict text -> any hit: do NOT post, FAIL              # public repo
    6. post one PR review comment (verdict body + reviewed head sha)
    7. exit 0 on PASS, 1 on NEEDS-FIX or reviewer-unavailable               # this job = check `review`
branch protection: add required check `review`.  Merging on green = GitHub auto-merge (per PR).
```

- **Reviewer reuse, not a rewrite.** Steps 3–4 call the existing pool (`mxr review`, profiles,
  canary, timeouts). The ledger records jobs and outcome tags exactly as today (invariant 1;
  invariant 4, since the workflow is just another transport).
- **Trust boundary moves** from "the hand-copied installed scripts" to "`main` branch content".
  `main` is PR-only and reviewed, and `pull_request_target` always runs main's workflow and
  scripts. That is what makes `deploy-sync` + the trusted-install checks deletable.
- **No lobster synthesis** (carried over from subtraction #2). Delivered body = kilabz (gate) +
  oracle (UNVERIFIED second opinion).
- **Fail-closed diff** (carried over from subtraction #3), now in step 1.

## Deleted after cutover (per repo, runtime first)

| Removed | Replaced by |
|---|---|
| pre-push hook + `play-review.sh` FRONT (trunk_ref/lag/fold_walk, lock/contention/STALE) | `pull_request_target` + concurrency group |
| controller backstop + `review_cursor` + chunker | required check (the skills indexer stays) |
| `automerge-tick/preflight` + PLAY_GATE | GitHub auto-merge |
| `deploy-sync.sh` + trusted-install verification | main-branch workflow |
| inbox relay of routine verdicts | PR comment; Mack reads via `gh` |
| `repo_id = basename(cwd)` | `github.repository` |

**Kept:** the pool/ledger/Command API, outcome labeling (no GitHub analog; it's the learning
data), substrate deploy (not review), the doorbell for non-review alerts.
**Parked:** autofix. It fires only on main-branch reviews (#179), and those disappear with the
backstop. Revisit separately.

## Security surface

- **Untrusted:** the PR diff and head files (prompt injection into reviewers); PR title, body and
  branch name.
- **Actions injection:** never interpolate `${{ github.event.* }}` into `run:`; pass via `env:`.
- **`pull_request_target` is privileged:** no step may execute PR content (no install, build or
  test of the head; the head snapshot is read-only data for the reviewer).
- **The same-repo `if:` is mandatory, not belt.** Assumption to verify: fork-triggered
  `pull_request_target` runs are NOT held by the fork-approval setting.
- **Exfil path:** codex's read-only sandbox can still READ what `jefe` can read (auth.json,
  `.secrets`, ops DB). An injected reviewer could echo a secret into the verdict, and on a public
  repo that is publication.
  - Mitigations: gitleaks on the verdict before posting (fail closed); same-repo-only authors.
  - Residual: a non-pattern secret. Stronger fix, later: run the reviewer under the curator
    confinement pattern (scratch HOME, `--tools`, `--strict-mcp-config`).
- **Pin third-party actions by SHA** (repo has `sha_pinning_required: false`).
- **Stale comment fix:** `ci.yml`'s ACCEPTED RISK note says "private" — the repo is public; fix
  the comment in the build PR.

## Failure modes

- **Reviewer unavailable** (credits out, pool down, canary fail, Mini offline): the check fails
  or stays pending, and merges wait. That is fail-closed by design. **No override label** (agents
  run `gh` as Jefe, so they could self-override). The escape hatch is Jefe editing branch
  protection: manual, rare, visible.
- **Diff too large:** fail "split the PR". A **failing diff:** fail closed.
- **Force-push or rebase mid-review:** the run is cancelled and the new head reviewed.
- **Web-UI edit:** it's a PR, so it gets reviewed. Direct push to `main` is blocked by protection.

## Rollout (atomic per repo, runtime first)

1. Land `review.yml`, not required, and run it shadow next to the pre-push review on the next 3
   PRs. Compare verdicts and costs.
2. Make `review` required on runtime `main`; turn on repo auto-merge.
3. Remove runtime's pre-push hook and drop runtime from the controller backstop. Delete the dead
   code in a follow-up PR.
4. Repeat for FV on `mini-fieldvision`.

## Deliberately NOT built

No chunking, cursor, backstop, override label, merge queue, new table, or Command-API change.
**Not Codex's native `@codex review`:** comment-only (no evidence it can be a required check),
separately billed, and runs in OpenAI's cloud, so it can't record outcomes to our ledger.

## Open questions for review

1. `pull_request_target` + the same-repo `if:` + no-execute: is there any path where main's
   workflow still executes PR-controlled content?
2. Do fork `pull_request_target` runs bypass the outside-contributor approval gate? (Assumed yes.)
3. Full PR diff per push vs incremental since the last reviewed sha (read from the previous
   verdict comment). Per-review cost rises and review count falls. Decide from shadow data.
4. Is "no override" survivable given yesterday's two credit outages (~15h total)?
5. Dependabot PRs under `pull_request_target`: token and secret scope, and should they be
   reviewed at all?
6. Is gitleaks-on-verdict enough for launch, or is confinement a prerequisite on a public repo?

## Still next, independent of this design

The "honest checks" PR: `orchestrator/test.sh` has 9 failures on main and isn't in CI. Also sweep
the `! grep -q … && ok || bad` assertions (they pass on grep errors).
