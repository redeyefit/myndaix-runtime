# Always-On System — Stress Test / Acceptance Matrix

**What:** the repeatable pass/fail checklist that grades whether the always-on story is
TRUSTWORTHY, not just working-when-we-tried-it. Run it once deliberately after the
folders-move-home migration + reindex unit + phone deploy land; re-run after any change
to the tailnet, the Mini's launchd set, or the phone surface. It is a DOC, not code —
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
- [ ] reindex unit live on the Mini (else every Freshness cell fails for a KNOWN reason
      and the run teaches nothing — it is a prerequisite, not an optional extra).
- [ ] phone surface deployed (`~/.myndaix/bin/mxr-phone`, `--sshd` leg green on the Mini,
      authorized_keys line + Tailscale ACL in place, Shortcuts built).
- [ ] Mini `pmset -g | grep autorestart` = 1; `launchctl list | grep ai.myndaix` shows the
      full expected unit set loaded.

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

Any FAIL here is tailnet or Mini-side, never the feature. Note WHICH state failed —
cellular-only failure points at Tailscale routing; all-states failure points at the Mini.

## 2. Freshness — the multi-machine seam (indexes are per-machine)

The scenario that WILL surprise you: edit on the laptop, ask from the phone, and the
answer lags until the Mini re-indexes. This section measures that lag — the number that
sets the reindex cadence requirement.

| # | Procedure | PASS criteria |
|---|---|---|
| F1 | Add a note with a unique token to `~/research` on the MacBook. `ask research <token>` from the phone IMMEDIATELY. | Either (a) found — reindex is event-driven; or (b) not found — EXPECTED, proceed to F2 |
| F2 | Wait one reindex cycle (the unit's interval). Ask again. | now found. **Record the observed lag** — that IS the freshness SLA |
| F3 | Edit an existing doc on the MacBook (change a fact). Ask the changed fact after one cycle. | new answer reflects the edit (not the stale one) |
| F4 | Delete a doc on the MacBook. Ask its content after one cycle + a sync. | no longer returned (index dropped it — not serving a ghost) |
| F5 | While a reindex is MID-RUN on the Mini, fire a phone `ask`. | ask still answers (from the prior index — never an empty/partial index; reindex is fail-closed) |

If F5 ever returns "not in corpus" for a known-good query during a reindow, the reindex
unit is NOT fail-closed — that is a P1 against the reindex unit's design.

## 3. Concurrency — the caps and the shared pool

| # | Procedure | PASS criteria |
|---|---|---|
| C1 | Fire two phone `ask`s within a second (double-tap). | both answered, OR the 2nd gets a clean "line busy" — never a corrupted/interleaved reply |
| C2 | Phone `ask` WHILE the Mini's controller is mid-review (push something to a watched repo, then ask). | both complete; the pool serves both (per-repo locks + 8 workers) |
| C3 | Burn a daily cap (script N `ask`s to the limit), then ask once more. | the cap holds; the over-limit call denies cleanly; the count is correct (no lost increments under the double-tap of C1) |
| C4 | `get` a foreign job id (one the phone never issued). | denied "not a phone-issued job" — the ownership boundary holds live |

## 4. Cold start — the real trust test

This is the one that proves "always-on home" is real and not aspirational. A human is
NOT at the Mini.

| # | Procedure | PASS criteria |
|---|---|---|
| K1 | Power-cycle the Mini (pull power or `sudo shutdown -r now` via tailnet, then let it boot untouched). | it boots to login and auto-logs-in far enough for launchd gui-domain jobs — OR document the FileVault-relogin gotcha (post-power-loss needs a password AT the Mini; Screen Sharing can't reach pre-boot) |
| K2 | After boot, from the phone: `status`. | healthy within a few minutes — the whole stack (pool, canaries, phone surface, index) came back with no hands |
| K3 | After boot: `ask` a known hit. | answers — the index survived / rebuilt, the pool is serving |
| K4 | Check the MacBook's tailnet-watch. | it observed the Mini's brief absence and recovery, and (if the outage crossed the threshold) alerted then cleared |

K1's FileVault behavior is the known constraint (recovery keys on Jefe's iPhone) — the
test is to CONFIRM the documented behavior, not to be surprised by it.

## 5. Failure injection — does degradation stay honest

| # | Procedure | PASS criteria |
|---|---|---|
| I1 | Drop the tailnet mid-`ask` (toggle the phone's Tailscale off during a slow ask). | Shortcut shows a connection error — no partial/garbled state; retry after re-enabling works |
| I2 | Stop the Mini's pool (`launchctl kickstart` a stop, or hold it), then phone `ask`. | the wrapper reports "factory unreachable / pool down" honestly — never a hang, never a false answer |
| I3 | Interrupt an SMB write mid-transfer (kill wifi during a large phone save). | truncated file syncs to the MacBook; the intact prior version is in the MacBook's `.stversions` (the documented recovery path holds) |
| I4 | Force an oracle flake during a review (or observe one). | the round degrades to kilabz-solo and SAYS SO in the verdict — never silently drops a reviewer without announcing it |

## 6. Delete-restore (references the migration drills)

The per-folder delete-restore drills from `folders-move-home-design.md` §6 are part of
this acceptance run — a delete on either machine/phone is recoverable from the receiver's
`.stversions` or Time Machine, per the directional-protection statement (§4 there). Do at
least one from the PHONE (SMB delete → restore from the MacBook side) here.

---

## Scoring

- Every cell PASS = the always-on thesis is trustworthy; the Mini can be leaned on daily.
- Any Reachability (§1) or Cold-start (§4) FAIL = the "always-on home" claim is not yet
  earned — fix before relying on it away from the desk.
- Freshness (§2) numbers become the reindex-cadence spec and the honest answer to "how
  fast does an edit reach my phone."
- File this run's results (date + per-cell PASS/FAIL + observed numbers) so the next run
  is a regression comparison, not a fresh guess.
