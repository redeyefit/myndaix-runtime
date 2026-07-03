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
