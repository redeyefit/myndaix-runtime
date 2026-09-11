-- Migration 0016: knowledge_scope_gen — the per-scope commit-generation counter that IS the
-- walk fence from here on.
--
-- WHY (review 20260910200253 P2, kilabz, confirmed): the fence was MAX(seq) over knowledge_doc,
-- which only advances when a row is INSERTED. An accepted TRUE NO-OP sync moves nothing — e.g.
-- a file created then deleted before either walker commits needs no tombstone because it was
-- never indexed — so a stale walker holding the older fence still passes the equality check and
-- commits the deleted file back to active. The fix: this counter bumps under the per-scope
-- advisory lock on EVERY accepted sync/rebuild, including true no-ops; conflict rejects return
-- before the bump so a rejected walk still mutates nothing.
--
-- Idempotent (IF NOT EXISTS / ON CONFLICT DO NOTHING) — re-run on every serve() boot under
-- migrate()'s advisory lock.
CREATE TABLE IF NOT EXISTS knowledge_scope_gen (
    scope text PRIMARY KEY,
    gen   bigint NOT NULL DEFAULT 0
);

-- Seed existing scopes from the OLD fence value (MAX seq) so a fence stamped under the old
-- semantics still compares correctly across the upgrade: no interleaving commit keeps equality
-- (accept), any accepted commit bumps past it (conflict). Scopes with no doc rows start at 0
-- via the readers' COALESCE; the ON CONFLICT keeps every re-run from clobbering live counters.
INSERT INTO knowledge_scope_gen (scope, gen)
SELECT scope, MAX(seq) FROM knowledge_doc GROUP BY scope
ON CONFLICT (scope) DO NOTHING;
