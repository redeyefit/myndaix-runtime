-- Migration 0007: capture_candidate — the auto-capture rung ("the proposer") recurrence ledger.
--
-- Idempotent (CREATE ... IF NOT EXISTS); re-run on every serve() boot under migrate()'s advisory
-- lock. Inert until the auto-capture proposer runs. Design: docs/auto-capture-design.md.
--
-- WHY: the +learning rung injects HUMAN-seeded skills; a corpus that depends on the human
-- remembering to seed it won't get fed. Auto-capture watches our OWN reviewers: when the same
-- file-class keeps drawing NEEDS-FIX, it DRAFTS a skills/<name>/SKILL.md and opens a PR — the
-- proposer NEVER promotes (skills/ is denylisted from auto-merge; the human merge under branch
-- protection is the unchanged promotion). This table is the DETERMINISTIC recurrence counter (no
-- LLM in the trigger): a row per (repo, normalized path-glob); `seen_count` crosses a threshold ->
-- propose ONCE. State is a strict lifecycle; archive-not-delete (a declined candidate is remembered
-- so it isn't re-proposed on the next recurrence).
CREATE TABLE IF NOT EXISTS capture_candidate (
    fingerprint  text PRIMARY KEY,                 -- sha256(repo_scope \0 path_glob): the recurrence class key
    repo_scope   text NOT NULL,
    path_glob    text NOT NULL,                     -- the path_trigger a proposed skill would carry
    seen_count   int  NOT NULL DEFAULT 1 CHECK (seen_count >= 1),
    state        text NOT NULL DEFAULT 'candidate'
                 CHECK (state IN ('candidate','proposed','promoted','declined')),
    pr_number    int,                               -- the open proposal PR (when state='proposed')
    first_seen   timestamptz NOT NULL DEFAULT now(),
    last_seen    timestamptz NOT NULL DEFAULT now()
);
