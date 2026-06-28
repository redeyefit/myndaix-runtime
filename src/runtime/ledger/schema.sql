-- MyndAIX Team Runtime - ledger (DESIGN.md C2). The durable state machine that
-- replaces file-IPC. The Command API is the ONLY writer. State transitions here
-- are the only legal moves; row-locking + leases give crash recovery and
-- exactly-once-ish behavior.

CREATE TABLE inbound_event (
    id          uuid PRIMARY KEY,
    transport   text NOT NULL,
    envelope    jsonb NOT NULL,                 -- the transport envelope (C3)
    body        text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    dedupe_key  text NOT NULL UNIQUE            -- exactly-once ingest
);

CREATE TABLE job (
    id                  uuid PRIMARY KEY,
    parent_id           uuid REFERENCES job(id),
    root_id             uuid NOT NULL,          -- for cost_budget / chain_ttl over a tree
    depth               int  NOT NULL DEFAULT 0,-- for max_depth admission limit
    created_by          text NOT NULL,          -- agent_id | 'human' | inbound_event id
    inbound_event_id    uuid REFERENCES inbound_event(id),  -- originating transport event; lets outbound resolve transport+reply_target
    to_agent            text NOT NULL,
    body                text NOT NULL,
    context             jsonb NOT NULL DEFAULT '{}',  -- free-form per-job input (e.g. {"image_url": ...}); existing DBs: migrations/0001_add_job_context.sql
    capability_required text,
    priority            int  NOT NULL DEFAULT 0,
    status              text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','leased','running','done','failed','dead')),  -- legal states enforced at the DB
    -- workspace (workspace-actors); base_ref = a prior job's artifact_ref enables chaining
    repo_id       text,
    base_ref      text,
    base_sha      text,
    worktree_path text,
    artifact_ref  text,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX job_queued_idx ON job (priority DESC, created_at) WHERE status = 'queued';
-- exactly-once dispatch: at most ONE job per originating inbound event (idempotent submit)
CREATE UNIQUE INDEX job_one_per_inbound ON job (inbound_event_id) WHERE inbound_event_id IS NOT NULL;

CREATE TABLE attempt (
    id               uuid PRIMARY KEY,
    job_id           uuid NOT NULL REFERENCES job(id),
    worker_id        text NOT NULL,
    lease_expires_at timestamptz NOT NULL,      -- expiry -> reclaim (crashed worker)
    started_at       timestamptz NOT NULL DEFAULT now(),
    ended_at         timestamptz,
    status           text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','ok','failed')),
    result           jsonb,
    error_class      text                       -- retryable|terminal|needs_human
);
CREATE INDEX attempt_lease_idx ON attempt (lease_expires_at) WHERE status = 'open';
-- at most ONE open attempt per job: the hard backstop behind lease_job's CAS (no double-lease)
CREATE UNIQUE INDEX attempt_one_open_per_job ON attempt (job_id) WHERE status = 'open';
-- per-repo concurrency cap (phase2 §4): the HARD count = count(*) open attempts joined to a repo's jobs
CREATE INDEX attempt_status_job_idx ON attempt (status, job_id);

-- per-repo concurrency cap counter (phase2 §4). `active` is a SOFT filter (perf only);
-- the COUNT(*) of open attempts under this row's FOR UPDATE lock at lease time is the HARD
-- cap authority, so counter drift can never breach the cap. Lazily seeded by lease_job
-- (INSERT ... ON CONFLICT DO NOTHING), self-healed by the reconciler. CHECK is a guard:
-- decrements use GREATEST(active-1,0) so it can never fire from correct code.
CREATE TABLE repo_concurrency (
    repo_id text PRIMARY KEY,
    active  int  NOT NULL DEFAULT 0 CHECK (active >= 0)
);

-- append-only progress side channel; NOT part of the hot state machine
CREATE TABLE attempt_log (
    id         bigserial PRIMARY KEY,
    attempt_id uuid NOT NULL REFERENCES attempt(id),
    ts         timestamptz NOT NULL DEFAULT now(),
    stream     text NOT NULL,                   -- stdout|stderr|heartbeat
    chunk      text NOT NULL
);

-- outbox pattern -> reliable, deduped delivery (decoupled from job completion)
CREATE TABLE outbound (
    id              uuid PRIMARY KEY,
    job_id          uuid NOT NULL REFERENCES job(id),
    transport       text NOT NULL,
    reply_target    text NOT NULL,              -- from the inbound envelope
    body            text NOT NULL,
    status          text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','leased','sent','failed')),
    provider_msg_id text UNIQUE,                -- dedupe delivery (exactly-once send)
    tries           int  NOT NULL DEFAULT 0
);
CREATE INDEX outbound_pending_idx ON outbound (status) WHERE status = 'pending';

CREATE TABLE dead_letter (
    id         uuid PRIMARY KEY,
    source_id  uuid NOT NULL,                   -- the job/outbound that exhausted retries
    reason     text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- controller-loop ("the brain") cursor — the proactive review scheduler's only state
-- (DESIGN v0.2 §2). A level-triggered reconciler tracks, per (repo, ref): the last SHA
-- whose review DELIVERED (reviewed_sha, the cursor) and whether one is in flight
-- (pending_sha). baseline_sha is the high-water mark seeded at first sight so a fresh
-- repo is NOT whole-tree-reviewed. Existing DBs: migrations/0003_review_cursor.sql.
CREATE TABLE review_cursor (
    repo_id      text NOT NULL,
    ref          text NOT NULL,
    baseline_sha text NOT NULL,
    reviewed_sha text NOT NULL,
    pending_sha  text,
    state        text NOT NULL DEFAULT 'baseline'
        CHECK (state IN ('baseline','dispatching','delivered','blocked')),
    attempts     int  NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (repo_id, ref)
);

-- docs-only PR auto-merge gate decision log (automerge DESIGN v0.3 §4). One row per
-- (repo, PR, head sha) records the terminal decision so the hourly gate never re-reviews
-- the same head; a new push = a new head = a new row. Existing DBs: migrations/0004_automerge_seen.sql.
CREATE TABLE automerge_seen (
    repo_id    text NOT NULL,
    pr_number  int  NOT NULL CHECK (pr_number > 0),
    head_sha   text NOT NULL,
    decision   text NOT NULL
        CHECK (decision IN ('merged','needs_fix','skipped','error')),
    reason     text,
    decided_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (repo_id, pr_number, head_sha)
);

-- +learning rung (review skills): cache + audit. Mirrors migrations/0005_skill.sql (fresh DBs
-- get this; existing prod gets the migration on serve boot). The BODY lives here (the indexer
-- reads it from a trusted merged ref); selection never rehashes disk.
-- PK is (repo_scope, name): a skill name is unique PER REPO, not globally (kilabz+oracle:
-- a global name PK lets two repos shipping the same skill slug collide on UPSERT).
CREATE TABLE skill (
    name         text NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9._-]*$'),
    description  text NOT NULL CHECK (length(description) <= 60),
    body         text NOT NULL CHECK (length(body) <= 2048),
    body_sha     text NOT NULL,
    content_sha  text NOT NULL,
    repo_scope   text NOT NULL,
    path_trigger text NOT NULL,
    provenance   text NOT NULL DEFAULT 'promoted' CHECK (provenance IN ('promoted')),
    state        text NOT NULL DEFAULT 'active'   CHECK (state IN ('active','stale','archived')),
    last_used_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (repo_scope, name)
);

CREATE TABLE skill_use (
    id          uuid PRIMARY KEY,
    review_play text NOT NULL,
    skill_name  text NOT NULL,
    body_sha    text NOT NULL,
    repo_scope  text NOT NULL,
    used_at     timestamptz NOT NULL DEFAULT now()
);
