# Always-On System — Stress Test / Acceptance Matrix

**What:** the repeatable pass/fail checklist that grades whether the always-on story is
TRUSTWORTHY, not just working-when-we-tried-it. Run it once deliberately after the
folders-move-home migration + phone deploy land; re-run after any change to the
tailnet, the Mini's launchd set, or the phone surface. It is a DOC, not code —
the acceptance gate for the whole "Mini is home" thesis.

**System under test (post-migration end state):**
- Mac Mini (factory) = always-on; holds folder TRUTH; runs the pool, controller, canaries,
  the phone SSH surface (`mxr-phone`), SMB shares, and the corpus index.
- MacBook (lab) = interactive; two-way Syncthing peer; may be asleep/off at any time.
- iPhone = tailnet node; reaches the Mini via SSH verbs + SMB. Never a replica.
- Two independent runtimes (one ledger + pool + index per machine). `mxr` talks to the
  local box. Files sync two-way; INDEXES are built per-machine (→ the Freshness section).

**Preconditions before a valid run:**
- [ ] folders-move-home complete (all 9 folders flipped + per-folder drills passed).
- [ ] Mini corpus ingested at least once per phone-reachable scope (`research`, `fitness`,
      `company`). There is NO scheduled reindex unit — `mxr ask` refreshes the derived
      index on every call (`recall_hits` syncs with `refresh=True`), so freshness rides
      on Syncthing propagation (→ the Freshness section measures exactly that).
- [ ] phone surface deployed (`~/.myndaix/bin/mxr-phone` via `orchestrator/deploy-sync.sh
      --apply`, both test legs green ON the Mini incl. `--sshd`, authorized_keys line +
      Tailscale ACL in place, Shortcuts built). The surface is PR #124's sshd
      forced-command wrapper — NOT Claude Remote Control.
- [ ] Mini `pmset -g | grep autorestart` = 1 AND `fdesetup status` = Off (FileVault back
      ON — e.g. re-enabled by a macOS-update prompt — silently breaks §4 cold start).
- [ ] every required launchd unit verified by the AUDITED health check — run the liveness
      canary once, not a bespoke predicate. (A hand-rolled gate here went through two
      review rounds re-learning what the canary already knows: healthy tick jobs sit at
      `state = waiting` between runs, `launchctl print` exits 0 for crashed-but-loaded
      labels, the declared set derives from `substrate/plists/` WITH sentinel gating, and
      health = last exit code + execution freshness, not a state string.) Run ON the
      Mini from the deploy clone:
      The gate requires the canary's POSITIVE clean line — "no DIVERGENT seen" is not
      health: a config-die, an ALARM early-exit, and the sleep/wake grace-skip all print
      no DIVERGENT while evaluating nothing. Failures accumulate into ONE final assert
      (a mid-block `false` doesn't halt a pasted interactive block):
      ```
      (
        fail=0
        # reconcile-grace pre-gate: the canary SILENTLY grace-skips any job whose plist
        # changed within its max gap (~30min) and still prints all-alive — a pass right
        # after a deploy proves nothing. Refuse to run inside the window:
        recent="$(find "$HOME/Library/LaunchAgents" -name 'ai.myndaix.*.plist' -mmin -30 2>/dev/null)"
        [ -z "$recent" ] || { echo "PREFLIGHT FAIL: plists changed <30min ago (grace window — wait it out):"; printf '%s\n' "$recent"; fail=1; }
        rc=0; out="$(bash substrate/liveness-canary.sh 2>&1)" || rc=$?
        printf '%s\n' "$out"
        [ "$rc" -eq 0 ] || { echo "PREFLIGHT FAIL: canary exited rc=$rc (config/env problem — not a pass)"; fail=1; }
        printf '%s\n' "$out" | grep -q "liveness: all declared jobs alive" \
          || { echo "PREFLIGHT FAIL: no all-alive signal (divergence, sleep/wake grace-skip, or aborted run — read above; re-run after a grace-skip)"; fail=1; }
        # plus the ONE long-lived daemon, where 'running' IS the healthy state:
        launchctl print "gui/$(id -u)/ai.myndaix.runtime" | grep -q "state = running" \
          || { echo "PREFLIGHT FAIL: serve pool not running"; fail=1; }
        [ "$fail" -eq 0 ]   # THE gate — last line, real exit status even when pasted interactively
      )   # subshell: rc/out/fail/recent never leak into the operator's shell
      ```

Record each cell PASS/FAIL + the observed number/behavior. A FAIL is a finding, not a
retry — capture it.

---

## 1. Reachability — the phone→Mini path must hold in every state

The core promise: your pocket reaches home regardless of the MacBook. Run the SAME
`ask` (a known-hit question) from each state.

| # | State | Procedure | PASS criteria |
|---|---|---|---|
| R1 | phone on **cellular**, MacBook awake | run "Ask Research" Shortcut | cited answer returns |
| R2 | phone on **cellular**, MacBook **asleep** | close the MacBook lid, wait for sleep, ask | identical answer — the MacBook is irrelevant to the path |
| R3 | phone on **cellular**, MacBook **powered off** | ask | identical answer |
| R4 | phone on **home wifi** | ask | identical answer (tailnet routes either way) |
| R5 | phone `status` verb, all above | run "Factory Status" | health summary returns; caps line increments |
| R6 | **SMB** file open, MacBook asleep | Files.app → Mini share → open a doc | opens; edit + save; confirm it lands on the Mini |
| R7 | **Syncthing conflict** | edit the doc on the MINI while the MacBook sleeps; then wake the MacBook and edit the SAME doc there before sync catches up | a `.sync-conflict-*` file appears; `mxr ask` neither crashes nor cites the conflict copy; the indexer skipped it. KNOWN GAP at doc time: `_eligible_file` has no sync-conflict filter yet — this row FAILS until that follow-up ships; the row stays, because catching exactly this is its job |

Any FAIL here is tailnet or Mini-side, never the feature. Note WHICH state failed —
cellular-only failure points at Tailscale routing; all-states failure points at the Mini.

## 2. Freshness — the multi-machine seam (indexes are per-machine)

The scenario that WILL surprise you: edit on the laptop, ask from the phone, and the
answer lags until the file REACHES the Mini. There is no reindex timer to wait out —
`mxr ask` refreshes the index on every call — so the lag being measured here is
**Syncthing propagation**, end to end.

| # | Procedure | PASS criteria |
|---|---|---|
| F1 | Add a note with a unique token to `~/research` on the MacBook. `ask research <token>` from the phone IMMEDIATELY. | Either (a) found — sync already delivered; or (b) not found — EXPECTED, proceed to F2 |
| F2 | Watch Syncthing report the file delivered to the Mini, then ask again. | found on the first ask after delivery (the ask itself refreshes). **Record wall-clock from save to found** — that IS the freshness SLA, and it is a SYNC number |
| F3 | Edit an existing doc on the MacBook (change a fact). After sync delivery, ask the changed fact. | new answer reflects the edit (not the stale one) |
| F4 | Delete a doc on the MacBook. After sync delivery, ask its content. | no longer returned (index dropped it — not serving a ghost) |
| F5 | Fire a phone `ask` WHILE the Mini's index refresh is mid-run (two asks back-to-back on a large corpus). | ask still answers from a COMPLETE index — stale-but-complete always beats fresh-but-partial; never an empty/partial result set for a known-good query |

If F5 ever returns "not in corpus" for a known-good query during a refresh, the refresh
is not atomic-swap — that is a P1 against the index-refresh design. (If a scheduled
reindex unit ships later, re-measure F2 against its cadence.)

KNOWN GAP at doc time (review-confirmed): `knowledgerecord._sync()` walks the corpus
BEFORE taking the per-scope advisory lock, so two concurrent asks can commit snapshots
in arrival order rather than walk order — a just-deleted doc can resurrect until the
next ask (violates F4/F5 as written). These rows stay: they are the acceptance gate for
the tracked walk-under-lock follow-up.

## 3. Concurrency — the caps and the shared pool

| # | Procedure | PASS criteria |
|---|---|---|
| C1 | SCRIPTED parallel `ask`s — a human double-tap is orders of magnitude too slow for a TOCTOU window: `for i in 1 2 3; do ssh -n -i <phone-key> mini 'ask research q' & done; wait` (`-n` is load-bearing: backgrounded ssh without it competes for terminal stdin and SIGTTIN-stalls — the test would hang instead of exercising the cap) | all answered and/or clean "factory line busy" (2-slot cap) — never a corrupted/interleaved reply; the cap counter advanced EXACTLY once per accepted call (the suite's 12-parallel test 8b is the code-level twin of this live check) |
| C2 | Phone `ask` WHILE the Mini's controller is mid-review (push something to a watched repo, then ask). | both complete; the pool serves both (per-repo locks + 8 workers) |
| C3 | Burn a daily cap (script N `ask`s to the limit), then ask once more. | the cap holds; the over-limit call denies cleanly; the count is correct (no lost increments under C1's parallel fire) |
| C4 | Via the PHONE PATH (Shortcut or ssh with the phone key — the boundary lives in the wrapper's jid registry, deliberately NOT in operator-side `mxr get`): `get` a foreign job id the phone never issued. | denied "not a phone-issued job" — the ownership boundary holds live |

## 4. Cold start — the real trust test

This is the one that proves "always-on home" is real and not aspirational. A human is
NOT at the Mini.

| # | Procedure | PASS criteria |
|---|---|---|
| K1 | Power-cycle the Mini (pull power, or `sudo shutdown -r now` via tailnet, then let it boot untouched). Precondition (§0) already asserted FileVault OFF. | full zero-hands recovery: boots, auto-logs-in as jefe, gui-domain launchd set up. PROVEN 2026-09-05: ~30s from power to pool under this exact config (FV off + autorestart + auto-login) |
| K2 | After boot, from the phone: `status`. | healthy within a few minutes — the whole stack (pool, canaries, phone surface, index) came back with no hands |
| K3 | After boot: `ask` a known hit. | answers — the index survived / rebuilt, the pool is serving |
| K4 | Check the MacBook's tailnet-watch. | it observed the Mini's brief absence and recovery, and (if the outage crossed the threshold) alerted then cleared |

FileVault branch (only if `fdesetup status` ever reads On again — a macOS-update prompt
can silently re-enable it): unattended cold start is IMPOSSIBLE (EFI pre-boot auth, no
tailscaled, Screen Sharing can't reach it). A PLANNED remote reboot must then use
`sudo fdesetup authrestart` (hands the key to firmware for ONE unattended boot); power
LOSS still means dark-until-hands. That state is itself a §4 FAIL — turn FileVault back
off rather than accommodating it.

## 5. Failure injection — does degradation stay honest

| # | Procedure | PASS criteria |
|---|---|---|
| I1 | Drop the tailnet mid-`ask` (toggle the phone's Tailscale off during a slow ask). | Shortcut shows a connection error — no partial/garbled state; retry after re-enabling works |
| I2a | Stop the Mini's POOL only: `launchctl bootout gui/$(id -u)/ai.myndaix.runtime` (re-bootstrap after; `kickstart` cannot stop). Postgres stays up. Phone `ask`. | the job SUBMITS but never runs → the wrapper's sync wait expires → "still thinking" + a job id handed to Get Answer (`MXR_SYNC_TIMEOUT` path) — never a hang, never a false answer; after re-bootstrap, `get <id>` returns the late reply |
| I2b | Stop the LEDGER: `brew services stop postgresql@16` (or the Mini's PG service name; restart after), pool still up. Phone `get <known-good id>`. | a factory error — NOT "no such job" (the marker contract: rc 1 without `MXR_NO_SUCH_JOB` must never claim the id is unknown; a down ledger once produced exactly that lie) |
| I3 | Interrupt an SMB write mid-transfer (kill wifi during a large phone save). | truncated file syncs to the MacBook; the intact prior version is in the MacBook's `.stversions` (the documented recovery path holds) |
| I4 | Force an oracle flake during a review (or observe one). | the round degrades to kilabz-solo and SAYS SO in the verdict. KNOWN GAP at doc time (review-confirmed): play-review feeds the oracle-unavailable notice only to TRIAGE — a clean PASS verdict can ship without naming the missing reviewer; this row FAILS until the verdict-tagging follow-up lands, and it stays because forcing that fix is its job |
| I5 | Adversarial phone inputs, one per call: a payload containing `$(id)` and backticks; an embedded newline (smuggled second verb); a C1/CSI byte; an oversized (>MAX) payload; a `get` with a path instead of a uuid. | every one DENIED cleanly by the wrapper's gates (grammar/bytewise-control/UTF-8/size) — nothing reaches `mxr`, nothing executes, the deny is logged. The fixture suite covers these exhaustively; these live rows prove the DEPLOYED copy enforces them |

## 6. Delete-restore (references the migration drills)

**BLOCKED until the folders-move-home migration lands** (its design doc lives on branch
`design/folders-move-home`, v0.4 corrections pending — not reachable from this tip; mark
these cells BLOCKED, not FAIL, until then). The MINIMUM drill is inlined here so the run
is reproducible from this artifact alone:

1. Create a disposable test file in a synced folder on the Mini; wait for it to appear
   on the MacBook.
2. From the PHONE, delete it via the SMB share (Files.app).
3. On the MacBook: the file disappears via sync; recover it from the MacBook's
   `.stversions/` (Syncthing trash) — or Time Machine as the second lock.
4. PASS = the recovered file is byte-identical and re-syncs cleanly to the Mini.

The full per-folder matrix (all 9 folders, both directions) is the migration design's
§6 and runs when that lands.

---

## Scoring

- Every cell PASS = the always-on thesis is trustworthy; the Mini can be leaned on daily.
- Any Reachability (§1) or Cold-start (§4) FAIL = the "always-on home" claim is not yet
  earned — fix before relying on it away from the desk.
- Freshness (§2) numbers become the reindex-cadence spec and the honest answer to "how
  fast does an edit reach my phone."
- File this run's results (date + per-cell PASS/FAIL + observed numbers) so the next run
  is a regression comparison, not a fresh guess.
