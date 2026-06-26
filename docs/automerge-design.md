# Auto-merge (docs-only PR-gate) — DESIGN v0.2

_North-star rung 4. v1 = the brain auto-merges **docs-only** PRs behind a hard gate. First removal of the human merge gate. Prior-art: `docs/automerge-research.md`. Status: **revised after a 34-agent adversarial design workflow (12 confirmed findings); awaiting Jefe prereq decisions + plan approval BEFORE any merge code ships.** Workflow result: `docs/reviews/automerge-design-v0.1-adversarial-workflow.md`._

**Decisions locked (Jefe):** safe class = **docs-only**; target = **auto-merge green human-authored PRs** (brain gates+merges, never authors). Defaults: **OFF by default**, revertible, hard gate, no *code* auto-merge to the runtime.

### v0.2 changelog — folded the adversarial workflow (12 confirmed; the design rests on a tiny set of must-be-perfect invariants)
- **B1 atomic merge:** bare `gh pr merge --merge` can *enable deferred auto-merge / add to a merge queue* and exit 0 without merging the commit we judged. → use **`--match-head-commit H`** (server rejects if head moved), **refuse any base with a merge queue**, and **post-merge assert** `state==MERGED` (not `autoMergeRequest`/OPEN) else disable-auto + leave for human.
- **B2 one pinned SHA:** read `headRefOid` → `H` ONCE; thread `H` through every gate; read CI for *that commit*; merge with `--match-head-commit H`. Closes the read-then-merge TOCTOU at the API layer.
- **B3 real PASS signal:** play-review's `done-<sha>` marker means *delivered*, not *passed* (written for NEEDS-FIX too, and shared with the controller). → a **synchronous `play-review --gate` mode** that runs inline and writes `$STATE/verdict-<H>` = exactly `PASS` | `NEEDS-FIX`, exit 0 ONLY on PASS. Abort/canary-fail/skip/contention/oracle-down/timeout = **NOT PASS** (fail-closed).
- **B4 one pinned range:** the diff-class gate and the review must judge the SAME content. → both use `R = merge-base(origin/main, H)...H`; drive play-review with that exact base..tip (don't let its FRONT recompute the base).
- **B5 docs aren't always inert:** a `*.md` that is an **agent-instruction file** (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, `**/rules/*.md`, a skill `.md`, anything the runtime/agents read as instructions) is executed-as-a-prompt. → a **path DENYLIST** overrides the `.md` allowlist.
- **MAJORs folded:** `mergeStateStatus==CLEAN` (not just `mergeable`); defensive `statusCheckRollup` parse (ALL `test` entries SUCCESS, reject zero/running, handle CheckRun+StatusContext); `git diff --raw -z -M` as the SOLE diff source with mode validation (reject `120000` symlink / `160000` gitlink / mode→exec); **author allowlist** (trusted-sender model) + per-author caps vs flooding; if branch protection requires reviews, assert `reviewDecision==APPROVED` (don't trust an admin-token `gh merge` to fail closed); fetch+pin H locally before diffing.

## 0. PREREQUISITES (environmental — need Jefe; some are BLOCKERs to a working deploy)
1. **`gh` auth under launchd** — on this box `gh`'s token is in the **macOS keyring**, which a launchd agent **cannot read**. The job MUST authenticate via a file-based token or a dedicated **fine-grained PAT** (scopes: `repo` on the target repos, `read:org` if needed) exported to the agent's env from a `chmod 600` secret. Until this is provisioned, the gate can't talk to GitHub from launchd. _(Jefe action.)_
2. **Branch protection on `main`** — `main` currently has **NO** branch protection, so there is **NO GitHub-side backstop** behind the brain's gate; the diff-class gate is the *only* thing between a bad merge and `main`. Recommend Jefe **add branch protection** (require the `test` check; optionally require 1 review) as a defense-in-depth layer — then the brain merges *within* it. _(Jefe decision: add protection, or accept the gate as sole backstop.)_
3. **Author allowlist** — to stop a flood of trivial docs PRs draining the daily cap, only auto-merge PRs whose author is in an explicit trusted allowlist (mirrors the bridge trusted-sender model). _(Jefe: who's trusted — likely just `redeyefit`/Jefe for v1.)_

## 1. What it does (unchanged intent, hardened mechanics)
A bounded, non-Claude (launchd, hourly) sibling of the controller-loop. Each tick: list open PRs against `main`; for each, pin one SHA `H`, run the hard gate; only if ALL pass, atomically merge at `H`. Off by default, revertible, capped.

## 2. The gate (single pinned `H` + range `R`; ALL must pass, else leave for human)
For an open PR `#n`: read `H = headRefOid` ONCE; `git fetch` `H` by sha into an automerge-owned ref + pin vs gc; `R = merge-base(origin/main, H)...H`.
1. **State** — `mergeStateStatus == CLEAN` (folds in conflicts/behind/blocked/draft); author ∈ allowlist; base is `main` and the base branch has **no merge queue**.
2. **Docs-only diff-class** (§3) over `R`.
3. **CI green for `H`** — read checks for the commit `H` specifically (`gh api repos/{o}/{r}/commits/{H}/check-runs` + status); **every** `test` entry COMPLETED+SUCCESS; reject if zero `test` entries or any still running/failing.
4. **Review PASS** — `play-review --gate` over `R` writes `$STATE/verdict-<H>`; merge iff it is exactly `PASS` (produced by THIS tick, keyed to `H`+base). Anything else (NEEDS-FIX/abort/skip/timeout/oracle-down) = no merge.
5. **Branch-protection** — if `main` requires reviews, assert PR `reviewDecision == APPROVED`.
6. **Bounds** — `AUTOMERGE_ENABLED` flag present; under per-tick + per-day + per-author caps; not DRY-RUN.
7. **Atomic merge** — `gh pr merge #n --merge --match-head-commit H`; then re-query and assert `state==MERGED` (`mergedAt` + `mergeCommit` set). If instead `autoMergeRequest != null` or still OPEN → `gh pr merge #n --disable-auto`, log loudly, leave for human.

## 3. The docs-only diff-class gate (load-bearing; un-gameable)
- **Sole source:** `git diff --raw -z -M base...H` (NUL-delimited; `gh --json files` is forbidden — it can't see rename old-sides or modes).
- **Every** entry must satisfy ALL: destination mode is a regular blob `100644` (or `100755` only if exec is allowed — recommend reject `100755`), **path ends in `.md`** (case-insensitive guarded; reject homoglyph/trailing-dot/space), AND for a rename/copy the **old side also ends in `.md`**.
- **Reject** on: any non-`.md` path (either side) · mode `120000` (symlink) · `160000` (gitlink/submodule) · mode→executable · empty changeset · a path on the **DENYLIST** below.
- **DENYLIST (overrides the `.md` allowlist — route to human):** `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, `COPILOT*.md`, `.cursorrules`, any `**/rules/*.md`, `SECURITY*.md`, `CODEOWNERS`, anything under `.github/`, and any `.md` the runtime/agents read as instructions (maintained list).
- Computed over the SAME `R` the review judges and the merge integrates.

## 4. The review gate — new synchronous `play-review --gate` mode
play-review today detaches + writes a `done-<sha>` "delivered" marker (shared with the controller, set for NEEDS-FIX too) — **unusable as a PASS signal.** Add a `--gate` mode: runs the review **inline** (no detach), over an explicit `base..tip = R`, and writes `$STATE/verdict-<H>` containing exactly `PASS` or `NEEDS-FIX`, exit 0 only on PASS. Canary-fail / diff-cap / contention / oracle-unavailable / any abort → write nothing-or-`NEEDS-FIX` and exit non-zero (the automerge job treats absence-or-not-PASS as fail-closed). This is the 3rd, additive play-review edit (a new mode; the hook path is byte-unchanged).

## 5. Safety pillars (north-star) — post-hardening
1. **Verify un-gameable:** `git diff --raw` mode+extension+denylist gate · per-commit CI · a real synchronous PASS · atomic `--match-head-commit` merge. The diff-class gate is THE backstop (no branch protection today).
2. **Bounded blast radius:** docs-only minus agent-instruction files; per-tick/day/author caps; author allowlist; uncertain → human.
3. **Instant rollback:** revertible merge commit; flag-off halts; DRY-RUN first; merge-queue refused so we never arm a deferred merge we can't revert.
4. **Learning IS safety — DEFERRED:** hard-coded single class; log every merge loudly; widening waits for the outcomes ledger.
5. **Self-runtime:** docs exempt EXCEPT the instruction-doc denylist (so a `CLAUDE.md`/rules change to the runtime never auto-merges); CODE never auto-merges to the runtime.

## 6. Security surface
Untrusted = the PR diff + author. Docs can't execute, but can carry *content* read later as instructions → the denylist (§3) + the review gate. Validate all `gh`/git output (SHA `^[0-9a-f]{40}$`, PR int, ref allowlist); array subprocess, never `shell=True`. The merge is the privileged action — 7-deep gate, atomic head-match, capped, flag-gated, MERGED-asserted. gh token from a `chmod 600` secret (prereq 0.1), not committed. Trigger = launchd (classifier-clean). No `--admin`, no force, no merge-queue.

## 7. Components & footprint
- **New:** `src/runtime/automerge.py` (~250–300 lines now: list → pin H → 7-gate → atomic merge → assert) + `python -m runtime.automerge tick`; `play-review.sh --gate` mode (additive); `orchestrator/automerge-tick.sh` + plist example; `automerge_seen(repo, pr, head, decision)` table + migration `0004`; `tests/test_automerge.py` (the §3 gate adversarial truth-table: symlink/gitlink/rename/denylist/mode/homoglyph; CI-parse; merge-queue refusal; head-mismatch; fail-closed review) + a `test.sh` live check (`gh auth status` from launchd context, e2e DRY-RUN on a throwaway docs PR).
- **Reused:** play-review (now with `--gate`), the controller's lock/cap/flag/DRY-RUN patterns, `gh`, the ledger.
- **Knobs:** `AUTOMERGE_ENABLED` (durable, OFF) · `MAX_AUTOMERGE_PER_TICK` (1) · `_PER_DAY` (3) · `_PER_AUTHOR_DAY` (1) · author allowlist · DRY-RUN · the doc allow/deny lists.
- **Rollback:** `launchctl unload`; `git revert` any merge.

## 8. Decisions for Jefe (prereqs + scope)
1. **gh auth for launchd** (§0.1) — provision a file token / fine-grained PAT? (BLOCKER to a working deploy.)
2. **Branch protection on `main`** (§0.2) — add it (recommended), or accept the gate as the sole backstop?
3. **Author allowlist** (§0.3) — who? (recommend: just Jefe/`redeyefit` for v1.)
4. Proceed to the implementation plan on this hardened design, or one more cross-family pass on v0.2 first?
