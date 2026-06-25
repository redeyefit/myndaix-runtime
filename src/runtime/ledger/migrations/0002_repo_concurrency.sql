-- Migration 0002: per-repo concurrency cap (DESIGN phase2 §4 Part A).
--
-- schema.sql is fresh-DB DDL (run once by init_schema). An EXISTING Postgres
-- deployment runs THIS idempotent migration on every serve() boot (behind the
-- migrate() advisory lock), so it MUST be safe to re-run: CREATE ... IF NOT EXISTS
-- only, and the backfill recomputes ABSOLUTE truth (not an additive delta) so a
-- second run converges instead of double-counting.
--
--   psql "$MYNDAIX_DSN" -f src/runtime/ledger/migrations/0002_repo_concurrency.sql

-- The soft-filter / hard-count counter. One row per repo, lazily seeded by
-- lease_job. Correctness NEVER depends on `active` being exact: the COUNT(*) of
-- open attempts taken under this row's FOR UPDATE lock at lease time is the HARD
-- cap authority. `active` only keeps workers off likely-capped repos (perf), and
-- is self-healed by the reconciler. CHECK(active>=0) is a guard: every decrement
-- uses GREATEST(active-1,0) so it can never fire from our code — it catches a
-- future bug loudly instead of silently corrupting the soft filter.
CREATE TABLE IF NOT EXISTS repo_concurrency (
    repo_id text PRIMARY KEY,
    active  int  NOT NULL DEFAULT 0 CHECK (active >= 0)
);

-- Day-1 index for the HARD count: count(*) open attempts for a repo joins attempt
-- by job_id and filters status='open'. (status, job_id) serves the count after the
-- join key; open attempts are a small hot set.
CREATE INDEX IF NOT EXISTS attempt_status_job_idx ON attempt (status, job_id);

-- Confirm the queued PICK index exists even on a DB created before schema.sql
-- shipped it (idempotent twin of job_queued_idx). Matches the lease PICK's
-- ORDER BY priority DESC, created_at under status='queued'.
CREATE INDEX IF NOT EXISTS job_queued_idx
    ON job (priority DESC, created_at) WHERE status = 'queued';

-- One-time, idempotent backfill: seed active to CURRENT truth = count of open
-- attempts per repo whose job is live (leased/running) — the SAME predicate the
-- hard count and the reconciler use, so a fresh migrate and the first reconcile
-- agree. NULL repo_id is cap-EXEMPT and never gets a row. Idempotent via
-- ON CONFLICT DO UPDATE = the recomputed count (absolute, not active+delta).
INSERT INTO repo_concurrency (repo_id, active)
SELECT j.repo_id, count(*)
  FROM attempt a
  JOIN job j ON j.id = a.job_id
 WHERE a.status = 'open'
   AND j.repo_id IS NOT NULL
   AND j.status IN ('leased','running')
 GROUP BY j.repo_id
ON CONFLICT (repo_id) DO UPDATE SET active = EXCLUDED.active;

-- The upsert above only touches repos that CURRENTLY have live open attempts. On a
-- re-run, a row whose repo has drained to zero open attempts would otherwise keep its
-- old (stale-high) active until the slow reconciler. Zero those too, so the migration
-- converges to ABSOLUTE truth on every run (mirrors the reconciler's drift-down step).
UPDATE repo_concurrency SET active = 0
 WHERE active <> 0
   AND NOT EXISTS (
       SELECT 1 FROM attempt a JOIN job j ON j.id = a.job_id
        WHERE a.status = 'open' AND j.repo_id = repo_concurrency.repo_id
          AND j.status IN ('leased','running'));
