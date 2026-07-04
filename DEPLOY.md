# Deploying myndaix-runtime

There are **two deploy targets**, and a change can touch either or both:
1. **`serve`** — the worker pool + API (Python under `src/`, run as a launchd service). Covered
   just below.
2. **the orchestrator** — the autonomous review loop (`play-review.sh` + the `controller`). It has
   its OWN deploy surfaces; see [Orchestrator deploy](#orchestrator-deploy-the-review-loop). A change
   to `orchestrator/play-review.sh` does NOT ship by pulling code + restarting serve — the worker
   runs a TRUSTED INSTALLED COPY, not the repo tree.

## TL;DR (serve)

`serve` now **auto-applies pending migrations on startup**, so the old footgun is gone:
you can deploy new code and just (re)start `serve` — it migrates the schema before it
leases any jobs.

```bash
# pull the new code, then:
MYNDAIX_DSN=postgresql://localhost/runtime PYTHONPATH=src python3 -m runtime.serve
# [serve] schema migrations ensured (idempotent): 0001_add_job_context.sql
# [serve] MyndAIX runtime up: 4-worker pool draining ...
```

On a host where `serve` runs under launchd (the Mini), restart it with:

```bash
launchctl kickstart -k gui/$(id -u)/ai.myndaix.runtime
```

## Orchestrator deploy (the review loop)

The autonomous review loop deploys across **THREE surfaces**. A deploy that updates only some of
them is a **half-deploy** — it looks done but runs a mix of old and new code. This bit us
2026-07-02: `play-review.sh` was updated but the repo tree was left on a stale branch, so the
`controller` half of the same PR silently didn't ship. Update ALL THREE:

1. **Repo working tree — must be on `main` at `origin/main`.** Both the `serve` pool and the
   `controller` launchd job import Python (`src/runtime/controller.py`, `registry.py`, `runner.py`)
   FROM this tree via `PYTHONPATH`. The `controller` spawns fresh each launchd tick, so it picks up
   tree changes on the next tick automatically; `serve` is long-lived and needs the restart above.
   **The Mini is a PULL-ONLY MIRROR** — it must never carry a local commit or sit on a feature
   branch on `main`. Verify with `git branch --show-current` (want `main`) + `git log -1`.

2. **`$ORCH/play-review.sh` (and `play-fix.sh`) — the TRUSTED INSTALLED COPY.** The pre-push hook
   and the controller re-exec the worker from `$ORCH` (`PLAY_SELF=$HOME/.myndaix/orchestrator/
   play-review.sh`), NOT the repo copy — a defense so a push that edits the worktree script can't
   run as the worker. So a `play-review.sh` change ships ONLY when you copy it in:

   ```bash
   cp orchestrator/play-review.sh ~/.myndaix/orchestrator/play-review.sh
   ```

   (When autofix is armed, `orchestrator/autofix-arm.sh arm` does this cp for BOTH scripts + re-runs
   its gates — prefer it on an autofix host. Run it only from a clean, up-to-date `main` checkout.)

3. **`serve` restart** — `launchctl kickstart -k gui/$(id -u)/ai.myndaix.runtime`, to reload
   `registry.py`/`runner.py` into the long-lived pool (e.g. an agent profile-timeout or adapter
   change). Skipped only if the deploy touched nothing serve imports.

**The full Mini deploy, one line** (covers all three surfaces):

```bash
cd ~/code/active/myndaix-runtime && git switch main && git pull --ff-only \
  && cp orchestrator/play-review.sh orchestrator/play-fix.sh ~/.myndaix/orchestrator/ \
  && launchctl kickstart -k gui/$(id -u)/ai.myndaix.runtime
```

Both worker scripts ship because the trusted installed surface is `$ORCH/play-review.sh` AND
`$ORCH/play-fix.sh` — copying only the review script leaves a `play-fix.sh` change live-stale on the
autofix host (the half-deploy this doc exists to prevent).

**Verify the deploy landed** (read-only): `git log -1` (the merge sha), a `grep` for the new code in
the repo `src/runtime/controller.py` AND in BOTH installed workers (`$ORCH/play-review.sh` and
`$ORCH/play-fix.sh`), and a fresh serve pid (`launchctl print gui/$(id -u)/ai.myndaix.runtime | grep
pid`). A claimed deploy that skipped the `cp` runs the OLD worker(s); one that skipped the branch/pull
runs the OLD controller.

## Why this exists

On 2026-06-24 a deploy took dispatch down: `serve` was restarted onto code that read
`job.context` **before** the migration adding that column had been applied, so every
job dispatch errored against the stale schema.

The root cause was a manual ordering rule — *migrate first, then restart* — that is easy
to get backwards. `serve.migrate()` removes the decision: migrations run automatically,
in order, before the worker pool starts. A broken migration is **fail-closed** — `serve`
raises and never comes up, rather than serving a half-migrated DB.

## Fresh database (one time)

```bash
createdb runtime
psql runtime < src/runtime/ledger/schema.sql     # full DDL for a new DB
# first `serve` boot then runs migrations/ idempotently (all no-ops on a fresh DB)
```

## Adding a migration

1. Drop a file in `src/runtime/ledger/migrations/` named `NNNN_description.sql`
   (zero-padded, monotonic — they run in sorted filename order).
2. It **MUST be idempotent** — `serve` re-runs every migration on every boot. Use
   `ADD COLUMN IF NOT EXISTS`, `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT
   EXISTS`, etc. A non-idempotent migration will crash the second boot.
3. Also update `schema.sql` so a fresh DB gets the column directly (schema.sql is plain
   `CREATE`, not re-run; migrations are the path for existing DBs).
4. Add/extend a test in `tests/test_postgres_ledger.py`
   (see `test_zz_migrate_heals_stale_schema`).

## Manual fallback

If you ever need to apply a migration by hand (e.g. migrating a DB without deploying
new code):

```bash
psql "$MYNDAIX_DSN" -f src/runtime/ledger/migrations/0001_add_job_context.sql
```

Because migrations are idempotent, running one by hand and then letting `serve`
re-run it on boot is harmless.
