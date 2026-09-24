# Review on GitHub — make the AI verdict a required check, delete the scaffolding (design, 2026-09-24)

**Status:** DRAFT, round 2 of cross-family design review. Round 1 (oracle only) is folded; see
"Review round 1".
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
    5. PUBLIC repo (runtime): post NOTHING but the check result + one line
       "NEEDS-FIX: <n> findings (ledger job <id>)". The verdict BODY never leaves the ledger.
       PRIVATE repo (FV): gitleaks the body -> any hit: don't post, FAIL; else post one PR comment
    6. exit 0 on PASS, 1 on NEEDS-FIX or reviewer-unavailable               # this job = check `review`
ruleset (not classic protection): required check `review`, bypass actor = repository admin,
  bypass mode = "pull requests only" (Jefe can merge ONE PR past a red/pending review in the UI;
  nobody can direct-push). Classic protection is unchanged. Merge on green = GitHub auto-merge.
```

**The review is always of the FULL PR diff** (`base...head`), never incremental since the last
reviewed sha. Fold pushes change callers of earlier pushes, and full-diff needs no stored state.
Cost is held down by `cancel-in-progress` + skipping drafts, not by shrinking the diff.

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
| inbox relay of routine verdicts | check result (+ PR comment on private repos); Mack reads bodies via `mxr get` |
| `repo_id = basename(cwd)` | `github.repository` |
| autofix: `autofix-arm.sh`, `play-fix.sh`, `fix-sweep.sh`, `autofix_fire()` | nothing — its only trigger (main-branch reviews, #179) dies with the backstop. Deleted, not parked; recoverable from git |

**Automerge needs no replacement classifier:** `automerge_seen` on the Mini (2026-09-24) shows
**2 merges ever** (the last on 2026-06-29), 91 skipped and 5 needs_fix. Native auto-merge is
opt-in per PR by a human, and that choice is the classification.

**Kept:** the pool/ledger/Command API, outcome labeling (no GitHub analog; it's the learning
data), substrate deploy (not review), the doorbell for non-review alerts.

## Security surface

- **Untrusted:** the PR diff and head files (prompt injection into reviewers); PR title, body and
  branch name.
- **Actions injection:** never interpolate `${{ github.event.* }}` into `run:`; pass via `env:`.
- **`pull_request_target` is privileged:** no step may execute PR content (no install, build or
  test of the head; the head snapshot is read-only data for the reviewer).
- **The same-repo `if:` is mandatory, not belt.** Assumption to verify: fork-triggered
  `pull_request_target` runs are NOT held by the fork-approval setting.
- **Exfil path:** codex's read-only sandbox can still READ what `jefe` can read (auth.json,
  `.secrets`, ops DB), so an injected reviewer could echo a secret into its verdict.
  - A pattern scanner can't stop this: base64, rot13 or spaced-out text defeats gitleaks
    (oracle, round 1, CRITICAL).
  - Closed by **not publishing**: on the public repo the verdict body never leaves the ledger,
    and the only public output is one bit plus a count.
  - On private FV, comments reach repo collaborators only (Jefe); gitleaks there is belt.
  - This is today's exposure unchanged: verdicts already land in the private inbox.
  - Confinement (curator pattern: scratch HOME, `--tools`, `--strict-mcp-config`) stays a later
    hardening, not a launch gate, BECAUSE nothing is published.
- **Fork PRs never get a `review` result**, so they can never merge without Jefe's admin bypass.
  Intended: a solo repo, 0 forks, and outside code should need Jefe anyway.
- **Pin third-party actions by SHA** (repo has `sha_pinning_required: false`).
- **Stale comment fix:** `ci.yml`'s ACCEPTED RISK note says "private" — the repo is public; fix
  the comment in the build PR.

## Failure modes

- **Reviewer unavailable** (credits out, pool down, canary fail, auth lost, Mini offline): the
  check fails or stays pending. That is fail-closed by design.
  - Escape hatch (oracle round 1, HIGH: "no override" is unsurvivable after a ~15h outage day):
    the ruleset's **admin bypass, pull-requests-only**. Jefe merges that one PR in the web UI;
    the gate stays up for every other PR.
  - **No override label.** Agents run `gh` as Jefe, so they could self-apply one.
  - For the same reason the MacBook `destructive-blocker` hook denies `gh pr merge --admin`: a
    bypass is a human click, never an agent command.
  - Two real cases on 2026-09-23/24 show why fail-closed matters: Mini Codex credits ran out, and
    the MacBook Codex login file was lost when the disk filled. Today both failed OPEN (reviews
    aborted, merges proceeded). Under this design both hold the merge.
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

## Review round 1 (2026-09-24, oracle only; kilabz was logged out) — folded

1. CRITICAL exfil via obfuscation: folded as "don't publish" (Security).
2. HIGH fork PRs stuck: accepted as intended (Security).
3. HIGH no override: folded as the ruleset admin bypass, PRs only, plus the `--admin` hook
   deny. Oracle's suggested setting ("bypass required pull requests") only exempts the PR
   requirement, not a status check, so it was not taken.
4. MED automerge classification: refuted by data (2 merges ever).
5. MED full vs incremental diff: full (Design).
6. LOW autofix orphaned: delete, not park (Deleted table).

Confirmed closed by oracle: the required check covers web-UI edits, force-pushes, stale results
from older shas, and Dependabot (same-repo branches).

## Open questions for round 2 (kilabz trust boundaries)

1. `pull_request_target` + the same-repo `if:` + no-execute: is there any path where main's
   workflow still executes PR-controlled content (e.g. the reviewer's read-only snapshot, git
   hooks/attributes/filters in the fetched head, submodules)?
2. Does "pull requests only" ruleset bypass exist for a user-owned public repo with a
   repository-admin actor? Verify at build with a throwaway PR before relying on it.
3. Dependabot PRs under `pull_request_target`: token and secret scope, and should they be
   reviewed at all?
4. Is the one public line ("NEEDS-FIX: n findings (ledger job id)") truly free of
   attacker-controlled content?

## Still next, independent of this design

The "honest checks" PR: `orchestrator/test.sh` has 9 failures on main and isn't in CI. Also sweep
the `! grep -q … && ok || bad` assertions (they pass on grep errors).
