# MyndAIX Two-Machine System — Design (v0.2)

**Status:** v0.2 — folds cross-family review round 1 (kilabz + oracle, lobster synthesis: 4 CRITICAL / 3 HIGH / 5 MEDIUM, all accepted). For Jefe's read + re-review. NOT approved to build.
**Branch:** `design/two-machine-system`
**Author:** Mack, 2026-07-14
**Supersedes the ad-hoc:** `DEPLOY.md` + `docs/controller-migration-to-mini.md` (become history; this is the live process).

### Changelog v0.1 → v0.2 (what the r1 gauntlet forced)
- **C1 (migration deadlock):** `serve` is the SOLE migration owner. reconcile restarts-then-verifies (never verify-then-abort). §2.2, §2.6.
- **C2 (pull-only was a lie):** introduced the **three-area model** — a pull-only DEPLOY clone that is NEVER used for automerge/play-fix git work; autonomous git work happens in separate ephemeral clones. §2.1.
- **C3 (`RUNNING_SHA` false health):** drift detector is now `reconcile.sh --dry-run` + an artifact manifest (script/plist hashes, service health, migration head). SHA is demoted to a cheap first-line hint. §2.6.
- **C4 (launchd lifecycle):** explicit `bootout`→`bootstrap` for plist-*definition* changes; `kickstart` only for code-only changes; in-flight ticks are accepted-killable + must be retryable. §2.2, §2.4.
- **H1 (overwrite crash):** all file installs are atomic (`install`/`mv` inode-swap, or releases-dir + symlink flip). §2.3.
- **H2 (untracked survives `reset --hard`):** ALL mutable state lives under `$MYNDAIX_HOME`, outside any checkout; dep-sync (`pip install`) is an explicit reconcile step. §2.1.
- **H3 (self-bricking reconcile):** a tiny static **bootstrap-fetcher** that reconcile never overwrites fetches+checks-out before any complex logic runs. §2.2.
- **M1/M2 (config injection / stale plists):** strict dotenv parse (no `source`), `plistlib` generation, value validation, fail-closed on bad `MACHINE_ROLE`; installer `bootout`s old labels before `bootstrap`. §2.4.
- **M3 (PR misorder):** PR-1+2+3 merge — reconcile + templating + timer/canary ship together or the wound isn't closed. §9.
- **M4 (auto-deploy trust):** substrate files (`reconcile.sh`, plist templates, migrations, launchd labels) are EXCLUDED from automerge; human-approval required. §5, §9.
- **M5 (contradictions):** resolved explicitly in the new §10.
- **Decisions folded:** trigger = poll-only (§2.7); `$ORCH`-copies question presented as two costed options for Jefe (§2.3).

---

## §0 Why this exists (the honest problem statement)

Two machines — **LAB** (MacBook, dev/interactive) and **FACTORY** (Mac Mini, always-on autonomous brain). Every *feature* got a rigorous design gauntlet; the *substrate underneath them* — deploy, config consistency, sync, drift — never did. It accreted.

On 2026-07-14 the bill came due three ways: a merged HIGH fix (#84) **sat undeployed for days** (`$ORCH/play-review.sh` never re-synced after a pool restart); the `127.0.0.1` DSN pin reached `serve`+`mxr` but **not** `automerge-tick` (FACTORY automerge dying on the IPv6-loopback wedge); the FACTORY week-1 snapshot **never taken**.

**Diagnosis (load-bearing):** the half-deploy risk was *already documented in `DEPLOY.md`* and drifted anyway → **documentation-as-discipline fails.** There is no single source of truth for what should be running, and no loop that proves it is. "Deploy = remember to cp scripts + restart + migrate by hand across surfaces" is *incremental deploy executed by a human* — the same brittle accreted glue that made openclaw fail. We purged the openclaw ghost from the *orchestration* model; it survived in *operations*. This kills it there.

---

## §1 Goals / Non-goals

### Goals
- **G1** — `origin/main` (a nameable SHA) *is* the desired state of every machine.
- **G2** — One verified, idempotent `reconcile.sh` converges a machine; no memorized multi-surface checklist.
- **G3** — Drift is loud: divergence of running artifacts (not just SHA) alerts within one poll interval; detection and correction are the same code path.
- **G4** — The FACTORY **deploy checkout** is pull-only and never hand-edited or used for git work; prod state is always a nameable SHA.
- **G5** — Extractable / open-source-able: zero hardcoded personal paths, usernames, org names; everything machine-specific is validated config.
- **G6** — Compose, don't rebuild: cleanly hosts existing features + the north-star agent without a rewrite.

### Non-goals
- **N1** — No fleet machinery (k8s, ArgoCD/Flux servers, Ansible/Puppet/Chef the tools, Docker, Vagrant). 2 macOS machines, not a cluster.
- **N2** — No shared DB / replication / failover. Each host has its own local Postgres (LAB dev, FACTORY prod).
- **N3** — **No inbound deploy surface of any kind** (no inbound SSH, no webhook, no push-into-FACTORY). FACTORY *pulls*. (r1 caught that a "post-merge webhook" is inbound — killed; see §2.7.)
- **N4** — Agent-loop-first, not gateway-first (§6).
- **N5** — No new alerting stack; alerts ride `mxr` / jefe-inbox.

---

## §2 The substrate: GitOps pull-reconcile

**Thesis (borrowed, machinery rejected):** desired state lives in git; a loop on each host pulls it and reconciles the *whole host* to it. Full-state sync, not incremental deploy.

### 2.1 The three-area model (resolves C2, H2)

The single biggest v0.2 change. FACTORY has three cleanly separated areas — the "pull-only" promise is only true if git *work* never touches the deploy checkout:

1. **DEPLOY CLONE** — e.g. `$MYNDAIX_HOME/deploy/myndaix-runtime`. Pull-only, `reset --hard`-safe, launchd/services resolve code from here. NEVER used for commits, worktrees, automerge, or hand-edits. This is the "prod = a nameable SHA" guarantee made literal.
2. **WORK AREA** — automerge and play-fix create **ephemeral, dedicated clones/worktrees** under `$MYNDAIX_HOME/work/` (or `/tmp`), entirely separate from the deploy clone. A `reset --hard` in deploy can never nuke in-flight autonomous git work because that work isn't there. (Verify during build: does `automerge` mutate a local checkout, or is it purely `gh pr merge` server-side? play-fix definitely makes worktrees — those move to the work area regardless.)
3. **STATE DIR** — `$MYNDAIX_HOME` holds ALL mutable state: `config.env`, sentinels, `RUNNING_SHA`, logs, the local Postgres, agent scratch. NOTHING mutable lives under a checkout, so `reset --hard` (and even `git clean -fdx`) in the deploy clone is always safe. `.venv` lives outside the tree or is treated as a reconcile-managed artifact (explicit `pip install`, §2.2).

### 2.2 `reconcile.sh` (repo root, run from the deploy clone)

```
reconcile.sh [--dry-run]
  set -euo pipefail
  # STAGE 0 — STATIC BOOTSTRAP (H3): a tiny, SEPARATE, never-reconcile-overwritten fetcher
  #   ($MYNDAIX_HOME/bin/bootstrap-fetch) does: git fetch origin && git reset --hard origin/main.
  #   reconcile.sh proper is re-exec'd from the freshly checked-out copy AFTER this. A broken
  #   reconcile.sh in origin/main thus can't brick the fetch — bootstrap-fetch always runs the fix in.
  0. resolve+validate config: MYNDAIX_HOME, MACHINE_ROLE∈{lab,factory} (fail-closed if missing/invalid),
     REPO=deploy-clone dir, PYTHON. (strict dotenv parse — §2.4, never `source`.)
  1. (bootstrap already fetched+checked-out origin/main into the deploy clone.)
     LAB guard: if working tree dirty, WARN + skip reset (dev WIP protected); FACTORY: already clean.
  2. dep-sync (H2): if requirements changed, `pip install` into the managed venv. explicit, logged.
  3. install artifacts ATOMICALLY (H1): render plists (plistlib, §2.4) + place scripts via
     `install`/`mv` (inode swap) or releases/<sha> + `current` symlink flip. Bootout old labels if
     definitions changed (§2.4). (§2.3 decides whether $ORCH copies exist at all.)
  4. restart (C1, C4): serve is the SOLE migration owner. Reconcile RESTARTS serve — serve applies
     migrations on startup under its advisory lock — it does NOT verify-then-abort. launchd: if a plist
     DEFINITION changed → bootout→bootstrap; else `kickstart`. In-flight ticks may be killed → they
     must be retryable (they already re-fire).
  5. VERIFY (post-restart health, C3): serve up? migration head == latest file? artifact manifest
     matches (rendered plist hashes, installed script hashes, service pids)? If not → ALARM, exit nonzero.
  6. commit point: write RUNNING_SHA + the artifact manifest to $MYNDAIX_HOME/state (temp + atomic mv).
  --dry-run: STAGES 0-1 fetch + compute the diff for 2-5 (what WOULD change + manifest mismatch),
     write NOTHING, exit nonzero if any drift. THIS is the drift detector (§2.6).
```

Migrations must remain **backward-compatible / additive** (`IF NOT EXISTS`) so a migration applied at serve-restart can't break a controller tick that fires mid-reconcile against the new schema (kilabz P0-#3 tail).

### 2.3 DECISION — do `$ORCH` script copies still exist? (Jefe picks at review)

The `$ORCH/*.sh` copies exist today as a *trusted-installed-copy* defense: the worker/hooks re-exec `$ORCH/play-review.sh`, not the repo copy, so an untrusted worktree edit can't run as the worker. The three-area model changes the premise — if the DEPLOY CLONE is pull-only and never a worktree for untrusted work, its scripts are *already* trusted.

- **Option A — Eliminate `$ORCH` copies.** launchd + the pre-push hook resolve scripts directly from the pull-only deploy clone. Removes the copy step that drifted tonight *entirely*; one fewer surface. Cost: re-establishes the untrusted-worktree defense a different way (the defense becomes "untrusted work never happens in the deploy clone" — which the three-area model already guarantees). Trust model shifts from "trusted copy" to "trusted location."
- **Option B — Keep copies, make them atomic.** Retain `$ORCH` copies but install via `releases/<sha>/` + a `current` symlink flip (atomic, fixes H1). Smaller conceptual change; preserves the existing copy-based defense verbatim. Cost: keeps the copy surface (now atomic + reconcile-verified, so drift is caught, but the surface remains).

*Mack's lean:* **A**, if the build confirms the three-area isolation is airtight — it deletes the exact surface that failed tonight. B is the safe fallback if any untrusted-worktree path can still reach the deploy clone. Decide at review with the isolation proof in hand.

### 2.4 Config model — validated, not sourced (M1, M2)

- **`$MYNDAIX_HOME/config.env`** (git-ignored, `chmod 600`, per-machine): `MACHINE_ROLE`, `MYNDAIX_DSN` (FACTORY pins `127.0.0.1` — fixes issue #B), `MYNDAIX_HOME`, `OPERATOR_INBOX`, `AUTHOR_ALLOWLIST`, agent-CLI PATH additions.
- **Never `source` it** (arbitrary shell execution). Parse a **strict dotenv subset** (KEY=value, quoted, no expansion), validate each value against a whitelist/type, fail-closed on missing/invalid `MACHINE_ROLE`.
- **Plists generated with `plistlib`** (or an XML-safe templater), never `sed`/`envsubst` (a `&`/`<`/`>` in a value corrupts the XML and wedges `bootstrap`).
- **Transition safety (M2):** the installer `bootout`s the exact set of *old* (hardcoded-label) plists before `bootstrap`ing the new templated ones — else old+new run concurrently (double-processing).

### 2.5 Role model (`MACHINE_ROLE`)

One resolved role object, not scattered branches:
- **LAB (`lab`):** interactive `serve` pool + pre-push review hook. Autonomy loops OFF. Agents may run dry-run / scratch-DB.
- **FACTORY (`factory`):** `serve` + all autonomy loops + librarian; sole autonomous brain (matches 2026-06-28 reality); deploy clone pull-only.
- Adding a machine later = a new role, no code change.

### 2.6 Drift detection — `--dry-run` + manifest, NOT a SHA (C3)

- **`reconcile.sh --dry-run` is the real detector.** It re-fetches and reports any pending operation: files to (re)install, plists to re-render, migration head behind, service down, manifest mismatch. A stale/hand-edited `$ORCH` script or a stale loaded plist is caught even when the SHA matches origin — the exact hole in v0.1.
- **Artifact manifest:** the receipt is not just `RUNNING_SHA` — it's `{sha, per-script hash, per-plist hash, migration head, service pids}`. The canary compares *live artifacts* to the manifest + `origin/main`.
- **Drift-canary launchd job** (cheap `StartInterval`): runs `reconcile.sh --dry-run`; if it reports drift for >N min, alert via `mxr`/jefe-inbox. Loud smoke alarm; it does NOT auto-fix (reconcile's own timer does that).

### 2.7 Trigger — poll-only (decision; resolves the N3 contradiction)

**FACTORY polls `origin` on a launchd `StartInterval` (15-min floor; tunable).** No inbound surface. r1 correctly flagged that a "post-merge webhook" is *inbound* and contradicts N3; a LAB-side hook can't reach FACTORY without FACTORY listening (inbound) — so there is no honest latency win over polling. 15-min convergence vs *days* (tonight) is the whole win. LAB never auto-reconciles (dev machine); it gets `reconcile.sh --dry-run` as a pre-push sanity check.

---

## §3 Data flow

```
LAB: edit → commit → push branch → PR → CI green → merge to main
                                                      │  (origin/main = new desired SHA)
FACTORY poll timer (≤15 min) ─> bootstrap-fetch: reset --hard origin/main (DEPLOY CLONE only)
                               ─> dep-sync → atomic install artifacts (+ bootout/bootstrap if plist def changed)
                               ─> restart serve (serve migrates under advisory lock)
                               ─> VERIFY: serve up? migration head current? manifest matches?  ── no ──> ALARM
                               ─> write RUNNING_SHA + manifest
FACTORY drift-canary ─> reconcile --dry-run reports drift? ── yes >N min ──> ALERT jefe-inbox
Autonomous git work (automerge/play-fix) ─> separate ephemeral clones under $MYNDAIX_HOME/work (never the deploy clone)
```

---

## §4 Edge cases & failure modes (post-fold)

- **Broken `reconcile.sh` in origin/main:** the static bootstrap-fetcher (§2.2 stage 0), never overwritten by reconcile, always fetches+checks-out the fix. No self-brick (H3).
- **Migration dirty/failed state:** `serve` fail-closes on a bad migration (won't come up); reconcile's post-restart verify (step 5) catches "serve down / head behind" → ALARM, no false-healthy. Migrations additive + advisory-locked (C1).
- **`reset --hard` scope:** only tracked files in the deploy clone; all mutable state is under `$MYNDAIX_HOME` (H2), so nothing important is destroyed. reconcile logs any non-empty pre-reset `git status` (should always be empty on FACTORY — a non-empty one is a loud "someone hand-edited" signal).
- **Script swap during a running tick:** atomic inode swap / symlink flip (H1) → running instances finish on the old inode; new ticks get the new one. No `Text file busy`.
- **launchd def change:** `bootout`→`bootstrap` (C4/M2), not a bare `kickstart`, so the new definition actually loads.
- **In-flight tick killed by restart:** accepted; ticks are short-lived + re-fire (retryable, C4).
- **Network failure during fetch:** abort clean, keep current state, retry next poll. Never partial-apply (receipt written last).
- **Crash mid-install:** RUNNING_SHA/manifest written last → stale receipt → canary fires → next reconcile re-runs the full idempotent install. Self-healing.

---

## §5 Security surface

- **`reset --hard origin/main` trusts origin** — same trust root as today's `git pull`; no new exposure. Untrusted PR *content* is handled by the existing confinement model (unchanged).
- **Substrate files excluded from automerge (M4):** `reconcile.sh`, `bootstrap-fetch`, plist templates, migration SQL, launchd labels, `config.env.example` require **human approval** to merge — a malformed/compromised substrate commit must not self-deploy to FACTORY within 15 min unreviewed. (Enforce via automerge path-denylist.)
- **Config rendering (M1):** strict dotenv parse (no execution), value validation, `plistlib` generation, no `sh -c`. `config.env` is `chmod 600`, git-ignored, same tier as `.secrets`; reconcile never prints it.
- **Pull-only kills the inbound surface** (security improvement over any push/webhook alternative).
- **Drift/deploy alerts** route through the existing sanitized notification path.

---

## §6 The full system: substrate + the north-star agent (Hermes-borrowed)

- **LAB = the lab.** Build/design/drive; pushes to `origin/main`.
- **FACTORY = factory + always-on brain.** Autonomous review/self-learning loop + the personal agent (librarian → edit/act → operate).

**Borrow from [Hermes Agent](https://github.com/nousresearch/hermes-agent)** (independently validated our shape): gateway/agent-runtime/subagent split ≈ our pool + `mxr` + per-agent workers; persistent memory + FTS recall + agent-curated memory + learning loop ≈ curator/recall/MEMORY/outcomes-ledger; single role-selectable installer ≈ our reconcile + install. **Reject:** its 20+ chat-platform sprawl, Modal/Daytona serverless (we *are* always-on on the Mini), SQLite (we keep Postgres), Honcho user-modeling. Decisively **agent-loop-first, not gateway-first** — the correct side for our thin-controller model.

**Autonomy rides the substrate:** the librarian's rungs are FACTORY-side. Each rung is gated by evals + trust *and* by the substrate guaranteeing FACTORY runs exactly the reviewed SHA — you can't trust an autonomous agent's actions if you can't name the code producing them. The manifest+receipt is what makes widening autonomy trustworthy. The substrate is a **prerequisite**, not a side quest.

---

## §7 Borrow / reject

| Capability | Verdict | Borrow | Reject |
|---|---|---|---|
| Multi-host deploy | BORROW-PATTERN + BUILD `reconcile.sh` | GitOps pull-reconcile, full-state sync, desired-state=a-SHA | k8s, ArgoCD/Flux servers, ansible-pull the tool, Helm/Kustomize, blue-green |
| Drift detection | BUILD `--dry-run` + manifest | check-mode diff, artifact manifest, alert-on-divergence | Puppet/Chef agents, driftctl, dashboards |
| Personal agent | BORROW-PATTERN (Hermes) | gateway/runtime/subagent, persistent memory, learning loop, agent-loop-first | chat sprawl, serverless, SQLite, Honcho, gateway-first |
| Dev/prod split | BORROW-PATTERN (12-factor) | deploy-clone pull-only, role via env, config-not-code | Docker/Vagrant/k8s parity, staging, feature-flag services |

---

## §8 Open-source extraction plan (G5)

1. De-hardcode paths → plist templates + `config.env` rendered by reconcile; no `/Users/<name>` in repo.
2. De-personalize code → `AUTHOR_ALLOWLIST`, operator inbox, org names → validated config with documented defaults.
3. `install.sh` → render plists, `bootout` old + `bootstrap` new, provision `$MYNDAIX_HOME` skeleton + role sentinels.
4. `SETUP.md` rewrite → clone → `cp config.env.example config.env` → set role → `./install.sh` → `./reconcile.sh`.
5. Extraction is a consequence of doing §2 cleanly, not separate work. (D3: de-personalize as we touch each surface; don't block the substrate on 100% polish.)

---

## §9 Staged build plan (each PR cross-family reviewed before merge)

- **PR-1 — the substrate, whole (folds v0.1 PR-1+2+3 per M3):** three-area layout + `bootstrap-fetch` + `reconcile.sh` (dry-run + manifest + atomic install + serve-owns-migration + bootout/bootstrap) + plist templating + config parse/validate + the FACTORY poll timer + drift-canary. *Only this whole unit closes the wound* — a manual reconcile that can be forgotten doesn't. Ships with a rollback (revert = old launchd labels restored via bootout/bootstrap).
- **PR-2 — automerge/play-fix work-area isolation** (ephemeral clones under `$MYNDAIX_HOME/work`) + substrate-file automerge exclusion (M4). Makes "pull-only" true.
- **PR-3 — config-driven de-personalization** (allowlist, inbox, DSN pin folded properly) + `SETUP.md` extraction rewrite.
- **PR-4+ — the agent layer** (librarian edit/act rung, Hermes-borrowed structure) on the solid substrate. Separate design pass.
- **Tiny pre-fix (D4):** pin `automerge-tick` DSN to `127.0.0.1` as a one-line PR *now* so the FACTORY gate stops dying while PR-1 is built; the proper `config.env` home lands in PR-3.

---

## §10 Contradictions resolved (M5) + remaining decisions

**Resolved explicitly:**
- *"origin/main is full desired state" vs config/DB/venv outside git* → origin/main is the desired state of **code + rendered-artifact templates**; machine-specific config (`config.env`), DB data, and the venv are **reconcile-managed local state** under `$MYNDAIX_HOME`, declared by the repo but not stored in it. reconcile owns dep-sync; serve owns migrations.
- *"FACTORY pull-only" vs FACTORY commits/pushes* → the **deploy clone** is pull-only; automerge/play-fix use **separate work clones** (§2.1).
- *"reconcile migrates" vs "serve auto-migrates"* → **serve is the sole migration owner**; reconcile restarts-then-verifies (§2.2).
- *"post-merge hook" vs "no inbound deploy"* → **poll-only**, hook idea dropped (§2.7).

**Remaining for Jefe:**
- **§2.3 — `$ORCH` copies: Option A (eliminate) vs B (atomic-keep).** Mack leans A pending the isolation proof. *(Design-both, decide at review — Jefe.)*
- **D1 (cadence):** 15-min poll floor — tune later if needed.
- **D5:** keep `DEPLOY.md` + migration doc as history with a header pointing here.

---

## Appendix — grounding & review log
- **Current-state inventory (2026-07-14):** deploy surfaces, `$ORCH` copies, launchd jobs, hardcoded paths; confirmed no automated inter-machine sync, no if-mini/if-macbook code branching, serve auto-migrates, separate Postgres per host.
- **Prior-art brief (2026-07-14):** GitOps pull-reconcile (borrow idea, reject tools), drift = dry-run + manifest, Hermes Agent as the personal-agent reference, 12-factor dev/prod parity, launchd as macOS-native always-on. Sources cited in the research brief.
- **r1 cross-family review (2026-07-14):** oracle (lead) + kilabz + lobster synthesis; 4 CRITICAL / 3 HIGH / 5 MEDIUM, all accepted + folded above. Core GitOps model validated by both families.
