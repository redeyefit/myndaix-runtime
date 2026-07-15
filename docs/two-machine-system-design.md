# MyndAIX Two-Machine System — Design (v0.3)

**Status:** v0.3 — folds cross-family review rounds 1 (4C/3H/5M) + 2 (2 new CRITICAL / 3 HIGH / 3 MEDIUM). Core GitOps model validated by both families in BOTH rounds. For Jefe's read + r3 convergence check. NOT approved to build.
**Branch:** `design/two-machine-system`
**Author:** Mack, 2026-07-14
**Supersedes the ad-hoc:** `DEPLOY.md` + `docs/controller-migration-to-mini.md` (become history; this is the live process).

### Changelog v0.2 → v0.3 (r2 folds)
- **r2-C1 (work-area DB isolation, NEW CRIT):** the work area (§2.1 area 2) gets a dedicated **scratch DSN** — autonomous git work never touches the live FACTORY Postgres. §2.1.
- **r2-C2 (PR staging broke the trust boundary, NEW CRIT):** work-area isolation + substrate automerge-denylist are folded INTO PR-1 — the safety property ships with the substrate, not after. §9. *This also resolves the A/B conflict → Option A (§2.3).*
- **r2-H1 (restart race, convergent):** strict restart sequence — stop ticks → restart serve → wait healthy+migrated → start ticks. §2.2.
- **r2-H2 (dry-run/bootstrap paradox):** `--dry-run` does a NON-destructive `git fetch` + diff vs origin/main, never invokes the bootstrap-fetcher, never touches the tree. §2.2, §2.6.
- **r2-H3 (bootstrap-fetcher lifecycle):** once-per-invocation sentinel (no re-exec loop), expected-path validation, explicit fetcher update/rollback policy. §2.2.
- **r2-M1 (manifest gaps):** manifest expanded — loaded launchd-definition identity, venv package state, config render-inputs (no secrets), symlink→realpath resolution, process provenance, and `git status --porcelain` empty. §2.6.
- **r2-M2 (mixed-version symlink reads):** entrypoints resolve `current`→`realpath` once at process start (immutable release dir). §2.3.
- **r2-M3 (launchd labels/idempotency):** explicit old-label enumeration to `bootout`; killed ticks must be idempotent/retryable. §2.4.
- **A/B DECISION RESOLVED → Option A** (eliminate `$ORCH` copies), now that isolation ships in PR-1. §2.3.

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
2. **WORK AREA** — automerge and play-fix create **ephemeral, dedicated clones/worktrees** under `$MYNDAIX_HOME/work/` (or `/tmp`), entirely separate from the deploy clone. A `reset --hard` in deploy can never nuke in-flight autonomous git work because that work isn't there. (Verify during build: does `automerge` mutate a local checkout, or is it purely `gh pr merge` server-side? play-fix definitely makes worktrees — those move to the work area regardless.) **DB isolation (r2-C1):** the work area is injected with a **scratch/test DSN** (`MYNDAIX_WORK_DSN`, a dedicated DB), NEVER the live FACTORY `MYNDAIX_DSN`. Autonomous verification (play-fix tests, agent runs) must not read/write prod data or apply an undeployed PR's migration against the live ledger. File isolation without DB isolation is a half-fix.
3. **STATE DIR** — `$MYNDAIX_HOME` holds ALL mutable state: `config.env`, sentinels, `RUNNING_SHA`, logs, the local Postgres, agent scratch. NOTHING mutable lives under a checkout, so `reset --hard` (and even `git clean -fdx`) in the deploy clone is always safe. `.venv` lives outside the tree or is treated as a reconcile-managed artifact (explicit `pip install`, §2.2).

### 2.2 `reconcile.sh` (repo root, run from the deploy clone)

```
reconcile.sh [--dry-run]
  set -euo pipefail
  # STAGE 0 — STATIC BOOTSTRAP (H3): a tiny, SEPARATE, never-reconcile-overwritten fetcher
  #   ($MYNDAIX_HOME/bin/bootstrap-fetch) does: git fetch origin && git reset --hard origin/main.
  #   reconcile.sh proper is re-exec'd from the freshly checked-out copy AFTER this. A broken
  #   reconcile.sh in origin/main thus can't brick the fetch — bootstrap-fetch always runs the fix in.
  #   LIFECYCLE (r2-H3): a $RECONCILE_BOOTSTRAPPED sentinel guards re-exec to EXACTLY ONCE per
  #   invocation (no infinite Stage-0→re-exec loop); bootstrap-fetch VALIDATES it's operating on the
  #   expected deploy-clone path before any reset; the fetcher is itself updated only by an explicit
  #   `reconcile --update-bootstrap` step (versioned, human-approved) — NOT auto — so its semantics
  #   can't silently drift stale forever. --dry-run NEVER calls Stage 0 (r2-H2).
  0. resolve+validate config: MYNDAIX_HOME, MACHINE_ROLE∈{lab,factory} (fail-closed if missing/invalid),
     REPO=deploy-clone dir, PYTHON. (strict dotenv parse — §2.4, never `source`.)
  1. (bootstrap already fetched+checked-out origin/main into the deploy clone.)
     LAB guard: if working tree dirty, WARN + skip reset (dev WIP protected); FACTORY: already clean.
  2. dep-sync (H2): if requirements changed, `pip install` into the managed venv. explicit, logged.
  3. install artifacts ATOMICALLY (H1): render plists (plistlib, §2.4) + place scripts via
     `install`/`mv` (inode swap) or releases/<sha> + `current` symlink flip. Bootout old labels if
     definitions changed (§2.4). (§2.3 decides whether $ORCH copies exist at all.)
  4. restart — STRICT SEQUENCE (C1, C4, r2-H1): serve is the SOLE migration owner. Order MUST be:
     (a) QUIESCE dependent jobs (bootout/stop controller+automerge+fix-sweep ticks so none fire mid-
     migration), (b) restart serve — serve applies migrations on startup under its advisory lock,
     (c) WAIT (poll w/ timeout, r2-C1-tail) until serve is healthy AND migration head == latest,
     (d) THEN start the ticks (now guaranteed new-code-against-new-schema). launchd: plist DEFINITION
     changed → bootout→bootstrap; else `kickstart`. Killed in-flight ticks must be idempotent/retryable.
  5. VERIFY (post-restart health, C3): serve up? migration head == latest? full artifact manifest
     matches (§2.6)? git status --porcelain empty? If not → ALARM, exit nonzero.
  6. commit point: write RUNNING_SHA + the artifact manifest to $MYNDAIX_HOME/state (temp + atomic mv).
  --dry-run (the drift detector, §2.6): NON-DESTRUCTIVE `git fetch` (NEVER Stage-0 reset — r2-H2),
     then compute the diff for 2-5 (what WOULD change + manifest mismatch) WITHOUT touching the tree or
     any service. Write NOTHING; exit nonzero on any drift.
```

Migrations must remain **backward-compatible / additive** (`IF NOT EXISTS`) so a migration applied at serve-restart can't break a controller tick that fires mid-reconcile against the new schema (kilabz P0-#3 tail).

### 2.3 DECISION — do `$ORCH` script copies still exist? (Jefe picks at review)

The `$ORCH/*.sh` copies exist today as a *trusted-installed-copy* defense: the worker/hooks re-exec `$ORCH/play-review.sh`, not the repo copy, so an untrusted worktree edit can't run as the worker. The three-area model changes the premise — if the DEPLOY CLONE is pull-only and never a worktree for untrusted work, its scripts are *already* trusted.

- **Option A — Eliminate `$ORCH` copies.** launchd + the pre-push hook resolve scripts directly from the pull-only deploy clone. Removes the copy step that drifted tonight *entirely*; one fewer surface. Cost: re-establishes the untrusted-worktree defense a different way (the defense becomes "untrusted work never happens in the deploy clone" — which the three-area model already guarantees). Trust model shifts from "trusted copy" to "trusted location."
- **Option B — Keep copies, make them atomic.** Retain `$ORCH` copies but install via `releases/<sha>/` + a `current` symlink flip (atomic, fixes H1). Smaller conceptual change; preserves the existing copy-based defense verbatim. Cost: keeps the copy surface (now atomic + reconcile-verified, so drift is caught, but the surface remains).

**DECISION (r2 → resolved): Option A.** r2 split — oracle chose A (reduced surface; on a single-user Mini the `$ORCH` symlink is no more protected than a deploy-clone file, so A is security-equivalent and simpler); kilabz chose B *only until* work-area isolation is "mechanically proven and deployed." Lobster identified the conflict is blocked on the PR-staging critical (r2-C2). **Folding work-area isolation + the substrate automerge-denylist INTO PR-1 (§9) satisfies kilabz's exact condition in the same PR** → both families now agree on A. A deletes the copy surface that drifted tonight.

**Consequence A introduces (for r3 to validate):** because launchd/hooks reference scripts *directly* from the deploy clone by absolute path, the Stage-1 `git reset --hard` rewrites live-referenced script files in place (git working-tree writes are not atomic). So with A, the tick QUIESCE (§2.2 step 4a) must bracket the *reset itself*, not just the serve restart: fetch → **quiesce ticks** → reset+install → restart serve → wait → verify → **start ticks**. Entrypoints resolve their own path once at process start (r2-M2). (Option B avoided this by keeping ticks on the old copies until an atomic swap — A trades that for a wider quiesce window. Acceptable: reconcile is infrequent and ticks are short + retryable.)

### 2.4 Config model — validated, not sourced (M1, M2)

- **`$MYNDAIX_HOME/config.env`** (git-ignored, `chmod 600`, per-machine): `MACHINE_ROLE`, `MYNDAIX_DSN` (FACTORY pins `127.0.0.1` — fixes issue #B), `MYNDAIX_HOME`, `OPERATOR_INBOX`, `AUTHOR_ALLOWLIST`, agent-CLI PATH additions.
- **Never `source` it** (arbitrary shell execution). Parse a **strict dotenv subset** (KEY=value, quoted, no expansion), validate each value against a whitelist/type, fail-closed on missing/invalid `MACHINE_ROLE`.
- **Plists generated with `plistlib`** (or an XML-safe templater), never `sed`/`envsubst` (a `&`/`<`/`>` in a value corrupts the XML and wedges `bootstrap`).
- **Transition safety (M2, r2-M3):** the installer carries an EXPLICIT enumerated list of *old* (hardcoded-label) plists — `ai.myndaix.{controller,automerge,fix-sweep,disk-cleanup,runtime}` etc. — and `bootout`s exactly those before `bootstrap`ing the new templated ones, else old+new run concurrently (double-processing). Ticks killed mid-flight by a quiesce/bootout must be **idempotent/retryable** (they already re-fire) — reconcile relies on this in §2.2 step 4.

### 2.5 Role model (`MACHINE_ROLE`)

One resolved role object, not scattered branches:
- **LAB (`lab`):** interactive `serve` pool + pre-push review hook. Autonomy loops OFF. Agents may run dry-run / scratch-DB.
- **FACTORY (`factory`):** `serve` + all autonomy loops + librarian; sole autonomous brain (matches 2026-06-28 reality); deploy clone pull-only.
- Adding a machine later = a new role, no code change.

### 2.6 Drift detection — `--dry-run` + manifest, NOT a SHA (C3)

- **`reconcile.sh --dry-run` is the real detector.** NON-destructive `git fetch` (never Stage-0 reset — r2-H2), then reports any pending operation: files to (re)install, plists to re-render, migration head behind, service down, manifest mismatch, `git status --porcelain` non-empty. A stale/hand-edited script or a stale loaded plist is caught even when the SHA matches origin — the exact hole in v0.1.
- **Artifact manifest (expanded, r2-M1):** the receipt is not just `RUNNING_SHA`. It covers `{sha; per-script hash (resolved via realpath, not the symlink); per-rendered-plist hash; LOADED launchd-definition identity post-bootstrap (launchctl print), not just pid; migration head; venv package-state hash; config render-input hash (no secrets); process provenance — each service pid's exe resolves to the expected release path}`. The canary compares *live artifacts* to the manifest + `origin/main`. A pid alone is weak evidence (kilabz M-1); provenance closes it.
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

- **PR-1 — the substrate + its trust boundary, as ONE unit (r2-C2; folds v0.1 PR-1+2+3):** three-area layout (deploy clone + work-area isolation **incl. scratch DSN** + state dir) + `bootstrap-fetch` (once-only, path-validated) + `reconcile.sh` (non-destructive dry-run + expanded manifest + atomic install + serve-owns-migration + strict restart sequence + bootout/bootstrap) + plist templating + config parse/validate + **substrate-file automerge-denylist (M4)** + the FACTORY poll timer + drift-canary. **The trust boundary (work isolation + denylist) MUST ship with the substrate — not after — or "pull-only" is false during the window (r2-C2).** Only this whole unit closes the wound. Rollback = old enumerated launchd labels restored via bootout/bootstrap.
- **PR-2 — config-driven de-personalization** (allowlist, inbox, DSN pin folded properly) + `SETUP.md` extraction rewrite.
- **PR-3+ — the agent layer** (librarian edit/act rung, Hermes-borrowed structure) on the solid substrate. Separate design pass.
- **Tiny pre-fix (D4):** pin `automerge-tick` DSN to `127.0.0.1` as a one-line PR *now* so the FACTORY gate stops dying while PR-1 is built; the proper `config.env` home lands in PR-2.

---

## §10 Contradictions resolved (M5) + remaining decisions

**Resolved explicitly:**
- *"origin/main is full desired state" vs config/DB/venv outside git* → origin/main is the desired state of **code + rendered-artifact templates**; machine-specific config (`config.env`), DB data, and the venv are **reconcile-managed local state** under `$MYNDAIX_HOME`, declared by the repo but not stored in it. reconcile owns dep-sync; serve owns migrations.
- *"FACTORY pull-only" vs FACTORY commits/pushes* → the **deploy clone** is pull-only; automerge/play-fix use **separate work clones** (§2.1).
- *"reconcile migrates" vs "serve auto-migrates"* → **serve is the sole migration owner**; reconcile restarts-then-verifies (§2.2).
- *"post-merge hook" vs "no inbound deploy"* → **poll-only**, hook idea dropped (§2.7).

- *"work-area isolated" but DB shared* (r2-C1) → work area gets a scratch DSN, never the live ledger (§2.1).

**Resolved decisions:**
- **§2.3 `$ORCH` copies → Option A (eliminate),** unblocked by folding isolation into PR-1 (§2.3). Both families agree.
- **D1 cadence → 15-min poll floor** (tunable).
- **D5 → keep `DEPLOY.md` + migration doc as history** with a header pointing here.

**Remaining for Jefe (ratify, not blocking r3):**
- The Option-A quiesce-brackets-the-reset consequence (§2.3) — a wider quiesce window; confirm acceptable (Mack: yes, reconcile is infrequent + ticks retryable).

---

## Appendix — grounding & review log
- **Current-state inventory (2026-07-14):** deploy surfaces, `$ORCH` copies, launchd jobs, hardcoded paths; confirmed no automated inter-machine sync, no if-mini/if-macbook code branching, serve auto-migrates, separate Postgres per host.
- **Prior-art brief (2026-07-14):** GitOps pull-reconcile (borrow idea, reject tools), drift = dry-run + manifest, Hermes Agent as the personal-agent reference, 12-factor dev/prod parity, launchd as macOS-native always-on. Sources cited in the research brief.
- **r1 cross-family review (2026-07-14):** oracle (lead) + kilabz + lobster; 4C/3H/5M, all folded (v0.2). Core GitOps model validated by both families.
- **r2 cross-family review (2026-07-14):** most r1 folds confirmed resolved; 2 new CRITICAL (work-area DB isolation, PR-staging trust-boundary) + 3 HIGH (restart race, dry-run/bootstrap paradox, bootstrap lifecycle) + 3 MEDIUM, all folded (v0.3). A/B resolved → Option A. Core validated again. → r3 convergence check pending.
