# DESIGN: Controller-autofix widening (Phase 2 completion)

_Mack + Jefe, 2026-07-03. v0.1 — DRAFT for cross-family review._

_Context: the push-mode autofix flip (docs/phase2-autonomous-fix-flip-design.md) is built,
merged, and ARMED on the MacBook as of today. This design widens the SAME fix stage to the
controller's autonomous backstop reviews — the last unbuilt piece of Phase 2._

## What — one paragraph

Let the controller's review-on-tick (the brain's backstop on the Mini) auto-draft a fix the
same way an armed push review does. Today `controller._review_env` sets
`PLAY_DISABLE_AUTOFIX=1` **unconditionally** (flip design B1), so even on a box where autofix
is armed, a brain review that finds a real bug can never pre-draft the fix — a human (or Mack)
must do the review→fix round trip by hand (exactly what happened with PR #61 on 2026-07-03:
the brain's first completed review found the chunker fail-open; the fix took a 90-minute
manual loop). The widening is ONE conditional: when the operator has dropped a NEW durable
flag `$ORCH/CONTROLLER_AUTOFIX_ENABLED` **and** the existing box arm `$ORCH/AUTOFIX_ENABLED`
is present, the controller omits the hard override from its dispatch env — play-review's
existing `autofix_armed()` + `autofix_fire()` gates then apply to the brain's NEEDS-FIX
verdicts exactly as they do to push reviews. Zero changes to play-review.sh or play-fix.sh.
The fix is still ONE bounded codex attempt, isolated env-scrubbed worktree, verify from a
clean base, policy-screened human-apply diff to the inbox, UNVERIFIED-honest, never merged.

## Why — problem it solves

The controller reviews already-merged main (the backstop). When it finds something real, the
verdict lands in the inbox with a hand-typed `play-fix.sh …` hint — the same manual round trip
the push-mode flip already eliminated for pushes. The asymmetry is now the bottleneck: the
brain can find its own bugs (proven today) but cannot draft its own fixes. Closing it makes
the full loop autonomous up to the human merge gate: merge → backstop review → finding →
drafted fix diff → human applies.

## Data flow (delta only)

1. Tick → `process_repo` → `trigger_review(repo, target, base)` → play-review FRONT →
   detached worker (unchanged).
2. `_review_env()` (controller.py): today always includes `"PLAY_DISABLE_AUTOFIX": "1"`.
   NEW: include it UNLESS both flags exist:
   `ORCH/AUTOFIX_ENABLED` (the existing box arm) AND `ORCH/CONTROLLER_AUTOFIX_ENABLED`
   (the new, separate human flip for the brain path). Fail-CLOSED: any doubt (missing flag,
   unreadable dir) → the override stays.
3. Worker (unchanged): NEEDS-FIX → deliver → `autofix_fire` runs its existing fail-closed
   gates — durable-flag armed, push confirmed (the controller passes an empty remote URL, so
   `confirm_pushed` is true by design), non-empty fixlist, repo `fail_to_pass:null` in
   repos.json, trusted non-symlink `$ORCH/play-fix.sh`, `base_sha==tip` assertion — then
   nohup-detaches play-fix.
4. play-fix (unchanged): one codex attempt → verify from clean base → sanitized diff →
   inbox file. Never commits/pushes/merges.

## Preconditions (become pre-arm gates)

- **The reap landmine (play-review.sh autofix_fire comment):** the detached fixer stays in
  the dispatcher's process group; under a launchd dispatcher it survives ONLY if the plist
  sets `AbandonProcessGroup=true`. The Mini's controller plist has had it since PR #42 —
  but the arm tool must VERIFY it (`launchctl print` on the controller job), not assume it.
- The box arm must be live and fresh (`autofix-arm.sh` gates: seatbelt probe, trusted
  installs hash-matched, loop-immunity).
- repos.json on the controller box lists the repo `fail_to_pass:null` (Mini: myndaix-runtime
  eligible; fieldvision ABSENT there → fails closed).

## Arming UX

`autofix-arm.sh` gains a `controller` subcommand:
- `autofix-arm.sh controller` (status) / `controller-arm` / `controller-disarm`.
- `controller-arm` requires the box arm FIRST, re-runs its gates, verifies
  `AbandonProcessGroup=true` on the controller launchd job, then drops
  `CONTROLLER_AUTOFIX_ENABLED`. Human-only flip, same as every autonomy switch.

## Security surface

- **No new trust:** the fix path, sandbox, policy screen, honesty caps, and inbox delivery
  are byte-identical to the armed push path. The widening changes WHO triggers, not what is
  trusted (same principle as the original flip).
- **Untrusted inputs unchanged:** the fix-list is still lobster triage output (untrusted LLM
  text) written to the worker's own run dir; play-fix's policy screen + sanitization already
  treat it as hostile.
- **Loop-immunity:** play-fix never pushes, so a drafted fix cannot retrigger the controller
  (its trigger is a REMOTE head change). The inbox has no watcher (arm gate).
- **Blast radius cap:** `PLAY_FIX_DAILY_CAP` (existing) bounds drafts/day; controller
  dispatches are already ≤ MAX_DISPATCH_PER_DAY.
- **automerge unaffected:** gate mode keeps `PLAY_DISABLE_AUTOFIX=1` unconditionally
  (automerge.py sets its own env; not touched).

## Failure modes

- Flag present but box arm absent → fail-closed (both required in `_review_env`, and
  `autofix_armed()` would refuse anyway without the durable flag). Double-gated.
- Controller plist loses AbandonProcessGroup (plist rebuild) → the detached fixer is reaped
  mid-run; the draft never lands but nothing corrupts (play-fix is idempotent-restartable and
  its lock/cap contains re-fires). The arm-time check + a status warning mitigate.
- A NEEDS-FIX on a CHUNK dispatch (controller now reviews chunk targets, PR #59): the fix
  base is `$tip` = the chunk sha — a real commit on main's history; play-fix worktrees from
  it exactly as any sha. No change needed (runtime assertion `base_sha==tip` already holds).

## Deliberate non-goals

- No auto-apply/auto-merge (unchanged, permanent for this rung).
- No selector/4th-arg path (stays UNVERIFIED-only, as the flip shipped).
- No fieldvision widening (absent from the Mini's repos.json; the MacBook `_note`/gate
  mismatch is fixed in this PR by documenting the note correctly — see below).
- No retry/multi-attempt fixes.

## Rider fix (small, related)

The MacBook repos.json `fieldvision._note` says "Autofix intentionally OFF" but the entry is
`fail_to_pass: null` = ELIGIBLE by the gate. The note is wrong (or the intent is). This PR
corrects the NOTE to match reality ("eligible; drafts are UNVERIFIED because verify is null")
— flipping actual eligibility stays Jefe's call and is one JSON edit either way.
repos.json.example gets the same doc fix.

## Build plan

1. `controller.py`: `_review_env()` conditional (+ module consts for the two flag paths);
   fail-closed on any OSError.
2. `autofix-arm.sh`: `controller` / `controller-arm` / `controller-disarm` subcommands with
   the plist gate.
3. Tests: `test_controller.py` — env carries the override by default; omits it ONLY with
   both flags; OSError → fail-closed. `orchestrator/test.sh` — a controller-shaped worker
   invocation (no PLAY_DISABLE_AUTOFIX, durable flag present) FIRES the stub fixer; with the
   env override present it does NOT (existing test 31 covers the override side).
4. Cross-family review (kilabz + oracle) + adversarial workflow on this design BEFORE build;
   review the built diff again after.

## Rollback

`autofix-arm.sh controller-disarm` (removes the flag; next tick's dispatches carry the hard
override again). Or delete the flag file by hand. No schema, no deploy coupling beyond the
normal pull+restart.

---

## v0.2 — cross-family + adversarial review folds (2026-07-03)

Reviewed by **kilabz** (codex), **oracle** (Gemini/Mini), and a **50-agent adversarial
workflow** (4 lenses × refuter panels). Verdict: **NEEDS-REVISION — 1 BLOCKER + 3 MAJORs.**
The "just flip the env conditional" v0.1 is too thin; the safe design needs real new
machinery. Findings, each with its required fold:

**F1 — Provenance gate missing (MAJOR, 3 reviewers converged).** `_branch_protection_ok`
(controller.py) is enforced ONLY on the low-risk skills-indexing path, fail-closed. The
review→autofix dispatch path checks NO provenance/protection, and the design's "push
confirmed" rests on `confirm_pushed` short-circuiting `true` on the controller's empty remote
URL — that is not proof the target is real remote/main content. On an unprotected main (a
solo founder who sometimes pushes direct — [[session_state]] confirms this happens on
FieldVision), a direct or force push, including from a compromised credential with zero human
PR review, is observed by the reconciler and (armed) fires codex. The asymmetry is the tell:
the LOW-risk path is protection-gated, the HIGH-risk code-execution path is not. **Fold: the
controller autofix path requires a real provenance check** — target reachable from the
configured remote main AND `_branch_protection_ok` for the watched branch — fail-closed. "No
new trust" was FALSE without this.

**F2 — Point-of-use enforcement + live kill switch (BLOCKER, kilabz+workflow).** The new
`CONTROLLER_AUTOFIX_ENABLED` is consulted ONLY in `_review_env` at dispatch time; once a
worker is dispatched without `PLAY_DISABLE_AUTOFIX`, `autofix_fire` rechecks ONLY the box arm.
So `controller-disarm` is NOT a live kill switch — an in-flight review still fires — and any
controller-shaped worker missing the override fires with only the box flag. **Fold: the
controller flag must be enforced at FIRE time** (autofix_fire, or a controller-specific fire
gate), not just at dispatch — so disarm is immediate and the flag is a real authority boundary.

**F3 — Flag-lifecycle stale resurrection (BLOCKER, workflow).** `autofix-arm.sh disarm` /
`arm` touch ONLY `AUTOFIX_ENABLED`; the script header instructs routine `arm` after every code
pull. Sequence: `controller-arm` → later `disarm` (leaves the controller flag stranded) →
routine `arm` after a pull → the both-flags conditional silently re-arms the brain path, WITHOUT
a human flip and WITHOUT re-running the AbandonProcessGroup plist check. **Fold: `arm`/`disarm`/
`status` become controller-flag-aware** — disarm clears both, arm refuses/warns on a stale
controller flag, status surfaces it. An autonomy switch must never turn itself back on.

**F4 — Historical-chunk fixes (MAJOR, oracle+kilabz).** The controller reviews CHUNK targets
(intermediate/historical shas, PR #59). Drafting a fix against a historical sha yields a diff
that may not apply to current main (surrounding code moved) or fixes an already-fixed bug —
burning `PLAY_FIX_DAILY_CAP` on obsolete patches. **Fold: auto-fire ONLY when `target ==
current remote head`** (the tip), never on a historical chunk. Chunk NEEDS-FIX still delivers a
verdict; it just doesn't auto-draft.

**F5 — Mini scope-widening (MAJOR, workflow).** The precondition ("box arm live") forces
creating `AUTOFIX_ENABLED` on the Mini — a box where it doesn't exist today. `autofix_armed`
is a bare OR on that flag, so it also arms PUSH-mode autofix for the Mini's own autonomous
pushes as a side effect. **Fold: either (a) the controller path gets its OWN arm not requiring
the box-wide flag, or (b) the design explicitly accepts + documents this and adds a pre-arm
audit of Mini-clone pre-push hooks.** (a) is cleaner and dovetails with F2's fire-time gate.

**F6 — Shared daily cap (MINOR, kilabz).** Controller findings contend with push findings for
one `PLAY_FIX_DAILY_CAP`. Backstop chunk-walking early in the day could starve a later
high-priority push fix. **Fold: document the shared cap, or give the controller path its own
small cap.**

**F7 — Launchd plist-gate fragility (MINOR, oracle).** `launchctl print <job>` fails in a
tmux/manual (non-launchd) controller, permanently blocking arm there; parsing its nested
output is false-negative-prone. **Fold: detect how the controller runs; override/skip the
plist assertion off-launchd (where the reap landmine doesn't apply).**

### Meta-conclusion — this is a Fable-grade build, not a patch

F1–F3 alone turn v0.1 from a one-conditional change into: a provenance+protection gate, a
fire-time authority check, a target==head guard, and a reworked flag lifecycle. That is enough
new safety-critical machinery — on the autonomy rung with the widest blast radius — that the
BUILD should get a fresh strongest-model design pass, not be rushed on the backup model during
a quota window. **Recommendation: fold F1–F7 into a v0.3 spec and build when Fable is back
(2026-07-03 credits exhausted → resets Jul 5).** The always-on backstop already delivers value
un-armed (it found a real chunker bug on its first completed review, 2026-07-03 — see
[[session_state]]); auto-drafting its fixes is the widening, and it can wait for a sound design.
Nothing regresses in the meantime: the brain reviews and surfaces; a human still drafts.
