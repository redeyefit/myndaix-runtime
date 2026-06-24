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
