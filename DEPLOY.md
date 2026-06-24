# Deploying myndaix-runtime

## TL;DR

`serve` now **auto-applies pending migrations on startup**, so the old footgun is gone:
you can deploy new code and just (re)start `serve` — it migrates the schema before it
leases any jobs.

```bash
# pull the new code, then:
MYNDAIX_DSN=postgresql://localhost/runtime PYTHONPATH=src python3 -m runtime.serve
# [serve] schema migrations ensured (idempotent): 0001_add_job_context.sql
# [serve] MyndAIX runtime up: 4-worker pool draining ...
```

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
