# Runbook — Migrate review autonomy (controller + automerge + fix-sweep) to the always-on Mini

**Status:** EXECUTED 2026-06-28 (attended). The Mini is now the autonomous brain — controller (hourly),
automerge (:30), fix-sweep (hourly) live on the always-on host; MacBook autonomy loops booted out + disabled
(pool kept for interactive `mxr`); cursor seeded at HEAD; CAPTURE_ENABLED + SKILLS_ENABLED armed on the Mini.
Designed + adversarially hardened by a multi-lens workflow (3 design lenses → synthesis → 3 adversarial
passes); the valid findings were folded below. Kept as the rollback + re-run reference.

**Goal:** make the *thin* autonomous brain run 24/7 on the Mini instead of sleeping with the MacBook. Move
ONLY the review/merge loops; the MacBook keeps its `ai.myndaix.runtime` pool for interactive `mxr`. Lean
controller, not a fat orchestrator (see the orchestration-layers decision: interactive=Mack, autonomous=thin
controller, Lobster=callable specialist). Everything reversible.

## Ground truth (verified 2026-06-28)
- **MacBook** (sole live brain now): `ai.myndaix.{controller(hourly),automerge(:30,ARMED),fix-sweep(hourly),runtime}` + `com.myndaix.mxq-draft` (OUT OF SCOPE). `$ORCH=~/.myndaix/orchestrator`: SKILLS+AUTOMERGE armed, CAPTURE/AUTOFIX off; `repos.json`→[fieldvision, myndaix-runtime]. PAT `~/.myndaix/.automerge-token` (r2, 93B, Contents+PRs RW + Metadata R on redeyefit/myndaix-runtime). DB `runtime`: review_cursor — myndaix-runtime reviewed=39287f4 **state=blocked attempts=3 pending=85618e0**, fieldvision baseline. serve serves the working tree (currently `chore/disk-cleanup-job`).
- **Mini** (target, M4/24GB, 24/7): only `ai.myndaix.runtime` (pool live). NO `$ORCH`. Clone STALE at 85618e0 (PR#32) — needs pull. DB `runtime`: migrations 0001-0006, review_cursor 0 rows, **0007 NOT applied**. PAT ABSENT. agy authed (works via pool), gh=redeyefit, secrets dir present (load.sh + env/).
- **Shared/critical:** separate Postgres DBs per host (no shared lock; cursor + `automerge_seen` are per-DB). branch protection on main = server-side (requires `test`, no required reviews). Two brains on the same GitHub repos = double reviews + automerge merge-RACE → **exactly one host may run these loops.**

## HARD pre-gates (do BEFORE the firebreak, while the MacBook is still live)
1. **launchd domain** — `ssh mini 'launchctl print gui/$(id -u jefe)/ai.myndaix.runtime'` must show it running. (We've used `gui/<uid>` over SSH all session, so expected-good — but confirm before tearing the MacBook down so a domain miss can't strand us with zero brains. If headless/system domain, retarget ALL launchctl ops.)
2. **check-runs scope** — RESOLVED ✓: probed live, the r2 token reads `commits/<sha>/check-runs` (`test = success`) with no `Checks:read`. No PAT change. (Refutes the workflow's "automerge silently never merges" CRITICAL.)
3. **Oracle-via-gate** — the automerge `--gate` ABORTS if agy/oracle is unreachable. Prove it from the gate path on the Mini (`ssh mini 'mxr oracle "reply READY"'` + a real `play-review --gate` pass returning verdict≠transient) BEFORE arming automerge.
4. **disk floor** — Mini ~84% full (~31GB). Confirm headroom; a silent `done-<sha>` marker-write failure wedges the cursor to BLOCKED.

## Cutover (ordered; reversible)
1. **MacBook (read-only):** snapshot seed = `git rev-parse origin/main` (= `d99baae…`), fieldvision HEAD, the cursor rows, the flags. `d99baae` is the ONLY seed used downstream.
2. **Mini:** `git fetch && checkout main && merge --ff-only origin/main` → confirm == d99baae; confirm `migrations/0007_*.sql` present. (`--ff-only` fails closed on divergence.)
3. **Mini:** `launchctl kickstart -k gui/<uid>/ai.myndaix.runtime` → serve auto-applies 0007. Verify `capture_candidate`/`capture_occurrence` exist + `mxr capture-record --list-tags` works. (This doubles as the live domain proof from pre-gate 1.)
4. **MacBook→Mini:** deliver the PAT with no insecure intermediate — `cat ~/.myndaix/.automerge-token | ssh mini 'umask 077; install -m 600 /dev/stdin ~/.myndaix/.automerge-token'` (after `ssh mini 'umask 077; mkdir -p ~/.myndaix && chmod 700 ~/.myndaix'`). Verify `-rw------- 93` + no `\r\n` corruption. **Never** scp a plaintext temp; never route through the secrets `set -a` env (would broadcast into all 8 worker envs).
5. **MacBook:** confirm quiescence — no live PID on `controller.lock`/`automerge.lock`, no `gh pr merge` mid-flight (tail `automerge.out`). If a merge is in flight, wait for it.
6. **MacBook — FIREBREAK:** `launchctl bootout` then `disable` `ai.myndaix.{automerge(FIRST),controller,fix-sweep}`. Verify with `launchctl print … = "Could not find service"` (NOT just `list|grep` — avoids a false-green if loaded in another domain). Do NOT touch `runtime`, `mxq-draft`, the plists, wrappers, flags, or the cursor (that's the rollback).
7. **MacBook:** park the PAT — `mv ~/.myndaix/.automerge-token ~/.myndaix/.automerge-token.decommissioned`. Do NOT revoke r2 (it's the rollback credential AND the token the Mini now uses).
8. **Mini:** provision `$ORCH` — `mkdir -p ~/.myndaix/orchestrator/{state,locks,runs,fix-runs,fix-state}`; `cp orchestrator/play-review.sh orchestrator/play-fix.sh ~/.myndaix/orchestrator/` (chmod 755); write `repos.json` with ONE entry (myndaix-runtime, path `/Users/jefe/…`, **every `/Users/stevenfernandez` rewritten to `/Users/jefe`** incl. the `.venv/bin/python` verify argv), chmod 600. Add a write probe: `touch state/.wtest && rm state/.wtest`.
9. **Mini:** `bash orchestrator/automerge-preflight.sh` → must end `N ok, 0 missing`. (Plus the pre-gate-3 Oracle proof + a positive check-runs probe — the preflight alone does NOT exercise the gate's CI path.)
10. **Mini:** seed the cursor — `INSERT INTO review_cursor … VALUES('myndaix-runtime','refs/heads/main', d99baae, d99baae, NULL,'baseline',0) ON CONFLICT DO NOTHING` (FULL 40-char SHA). Then **hard-assert** reviewed_sha==d99baae AND state='baseline' AND pending IS NULL AND attempts=0 (ABORT otherwise — `ON CONFLICT DO NOTHING` silently no-ops a pre-existing wrong row). Optional: create the `_ctl_reviewed_ref` git anchor. **Seed at HEAD, NOT the MacBook's 39287f4** — that span is 66 already-merged commits + the blocked/attempts=3 wedge; re-importing it adds zero safety and risks instant re-block.
11. **Mini:** dry-run controller (`MYNDAIX_CONTROLLER_DRY_RUN=1`) + automerge (`MYNDAIX_AUTOMERGE_DRY_RUN=1`); read `controller.out`/`automerge.out`. Expect 'up to date', dispatch/merge nothing. (If origin/main advanced since step 1, a single review of seed..newHEAD is ALSO success — re-check `origin/main` just before, don't misread a correct dispatch as a fault.) Watch for agy-not-found.
12. **Mini:** arm read-mostly flags — `touch SKILLS_ENABLED CAPTURE_ENABLED`. Leave AUTOMERGE/AUTOFIX off.
13. **Mini — GO LIVE:** install controller + fix-sweep plists from `*.plist.example` (rewrite ProgramArguments + StandardOut/ErrPath `/Users/stevenfernandez`→`/Users/jefe`), `launchctl bootstrap`. RunAtLoad fires one controller tick → confirm 'up to date'. (Mini is now sole brain; MacBook's three gone since step 6 → never overlap.)
14. **Mini — automerge DEAD-LAST:** `touch AUTOMERGE_ENABLED`; **first do one operator-observed armed tick by hand** (confirm zero eligible PRs, run `bash orchestrator/automerge-tick.sh`, watch it merge nothing) BEFORE installing the :30 calendar plist (so the first armed real decision isn't fired unattended).
15. **Verify single-brain over ~1 cycle:** Mini controller ticks 'up to date' until a real push; automerge :30 clean; `capture_candidate` accrues on delivered reviews; fix-sweep clean. MacBook `launchctl print` shows the three gone. Neutralize the MacBook's lingering flag: `mv $ORCH/AUTOMERGE_ENABLED $ORCH/AUTOMERGE_ENABLED.disabled`. **MANDATORY final gate:** push a trivial docs PR, confirm the Mini reviews + MERGES it within one :30 (automerge.out shows CI-green + merge, not 'transient'). Only then is the cutover proven.

## Rollback (NOT symmetric — read this)
- **Stop Mini merges instantly:** `ssh mini 'rm ~/.myndaix/orchestrator/AUTOMERGE_ENABLED'` (flag-only; leave the controller running). This is the PRIMARY rollback.
- **Full revert to MacBook-as-brain:** FIRST `ssh mini 'launchctl bootout … {automerge,controller,fix-sweep}'`; THEN MacBook `launchctl enable` (named, non-optional — bootstrap on a disabled service is a silent no-op) + `bootstrap` the three; `mv .automerge-token.decommissioned .automerge-token`; restore the flag. **CAVEAT:** `automerge_seen` is per-host — re-arming the MacBook automerge after the Mini decided PRs risks a double-merge; `pg_dump -t automerge_seen` Mini→MacBook first, or just don't re-arm MacBook automerge unless fully decommissioning the Mini.
- A bad automerge (the one near-irreversible risk) = `git revert` the merge commit + follow-up PR (no force-push to protected main). This is why automerge arms dead-last after a real merge is proven.
- Forward-only + safe to keep on any rollback: the Mini git pull (step 2) + 0007 (additive `CREATE IF NOT EXISTS`, Mini never had the tables).

## Scope decisions (Mack, lean defaults)
- **fieldvision:** OMITTED from the Mini (not cloned there; its pre-push hook stays its primary reviewer). It loses the controller-backstop until cloned on the Mini — acceptable for now. Follow-up: clone + seed (reviewed=53d8d72) on the Mini.
- **PAT:** reuse the r2 token on both hosts (park the MacBook copy). It's already single-repo minimal. Follow-up hygiene: mint a Mini-only fine-grained token and rotate r2 out so the secret lives on exactly one autonomy host.

## Adversarial fixes folded (from the hardening workflow)
check-runs CRITICAL refuted by live probe · launchd-domain promoted to a hard pre-gate · `launchctl print` (not `list|grep`) for teardown verify · rollback is flag-first + `automerge_seen` is per-host · Oracle-via-gate proven before arming · cursor seeded at HEAD with a hard assertion · operator-observed first armed automerge tick · mandatory live docs-PR merge as the final gate · `$ORCH/state` write probe + disk floor.

## Open follow-ups (non-blocking)
agy PATH in the tick wrappers (add `~/.local/bin` if the dry-run shows agy-not-found) · capture end-to-end through the controller-triggered review (verify a row lands) · controller(:00)/automerge(:30) play-review lock offset holds for Mini-speed reviews · a Mini disk-cleanup equivalent · the PAT-hygiene follow-up above.
