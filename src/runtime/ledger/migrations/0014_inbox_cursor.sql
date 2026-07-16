-- Migration 0014: inbox_cursor — the Inbox Assistant's per-account Gmail pull state.
--
-- An EXISTING Postgres deployment runs THIS idempotent migration on every serve()
-- boot (behind the migrate() advisory lock), so it MUST be safe to re-run:
-- CREATE ... IF NOT EXISTS only, no destructive backfill.
--
--   psql "$MYNDAIX_DSN" -f src/runtime/ledger/migrations/0014_inbox_cursor.sql
--
-- WHY a table (inbox-assistant DESIGN §3.9): the morning tick pulls each Gmail
-- account incrementally via users.history.list, which needs the last historyId whose
-- slice was fully PROCESSED — advanced only after labels/drafts/brief delivered, never
-- on pull alone, so a crash mid-run re-pulls rather than drops threads. State belongs
-- in the ledger, not files (north-star litmus). Google expires historyIds (a 404 from
-- users.history.list, DESIGN §4 CRITICAL); the tick then re-establishes via a bounded
-- backfill (checkpoint-before-scan: getProfile FIRST) and advances here. This row is
-- inert (read by nothing) until the tick runs, so shipping the migration is safe
-- ahead of the tick.
--
--   account_id — the full Gmail address (one row per INBOX_ACCOUNTS entry).
--   history_id — last historyId whose slice fully PROCESSED; next pull starts here.
--   state      — 'active' (healthy) | 'error' (pull/process failed) | 'stale'
--                (cursor expired, awaiting backfill re-establish). Advance resets
--                to 'active'.
--   attempts   — consecutive failed ticks for this account; reset to 0 on advance.
CREATE TABLE IF NOT EXISTS inbox_cursor (
    account_id text NOT NULL PRIMARY KEY,
    history_id text NOT NULL,
    state      text NOT NULL DEFAULT 'active'
        CHECK (state IN ('active','error','stale')),
    attempts   int  NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);
