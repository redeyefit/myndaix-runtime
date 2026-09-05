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
- [ ] every required launchd unit verified PER LABEL (substring-grepping `launchctl list`
      can false-green on partially-loaded units):
      `for u in runtime controller liveness drift-canary reconcile automerge fix-sweep \
         ledger-backup; do launchctl print "gui/$(id -u)/ai.myndaix.$u" >/dev/null || echo "MISSING $u"; done`

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
| R7 | **Syncthing conflict** | edit the SAME doc on both machines while the MacBook is asleep; wake it | a `.sync-conflict-*` file appears; `mxr ask` neither crashes nor cites the conflict copy; the indexer skipped it |

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

## 3. Concurrency — the caps and the shared pool

| # | Procedure | PASS criteria |
|---|---|---|
| C1 | SCRIPTED parallel `ask`s — a human double-tap is orders of magnitude too slow for a TOCTOU window: `for i in 1 2 3; do ssh -i <phone-key> mini 'ask research q' & done; wait` | all answered and/or clean "factory line busy" (2-slot cap) — never a corrupted/interleaved reply; the cap counter advanced EXACTLY once per accepted call (the suite's 12-parallel test 8b is the code-level twin of this live check) |
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
| I2 | Stop the Mini's pool: `launchctl bootout gui/$(id -u)/ai.myndaix.runtime` (re-bootstrap after the test; `kickstart` cannot stop — it only (re)starts), then phone `ask`. | the wrapper reports a factory error honestly (a down ledger must NOT read as "no such job" — the marker contract) — never a hang, never a false answer |
| I3 | Interrupt an SMB write mid-transfer (kill wifi during a large phone save). | truncated file syncs to the MacBook; the intact prior version is in the MacBook's `.stversions` (the documented recovery path holds) |
| I4 | Force an oracle flake during a review (or observe one). | the round degrades to kilabz-solo and SAYS SO in the verdict — never silently drops a reviewer without announcing it |
| I5 | Adversarial phone inputs, one per call: a payload containing `$(id)` and backticks; an embedded newline (smuggled second verb); a C1/CSI byte; an oversized (>MAX) payload; a `get` with a path instead of a uuid. | every one DENIED cleanly by the wrapper's gates (grammar/bytewise-control/UTF-8/size) — nothing reaches `mxr`, nothing executes, the deny is logged. The fixture suite covers these exhaustively; these live rows prove the DEPLOYED copy enforces them |

## 6. Delete-restore (references the migration drills)

The per-folder delete-restore drills from `folders-move-home-design.md` §6 (on branch
`design/folders-move-home` — not on main until the migration lands; v0.4 corrections
pending) are part of this acceptance run — a delete on either machine/phone is
recoverable from the receiver's `.stversions` or Time Machine, per the
directional-protection statement (§4 there). Do at least one from the PHONE (SMB
delete → restore from the MacBook side) here.

---

## Scoring

- Every cell PASS = the always-on thesis is trustworthy; the Mini can be leaned on daily.
- Any Reachability (§1) or Cold-start (§4) FAIL = the "always-on home" claim is not yet
  earned — fix before relying on it away from the desk.
- Freshness (§2) numbers become the reindex-cadence spec and the honest answer to "how
  fast does an edit reach my phone."
- File this run's results (date + per-cell PASS/FAIL + observed numbers) so the next run
  is a regression comparison, not a fresh guess.
