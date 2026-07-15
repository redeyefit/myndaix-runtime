# MyndAIX Two-Machine System — Design (v0.1 DRAFT)

**Status:** v0.1 draft, for Jefe's read + cross-family review (kilabz + oracle). NOT approved to build.
**Branch:** `design/two-machine-system`
**Author:** Mack, 2026-07-14
**Supersedes the ad-hoc:** `DEPLOY.md` runbook + `docs/controller-migration-to-mini.md` (both become inputs, not the process).

---

## §0 Why this exists (the honest problem statement)

We always knew this was a two-machine system — **LAB** (MacBook, dev/interactive, user `stevenfernandez`) and **FACTORY** (Mac Mini, always-on autonomous brain, user `jefe`). We designed every *feature* on top of it with rigor (review-context, shadow-dial, curator, librarian — each a real design gauntlet). We never designed the *substrate underneath them*: how code deploys, how config stays consistent, how the two stay in sync, how drift gets caught. It accreted.

On 2026-07-14 that bill came due, three ways at once:
1. A merged HIGH fix (#84 signal-trap lock) **sat undeployed in the live loop for days** — the pool was restarted but the controller's `$ORCH/play-review.sh` copy was never re-synced.
2. The DSN `127.0.0.1` pin was applied to `serve` + `mxr` but **not** `automerge-tick` → the FACTORY's automerge gate has been dying on the IPv6-loopback wedge (`asyncpg TimeoutError`).
3. The FACTORY's week-1 shadow-dial snapshot **was never taken** — the rhythm is manual and machine-specific.

**Diagnosis (load-bearing):** the half-deploy risk was *already documented in `DEPLOY.md`* and it drifted anyway. That proves the real failure: **documentation-as-discipline fails.** There is no single source of truth for what should be running on each machine, and no loop that proves it is. Our "deploy = remember to cp scripts + restart + apply migrations by hand across surfaces" is **incremental deploy executed by a human** — and that is exactly the class of brittle, accreted glue that made openclaw fail. We purged the openclaw ghost from the *orchestration* model; it survived in the *operations* model. This design kills it there too.

---

## §1 Goals / Non-goals

### Goals
- **G1 — One source of truth for running state.** `origin/main` (a nameable git SHA) *is* the desired state of every machine. Full-stop.
- **G2 — One verified, idempotent deploy.** A single `reconcile.sh` converges a machine to the desired state atomically, idempotently, and leaves a receipt (`RUNNING_SHA`). Re-runnable, no memorized multi-surface checklist.
- **G3 — Drift is loud, not silent.** A machine diverging from `origin/main` (or its installed files diverging from the repo) raises an alert within minutes. Detection and correction are the *same code path* (`reconcile.sh --dry-run`).
- **G4 — FACTORY is pull-only and never hand-edited.** Prod state is always a git SHA you can name. This is the single discipline that prevents the 2026-07-14 incident class.
- **G5 — Extractable / open-source-able.** Zero hardcoded personal paths, usernames, or org names. Everything machine-specific is env/config. A stranger can clone and run.
- **G6 — Compose, don't rebuild.** The substrate must cleanly host the existing features (spine, self-learning ledger, curator, librarian) and the north-star personal agent — without a rewrite.

### Non-goals
- **N1 — No fleet machinery.** No k8s, ArgoCD/Flux servers, Ansible/Puppet/Chef, Docker, Vagrant. This is 2 macOS machines sharing a git checkout, not a cluster. (See §7 rejects.)
- **N2 — No shared database / replication / failover.** Each host keeps its own local Postgres (LAB = scratch/dev data, FACTORY = the production autonomous-loop ledger). Cross-host DB state stays deliberately separate.
- **N3 — No push-based deploy.** No machine holds SSH creds to shove code into the other. Hosts *pull*.
- **N4 — Not a gateway-first agent.** We borrow Hermes's *agent-loop-first* stance, not OpenClaw's fat-gateway stance (§6).
- **N5 — No new alerting stack.** Drift/deploy alerts ride the existing `mxr` / jefe-inbox notification path.

---

## §2 The substrate: GitOps pull-reconcile (the foundation)

**Thesis (borrowed wholesale, machinery rejected):** desired state lives in git; a loop on each host pulls it and reconciles the *whole host* to it. Full-state sync, not incremental deploy — this eliminates drift *by definition* because the deployed state always equals the committed state.

### 2.1 `reconcile.sh` (repo root, ~60–100 lines bash)

One canonical entrypoint, no per-host special-casing (keeps it OSS-able). Pseudocode:

```
reconcile.sh [--dry-run]
  set -euo pipefail
  0. resolve config: MYNDAIX_HOME (default $HOME/.myndaix), MACHINE_ROLE (lab|factory), REPO (script dir)
  1. git fetch origin
  2. desired=$(git rev-parse origin/main); current=$(git rev-parse HEAD)
     # FACTORY: git reset --hard origin/main  (pull-only, never hand-edited → safe & correct)
     # LAB:     refuse reset if working tree dirty (dev machine may have WIP) → warn + skip
  3. install step (idempotent): render + place $ORCH/*.sh (play-review.sh, play-fix.sh, ...),
     render + place launchd plists from templates (§2.3), provision sentinels declared for this role
  4. migrate up: serve auto-migrates, but reconcile VERIFIES migration head == latest file;
     detect + alarm on dirty/failed migration state (never silently wedge — §4)
  5. restart: launchctl kickstart -k <role's services>   (only the services this role owns — §2.4)
  6. commit point: write RUNNING_SHA=$desired to $MYNDAIX_HOME/state/RUNNING_SHA (temp + atomic mv)
  --dry-run: do 1–2 + compute the diff for 3–5 (what WOULD change), write nothing, exit 0/‌nonzero-if-drift
```

**Properties:** atomic (git checkout + atomic healthfile write as the commit point), idempotent (full-state, re-runnable), verifiable (`RUNNING_SHA` is the receipt), pull-only (no inbound SSH). The restart *depends on* fetch+install+migrate succeeding — a failed step aborts before the kickstart, so we never restart onto a half-applied state.

### 2.2 Trigger

- **FACTORY:** a launchd `StartInterval` timer runs `reconcile.sh` on a poll (e.g. every 5–15 min) — pulls `origin/main` and converges. Optionally also a git `post-merge`/`post-receive` hook for immediacy. Poll is the floor guarantee.
- **LAB:** `reconcile.sh` is manual (dev machine; you don't want prod pulling under your WIP). LAB's job is to *push* to `origin/main`; FACTORY converges on its own.

### 2.3 Config & plists — de-personalized (G5)

Today plists hardcode `/Users/stevenfernandez/...` and `/Users/jefe/...`; `repos.json` has absolute venv paths; `AUTHOR_ALLOWLIST={"redeyefit"}`, the jefe inbox, etc. are baked in. The design:
- **Plist templates** (`orchestrator/launchd/*.plist.tmpl`) with `${REPO}`, `${MYNDAIX_HOME}`, `${PYTHON}` placeholders; `reconcile.sh` renders them per-machine at install. No absolute personal paths in the repo.
- **One config file** `$MYNDAIX_HOME/config.env` (git-ignored, per-machine): `MACHINE_ROLE`, `MYNDAIX_DSN` (FACTORY pins `127.0.0.1` here — fixes issue #B), `MYNDAIX_HOME`, `OPERATOR_INBOX`, `AUTHOR_ALLOWLIST`, agent-CLI `PATH` additions. Everything machine-specific lives here; nothing in code.
- **De-personalize code:** `AUTHOR_ALLOWLIST`, the `jefe` inbox recipient, and org names become config-driven with documented defaults. (Inventory §4 is the full blocker list.)

### 2.4 Role model (`MACHINE_ROLE`)

Same repo, asymmetric behavior via one env flag — **not** code branches scattered around, one resolved role object:
- **LAB (`lab`):** runs the interactive `serve` pool + the pre-push review hook. Autonomy loops (controller/automerge/fix-sweep) OFF. May run agents in dry-run / scratch-DB mode.
- **FACTORY (`factory`):** runs `serve` + all autonomy loops (controller, automerge, fix-sweep, disk-cleanup) + the librarian. This is the sole autonomous brain (matches the 2026-06-28 migration reality). Pull-only.
- `reconcile.sh` reads the role to decide which plists/sentinels/services it owns. Adding a third machine later = a new role, no code change.

### 2.5 Drift detection (G3) — same code path as deploy

- **`reconcile.sh --dry-run`** *is* the file/plist/migration drift detector (git status --porcelain + "installed ≠ rendered" + "migration head behind").
- **A FACTORY launchd drift-canary** (StartInterval, cheap): compares `RUNNING_SHA` vs `git ls-remote origin main`; if they diverge for >N minutes, alert via `mxr`/jefe-inbox. This single check catches the *exact* 2026-07-14 incident (merged code silently not running). It does NOT auto-fix (that's reconcile's job on its own timer) — it's the loud smoke alarm.

---

## §3 Data flow (the happy path, end to end)

```
LAB: edit → commit → push feature branch → PR → CI green → merge to main
                                                              │
                                              origin/main advances (new desired SHA)
                                                              │
FACTORY reconcile timer fires ──> git fetch; reset --hard origin/main
                                   ──> render+install $ORCH scripts + plists
                                   ──> migrate up (verify head; alarm if dirty)
                                   ──> kickstart -k factory services
                                   ──> write RUNNING_SHA = new SHA
                                                              │
FACTORY drift-canary ──> RUNNING_SHA == git ls-remote origin main?  ── yes ──> quiet
                                                              └── diverged >N min ──> ALERT jefe-inbox
```

The controller/automerge ticks then re-import the fresh code on their next fire (they already spawn per-tick). No manual step anywhere after `merge`.

---

## §4 Edge cases & failure modes

- **Dirty/failed migration.** A failed migration can leave the DB in a "dirty" state that blocks all further migrations (the golang-migrate failure mode). `reconcile.sh` must *detect* dirty state and alarm — never silently wedge or restart onto it. Migrations stay idempotent (`IF NOT EXISTS`); reconcile treats migrate-failure as abort-before-restart.
- **`reset --hard` on FACTORY nukes local changes.** That is *intended* — FACTORY is pull-only, any local delta is drift to be erased. But: if someone hand-edited FACTORY (the thing we're outlawing), reconcile silently reverts it. Mitigation: reconcile logs any non-empty `git status` it's about to erase (loud, so an accidental edit is visible), and the house rule (G4) forbids the edit in the first place.
- **Reconcile fires mid-tick.** A controller/automerge tick could be running when reconcile kickstarts serve. serve restart is already safe (KeepAlive, leases expire). Ticks are short-lived and re-fire hourly. Low risk; document that reconcile does not interrupt an in-flight review (it swaps code for the *next* tick). Consider a lightweight lock so reconcile and a tick don't both mutate `$ORCH` simultaneously.
- **Network failure during fetch.** Abort clean, keep current state, retry next timer. Never partial-apply.
- **Partial install (crash between step 3 and 6).** Because RUNNING_SHA is written LAST (step 6), a crash mid-install leaves RUNNING_SHA stale → the drift-canary fires → next reconcile re-runs the full idempotent install. Self-healing.
- **LAB working tree dirty on reconcile.** LAB refuses `reset --hard` (protects WIP), warns, and skips — LAB drift is expected and tolerated (it's the dev machine).

---

## §5 Security surface

- **`git reset --hard origin/main` trusts origin.** Origin is our own GitHub repo; the trust root is the same as today's `git pull`. No new exposure. (The untrusted-input surface is the reviewed PR *content*, already handled by the confinement model — unchanged here.)
- **Plist rendering = template + config substitution.** Must not allow config values to inject arbitrary launchd keys (treat `config.env` as trusted-but-validated; it's owner-only `chmod 600`, same tier as `.secrets`).
- **`config.env` / tokens** stay `chmod 600`, git-ignored, per-machine. The automerge PAT handling is unchanged (owner-only file); reconcile never prints it.
- **Pull-only kills the inbound-SSH surface** — no machine can push code into another. This is a security *improvement* over any push-based alternative.
- **Drift-canary alerts** route through the existing sanitized notification path; no raw content forwarded.

---

## §6 The full system: substrate + the north-star agent (Hermes-borrowed)

The substrate (§2–§5) is the foundation. On top of it, the two-machine system *as a whole*:

- **LAB = the lab.** Where Jefe + Mack build, design, and drive interactively. Pushes to `origin/main`.
- **FACTORY = the factory + the always-on brain.** Runs the autonomous review/self-learning loop *and* hosts the personal always-on agent (the librarian → edit/act → operate ladder).

**Borrow from [Hermes Agent](https://github.com/nousresearch/hermes-agent) (validated our shape independently):**
- Its **gateway (control plane) / agent-runtime (worker) / spawnable subagents** split ≈ our runtime-pool + `mxr` dispatch + per-agent workers. Independent convergence de-risks our architecture.
- Its **persistent memory + full-text recall + agent-curated memory files + learning loop** ≈ our curator (tsvector), recall, MEMORY, outcomes-ledger, proactive-skill-capture. Confirms "own the data, rent a swappable brain" is mainstream, not a shortcut.
- Its **single install script, role-selectable** feeds directly into our `reconcile.sh` + `install-launchd.sh`.

**Reject from Hermes:** its 20+ chat-platform gateway sprawl, Modal/Daytona serverless (we have an always-on Mini — that *is* our always-on), its SQLite (we keep Postgres), Honcho user-modeling (over-engineered for a solo operator). And decisively: **agent-loop-first, not gateway-first** — the correct side for our thin-controller / Lobster-is-a-specialist model.

**How the autonomy ladder rides the substrate:** the librarian's rungs (recall → edit/act → operate) are FACTORY-side agent capabilities. Each rung is gated by evals + trust *and* by the substrate guaranteeing FACTORY runs exactly the reviewed SHA. You can't trust an autonomous agent's actions if you can't name the code producing them — the RUNNING_SHA receipt is what makes the autonomy trustworthy. **The substrate is a prerequisite for widening autonomy, not a side quest.**

---

## §7 What we borrow / what we reject

| Capability | Verdict | Borrow | Reject |
|---|---|---|---|
| Multi-host deploy | BORROW-PATTERN + BUILD `reconcile.sh` | GitOps pull-reconcile, full-state sync, desired-state=a-SHA | k8s, ArgoCD/Flux servers, ansible-pull the tool, Helm/Kustomize, blue-green |
| Drift detection | BUILD `--dry-run` + canary | check-mode diff, RUNNING_SHA-vs-remote, alert-on-divergence | Puppet/Chef agents, driftctl, compliance dashboards |
| Personal agent | BORROW-PATTERN (Hermes) | gateway/runtime/subagent split, persistent memory, learning loop, agent-loop-first | chat-platform sprawl, serverless, SQLite, Honcho, gateway-first |
| Dev/prod split | BORROW-PATTERN (12-factor) | FACTORY pull-only + never-hand-edited, role via env, config-not-code | Docker/Vagrant/k8s parity, separate staging, feature-flag services |

---

## §8 Open-source extraction plan (G5)

Make it clonable-by-a-stranger, incrementally:
1. **De-hardcode paths** → plist templates + `config.env` (rendered by reconcile). No `/Users/<name>` in repo.
2. **De-personalize code** → `AUTHOR_ALLOWLIST`, operator inbox, org names become config with documented defaults.
3. **`install-launchd.sh`** → one-shot: render plists, `launchctl bootstrap`, provision `$MYNDAIX_HOME` skeleton + sentinels for the chosen role.
4. **`SETUP.md` rewrite** → "clone → `cp config.env.example config.env` → set role → `./install.sh` → `./reconcile.sh`". Two-machine section becomes "run install with `MACHINE_ROLE=factory` on the always-on box."
5. Extraction is a *consequence* of doing §2–§3 cleanly, not separate work.

---

## §9 Staged build plan (each PR cross-family reviewed before merge)

- **PR-1 — `reconcile.sh` + `RUNNING_SHA` healthfile + config.env resolution.** The core reconcile loop (fetch → role-aware install of `$ORCH` scripts → migrate-verify → kickstart → receipt). Dry-run mode. Replaces the manual `DEPLOY.md` dance. *This alone closes the 2026-07-14 drift class.*
- **PR-2 — plist templating + `install-launchd.sh` + de-hardcoded paths.** Renders launchd from templates; kills the personal-path blockers.
- **PR-3 — drift-canary launchd job** (RUNNING_SHA vs origin) + FACTORY reconcile timer. Turns silent drift loud + makes FACTORY self-converging.
- **PR-4 — config-driven de-personalization** (allowlist, inbox, DSN pin folded in — fixes issue #B properly) + `SETUP.md` rewrite for extraction.
- **PR-5+ — the agent layer** (librarian edit/act rung, Hermes-borrowed structure) rides the now-solid substrate. Separate design pass.

Order rationale: PR-1 is the load-bearing wound-closer and is self-contained; each subsequent PR is independently valuable and reviewable.

---

## §10 Open questions / decisions for Jefe

- **D1 — Reconcile trigger cadence on FACTORY.** Poll every 5 min (fast convergence, more `git fetch`) vs 15 min (quieter) vs post-merge-hook + slow poll fallback. *Recommend:* post-merge webhook/hook for immediacy **+** 15-min poll as the floor.
- **D2 — Does LAB reconcile at all, or stay fully manual?** *Recommend:* LAB stays manual (dev machine); only FACTORY auto-reconciles. LAB gets `reconcile.sh --dry-run` as a pre-push sanity check.
- **D3 — Scope of v1 extraction.** Full open-source-ready in this pass, or de-personalize "enough to be clean" now and finish extraction when actually publishing? *Recommend:* de-personalize as we touch each surface (PR-2/PR-4); don't block the substrate on 100% extraction polish.
- **D4 — Fold the automerge DSN-pin (issue #B) into PR-1 as a quick fix, or wait for PR-4's config.env?** *Recommend:* one-line pin now (separate tiny PR) so the FACTORY automerge gate stops dying while the substrate is built; the proper config.env home lands in PR-4.
- **D5 — Do we retire `DEPLOY.md` + `controller-migration-to-mini.md` into this doc, or keep as history?** *Recommend:* keep as history, add a header pointing here as the live process.

---

## Appendix — grounding

- **Current-state inventory:** full deploy-surface / machine-config / OSS-blocker inventory produced 2026-07-14 (deploy surfaces, `$ORCH` copies, launchd jobs, hardcoded paths). Key confirmations: no automated inter-machine sync exists; no "if mini/macbook" code branching (config-driven already); the half-deploy risk was documented yet still drifted.
- **Prior-art brief:** GitOps pull-reconcile (ArgoCD/Flux/ansible-pull — borrow idea, reject tools), drift = dry-run reconcile + SHA canary, Hermes Agent as the personal-agent reference, 12-factor dev/prod parity, launchd as the macOS-native always-on. Sources cited in the research brief (this session).
</content>
</invoke>
