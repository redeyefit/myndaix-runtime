# Folders Move Home — design v0.3

**Status:** DESIGN v0.3 — two rounds folded (r1 cross-family; r2 kilabz via the push
hook, 4 findings, all confirmed). Awaiting Jefe's runbook GO.
**Changelog v0.3 (r2):** quiescence HELD and re-verified, not spot-checked (newest-mtime
recheck adjacent to the mv + again pre-unpause); hash gate under pipefail with a
non-empty-manifest assertion; symlink/dir manifest gains the exclusions + readlink
targets; manifests are per-run mktemp paths, never a shared /tmp file.
**What:** folder TRUTH moves from the MacBook (lab) to the Mac Mini (factory, always-on).
MacBook becomes a full two-way Syncthing peer. Phone gets ONE file grip (Files.app → SMB
over tailnet) beside the ONE agent grip (PR #124 verbs). Executes the north-star jump;
Jefe greenlit FULL scope (all 9 folders incl. `personal`) 2026-09-03.
**Changelog v0.2:** relative NUL-safe hash gate (was broken-as-written, oracle H);
index-preserving path change via Advanced/config.xml — never remove/re-add (oracle H);
Revert-Local-Changes + clean-state preconditions (oracle M / kilabz H); writer quiescence
lock (kilabz H); delete protection restated DIRECTIONALLY (kilabz H); SMB: dedicated
phone-only user, LAN exposure named, tailnet ACL 445, version stores write-denied
(kilabz H×2); file-vs-agent plane = accepted-risk statement with the no-new-exposure
proof (kilabz H); rollback phased (kilabz M); folder-identity + ~/X preflight rules
(kilabz M×2); conflict legal-pair drills on the pathfinder folder (kilabz M); truncated
phone-write failure mode documented (oracle L); open questions → decisions (§9).

## 1. Why (unchanged)

Split truth (folders on the non-always-on lab) forced every phone/agent feature into
compensating machinery. One home resolves the class: agents sit beside the data they
serve, the phone touches truth directly, uniform rules.

## 2. End state (uniform)

| Piece | End state |
|---|---|
| All 9 folders (research, fitness, company, ask, personal, memory-runtime, MyndAIX_Ready, RedEyeFit_Ready, redeyefit-content) | **Mini `~/X` = truth**; MacBook `~/X` = full two-way peer |
| Delete protection | Staggered versioning BOTH peers (Mini 365d, MacBook 30d) + MacBook TM — see §4 for the honest directional statement |
| Phone files | Files.app → `smb://<mini-tailnet-ip>`, dedicated `phone` user (§5) |
| Phone agents | PR #124 verbs — unchanged; recall-gate allowlist does NOT widen |

**File plane ≠ agent plane — enforced vs accepted (kilabz H):** the recall-gate keeps
`personal` off the ask verb (enforced, tested). At the OS level, Mini processes running
as `jefe` can read `personal` — **this is NOT new exposure**: the Mini has held a full
`personal` mirror under `~/MacBookMirror/` since the backup architecture shipped, and
lab agents have always run beside the MacBook's copy. The move adds zero agent authority.
OS-level isolation for `personal` (separate user/ACL) is named as a possible LATER
hardening, deliberately out of v1. ACCEPTED RISK, stated.

## 3. Flip mechanics — per folder, one at a time

**Preflight (each folder):**
a. **Config backup:** copy Syncthing `config.xml` on both machines (dated).
b. **Clean cluster state (kilabz H):** the folder shows Up to Date on BOTH sides AND no
   "Local Additions" / "Revert Local Changes" (Mini) or "Override Changes" (MacBook)
   banner, no Failed Items, no folder marker errors. On the Mini, if Local Additions
   exist → **Revert Local Changes** first (oracle M): the receive-only state machine must
   be empty before the type ever changes.
c. **`~/X` preflight (kilabz M):** on the Mini, `~/X` must be exactly one of: the known
   symlink (research/fitness/company — remove it) or ABSENT. Anything else → archive
   aside by hand and re-run preflight. Prevents `mv` creating `~/X/X`.
d. **Writer quiescence — HELD, not spot-checked (kilabz r1 H + r2 H-1):** Syncthing pause
   does not freeze local writers, and a hash-time check alone leaves a window open until
   the mv. Enforcement: pause the folder BOTH sides; stop/hold agents that touch it
   (curator/index for corpus folders; SMB not yet enabled — §5 comes after all flips);
   confirm no open editors; capture the tree's NEWEST mtime at hash time
   (`find <root> -not -path '*/.stversions/*' -exec stat -f %m {} + | sort -rn | head -1`)
   — then RE-VERIFY it unchanged immediately before the `mv` AND again before unpause.
   Any change → abort this folder's window, restart from (b).
e. **Hash-manifest gate (oracle r1 H; kilabz r2 H-2/M-4 — pipefail + per-run paths):**
   ```
   m="$(mktemp /tmp/fmh-manifest.XXXXXX)"     # per-run path — concurrent/resumed gates must not cross-read
   ( cd <root> && set -o pipefail && find . -type f \
       -not -path './.stversions/*' -not -path './.stfolder*' -not -name '.DS_Store' \
       -print0 | sort -z | xargs -0 shasum -a 256 ) > "$m"
   [ -s "$m" ] || abort    # a traversal/sort failure must NEVER read as "identical"
   ```
   on MacBook `~/X` and Mini `~/MacBookMirror/X`; diff must be EMPTY. Also compare a
   symlink/dir manifest with the SAME exclusions and with symlink TARGETS recorded
   (kilabz r2 M-3 — same-named links to different destinations must diverge):
   ```
   ( cd <root> && find . \( -type l -o -type d \) \
       -not -path './.stversions/*' -not -path './.stfolder*' -print | sort ;
     find . -type l -not -path './.stversions/*' -exec sh -c \
       'printf "%s -> %s\n" "$1" "$(readlink "$1")"' _ {} \; | sort )
   ```
   Divergence → resume sync, re-gate.

**Flip:**
1. Mini: resolve `~/X` per preflight-c, then `mv ~/MacBookMirror/X ~/X`.
2. **Path change WITHOUT remove/re-add (oracle H):** Syncthing Actions → Advanced →
   folder → edit Path to `~/X` (or stop syncthing, edit config.xml, start). This
   PRESERVES the folder ID, device sharing, ignore + versioning config, and the local
   index — remove/re-add would drop the index, force a full re-hash/metadata exchange,
   and destroy the zero-transfer guarantee (kilabz M: folder identity rules).
3. Type changes: Mini → Send & Receive (from Receive-Only, AFTER the revert check);
   MacBook → Send & Receive (from Send-Only). Versioning: Mini staggered 365d (already);
   MacBook staggered 30d (NEW).
4. Unpause both → **first rescan must show zero transfers**; then §6 verification.
5. Next folder only after §6 passes.

**Rollout order:** `ask/` (pathfinder — gets the FULL drill set) → research, fitness,
company (corpus; recall + walk re-verified each) → memory-runtime, MyndAIX_Ready,
RedEyeFit_Ready, redeyefit-content → `personal` LAST.

**Rollback, phased honestly (kilabz M):** BEFORE unpause = configuration-only (restore
config.xml backup, `mv` back — hash gate guarantees content identity). AFTER unpause =
data-state decisions (canary edits/drill deletes exist): pick source of truth, recover
via `.stversions`/TM as needed. The per-folder window between unpause and §6 completion
is kept to minutes, and belts (§4) hold throughout.

## 4. Delete protection — the honest directional statement (kilabz H)

Syncthing versioning archives changes RECEIVED from another device — it does not version
local operations locally. So:
- Delete on MacBook → Mini receives it → **Mini's 365d `.stversions` archives it** ✓
  (plus MacBook TM until rotation).
- Delete on Mini/phone → MacBook receives it → **MacBook's 30d `.stversions` archives
  it** ✓ + **TM snapshots** ✓. **Window:** if the MacBook is offline/asleep, there is no
  receiver-side archive until it reconnects — during that window the only nets are the
  Mini-side belts and the MacBook's stale-but-intact peer copy (which, being offline,
  still HOLDS the file). Net: a delete is only ever unrecoverable if the MacBook syncs
  it AND its versioning+TM both fail. Stated, accepted.
- Phone-write truncation (oracle L, documented failure mode): an SMB write dropped
  mid-transfer leaves a truncated file that SYNCS; the intact prior version lands in the
  MacBook's `.stversions` — recovery is a manual restore. Known, bounded.
- REJECTED: `ignoreDelete` (divergence footgun).
- Transition belts until all 9 pass: `*.pre-mirror-20260903` copies + receive-only-era
  `.stversions` + TM. Cleanup afterward is Jefe's delete (never-auto-delete rule).
- SPOF honesty: unchanged from v0.1 — the Mini is watched (tailnet-watch, canaries), and
  the MacBook peer + TM is the promote-back recovery path.

## 5. Phone file grip (SMB over tailnet) — hardened (kilabz H×2)

- **Dedicated `phone` local user on the Mini** — Sharing-only account (no admin, no
  shell), member of nothing else. Per-share ACLs: RW on the 9 folders; **deny-ACL on
  every `.stversions/`** (the version stores must not be phone-writable). The `jefe`
  credential never leaves the Mac.
- **Shares:** the 9 folders individually (oracle's single-root consolidation DECLINED:
  it would relocate the physical dirs and break the $HOME-rooted canonical-path
  principle the symlink decision just established; iOS Files shows one server entry
  listing all shares — acceptable UX).
- **Exposure, stated honestly:** enabling File Sharing exposes smbd on ALL interfaces —
  "tailnet-only" is enforced by policy, not by the daemon: (a) Tailscale ACL `phone →
  mini:445` added; (b) guest access OFF; (c) the LAN the Mini sits on is the home
  network — LAN exposure of a password-protected share to that network is ACCEPTED and
  named (interface binding/pf rules = optional later hardening, out of v1).
- iPhone: Files → Connect to Server → `smb://<mini-ts-ip>` → `phone` credentials (iOS
  keychain). Known iOS quirk: occasional read-only mounts; reconnect fixes; FE File
  Explorer is the named fallback.
- SMB is enabled only AFTER all folder flips complete (writer-quiescence simplicity).

## 6. Verification

**Pathfinder folder (`ask/`) — the FULL drill set, run once (kilabz M conflict pairs):**
1–4. Hash gate · zero-transfer rescan · two-way canary edit · delete-restore drill both
directions (§4 semantics proven live).
5. **Conflict legal-pairs:** simultaneous edit both sides → exactly one
   `.sync-conflict-*` file, nothing lost; modify-vs-delete → survivor per Syncthing
   semantics + version archived; rename-vs-edit → both outcomes present.
6. **Offline-reconnect:** MacBook offline → Mini edit+delete → reconnect → converges,
   deletes versioned on arrival.

**Every subsequent folder (abbreviated):** hash gate · zero-transfer rescan · two-way
canary · one delete-restore drill · (corpus folders) `mxr recall` live hits + corpus
walk clean · TM snapshot contains the folder.
**Phone (once, after SMB enables):** Files.app read + write + delete-restore over
CELLULAR + confirm `.stversions` write-denied for the `phone` user.

## 7. Repo touchpoints (unchanged from v0.1 inventory)

MUST-CHANGE: `substrate/ledger-backup.sh` chain comments; north-star doc "not jumping
yet". Wording-only: tailnet-watch alert strings, lab/README, librarian READMEs. NO code
changes (knowledge.py roots, fences, curator, substrate units all location-agnostic).
The ledger-backup DATA folder (Mini→MacBook ~/MiniMirror, send-only) is untouched.

## 8. Non-goals (unchanged)

Ask-folder watcher (separate follow-up /feature); recall-gate widening; Taildrive;
`ignoreDelete`; third-party phone sync; per-folder topology exceptions; OS-level
`personal` isolation (named later-hardening).

## 9. Decisions (were open questions)

1. MacBook staggered window: **30d** — TM provides long-term durability; 365d would
   duplicate TM's job on laptop SSD (oracle).
2. Path/type change: **in-place via Advanced/config.xml + Revert-Local-Changes gate** —
   never remove/re-add (oracle H mechanics, kilabz identity rules).
3. SMB scope: **9 individual shares + dedicated `phone` user + version-store deny-ACLs**
   (kilabz); single-root consolidation declined (canonical-path principle).
