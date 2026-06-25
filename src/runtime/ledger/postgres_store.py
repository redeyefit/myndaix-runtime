"""asyncpg-backed PostgresLedger - the production CommandAPI (DESIGN.md C2).

The SQLite store (sqlite_store.py) and this one implement the SAME Command-API
verbs; persistence swaps behind the contract. This is where the design's core
thesis lives: row-locking + a status-guarded state machine end the concurrency
bug classes that file-IPC caused (double-lease, lost update, dup delivery, a
crashed worker's job stuck forever).

Design rules, enforced everywhere below:
  * ONE transaction per verb. The Command API is the only writer.
  * Every transition is a status-guarded compare-and-swap:
        UPDATE ... WHERE <pk> AND status IN (<legal sources>) RETURNING id
    zero rows back => the move was illegal/already-done => the named no-op path.
  * Where two writers can collide on the same row we take a row lock:
        FOR UPDATE          - serialize (admission, complete-vs-reclaim)
        FOR UPDATE SKIP LOCKED - hand DISTINCT rows to concurrent workers (lease, claim)
  * READ COMMITTED isolation (Postgres default) is sufficient: correctness comes
    from the locks + the WHERE re-check, not from snapshot isolation.
  * ONE canonical lock order everywhere: attempt-row THEN job-row THEN
    repo_concurrency-row (the per-repo cap counter is ALWAYS locked LAST). Holding
    to a single order is what keeps the locks deadlock-free, so no verb raises a
    serialization/deadlock error and no retry loop is needed. (A cancel() that
    locked job-then-attempt formed an ABBA cycle with complete/fail/reclaim - a
    real deadlock caught by adversarial review; cancel now locks attempt-first.)
    lease_job locks the job (PICK) before the rc row, and its hard COUNT is a
    NON-locking read, so it never holds an attempt lock while waiting on the job.
    Multi-repo writers (reclaim batch, reconciler) lock rc rows in repo_id order.
  * authority (retry-safety) is NOT in the DB - only job.to_agent is - so the
    retry decision consults the registry (fail-closed on unknown agents).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import uuid
from pathlib import Path
from typing import Optional
from uuid import UUID

import asyncpg

from runtime import registry
from runtime.contracts import (
    Authority, ErrorClass, Job, LostLease, Result, TransportEnvelope,
)

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_MIGRATIONS_DIR = Path(__file__).with_name("migrations")
# Fixed advisory-lock key so concurrent serve boots serialize their migrate() and
# don't race the same DDL. Arbitrary stable bigint ("mxrMIG").
_MIGRATE_LOCK_KEY = 0x6D7872_4D4947


def _new_id() -> str:
    return str(uuid.uuid4())


def _json(obj: dict) -> dict:
    """Pass-through; the jsonb codec (registered per connection) does json.dumps."""
    return obj


class PostgresLedger:
    # policy constants - NOT part of the Protocol; tuned at construction
    # LEASE + HEARTBEAT are set TOGETHER (~600, design M4). LEASE_SECONDS is the
    # window a fresh attempt gets; HEARTBEAT_SECONDS is the EXTEND amount each ping
    # pushes expiry to (must be >= LEASE so a ping never SHRINKS the window). The pool's
    # FIRING cadence (heartbeat_interval_s, a separate quantity) must be <= lease/3.
    # Tradeoff: a bigger lease means a crashed worker holds its repo slot up to ~LEASE
    # before reclaim frees it — mitigated by a tight janitor reclaim cadence.
    LEASE_SECONDS = 600          # a fresh lease; heartbeat extends long jobs
    HEARTBEAT_SECONDS = 600      # each heartbeat pushes expiry to now()+this (>= LEASE)
    RECLAIM_BATCH = 100          # reclaim_expired processes at most this per call
    OUTBOUND_MAX_TRIES = 5       # mark_outbound_failed exhaustion threshold
    MAX_CHILDREN = 32            # admission: max direct children per parent
    MAX_DEPTH = 8                # admission: max job-tree depth
    MAX_ATTEMPTS = 3             # poison ceiling: a job that fails/crashes this many
                                 # times stops requeuing -> dead + dead_letter (both
                                 # fail_attempt and reclaim_expired enforce it)
    # per-repo concurrency cap (design §4). FEATURE FLAG: a huge value disables the cap
    # (soft filter and hard count never trip) for instant rollback WITHOUT a code revert
    # — set $MYNDAIX_MAX_PER_REPO=1000000 in the pool's env and restart. Default 4.
    MAX_PER_REPO = int(os.environ.get("MYNDAIX_MAX_PER_REPO") or 4)
    LEASE_MAX_REPICKS = 16       # bounded re-PICK budget per lease_job call: each over-cap
                                 # repo is excluded from the next PICK, so the eligible set
                                 # strictly shrinks (anti-spin). Exhaustion -> None (re-poll).

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    # ---- lifecycle ---------------------------------------------------------
    @classmethod
    async def connect(cls, dsn: str, *, min_size: int = 2, max_size: int = 32) -> "PostgresLedger":
        pool = await asyncpg.create_pool(
            dsn, min_size=min_size, max_size=max_size,
            command_timeout=30.0, init=cls._init_connection,
        )
        return cls(pool)

    @staticmethod
    async def _init_connection(con: asyncpg.Connection) -> None:
        # dict <-> jsonb/json. Registered once per physical connection.
        for typ in ("jsonb", "json"):
            await con.set_type_codec(
                typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")

    async def close(self) -> None:
        await self._pool.close()

    async def init_schema(self) -> None:
        """Run schema.sql (DDL is plain CREATE, so call once on a fresh DB)."""
        sql = _SCHEMA_PATH.read_text()
        async with self._pool.acquire() as con:
            await con.execute(sql)

    async def migrate(self) -> list[str]:
        """Apply every migrations/*.sql in filename order, idempotently.

        serve() calls this on startup so deploying new code can NEVER run against a
        stale schema. That ordering footgun took dispatch down on 2026-06-24: serve was
        restarted onto code reading job.context before the column existed, so every
        dispatch errored. Auto-migrate-on-start removes the ordering decision entirely.

        Migrations MUST be idempotent (CREATE/ALTER ... IF NOT EXISTS) — they are
        re-run on every boot, including ones already applied by hand on prod. A pg
        advisory lock serializes concurrent serve instances so two boots can't race the
        same DDL. A failing migration raises (fail-closed: serve refuses to come up on a
        broken schema rather than serve a half-migrated DB). Returns filenames applied."""
        files = sorted(_MIGRATIONS_DIR.glob("*.sql"))
        if not files:
            return []
        async with self._pool.acquire() as con:
            await con.execute("SELECT pg_advisory_lock($1)", _MIGRATE_LOCK_KEY)
            try:
                for path in files:
                    await con.execute(path.read_text())
            finally:
                await con.execute("SELECT pg_advisory_unlock($1)", _MIGRATE_LOCK_KEY)
        return [p.name for p in files]

    # ---- helpers -----------------------------------------------------------
    @staticmethod
    def _requeue_safe(to_agent: str) -> bool:
        """Authority-driven retry safety. FAIL-CLOSED: a workspace_actor (or an
        unknown agent, or a composite) is never auto-requeued - replaying a
        partial file mutation is unsafe. Only responders/controllers requeue.

        NOTE: the registry is in-memory today, so this is non-blocking even though
        fail_attempt/reclaim_expired call it while holding row locks. If the
        registry ever becomes I/O-backed, precompute the authority map OUTSIDE the
        transaction so no I/O happens under a lock."""
        try:
            authority = registry.get(to_agent).authority
        except Exception:
            return False
        return authority in (Authority.RESPONDER, Authority.CONTROLLER)

    # ---- ingest / dispatch -------------------------------------------------
    async def ingest_inbound(self, envelope: TransportEnvelope, body: str) -> UUID:
        """Create an inbound_event; dedupe on envelope.dedupe_key (exactly-once
        ingest). A duplicate returns the ORIGINAL id and raises nothing - the
        loser must not go on to submit a second job."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """INSERT INTO inbound_event (id, transport, envelope, body, dedupe_key)
                   VALUES ($1, $2, $3, $4, $5)
                   ON CONFLICT (dedupe_key) DO UPDATE SET dedupe_key = inbound_event.dedupe_key
                   RETURNING id""",
                _new_id(), envelope.transport, _json(envelope.model_dump(mode="json")),
                body, envelope.dedupe_key)
            return row["id"]

    async def submit_job(
        self, *, to_agent: str, prompt: str,
        context: Optional[dict] = None,
        parent_id: Optional[UUID] = None,
        inbound_event_id: Optional[UUID] = None,
        created_by: str = "human",
        repo_id: Optional[str] = None, base_ref: Optional[str] = None,
        priority: int = 0,
    ) -> UUID:
        """Queue a job. Admission (max_depth / max_children) is serialized by a
        FOR UPDATE lock on the parent row, so concurrent siblings funnel one at a
        time and the limit holds under load. Rejected -> a 'dead' job + dead_letter
        (returns a real id get_status can report)."""
        jid = _new_id()
        async with self._pool.acquire() as con:
            async with con.transaction():
                depth = 0
                root_id = jid                       # a root job is its own tree root
                if parent_id is not None:
                    parent = await con.fetchrow(
                        "SELECT depth, root_id, status FROM job WHERE id = $1 FOR UPDATE",
                        parent_id)
                    if parent is None:
                        raise ValueError(f"submit_job: unknown parent_id {parent_id}")
                    depth = parent["depth"] + 1
                    root_id = parent["root_id"]
                    nkids = await con.fetchval(
                        "SELECT count(*) FROM job WHERE parent_id = $1 AND status <> 'dead'",
                        parent_id)
                    # reject a child of a terminal parent (cancel() containment) OR over-limit
                    parent_live = parent["status"] in ("queued", "leased", "running")
                    reason = None
                    if not parent_live:
                        reason = (f"admission rejected: parent {parent_id} is "
                                  f"'{parent['status']}', not live")
                    elif depth > self.MAX_DEPTH or nkids >= self.MAX_CHILDREN:
                        reason = (f"admission rejected: depth={depth} (max {self.MAX_DEPTH}), "
                                  f"children={nkids} (max {self.MAX_CHILDREN})")
                    if reason is not None:
                        await con.execute(
                            """INSERT INTO job (id, parent_id, root_id, depth, created_by,
                                   inbound_event_id, to_agent, body, context, priority, status)
                               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'dead')""",
                            jid, parent_id, root_id, depth, created_by, inbound_event_id,
                            to_agent, prompt, context or {}, priority)
                        await con.execute(
                            "INSERT INTO dead_letter (id, source_id, reason) VALUES ($1,$2,$3)",
                            _new_id(), jid, reason)
                        return jid
                try:
                    async with con.transaction():  # savepoint around the insert
                        await con.execute(
                            """INSERT INTO job (id, parent_id, root_id, depth, created_by,
                                   inbound_event_id, to_agent, body, context, repo_id, base_ref,
                                   priority, status)
                               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,'queued')""",
                            jid, parent_id, root_id, depth, created_by, inbound_event_id,
                            to_agent, prompt, context or {}, repo_id, base_ref, priority)
                except asyncpg.UniqueViolationError:
                    # a job for this inbound_event already exists -> idempotent dispatch
                    # (a redelivered message must not spawn a second job + second reply).
                    return await con.fetchval(
                        "SELECT id FROM job WHERE inbound_event_id = $1", inbound_event_id)
        return jid

    # ---- worker lifecycle --------------------------------------------------
    # ONE INSERT-attempt CTE used by both the NULL-repo (cap-exempt) and the capped
    # CLAIM paths: flip job queued->leased and open the attempt in one statement.
    _CLAIM_SQL = """WITH leased AS (
                        UPDATE job SET status = 'leased'
                         WHERE id = $1 AND status = 'queued'
                        RETURNING id
                    )
                    INSERT INTO attempt (id, job_id, worker_id, lease_expires_at, status)
                    SELECT $2, id, $3,
                           statement_timestamp() + ($4 * interval '1 second'), 'open'
                      FROM leased
                    RETURNING id"""

    async def lease_job(self, worker_id: str, capabilities: list[str]) -> Optional[UUID]:
        """Atomically lease ONE queued job and open its attempt, enforcing the per-repo
        cap (design §4): PICK -> LOCK+SEED -> HARD-COUNT -> CLAIM, with a bounded re-PICK.

        PICK applies the SOFT filter (repo_id IS NULL OR cached active < cap) under
        FOR UPDATE OF j SKIP LOCKED LIMIT 1, so racing workers get DIFFERENT job rows.
        For a non-NULL repo we then seed+lock the rc row and take the HARD AUTHORITY:
        count(*) of OPEN attempts on the repo's LEASED/RUNNING jobs, UNDER that rc lock.
        < cap -> CLAIM (job->leased, INSERT attempt, set active=count+1). >= cap -> the
        soft filter was stale; sync the cache up and re-PICK with this repo EXCLUDED
        (so no spin). NULL repo_id is cap-EXEMPT and claims directly without touching rc.

        Because the cached `active` is only a soft filter, drift can never breach the
        cap — only the count-under-lock gates admission. Lock order is the canonical
        attempt -> job -> repo_concurrency (counter LAST): PICK locks the job (FOR UPDATE
        OF j); the rc row is locked AFTER, and the hard COUNT is a NON-locking read, so
        lease never holds an attempt lock while waiting on the job (no ABBA / 40P01).
        The hard count is scoped to leased/running jobs, so an orphan open attempt on an
        already-dead job (a transient cancel-race artifact) can never inflate the count.

        `capabilities` is accepted (Protocol) but capability-gated routing is a later
        slice: filtering on it now would only STARVE a gated job."""
        skip_repos: list[str] = []   # repos found AT-CAP this call; excluded from re-PICK
        async with self._pool.acquire() as con:
            for _ in range(self.LEASE_MAX_REPICKS):
                # one transaction per iteration: a re-PICK 'continue' COMMITS, releasing
                # the rejected candidate's job lock + the rc lock before the next PICK, so
                # locks never accumulate across iterations (no queue-wide serialization).
                async with con.transaction():
                    picked = await con.fetchrow(
                        """SELECT j.id, j.repo_id FROM job j
                             LEFT JOIN repo_concurrency rc ON rc.repo_id = j.repo_id
                            WHERE j.status = 'queued'
                              AND NOT EXISTS (SELECT 1 FROM attempt a
                                               WHERE a.job_id = j.id AND a.status = 'open')
                              AND (j.repo_id IS NULL OR COALESCE(rc.active, 0) < $1)
                              AND (j.repo_id IS NULL OR j.repo_id <> ALL($2::text[]))
                            ORDER BY j.priority DESC, j.created_at, j.id
                            FOR UPDATE OF j SKIP LOCKED
                            LIMIT 1""",
                        self.MAX_PER_REPO, skip_repos)
                    if picked is None:
                        return None                       # nothing leasable right now
                    repo_id = picked["repo_id"]
                    attempt_id = _new_id()

                    if repo_id is None:                   # NULL repo -> cap-EXEMPT
                        leased = await con.fetchval(
                            self._CLAIM_SQL, picked["id"], attempt_id, worker_id,
                            self.LEASE_SECONDS)
                        if leased is not None:
                            return leased
                        continue                          # lost the status race -> re-PICK

                    # SEED then LOCK the rc row (B4: ON CONFLICT DO NOTHING doesn't lock the
                    # conflicting row, so a separate FOR UPDATE select is required). Counter
                    # locked LAST, after the job row from PICK -> canonical order.
                    await con.execute(
                        "INSERT INTO repo_concurrency (repo_id, active) VALUES ($1, 0)"
                        " ON CONFLICT (repo_id) DO NOTHING", repo_id)
                    await con.fetchval(
                        "SELECT active FROM repo_concurrency WHERE repo_id = $1 FOR UPDATE",
                        repo_id)
                    # HARD authority: reality under the rc lock (NON-locking read of attempt).
                    open_now = await con.fetchval(
                        """SELECT count(*) FROM attempt a JOIN job j2 ON j2.id = a.job_id
                            WHERE a.status = 'open' AND j2.repo_id = $1
                              AND j2.status IN ('leased','running')""",
                        repo_id)
                    if open_now >= self.MAX_PER_REPO:
                        # cache was stale-low: sync it up to truth so OTHER workers' soft
                        # filter excludes this repo too, then exclude it from THIS call's
                        # next PICK (deterministic anti-spin) and re-PICK.
                        await con.execute(
                            "UPDATE repo_concurrency SET active = $2 WHERE repo_id = $1",
                            repo_id, open_now)
                        skip_repos.append(repo_id)
                        continue

                    leased = await con.fetchval(
                        self._CLAIM_SQL, picked["id"], attempt_id, worker_id,
                        self.LEASE_SECONDS)
                    if leased is None:
                        continue                          # job no longer queued -> re-PICK
                    # keep the soft filter honest: active := the true count we measured + 1
                    # (absolute, so it self-corrects prior increment-side drift too).
                    await con.execute(
                        "UPDATE repo_concurrency SET active = $2 WHERE repo_id = $1",
                        repo_id, open_now + 1)
                    return leased
            return None   # re-PICK budget exhausted (all eligible repos capped) -> re-poll

    async def get_attempt_job(self, attempt_id: UUID) -> Optional[Job]:
        """Internal: the Job a worker should run after leasing (the Protocol's
        lease_job returns only an id; a worker needs the prompt/repo to run).
        Returns None if the lease is no longer valid (attempt closed / job
        cancelled or reclaimed) so the worker skips running already-discarded work."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """SELECT j.id, j.to_agent, j.body, j.context, j.repo_id, j.base_ref,
                          j.base_sha, j.worktree_path
                     FROM job j JOIN attempt a ON a.job_id = j.id
                    WHERE a.id = $1 AND a.status = 'open'
                      AND j.status IN ('leased','running')""", attempt_id)
        if row is None:
            return None
        return Job(id=row["id"], to_agent=row["to_agent"], prompt=row["body"],
                   context=row["context"] or {},
                   repo_id=row["repo_id"], base_ref=row["base_ref"],
                   base_sha=row["base_sha"], worktree_path=row["worktree_path"])

    async def heartbeat_attempt(self, attempt_id: UUID) -> None:
        """Extend the lease so a long job isn't reclaimed as crashed. A no-op on a
        closed/reclaimed attempt -> raise LostLease so the zombie worker aborts
        and never later calls complete_attempt."""
        async with self._pool.acquire() as con:
            row = await con.fetchval(
                """UPDATE attempt a
                      SET lease_expires_at = statement_timestamp() + ($2 * interval '1 second')
                     FROM job j
                    WHERE a.id = $1 AND a.status = 'open'
                      AND j.id = a.job_id AND j.status IN ('leased','running')
                   RETURNING a.id""",
                attempt_id, self.HEARTBEAT_SECONDS)
        if row is None:
            raise LostLease(f"heartbeat: attempt {attempt_id} no longer open")

    async def complete_attempt(self, attempt_id: UUID, result: Result) -> None:
        """attempt open->ok AND job leased/running->done, in one tx, locking BOTH
        rows (FOR UPDATE OF a,j - attempt first, the canonical order). Atomically
        queues the reply (transactional outbox) for a transport-originated job, so
        a 'done' job can never lose its reply. Zero rows back means the lease was
        reclaimed out from under this worker -> LostLease (NOT a harmless no-op).

        Policy (intentional): a worker that finishes AFTER its lease expired but
        BEFORE reclaim_expired runs still wins - 'finished-before-the-janitor-
        noticed'. reclaim's SKIP LOCKED yields to the in-flight completion."""
        # ONE statement (no explicit BEGIN round-trip, so it stays fast enough to
        # win the complete-vs-reclaim race): the `reply` data-modifying CTE always
        # executes and queues the outbound atomically (no-op for a controller job
        # with no inbound_event), while the final SELECT returns the job id - or
        # nothing, meaning the lease was reclaimed (LostLease).
        async with self._pool.acquire() as con:
            job_id = await con.fetchval(
                """WITH locked AS (
                       SELECT a.id, a.job_id
                         FROM attempt a JOIN job j ON j.id = a.job_id
                        WHERE a.id = $1 AND a.status = 'open'
                          AND j.status IN ('leased','running')
                        FOR UPDATE OF a, j
                   ),
                   closed AS (
                       UPDATE attempt a
                          SET status = 'ok', ended_at = statement_timestamp(), result = $2
                         FROM locked l WHERE a.id = l.id
                       RETURNING a.job_id
                   ),
                   dec AS (   -- per-repo cap: decrement gated on the attempt open->ok
                              -- close (RETURNING job_id), counter LAST. NULL repo_id ->
                              -- no matching rc row -> no-op (cap-exempt). GREATEST floors drift.
                       UPDATE repo_concurrency rc
                          SET active = GREATEST(rc.active - 1, 0)
                         FROM closed c JOIN job j2 ON j2.id = c.job_id
                        WHERE j2.repo_id IS NOT NULL AND rc.repo_id = j2.repo_id
                       RETURNING rc.repo_id
                   ),
                   done AS (
                       UPDATE job j SET status = 'done', artifact_ref = $3
                         FROM closed c
                        WHERE j.id = c.job_id AND j.status IN ('leased','running')
                       RETURNING j.id, j.inbound_event_id
                   ),
                   reply AS (
                       INSERT INTO outbound (id, job_id, transport, reply_target, body, status)
                       SELECT $4, d.id, ie.transport,
                              COALESCE(ie.envelope->>'reply_target', ie.transport || ':unknown'),
                              $5, 'pending'
                         FROM done d JOIN inbound_event ie ON ie.id = d.inbound_event_id
                       RETURNING id
                   )
                   SELECT id FROM done""",
                attempt_id, _json(result.model_dump(mode="json")), result.artifact_ref,
                _new_id(), result.text)
        if job_id is None:
            raise LostLease(f"complete: attempt {attempt_id} was reclaimed/closed")

    async def fail_attempt(self, attempt_id: UUID, result: Result) -> None:
        """attempt open->failed, then route the job by error_class AND authority:
          retryable + responder/controller -> queued (requeue)
          retryable + workspace_actor/unknown -> dead + dead_letter (never replay a mutation)
          terminal / needs_human / unknown class -> failed (fail-closed)
        Raises LostLease if the attempt is already closed (reclaimed) - symmetric
        with complete_attempt, so a worker never double-counts a job it lost."""
        async with self._pool.acquire() as con:
            async with con.transaction():
                att = await con.fetchrow(
                    "SELECT id, job_id, status FROM attempt WHERE id = $1 FOR UPDATE", attempt_id)
                if att is None or att["status"] != "open":
                    raise LostLease(f"fail: attempt {attempt_id} no longer open")
                ec = result.error_class.value if result.error_class else None
                await con.execute(
                    """UPDATE attempt SET status='failed', result=$2, error_class=$3,
                           ended_at = statement_timestamp() WHERE id = $1""",
                    attempt_id, _json(result.model_dump(mode="json")), ec)
                job = await con.fetchrow(
                    "SELECT id, to_agent, repo_id FROM job WHERE id = $1 FOR UPDATE",
                    att["job_id"])
                to_agent = job["to_agent"]
                dead_reason = None
                if result.error_class is ErrorClass.RETRYABLE and self._requeue_safe(to_agent):
                    # poison ceiling: count ALL attempts (incl. the one just closed above,
                    # so the off-by-one is intentional) and dead-letter instead of requeuing
                    # once it reaches MAX_ATTEMPTS — a job that fails retryably every time
                    # can't requeue forever.
                    n_att = await con.fetchval(
                        "SELECT count(*) FROM attempt WHERE job_id = $1", att["job_id"])
                    if n_att >= self.MAX_ATTEMPTS:
                        new_status = "dead"
                        dead_reason = (f"poison: {n_att} attempts reached MAX_ATTEMPTS "
                                       f"({self.MAX_ATTEMPTS}) on '{to_agent}'")
                    else:
                        new_status = "queued"
                elif result.error_class is ErrorClass.RETRYABLE:
                    new_status = "dead"   # mutating/unknown agent: do not replay
                    dead_reason = f"retryable failure on non-retry-safe agent '{to_agent}'"
                else:
                    new_status = "failed"  # terminal / needs_human / None -> fail-closed
                await con.execute(
                    "UPDATE job SET status = $2 WHERE id = $1 AND status IN ('leased','running')",
                    att["job_id"], new_status)
                if new_status == "dead":
                    await con.execute(
                        "INSERT INTO dead_letter (id, source_id, reason) VALUES ($1,$2,$3)",
                        _new_id(), att["job_id"], dead_reason)
                # per-repo cap: counter LAST. We KNOW the attempt went open->failed (the
                # FOR UPDATE + status guard above raised LostLease otherwise), so exactly
                # one slot is freed regardless of the requeue/dead/failed route. NULL repo
                # is cap-exempt; GREATEST floors any soft-cache drift at 0.
                if job["repo_id"] is not None:
                    await con.execute(
                        "UPDATE repo_concurrency SET active = GREATEST(active - 1, 0) "
                        "WHERE repo_id = $1", job["repo_id"])

    async def append_log(self, attempt_id: UUID, stream: str, chunk: str) -> None:
        async with self._pool.acquire() as con:
            await con.execute(
                "INSERT INTO attempt_log (attempt_id, stream, chunk) VALUES ($1,$2,$3)",
                attempt_id, stream, chunk)

    # ---- outbox (reliable, deduped delivery) -------------------------------
    async def enqueue_outbound(self, job_id: UUID, body: str) -> UUID:
        """Queue a reply, resolving transport+reply_target from the job's
        originating inbound_event (the outbox replies to the source transport)."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """INSERT INTO outbound (id, job_id, transport, reply_target, body, status)
                   SELECT $1, j.id, ie.transport, ie.envelope->>'reply_target', $3, 'pending'
                     FROM job j JOIN inbound_event ie ON ie.id = j.inbound_event_id
                    WHERE j.id = $2
                   RETURNING id""",
                _new_id(), job_id, body)
        if row is None:
            raise ValueError(
                f"enqueue_outbound: job {job_id} has no inbound_event "
                "(controller-originated replies are a later slice)")
        return row["id"]

    async def claim_outbound(self, transport: str) -> Optional[dict]:
        """Claim ONE pending row for a transport (SKIP LOCKED -> two senders get
        different rows) and return its payload {id, reply_target, body}, ready to
        deliver. pending->leased, tries+1. None if nothing pending."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """WITH picked AS (
                       SELECT id FROM outbound
                        WHERE transport = $1 AND status = 'pending'
                        ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1
                   )
                   UPDATE outbound o SET status = 'leased', tries = tries + 1
                     FROM picked p WHERE o.id = p.id
                   RETURNING o.id, o.reply_target, o.body""", transport)
        if row is None:
            return None
        return {"id": row["id"], "reply_target": row["reply_target"], "body": row["body"]}

    async def mark_outbound_sent(self, outbound_id: UUID, provider_msg_id: str) -> None:
        """leased->sent, recording provider_msg_id (exactly-once via UNIQUE). A
        re-send of the SAME row is a no-op (status no longer 'leased'). If the id
        is already owned by a DIFFERENT row, that's an anomalous duplicate send ->
        dead-letter THIS row rather than silently marking it sent with no id
        (which would hide the anomaly from get_status / the audit trail)."""
        async with self._pool.acquire() as con:
            async with con.transaction():
                try:
                    async with con.transaction():  # savepoint
                        await con.execute(
                            """UPDATE outbound SET status='sent', provider_msg_id=$2
                                WHERE id = $1 AND status = 'leased'""",
                            outbound_id, provider_msg_id)
                except asyncpg.UniqueViolationError:
                    await con.execute(
                        "UPDATE outbound SET status='failed' WHERE id=$1 AND status='leased'",
                        outbound_id)
                    await con.execute(
                        "INSERT INTO dead_letter (id, source_id, reason) VALUES ($1,$2,$3)",
                        _new_id(), outbound_id,
                        f"duplicate provider_msg_id {provider_msg_id} (already delivered)")

    async def mark_outbound_failed(self, outbound_id: UUID) -> None:
        """leased->pending (retry) until tries exhaust, then leased->failed + dead_letter."""
        async with self._pool.acquire() as con:
            async with con.transaction():
                row = await con.fetchrow(
                    """UPDATE outbound
                          SET status = CASE WHEN tries >= $2 THEN 'failed' ELSE 'pending' END
                        WHERE id = $1 AND status = 'leased'
                       RETURNING status""",
                    outbound_id, self.OUTBOUND_MAX_TRIES)
                if row is not None and row["status"] == "failed":
                    await con.execute(
                        "INSERT INTO dead_letter (id, source_id, reason) VALUES ($1,$2,$3)",
                        _new_id(), outbound_id, "outbound delivery failed after max tries")

    # ---- janitor / control -------------------------------------------------
    async def reclaim_expired(self) -> int:
        """Requeue (or dead-letter) jobs whose lease expired - a crashed worker.
        FOR UPDATE OF a,j SKIP LOCKED means the janitor NEVER grabs a row a worker
        is mid-completing (that worker holds the lock without SKIP LOCKED, so it
        wins). Authority decides the fate: workspace_actors go dead, not requeued."""
        async with self._pool.acquire() as con:
            async with con.transaction():
                rows = await con.fetch(
                    """WITH expired AS (
                           SELECT a.id AS attempt_id, a.job_id, j.to_agent, j.repo_id
                             FROM attempt a JOIN job j ON j.id = a.job_id
                            WHERE a.status = 'open'
                              AND a.lease_expires_at <= statement_timestamp()
                              AND j.status IN ('leased','running')
                            FOR UPDATE OF a, j SKIP LOCKED
                            LIMIT $1
                       ),
                       closed AS (
                           UPDATE attempt a
                              SET status='failed', ended_at=statement_timestamp(),
                                  error_class='retryable'
                            WHERE a.id IN (SELECT attempt_id FROM expired)
                           RETURNING a.id, a.job_id
                       )
                       SELECT c.job_id, e.to_agent, e.repo_id
                         FROM closed c JOIN expired e ON e.attempt_id = c.id""",
                    self.RECLAIM_BATCH)
                if not rows:
                    return 0
                # Split the expired batch: workspace_actors die (never replay a mutation);
                # requeue-safe jobs requeue UNLESS they've hit the poison ceiling (a worker
                # that keeps crashing mid-run would otherwise reclaim->requeue forever). The
                # count includes the attempt the `closed` CTE just failed above (off-by-one
                # intended), so the Nth crash is the one that dead-letters. Every closed
                # attempt frees a repo slot (BOTH requeue and dead), so accumulate the
                # per-repo decrement here (NULL repo_id is cap-exempt -> excluded).
                requeue, dead, dead_reasons = [], [], {}
                repo_dec: dict[str, int] = {}
                for r in rows:
                    jid = r["job_id"]
                    if r["repo_id"] is not None:
                        repo_dec[r["repo_id"]] = repo_dec.get(r["repo_id"], 0) + 1
                    if not self._requeue_safe(r["to_agent"]):
                        dead.append(jid)
                        dead_reasons[jid] = "lease expired; workspace_actor not auto-retried"
                        continue
                    n_att = await con.fetchval(
                        "SELECT count(*) FROM attempt WHERE job_id = $1", jid)
                    if n_att >= self.MAX_ATTEMPTS:
                        dead.append(jid)
                        dead_reasons[jid] = (f"poison: {n_att} attempts reached MAX_ATTEMPTS "
                                             f"({self.MAX_ATTEMPTS}) after lease expiry")
                    else:
                        requeue.append(jid)
                if requeue:
                    await con.execute(
                        """UPDATE job SET status='queued'
                            WHERE id = ANY($1::uuid[]) AND status IN ('leased','running')""",
                        requeue)
                if dead:
                    await con.execute(
                        """UPDATE job SET status='dead'
                            WHERE id = ANY($1::uuid[]) AND status IN ('leased','running')""",
                        dead)
                    for jid in dead:
                        await con.execute(
                            "INSERT INTO dead_letter (id, source_id, reason) VALUES ($1,$2,$3)",
                            _new_id(), jid, dead_reasons[jid])
                # per-repo cap: counter LAST, applied per-repo in repo_id ORDER (so two
                # concurrent reclaimers/the reconciler acquire rc rows in a consistent
                # order -> no rc<->rc ABBA). GREATEST floors soft-cache drift at 0.
                for rid in sorted(repo_dec):
                    await con.execute(
                        "UPDATE repo_concurrency SET active = GREATEST(active - $2, 0) "
                        "WHERE repo_id = $1", rid, repo_dec[rid])
                return len(rows)

    async def reapable_attempt_ids(self, min_age_s: float) -> set[str]:
        """Attempt ids the worktree GC may safely reap: CLOSED (status <> 'open') and
        closed at least min_age_s ago (by ended_at). This EXCLUDES live leases AND
        just-closed attempts, so the sweep can never delete a worktree whose worker might
        still be writing — a lost lease is only noticed on the worker's next heartbeat, so
        the grace window gives it time to abort and run its own cleanup first. Decided by
        attempt state, never filesystem mtime (an in-place edit doesn't refresh dir mtime)."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """SELECT id FROM attempt
                    WHERE status <> 'open' AND ended_at IS NOT NULL
                      AND ended_at <= statement_timestamp() - make_interval(secs => $1)""",
                float(min_age_s))
        return {str(r["id"]) for r in rows}

    async def reconcile_repo_concurrency(self) -> int:
        """Slow soft-cache backstop (design §4 M1) — runs off the janitor at a slow
        cadence. Pure soft-cache maintenance: heals repo_concurrency.active back to the
        TRUTH = count of open attempts whose job is leased/running, per repo. Correctness
        never depends on it (the hard COUNT under the rc lock at lease time is the cap
        authority); this only restores soft-filter fairness, so disabling it leaves the
        cap intact and, via the attempt-close decrements alone, active still reconverges
        to EXACTLY 0 at quiescence.

        Three steps, all idempotent:
          0. ORPHAN backstop: close any open attempt whose job is ALREADY terminal (the
             transient cancel-race artifact). The hard count is scoped to leased/running
             so an orphan never breaches the cap, but closing it keeps the attempt table
             clean and lets the count below see truth.
          1. UPSERT-heal active to truth for every repo with live open attempts — UPSERT
             so a MISSING row is created (a missing row reads 0 -> over-FILTERS, never
             over-admits since the hard COUNT gates).
          2. Zero any rc row whose repo now has no live open attempts (drift-down).
        Locks every rc row FOR UPDATE in PK order first, so it serializes with — and never
        clobbers — a concurrent lease/decrement (which lock the rc row last), and can't
        ABBA another multi-repo writer. Returns the number of rc rows healed."""
        async with self._pool.acquire() as con:
            async with con.transaction():
                # 0. orphan backstop: an open attempt on a non-live job is dead work.
                await con.execute(
                    """UPDATE attempt a
                          SET status='failed', ended_at=statement_timestamp(),
                              error_class='terminal'
                         FROM job j
                        WHERE a.job_id = j.id AND a.status = 'open'
                          AND j.status NOT IN ('leased','running')""")
                # serialize against lease/decrement: lock all rc rows in PK order.
                await con.execute(
                    "SELECT repo_id FROM repo_concurrency ORDER BY repo_id FOR UPDATE")
                # 1. heal/create rows to truth (open attempts on leased/running jobs).
                healed = await con.fetch(
                    """INSERT INTO repo_concurrency (repo_id, active)
                       SELECT j.repo_id, count(*)
                         FROM attempt a JOIN job j ON j.id = a.job_id
                        WHERE a.status = 'open' AND j.repo_id IS NOT NULL
                          AND j.status IN ('leased','running')
                        GROUP BY j.repo_id
                       ON CONFLICT (repo_id) DO UPDATE SET active = EXCLUDED.active
                       RETURNING repo_id""")
                # 2. zero rc rows whose repo has no live open attempts (drift-down).
                await con.execute(
                    """UPDATE repo_concurrency SET active = 0
                        WHERE active <> 0
                          AND NOT EXISTS (
                              SELECT 1 FROM attempt a JOIN job j ON j.id = a.job_id
                               WHERE a.status = 'open' AND j.repo_id = repo_concurrency.repo_id
                                 AND j.status IN ('leased','running'))""")
                return len(healed)

    async def dead_letter(self, source_id: UUID, reason: str) -> None:
        """Pure log-write. The owning verb already performed the source's state
        transition; this only records why."""
        async with self._pool.acquire() as con:
            await con.execute(
                "INSERT INTO dead_letter (id, source_id, reason) VALUES ($1,$2,$3)",
                _new_id(), source_id, reason)

    async def cancel(self, job_id: UUID) -> None:
        """Administratively terminate a non-terminal job -> 'dead', failing its open
        attempt, in ONE atomic statement. The supervisor kills the OS process on the
        worker's next heartbeat (which will now raise LostLease).

        ONE statement closes the queued-cancel orphan-attempt race: today's two-step
        form let lease_job interleave between the attempt-close and the job-flip (cancel
        closes 0 attempts on a queued job, blocks on the job lock, lease opens an attempt
        + increments active, cancel flips job->dead -> an orphan open attempt + leaked
        increment forever). Collapsing it removes that window.

        Lock order: attempt THEN job THEN repo_concurrency (counter LAST) — the `closed`
        UPDATE locks the attempt first, `killed` the job, `dec` the rc row, so cancel
        keeps the canonical A->J->R order and can't ABBA with complete/fail/reclaim. The
        decrement is gated on `closed` actually closing a REAL open attempt (RETURNING),
        so a queued / already-terminal / duplicate cancel decrements ZERO. NULL repo_id is
        cap-exempt. (Belt-and-suspenders: should a transient orphan ever arise, the hard
        count ignores it — scoped to leased/running jobs — and the reconciler closes it.)"""
        async with self._pool.acquire() as con:
            await con.execute(
                """WITH closed AS (
                       UPDATE attempt SET status='failed', ended_at=statement_timestamp(),
                              error_class='terminal'
                        WHERE job_id = $1 AND status = 'open'
                       RETURNING job_id
                   ),
                   killed AS (
                       UPDATE job SET status='dead'
                        WHERE id = $1 AND status IN ('queued','leased','running')
                       RETURNING id
                   ),
                   dec AS (   -- counter LAST; gated on a REAL open-attempt close.
                       UPDATE repo_concurrency rc SET active = GREATEST(rc.active - 1, 0)
                         FROM closed c JOIN job j ON j.id = c.job_id
                        WHERE j.repo_id IS NOT NULL AND rc.repo_id = j.repo_id
                       RETURNING rc.repo_id
                   )
                   SELECT 1""",
                job_id)

    async def get_status(self, job_id: UUID) -> dict:
        """Job + its attempts + outbound, as a plain dict. {} for an unknown id."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """SELECT j.id, j.to_agent, j.status, j.depth, j.artifact_ref,
                          j.created_at, j.created_by,
                          (SELECT json_agg(json_build_object(
                                'id', a.id, 'status', a.status, 'worker_id', a.worker_id,
                                'error_class', a.error_class, 'text', a.result->>'text'))
                             FROM attempt a WHERE a.job_id = j.id) AS attempts,
                          (SELECT json_agg(json_build_object(
                                'id', o.id, 'status', o.status, 'reply_target', o.reply_target,
                                'body', o.body, 'provider_msg_id', o.provider_msg_id))
                             FROM outbound o WHERE o.job_id = j.id) AS outbound
                     FROM job j WHERE j.id = $1""", job_id)
        if row is None:
            return {}
        out = dict(row)
        out["id"] = str(out["id"])
        if isinstance(out.get("created_at"), _dt.datetime):
            out["created_at"] = out["created_at"].isoformat()
        return out

    async def count_queued(self) -> int:
        """Cheap queue-depth probe for a pool's idle detection."""
        async with self._pool.acquire() as con:
            return await con.fetchval("SELECT count(*) FROM job WHERE status = 'queued'")
