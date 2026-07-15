# Two-Machine Substrate — Operator Guide (PR-1)

The GitOps pull-reconcile substrate. `origin/main` is the desired state; one idempotent
`reconcile.sh` converges a machine to it; drift is loud. Full rationale + review history:
`docs/two-machine-system-design.md` (v0.3, twice cross-family reviewed).

> **PR-1 scope.** This ships the substrate mechanism + its trust boundary. **Deferred:**
> serve-pool plist ownership + venv relocation (PR-1b — reconcile restarts serve via
> `kickstart` on the existing hand-managed label for now); config-driven de-personalization
> of the inbox / author-allowlist / DSN defaults (PR-2). Merging PR-1 does **not** change the
> currently-running loop until you deliberately perform the topology migration below.

## The three areas (all under `$MYNDAIX_HOME`, e.g. `~/.myndaix`)

| Area | Path | Rule |
|---|---|---|
| **DEPLOY CLONE** | `$MYNDAIX_HOME/deploy/myndaix-runtime` | Pull-only. `reset --hard`-safe. launchd resolves code here. NEVER hand-edited / used for git work. |
| **WORK AREA** | `$MYNDAIX_HOME/work` (+ play-fix `/tmp` scratch) | Ephemeral clones/worktrees for autonomous verify. Network-denied sandbox — never the live ledger. |
| **STATE** | `$MYNDAIX_HOME` (`config.env`, `state/`, `orchestrator/`, `bin/`, Postgres) | ALL mutable state. Nothing mutable under a checkout. |

## `config.env` (per-machine, `chmod 600`, git-ignored)

Copy `substrate/config.env.example` → `$MYNDAIX_HOME/config.env` and fill in. It is **parsed,
never sourced** (`config_parse.py` — strict `KEY=value`, fail-closed on a bad/missing
`MACHINE_ROLE`, no shell execution). Keys: `MACHINE_ROLE` (`lab`|`factory`), `MYNDAIX_HOME`,
`MYNDAIX_DSN` (factory pins `127.0.0.1`), `MYNDAIX_WORK_DSN` (optional scratch), `OPERATOR_INBOX`,
`AUTHOR_ALLOWLIST`, `AGENT_CLI_PATH`, `POLL_INTERVAL_S`, `DEPLOY_CLONE`.

## Roles

- **`lab`** (MacBook): interactive `serve` + pre-push review hook, autonomy OFF. Runs only
  `reconcile.sh --dry-run` (a pre-push drift sanity check) — never a full converge.
- **`factory`** (Mini): `serve` + all autonomy ticks + the 15-min reconcile poll + drift-canary.
  The deploy clone is pull-only; converge is factory-only.

## Commands

```
reconcile.sh                 # converge to origin/main (factory; via Stage-0 bootstrap-fetch)
reconcile.sh --dry-run       # non-destructive drift report (any machine); exit 1 on drift
reconcile.sh --update-bootstrap   # (re)install the static $MYNDAIX_HOME/bin/bootstrap-fetch
manifest.py build|check <config.env>   # the artifact receipt / drift core
```

`bootstrap-fetch` is the **static Stage-0 fetcher** installed to `$MYNDAIX_HOME/bin/` (NOT
auto-overwritten by reconcile — a broken reconcile in origin/main can't brick the fetch). It
quiesces the mutating ticks, `reset --hard origin/main`s the deploy clone, then re-execs the
fresh `reconcile.sh` exactly once (`$RECONCILE_BOOTSTRAPPED` guard).

## FACTORY topology migration (one-time, deliberate — do LAB dry-run first)

1. `git clone <origin> $MYNDAIX_HOME/deploy/myndaix-runtime` (the pull-only deploy clone).
2. Create `$MYNDAIX_HOME/deploy/myndaix-runtime/.venv` and `pip install -e .` (in-tree for PR-1).
3. Write `$MYNDAIX_HOME/config.env` (`MACHINE_ROLE=factory`, pin DSN to `127.0.0.1`, set
   `AUTHOR_ALLOWLIST` + `OPERATOR_INBOX`, `DEPLOY_CLONE=$MYNDAIX_HOME/deploy/myndaix-runtime`).
4. Point `$MYNDAIX_HOME/orchestrator/repos.json`'s runtime entry at a **work clone** (NOT the
   deploy clone) so review/fix git work never mutates the pull-only clone.
5. From the deploy clone: `substrate/reconcile.sh --update-bootstrap`, then `substrate/reconcile.sh`.
   reconcile renders + installs the tick / poll / drift-canary plists (injecting
   `PLAY_SELF=<deploy-clone>/orchestrator/play-review.sh` — Option A, so the `$ORCH` script
   copies are no longer referenced), restarts serve, waits for the migration head, starts the
   ticks, and writes the `state/RUNNING_SHA` + `state/manifest.json` receipt.

Rollback: the old enumerated launchd labels are unchanged names — `launchctl bootout` + re-bootstrap
the prior plists, or `git reset` the deploy clone to a known SHA and re-run reconcile.

## Test

`substrate/test.sh` — fixture-only smoke + security harness (config fail-closed, plist
XML-injection safety, dry-run non-destructiveness, M4 denylist, migration-head pin, sandbox
isolation, shellcheck). The launchd-bootstrap / serve-restart / live psql migration probe are
**live-verified at deploy (LAB first)** — they need real launchd + a running serve + Postgres.
