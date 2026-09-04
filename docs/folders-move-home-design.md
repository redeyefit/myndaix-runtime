# Folders Move Home — design v0.1

**Status:** DESIGN — pre cross-family review (data-safety class → kilabz + oracle both).
**What:** folder TRUTH moves from the MacBook (lab, not always-on) to the Mac Mini
(factory, always-on). The MacBook becomes a full two-way Syncthing peer. The phone gets
ONE file grip — Files.app → SMB over the tailnet — beside the ONE agent grip already
built (the phone→tailnet SSH verbs, PR #124). Executes the north-star "Mini becomes home"
jump that was parked 2026-07 ("not jumping yet"); Jefe greenlit the jump and the FULL
scope on 2026-09-03.

## 1. Why, and why now

Jefe's requirements: "questions answered via the ask folder; access my folders via my
phone and agents." Every incremental design (per-folder promotions, read-only SMB interim,
a Syncthing carve-out for ask/) compensated for split truth with accumulating special
cases — rejected as an unclean lattice. Resolving the split is smaller than compensating
for it: one home, uniform rules, every folder-agent (ask watcher, curator, producers)
sits next to the data it serves on the machine that never sleeps.

## 2. End state (uniform — zero exceptions)

| Piece | Today | End state |
|---|---|---|
| Folder truth (all 9: research, fitness, company, ask, personal, memory-runtime, MyndAIX_Ready, RedEyeFit_Ready, redeyefit-content) | MacBook `~/X`; Mini has receive-only mirror `~/MacBookMirror/X` (+ tonight's symlinks `~/X → mirror` for 3) | **Mini `~/X` = truth.** MacBook `~/X` = full two-way peer (edit anywhere, syncs everywhere) |
| Delete protection | Mini receive-only + staggered versioning + MacBook Time Machine | **Staggered versioning on BOTH peers** + MacBook TM (a delete on either side is versioned on the receiver; TM is the second net) |
| Phone files | none | Files.app → `smb://<mini-tailnet-ip>` (jefe account) — direct access to truth, NO phone replica |
| Phone agents | — | PR #124 verbs (ask/get/status/reel) — unchanged |
| `personal` | mirror only | Moves home like everything else (Jefe's explicit call). Phone-reachable as FILES only; the ask-verb allowlist (research\|fitness\|company, recall-gate.py) does NOT widen — file plane ≠ agent plane |

## 3. Flip mechanics — per folder, one at a time (the risky part, exact)

Precondition each folder: Syncthing shows the folder **up to date on both sides**.

1. **Hash-manifest gate:** `find ~/X -type f -not -path '*/.stversions/*' | sort | xargs shasum -a 256`
   on both machines (MacBook `~/X`, Mini mirror) → diff must be EMPTY. Any divergence →
   let Syncthing finish / resolve, re-gate. This makes a flip-time delete storm impossible:
   identical content on both sides means the first rescan after the flip transfers nothing.
2. **Pause** the folder on both devices (Syncthing UI).
3. **Mini:** remove the symlink if present (`~/X`, the 2026-09-03 three) → `mv ~/MacBookMirror/X ~/X`
   (the mirror's content BECOMES truth in place; its `.stversions` history rides along).
4. **Re-point the Mini's Syncthing folder** path to `~/X`, type **Send & Receive**, staggered
   versioning ON (365d, same as today). **MacBook:** type Send & Receive (from send-only),
   staggered versioning ON (NEW on this side — 30d).
5. **Unpause → verify zero transfers** on first rescan; then the edit-both-ways check (§6).
6. Only then the next folder.

Rollout order (staged risk, uniform end state): **ask/** (lowest stakes, proves the
pattern) → **research, fitness, company** (corpus: re-verify recall + corpus walk after
each) → memory-runtime, MyndAIX_Ready, RedEyeFit_Ready, redeyefit-content → **personal
LAST** (after 8 clean drills).

Rollback per folder (any step): re-add the folder exactly as before (Mini receive-only at
the mirror path, `mv` back); the hash gate guarantees content identity, so rollback is
configuration-only, never data recovery.

## 4. Delete-protection equivalence (the invariant this must not weaken)

Today's guarantee: a bad delete on the MacBook cannot destroy the Mini's copy silently
(receive-only + versioning). End-state guarantee, per direction:
- Delete on MacBook → propagates → **Mini's `.stversions` keeps it** (365d staggered) + it
  remains in MacBook TM until TM rotates it.
- Delete on Mini/phone → propagates → **MacBook's `.stversions` keeps it** (30d) + **TM
  snapshots keep it** (the MacBook peer copy is TM-backed — this is why the MacBook keeps
  a full peer copy, not a thin client).
- REJECTED: Syncthing `ignoreDelete` (documented divergence footgun).
- Transition belts, kept until ALL nine folders pass drills: the Mini's
  `*.pre-mirror-20260903` copies (research/fitness/company) + the receive-only-era
  `.stversions`. Cleanup afterward is Jefe's delete, per the never-auto-delete rule.
- SPOF honesty: the Mini becomes the single point for LIVE truth. Accepted meaning of
  "always-on home"; watched by tailnet-watch + liveness/drift canaries; MacBook peer + TM
  is the recovery path if the Mini dies (promote the peer = reverse this design's flip).

## 5. Phone file grip (SMB over tailnet)

- Mini: System Settings → File Sharing ON, share the 9 folders (jefe account). No new
  LAN exposure decision needed beyond what File Sharing implies on the local network the
  Mini already sits on; the PHONE path is tailnet-only (100.67.43.104). (Optional later
  hardening: bind SMB to the tailnet interface — out of scope v1.)
- iPhone: Files → ⋯ → Connect to Server → `smb://100.67.43.104` → jefe credentials
  (stored in iOS keychain). Tailscale's own documented flow.
- Known iOS quirk: Files.app occasionally mounts read-only; reconnect fixes; FE File
  Explorer is the named fallback app, not a dependency.
- No phone replica: offline phone = no folder access (the verbs still answer). Deliberate.

## 6. Verification (per folder, no exceptions)

1. Hash-manifest identical (pre-flip gate, §3.1).
2. First rescan: zero transfers.
3. Two-way edit check: touch a canary file on Mac → appears on Mini; edit on Mini →
   appears on Mac.
4. **Delete-restore drill:** delete the canary on side A → confirm versioned on side B's
   `.stversions` → restore → confirm content. Repeat in the other direction.
5. Corpus folders only: `mxr recall --scope X` on the Mini returns live hits; corpus walk
   skips nothing (knowledge.py symlink-reject + realpath-traversal belts — roots are now
   REAL dirs, strictly simpler than tonight's symlink case).
6. Phone (once, after the first folder): Files.app read + write + the delete-restore
   drill over CELLULAR.
7. MacBook TM: confirm the folder appears in the latest snapshot post-flip.

## 7. Repo touchpoints (small; from the exploration inventory)

- MUST-CHANGE: `substrate/ledger-backup.sh` durability-chain comments (direction
  narrative); `docs/agent-orchestrator-north-star.md` "not jumping yet" → jumped (dated).
- Wording-only: `substrate/lab/tailnet-watch.sh` alert strings ("backup mirror" → "primary
  folders"), `substrate/lab/README.md`, `orchestrator/librarian/README.md` stale v0 note.
- NO code changes: knowledge.py scope resolution ($HOME-rooted stays correct on both
  machines — simpler post-flip), librarian fence/rc-wrapper, curator staged-copy design,
  substrate units, machine roles. The ledger-backup DATA chain (Mini→MacBook ~/MiniMirror)
  is a separate send-only folder and is NOT touched by this migration.

## 8. What this deliberately does NOT build

- The ask-folder watcher agent (separate follow-up /feature on this clean base; substrate
  unit pattern already documented from drift-canary.json).
- Any recall-gate/scope widening (personal stays off the ask verb).
- Taildrive, third-party phone sync, `ignoreDelete`, per-folder topology exceptions.
- Moving the ledger, repos, or anything under git — this is FOLDERS only.

## 9. Open questions for review

1. Is the 30d staggered window on the MacBook side enough, given TM behind it — or match
   365d both sides?
2. Any reason to prefer folder-type flip WITHOUT the remove/re-add (in-place type change)
   — reviewers should check Syncthing's receive-only→send-receive local-state semantics
   (the "revert local changes" state machine) for a cleaner in-place path.
3. SMB share scope: all 9 folders as one share vs per-folder shares (revocation granularity
   on the phone side)?
