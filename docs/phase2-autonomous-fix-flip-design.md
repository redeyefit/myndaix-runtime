# DESIGN: Autonomous-fix flip (Phase 2)

_Mack + Jefe, 2026-06-25. Prior-art input: `docs/phase2-autonomous-fix-flip-research.md`.
Synthesized via a judge-panel + 2 adversarial critics (workflow), then hardened against both
critics' `needs-revision` findings. Goes to cross-family review (codex + Oracle) before any code._

## What — one paragraph
Flip the existing **manual** `play-fix.sh` invocation into an **opt-in auto-trigger** by adding a
small, fail-soft block to `play-review.sh`'s existing NEEDS-FIX else-branch (`play-review.sh:202-208`).
When `PLAY_AUTOFIX=1`, after the NEEDS-FIX review is durably delivered to the jefe inbox and the push
is confirmed landed, the branch writes the lobster fix-list (`$triage`) to the worker's own run dir and
`nohup`-**detaches** `play-fix.sh` with three args `(repo_id=$repo_id, base_sha=$tip,
fixlist=$run/fixlist.txt)` — **no 4th-arg selector in v1**. Zero new scripts, zero changes to
`play-fix.sh`, zero changes to the lobster prompt. The fix runs under play-fix's own independent
lock/cap, caps at **UNVERIFIED** (`play-fix.sh:318`), and lands as a sanitized, policy-screened,
human-apply diff in the inbox — byte-identical in actionability to a human typing the three args today.
**The flip changes only WHO invokes play-fix, never how much the fix is trusted.** It auto-fires only on
repos configured `fail_to_pass:null` and only through a **trusted installed** play-fix copy; both are
enforced fail-closed (not merely documented). The selector path (which unlocks `REGRESSION_CHECK_ONLY`)
is a documented, additive, separately-flagged follow-on — not built here.

## Why — problem it solves, what fails without it
Today a NEEDS-FIX verdict requires the operator to hand-type
`play-fix.sh <repo_id> <40-hex-sha> <fixlist-file>` to get a candidate fix: locate the reviewed SHA,
dump the triage to a file, run the tool. Without the flip, every fix is a manual round-trip, so in
practice fixes get deferred and the review→fix loop never closes autonomously. The flip removes that
keystroke for the common case while preserving every safety property of the manual path. It deliberately
does **not** try to make fixes more trustworthy — **UNVERIFIED in, UNVERIFIED out** — because the honest
value is "pre-draft the fix the operator would have requested anyway," not "trust the machine."

## Data Flow — input → process → output
1. **Trigger source (existing):** push → pre-push hook → front detaches the worker (`:63`) → canary →
   kilabz review (`$review`, untrusted LLM) → lobster triage (`$triage`, untrusted LLM = the ordered
   fix-list) → PASS gate (`:196`). NEEDS-FIX is the **else** branch (`:202-208`). In scope: `$repo_id`
   (basename, = repos.json key, `:72`), `$tip` (reviewed 40-hex SHA, `:71`), `$base` (range lower bound /
   EMPTY_TREE sentinel — **never** used for the fix), `$ref`, `$run` (`$RUNS/$play`, `:75`), `$triage`,
   `$review`, the global review lock `$STATE/lock` (held `:143`→EXIT trap `:153`).
2. **Capture `confirm_pushed` ONCE (new):** compute `pushed=confirm_pushed; rc=$?` into a variable BEFORE
   `mark_done`, and reuse that one value for both the done-marker decision and the fire gate. **Never call
   `git ls-remote` a second time** — a slow/dead remote inside the held review lock would wedge ALL reviews
   until the 1800s STALE reap (`:143-153`). `mark_done` is refactored to consume the captured value
   instead of calling `confirm_pushed` again (`:133`).
3. **Deliver the review first:** `deliver "review NEEDS-FIX — $ref" "<body>"` (`:85-102`); returns 1 on
   durable-write failure (`:91`). The fire path runs only **after** this succeeds.
4. **Build the body with an ALWAYS-present manual hint (new):** the NEEDS-FIX body always ends with a
   copy-paste `play-fix.sh "$repo_id" "$tip" "$run/fixlist.txt"` hint (valid for `PRUNE_DAYS=14`; re-push
   to regenerate after that). When armed AND the fire actually launches, the body additionally notes
   *"an auto-fix attempt is running and will arrive as a SEPARATE inbox file (no extra ping)."* This closes
   the armed-but-suppressed gap — every suppression path (below) still leaves the operator a working
   manual fallback — and sets honest expectations for the silent second file (play-fix has **no** notifier).
5. **Write the fix-list file (always, in NEEDS-FIX):**
   `printf '%s' "$triage" > "$run/fixlist.txt" 2>/dev/null || true` into the worker's exclusive
   single-writer run dir (`$play = timestamp-$$`, no lock needed). The `|| true` (NOT `&&`) is load-bearing:
   under `set -e` a redirection failure must not abort the already-delivered worker.
6. **Gate the fire — ALL conditions required (fail-closed):** fire iff —
   `PLAY_AUTOFIX=1` **AND** `deliver` returned 0 **AND** `$pushed` is true (the captured value)
   **AND** `repos.json .[$repo_id].fail_to_pass` resolves to JSON `null`
   **AND** a **trusted** fixer is executable **AND** `fixlist.txt` is non-empty.
   Any false condition → no fire; the always-present manual hint is the fallback.
7. **`fail_to_pass:null` enforcement (new, fail-closed):**
   `f2p="$(jq -r --arg r "$repo_id" '.[$r].fail_to_pass // "null"' "$REPOS_JSON" 2>/dev/null || echo ERR)"`;
   fire only if `f2p == "null"`. Rationale: `play-fix.sh:158` reads `.fail_to_pass` **independently** of
   the 4th-arg selector, so a repo with a static `.fail_to_pass` array would let a 3-arg auto-fire sail past
   the UNVERIFIED ceiling (`:318`) into precheck/verify and reach `REGRESSION_CHECK_ONLY`/`TAMPERED` — an
   autonomous high-trust outcome no human chose. Enforcing null makes the headline "UNVERIFIED in/out" claim
   *true by construction*; to enable `REGRESSION_CHECK_ONLY` autonomously the operator must consciously adopt
   the selector follow-on, not just edit a config. `jq` missing / repos.json unreadable → `ERR` → no fire.
8. **Resolve fixer — TRUSTED INSTALL ONLY on the auto path (new, fail-closed):**
   `fixer="${PLAY_FIX_SELF:-$ORCH/play-fix.sh}"; [[ -x "$fixer" ]] || { <suppress fire>; }`. **No fallback
   to `$repo/orchestrator/play-fix.sh`** for the autonomous path. An attacker-controlled push to the repo
   modifies exactly that worktree copy, and `play-fix.sh` runs **unsandboxed** (it is the thing that *builds*
   the sandbox), so an auto-trigger must never exec the worktree copy. (The *manual* hint still points at the
   in-repo path — acceptable because a human is in the loop and chose to run it.) Mirrors and tightens the
   `$self` defense-in-depth at `:49-51`.
9. **Detach with a scrubbed env (new):**
   `nohup env -u MYNDAIX_FIX_TEST_MODE -u MYNDAIX_FIX_PATCH_OVERRIDE "$fixer" "$repo_id" "$tip" "$run/fixlist.txt" </dev/null >/dev/null 2>&1 &`.
   **base_sha=$tip, NEVER $base** (pinned with an inline comment — see Edge Cases). `nohup` inherits the
   push-time env, so the two test-seam vars are stripped to deny a stray `MYNDAIX_FIX_TEST_MODE=1` +
   `MYNDAIX_FIX_PATCH_OVERRIDE` (operator misconfig) from turning every auto-fire into a cap-skipping,
   fixed-override run. (play-fix already fail-closes a lone override without TEST_MODE at `:248-249`; the
   scrub removes the both-set hazard too.) stdin←`/dev/null` avoids the recorded agy/codex inherited-stdin hang.
10. **Output (unchanged play-fix contract):** detached play-fix takes its own `fix-state/lock` (`:188-192`),
    charges its own `PLAY_FIX_DAILY_CAP` (`:28/:236`), self-establishes PATH + codex-pool auth
    (`:20/:251-252`), runs codex + sandboxed verify, and writes a sanitized diff + one verdict line
    (`NO_FIX|UNVERIFIED|TAMPERED|REGRESSION_CHECK_ONLY|ABORTED`) to the jefe inbox (`:55-64`). With three
    args + `fail_to_pass:null`, it finishes **UNVERIFIED** before verify even runs (`:318`). It **never
    commits, never pushes.**

## Idempotency & loop-immunity
**No new marker.** The earlier draft's `fix-fired-$tip` is **dropped** — it was redundant with the existing
`done-$tip`. Mechanism: the worker checks `done-$tip` at `:160` (after acquiring `state/lock`) and exits
early on a re-review; `mark_done` writes `done-$tip` iff `$pushed` is true; the fire gate requires the same
captured `$pushed`. So the fire and the done-marker are written under one condition, and the global review
lock (`:143-153`) serializes workers — a re-pushed identical tip hits the `:160` early-exit (or `contention()`)
and cannot double-fire. Latest-SHA-wins falls out for free (distinct tip per push). Dropping the marker also
drops a prune branch and the "load-bearing keep-it-under-the-lock" ceremony.

**One residual (accepted):** the `:160` dedup and the fire both live under the *current* single global review
lock. If a future PR makes review locks *per-repo* (PR-2 territory), workers for *different* repos could run
the branch concurrently — but `done-$tip` is keyed on the globally-unique tip SHA, so per-repo locking does
not reintroduce a double-fire for the *same* tip. Noted so a future editor re-checks this if the lock scope changes.

**Loop immunity — the proof must reach past play-fix's own lines.** play-fix's only durable output is the
jefe inbox markdown (`:55-64`; INBOX is human-only — *no agent watcher may ever be added there*, pinned as a
load-bearing comment). play-fix itself never commits/pushes. play-review fires **only on push**. So the cycle
is closed **at play-fix's boundary**. BUT play-fix delegates code generation to **codex running unsandboxed in
a linked worktree of `repo_path`** (`play-fix.sh:263`, `mxr codex --repo`), and that worktree shares `.git`
(so `origin` is configured). An injected fix-list could try to make codex `git push` (which would re-enter
play-review's pre-push hook) or call out to exfil. The loop/exfil immunity therefore depends on the **codex
builder running with network disabled** (registry `net=false`, per the runtime config). **This is a pre-ship
verification gate (below), not an assumption** — the proof rests on the codex sandbox, not on play-fix.sh's
literal lines. Any future change that gives the codex builder network, or that makes play-fix auto-commit/
auto-apply, re-opens the loop and is disqualified.

## Selector contract — v1 derives NO selector
The bridge passes exactly 3 args. The lobster triage prompt (`:190`) emits free-text prose plus the
exact-match `PLAY_PASS` sentinel — there is no parseable selector token today, so the minimal change parses
nothing and adds no untrusted-text parser. With no selector + `fail_to_pass:null`, play-fix caps at
**UNVERIFIED** (`:317-318`) before verify runs. This deliberately forfeits only `REGRESSION_CHECK_ONLY`, which
"found-by-reading" findings (the majority of kilabz output) cannot reach anyway — play-fix is forbidden from
authoring a red test (TAMPER_RE `:34/:308/:349`).

**Follow-on (NOT built; pointer only):** a separately-flagged `PLAY_AUTOFIX_SELECTOR` extractor could, when
lobster names an existing already-red test, append one `PLAY_F2P: <relative/test/path>` line, parse it with an
anchored first-match regex behind a testable boundary (never inlined into the hot path), strip it from both the
human body and the codex fix-list, and pass it as a single positional 4th arg. play-fix's existing 11-check gate
(`:164-185`) is the sole authority — the bridge pre-trusts nothing; the worst a valid-but-malicious selector can
do is name another existing tracked test, which self-limits to UNVERIFIED via the fail-on-clean-base precheck
(`:324-326`). It ships only after the safe UNVERIFIED path is proven, with an offline injection-test harness.
(Note for that work: play-review's `clean()` strips C0 but **not** CR `\015`; the extractor must anchor/strip CR itself.)

## Product decision — REGRESSION_CHECK_ONLY surfacing + no-selector auto-trigger
**(1) REGRESSION_CHECK_ONLY stays plain human-apply — surface, never auto-action.** Unanimous across all three
judges. It must NOT auto-apply or auto-merge (disqualifying — breaks loop immunity and the honest-minimal
contract; play-fix has no PASS verdict and labels it "a regression signal, NOT a guarantee," `:357`). Surfacing
is already free: play-fix stamps the verdict into the inbox **filename + markdown subject** ("# fix
REGRESSION_CHECK_ONLY — <repo>", `:56-58`) so the operator can see/grep/sort by verdict. **Do not build a
ranker, queue, or priority layer** — the verdict line IS the sort key. **Honest caveat:** play-fix has **no**
iMessage/desktop notifier (unlike play-review's `deliver`, `:95-100`) — the fix verdict lands as a *silent*
second inbox file. The NEEDS-FIX review (which *does* ping) pre-announces it (step 4), so the silent file is
expected, not a blind spot. In v1 this verdict is unreachable anyway (no selector + enforced null).

**(2) No-selector auto-trigger: YES, fire on every NEEDS-FIX — but default-OFF (`PLAY_AUTOFIX=0`).** Unanimous
for v1. Even at the UNVERIFIED ceiling, play-fix pre-drafts a patch-policy-gated, secret-scanned candidate diff
that removes the manual keystroke — the faithful realization of "change WHO invokes play-fix." Honest because
UNVERIFIED is identical to the manual 3-arg outcome. The over-firing concern is resolved by operator-gating
(off-by-default), not by refusing to fire.

**Future selector-mode default (recorded, one line):** when the follow-on lands, add an operator-configurable
mode `PLAY_FIX_AUTO=selector|all|off`; ship with `all` initially (don't make the flip near-inert on an
unreliable LLM token), recommend `selector` as steady-state if observed auto-fire volume creates cap/compute
pressure. v1 is unaffected (no selector mechanism exists yet).

## Edge Cases
- **`$triage` > MAX_FIXLIST (64KB):** play-fix fails closed → ABORTED (`:145`); review already delivered; no
  re-fire (done-$tip). Clean abort, never truncation.
- **Rapid pushes of distinct tips:** each tip fires once; play-fix's `fix-state/lock` serializes the actual
  fixes, so a 2nd concurrent fire ABORTS "another fix is running" (`:189-191`). Honest caveat: under a burst a
  stale earlier tip's fix may complete while the latest ABORTs — operator re-runs the latest via the manual hint.
  No debouncer (latest-SHA-wins + the lock suffice).
- **Push rejected by remote (`$pushed` false):** no fire (don't draft a fix for code that didn't land);
  `done-$tip` also not written; re-push re-evaluates. Manual hint still delivered.
- **Inbox write of NEEDS-FIX fails (`:91`):** `deliver` returns 1 → fire suppressed; worker doesn't mark done;
  retries next push.
- **`fail_to_pass` non-null in repos.json:** fire suppressed (step 7); review + manual hint delivered. Prevents
  a silent ceiling-lift on the autonomous path.
- **No trusted `$ORCH/play-fix.sh` (or `PLAY_FIX_SELF`):** auto fire suppressed (step 8); review + manual hint
  delivered. The auto path NEVER execs the worktree copy.
- **`PLAY_FIX_DAILY_CAP` (20) exhausted:** extra fires → ABORTED (`:236`), fail-safe. Shared with manual (see NOT built).
- **`repo_id` (basename) ≠ repos.json key:** the `fail_to_pass` lookup (step 7) returns `null`→ wait, returns
  empty→`"null"` default → would *pass* the null check, but play-fix then fails closed on the path lookup
  (`:138-140`, empty path → ABORTED). Net: never a wrong-repo fix. **Operator keeps key = dir basename**
  (documented prerequisite). *(Reviewer note: confirm the `// "null"` default can't mask a genuinely
  misconfigured repo into firing — it can't cause a wrong fix, but it does spend a fire that immediately ABORTs;
  acceptable, flagged.)*
- **base_sha mis-wire trap:** for a **new/root branch** `$base` is EMPTY_TREE and fails play-fix's 40-hex gate
  (`:142-143`) → ABORTED. **For an incremental push `$base` is a real parent commit** — both gates would pass and
  play-fix would silently fix the WRONG (parent) base. There is **no runtime gate** distinguishing `$tip` from
  `$base` here; protection = `base_sha=$tip` pinned with an inline comment **and** a hard test.sh assertion
  (arg2 == $tip). This is the single most important code-review focus.
- **Marker/dedup pruned at 14 days then same tip re-reviewed:** one duplicate fire (fresh codex attempt on an
  old SHA). Rare, fail-safe, wasted compute only.
- **Crash mid-fire (after done-marker, before play-fix output):** that tip is "done" until the 14-day prune;
  recover via empty-commit re-push → new tip. Two-phase provisional claim deliberately not built (solo machine).

## Security Surface
- **Untrusted `$triage`** (lobster LLM over a possibly-injected diff): written verbatim to `$run/fixlist.txt`
  and passed only as a **file-path arg** — never parsed/interpolated by the bridge in v1. play-fix re-reads it
  as DATA, enforces MAX_FIXLIST fail-closed, and fences it to codex with its own nonce (`:91/:257-260`). Not
  deriving a selector removes the only new untrusted-text-parsing surface in v1.
- **Marker/dedup filenames** embed `$tip`, a 40-hex SHA re-validated by play-fix (`:142`) — no shell metachars.
- **`repo_id`** is a basename used only as a repos.json lookup key; the worktree path comes solely from trusted
  repos.json `.path`; a mismatch fails closed (`:138-140`).
- **Trusted-install-only fixer (step 8)** closes the "attacker's pushed worktree copy runs unsandboxed" vector
  on the auto path. Deploy prerequisite: `cp orchestrator/play-fix.sh "$ORCH/play-fix.sh"` (re-copy on update).
- **Env-scrubbed detach (step 9):** `env -u MYNDAIX_FIX_TEST_MODE -u MYNDAIX_FIX_PATCH_OVERRIDE` strips the
  inherited test-seam so a misconfigured push env can't downgrade an auto-fire to a cap-skipping override run.
- **`PLAY_AUTOFIX` default `0`:** installing the updated play-review.sh does **not** silently spend codex budget;
  arming is explicit; rollback = unset one env knob. (A per-push env var matches the existing
  `PLAY_DAILY_CAP`/`PLAY_SELF`/`PLAY_IMESSAGE_TO` convention.)
- **Bounded cost-DoS (accepted):** forced-NEEDS-FIX pushes can drive up to `PLAY_FIX_DAILY_CAP=20` codex submits/
  day; bounded, fail-safe, operator-gated by the off-by-default flag. The attacker cannot pre-create a
  suppressing marker (`$STATE` is outside the repo).
- **Exfil:** verify runs sandboxed (`deny network*`, `:100`); inbox is human-only; patch is `clean()`'d +
  `SECRET_RE`-redacted + body-withheld on a secret hit (`:62/:76-78`). Residual: `SECRET_RE` is a crude
  signature list, and the **codex-submit stage's network is the real boundary** — see the pre-ship gate.
- **Loop immunity** is the core security property; its proof depends on the codex builder having no network
  and play-fix never pushing (pre-ship gate).

## Pre-ship verification gates (must pass before arming `PLAY_AUTOFIX=1`)
1. **Codex builder has NO network and NO push capability.** Verify against the live runtime that the codex
   builder spec runs `net=false` and that the linked fix worktree cannot `git push` (network-denied). The
   loop-immunity and exfil proofs rest on this, not on play-fix.sh's own lines. If unconfirmed → do NOT arm.
2. **Trusted install present:** `$ORCH/play-fix.sh` exists, is executable, and is byte-identical to the
   reviewed `orchestrator/play-fix.sh` (`shasum -a 256`).
3. **Repos to be armed are `fail_to_pass:null`** in the live repos.json (the enforcement also checks this at
   fire time; verify the config too).
4. **No agent watcher on `inbox/jefe/`** (loop-immunity invariant).

## Files
**Created:**
- `docs/phase2-autonomous-fix-flip-design.md` — this DESIGN doc (already written; cross-family reviewed before coding).
- `docs/phase2-autonomous-fix-flip-research.md` — prior-art brief (already written).

**Modified:**
- `orchestrator/play-review.sh` — the ONLY runtime code change. Define `REPOS_JSON="${MYNDAIX_REPOS_JSON:-$ORCH/repos.json}"`;
  capture `confirm_pushed` once and refactor `mark_done` to reuse it; restructure the NEEDS-FIX else-branch
  (`:202-208`): always write `$run/fixlist.txt` (set-e-safe), always append the manual hint, deliver, set
  `done-$tip`; then the fail-closed fire gate (PLAY_AUTOFIX + `$pushed` + `fail_to_pass:null` + trusted-fixer-x
  + non-empty fixlist), env-scrubbed `nohup` with `base_sha=$tip`. All other lines unchanged; `play-fix.sh`
  and the lobster prompt are **byte-for-byte unchanged**.
- `orchestrator/test.sh` — add smoke cases (hard, blocking assertions; stub fixer via `PLAY_FIX_SELF`, no real
  codex): (a) writes `$run/fixlist.txt`; (b) invokes fixer with **arg2 == $tip** (NOT `$base`) and **exactly 3
  args**; (c) fires at most once per tip across two same-tip runs; (d) does NOT fire when `confirm_pushed` is
  false (force via a non-empty bogus `remote_url` whose `ls-remote` ref ≠ tip — empty url returns true at
  `:125`); (e) does NOT fire on `fail_to_pass` non-null; (f) does NOT fire when no trusted install is present
  (never resolves to `$repo/orchestrator/play-fix.sh`); (g) PASS branch never writes fixlist/never fires; (h)
  armed-but-suppressed still emits the manual hint; (i) `PLAY_AUTOFIX` unset/0 → manual hint present, no fire;
  (j) `shasum` guard that `orchestrator/play-fix.sh` is unchanged by this PR.

## Dependencies
**Depends on:** merged `play-fix.sh` (signature `:130-131`, 40-hex base gate `:142-143`, `.fail_to_pass` read
`:158`, selector gate `:164-185`, fixlist cap `:144-145`, UNVERIFIED ceiling `:318`, `fix-state/lock`
`:188-192`, own daily cap `:28`, override double-gate `:248-249`); `play-review.sh` primitives
(`confirm_pushed:124-128`, `mark_done:133`, run dir `:75`, `deliver:85-102`, global lock `:143-153`,
`done-$tip:160`, `-type f` prune `:157`, nohup idiom `:63`, `$self` resolution `:49-51`); `repos.json`
(key=basename, absolute `.path`, `.fail_to_pass`); `jq`; the `mxr` wrapper → launchd codex pool
(`ai.myndaix.runtime`) running the codex builder **net=false**.
**Depended on by:** nothing programmatic — output is human-read inbox markdown. The (future) selector follow-on
depends on this branch's structure + repos.json `fail_to_pass_template`.

## Deliberately NOT built
- **No selector extraction / no `PLAY_F2P` prompt clause in v1** — lobster prompt untouched; forfeits
  `REGRESSION_CHECK_ONLY` (caps at UNVERIFIED). Riskiest surface deferred behind its own flag + injection harness.
- **No auto-apply / auto-merge / auto-commit** — preserves the honest-minimal contract and loop immunity (disqualifying).
- **No new PASS verdict.**
- **No new script, daemon, queue, Postgres row, or launchd service** — inline in an existing branch, existing idioms.
- **No inbox ranker / priority layer** — verdict-in-subject is the sort key.
- **No debounce window** — latest-SHA-wins + play-fix's lock.
- **No auto-retry** — one fire per tip; the human is the retry decision.
- **No separate auto-fire daily cap in v1** — shares `PLAY_FIX_DAILY_CAP=20` (over-cap fails safe). A
  `PLAY_AUTOFIX_DAILY_CAP` is shelved, to be built only if auto volume starves manual runs.
- **No dedicated `fix-fired-$tip` marker** — redundant with `done-$tip` once `confirm_pushed` is captured once.
- **No two-phase provisional claim / EXIT-trap rollback** — rare crash-mid-fire window accepted (re-push recovers).
- **No worktree-copy fixer fallback on the auto path** — trusted install only (manual path keeps the in-repo hint).
- **Zero changes to `play-fix.sh` and the lobster prompt** — the gate, sandbox, caps, lock, verdicts are already the authority.

## Open questions for cross-family review (codex / Oracle stress-test)
1. **Codex-builder network (the deepest one):** is the codex builder genuinely `net=false` and push-incapable in
   the linked fix worktree? The whole loop-immunity/exfil story depends on it. How do we *assert* this in CI/deploy?
2. **`base_sha=$tip` vs `$base`:** for an incremental push both are real commits and no runtime gate distinguishes
   them — is the inline comment + test assertion enough, or do we want a defensive runtime check (e.g. assert the
   passed base equals `$tip`)?
3. **Capture-once `confirm_pushed`:** confirm the refactor removes the second `ls-remote` and that no path can
   wedge the held review lock; is the un-timeout'd first `ls-remote` (pre-existing) acceptable, or wrap it?
4. **`fail_to_pass:null` enforcement:** is fire-time `jq` of repos.json the right place, and does the
   `// "null"` default correctly fail-closed for a missing/misconfigured repo (it spends an ABORT fire — ok?)?
5. **Trusted-install-only fixer:** is fail-closed-to-`$ORCH/play-fix.sh` correct, and is the `shasum`
   byte-identity check the right deploy gate?
6. **Env-scrub completeness:** are `MYNDAIX_FIX_TEST_MODE` + `MYNDAIX_FIX_PATCH_OVERRIDE` the only env vars worth
   stripping on the nohup, or should we pass a minimal allowlisted env instead?
7. **Surfacing honesty:** is "the NEEDS-FIX ping pre-announces a silent second inbox file" an acceptable v1 UX, or
   does the fix verdict need its own ping?
