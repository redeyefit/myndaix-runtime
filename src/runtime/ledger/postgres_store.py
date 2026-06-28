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
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Optional
from uuid import UUID

import asyncpg

from runtime import registry, skillmatch
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
    # LEASE + HEARTBEAT set TOGETHER (design M4 structure). LEASE_SECONDS is the window a
    # fresh attempt gets; HEARTBEAT_SECONDS is the EXTEND amount each ping pushes expiry to
    # (must be >= LEASE so a ping never SHRINKS the window). The pool's FIRING cadence
    # (heartbeat_interval_s, a separate quantity) must be <= lease/3 so a healthy job
    # heartbeats well within the window.
    #   CRASH-RECOVERY: the value also bounds slot-hold-after-crash. A crashed worker's
    #   lease expires HEARTBEAT_SECONDS after its LAST heartbeat — and a tight reclaim
    #   cadence does NOT shrink that (it only reclaims promptly AT expiry, not sooner). So
    #   with a per-repo cap, a long value would let a few crashed workers paralyze a repo
    #   for that long (cross-family review caught the design's flawed "reclaim mitigates"
    #   claim). Kept at 120s: heartbeats still cover arbitrarily long live jobs (the ping
    #   re-extends every <= lease/3), so a short window costs nothing and frees a crashed
    #   slot in ~2min instead of ~10. Tune up only if heartbeat churn ever matters.
    LEASE_SECONDS = 120          # a fresh lease; heartbeat extends long jobs
    HEARTBEAT_SECONDS = 120      # each heartbeat pushes expiry to now()+this (>= LEASE)
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
    # +learning rung: review-skill lifecycle windows (deterministic prune, archive-not-delete).
    SKILL_STALE_DAYS = 30        # active -> stale after this many days with no skill_use
    SKILL_ARCHIVE_DAYS = 90      # stale -> archived after this many days (reactivation = human re-arm)
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
            spec = registry.get(to_agent)
            # a non-idempotent PAID supplier (its submit charges credits, no dedup) must NEVER
            # auto-requeue on crash/lease-expiry — a re-submit would double-charge. It goes
            # dead+surfaced regardless of authority (cross-family review CRITICAL).
            if spec.adapter.get("non_idempotent"):
                return False
            authority = spec.authority
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
        wins). Authority decides the fate: workspace_actors go dead, not requeued.

        Also heals the transient cancel-race orphan FAST (every tick): a queued job
        cancelled while a lease is mid-CLAIM can end up 'dead' with an open attempt the
        cancel's snapshot couldn't see (and a leaked rc slot). The hard count already
        ignores it (scoped to leased/running), so it's never a cap breach — but left
        until the slow reconciler it could soft-filter-starve the repo for ~45s. Closing
        it here (attempt-first lock, canonical order) + freeing its slot shrinks that to
        one janitor tick. Returns the count of EXPIRED leases reclaimed (orphans are a
        side-effect heal, not counted)."""
        async with self._pool.acquire() as con:
            async with con.transaction():
                # FAST orphan heal — runs unconditionally, before the expired-lease pass.
                orphans = await con.fetch(
                    """WITH o AS (
                           SELECT a.id, j.repo_id FROM attempt a JOIN job j ON j.id = a.job_id
                            WHERE a.status='open' AND j.status NOT IN ('leased','running')
                            FOR UPDATE OF a SKIP LOCKED
                       ),
                       oc AS (
                           UPDATE attempt a SET status='failed', ended_at=statement_timestamp(),
                                  error_class='terminal'
                            WHERE a.id IN (SELECT id FROM o) RETURNING id
                       )
                       SELECT repo_id FROM o WHERE repo_id IS NOT NULL""")
                if orphans:
                    odec: dict[str, int] = {}
                    for r in orphans:
                        odec[r["repo_id"]] = odec.get(r["repo_id"], 0) + 1
                    for rid in sorted(odec):      # counter LAST, repo_id order (deadlock-safe)
                        await con.execute(
                            "UPDATE repo_concurrency SET active = GREATEST(active - $2, 0) "
                            "WHERE repo_id = $1", rid, odec[rid])
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
        cap intact. For the NORMAL close paths (complete/fail/reclaim of a live lease) the
        attempt-close decrements alone reconverge active to EXACTLY 0 at quiescence; the
        ONE case the decrements can't self-heal is the queued-cancel orphan (see cancel()
        — a leaked slot + orphan attempt on a dead job), which reclaim_expired's fast
        orphan-heal closes within a janitor tick. THIS reconciler is the slow absolute
        backstop that heals any residual drift (incl. that orphan if reclaim is idle).

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
                #    Lock the rows in a deterministic id order (FOR UPDATE OF a SKIP LOCKED)
                #    BEFORE updating, so two concurrent reconcilers (multiple serve
                #    instances) can't acquire attempt locks in clashing heap order -> no
                #    40P01. SKIP LOCKED yields any orphan a fast-heal/cancel already holds.
                await con.execute(
                    """WITH o AS (
                           SELECT a.id FROM attempt a JOIN job j ON j.id = a.job_id
                            WHERE a.status = 'open' AND j.status NOT IN ('leased','running')
                            ORDER BY a.id FOR UPDATE OF a SKIP LOCKED
                       )
                       UPDATE attempt a
                          SET status='failed', ended_at=statement_timestamp(),
                              error_class='terminal'
                        WHERE a.id IN (SELECT id FROM o)""")
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
        attempt. The supervisor kills the OS process on the worker's next heartbeat
        (which will now raise LostLease).

        THREE EXPLICIT SEQUENTIAL statements, NOT one CTE: sibling data-modifying CTEs in
        a single statement execute in a planner-UNPREDICTABLE order (Postgres gives no
        text-order guarantee), so a single-CTE form could lock the job (`killed`) BEFORE
        the attempt (`closed`) -> job-then-attempt -> an ABBA / 40P01 deadlock with
        complete/fail (which lock attempt-then-job). Sequential statements GUARANTEE the
        canonical attempt -> job -> repo_concurrency (counter LAST) order. (Cross-family
        review — codex + Oracle — both caught the CTE-order risk; the single CTE also gave
        ZERO benefit since it never closed the orphan race below.) The decrement is gated
        on a REAL open-attempt close (stmt 1's RETURNING), so a queued / already-terminal /
        duplicate cancel decrements ZERO. NULL repo_id is cap-exempt.

        NOTE — this does NOT fully close the queued-cancel-vs-lease race: a lease that
        COMMITS its CLAIM after cancel's stmt-1 snapshot (while stmt-2 is about to flip the
        job) leaves an orphan open attempt on the dead job + a leaked soft slot. That
        orphan is HARMLESS to the cap (the hard count is scoped to leased/running, so it's
        never counted) and is healed within ONE janitor tick by reclaim_expired's fast
        orphan-heal (+ the reconciler backstop). A job-first cancel that COULD close it is
        rejected — it reintroduces the ABBA above."""
        async with self._pool.acquire() as con:
            async with con.transaction():
                # 1. close any open attempt FIRST (locks the attempt row). RETURNING tells
                #    us whether a REAL open attempt closed here.
                closed_job = await con.fetchval(
                    """UPDATE attempt SET status='failed', ended_at=statement_timestamp(),
                           error_class='terminal'
                        WHERE job_id = $1 AND status = 'open' RETURNING job_id""",
                    job_id)
                # 2. flip the job dead (locks the job row) — attempt-then-job, canonical.
                await con.execute(
                    """UPDATE job SET status='dead'
                        WHERE id = $1 AND status IN ('queued','leased','running')""",
                    job_id)
                # 3. counter LAST: decrement iff a real open attempt closed, non-NULL repo
                #    (the job row is already locked by stmt 2 in this tx).
                if closed_job is not None:
                    await con.execute(
                        """UPDATE repo_concurrency rc SET active = GREATEST(rc.active - 1, 0)
                             FROM job j WHERE j.id = $1 AND j.repo_id IS NOT NULL
                              AND rc.repo_id = j.repo_id""",
                        job_id)

    async def get_status(self, job_id: UUID) -> dict:
        """Job + its attempts + outbound, as a plain dict. {} for an unknown id."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """SELECT j.id, j.to_agent, j.status, j.depth, j.artifact_ref,
                          j.base_sha, j.base_ref, j.repo_id, j.created_at, j.created_by,
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

    # ---- docs-only PR auto-merge gate (DESIGN v0.3 §4) ---------------------
    async def automerge_decision(self, repo_id: str, pr_number: int, head_sha: str) -> Optional[str]:
        """The terminal decision already recorded for this exact (repo, PR, head), or None
        if unseen. The gate skips a (PR, head) it has already decided so it never re-reviews
        the same head every tick; a new push (new head_sha) is a fresh row, so a re-pushed
        PR is re-evaluated."""
        async with self._pool.acquire() as con:
            return await con.fetchval(
                """SELECT decision FROM automerge_seen
                    WHERE repo_id = $1 AND pr_number = $2 AND head_sha = $3""",
                repo_id, pr_number, head_sha)

    async def record_automerge(self, repo_id: str, pr_number: int, head_sha: str,
                               decision: str, reason: Optional[str] = None) -> None:
        """Record the terminal decision for (repo, PR, head). Idempotent UPSERT on the head
        key (a re-evaluation of the same head overwrites the reason, e.g. a transient skip
        that later merged). `decision` ∈ merged|needs_fix|skipped|error (DB-checked)."""
        async with self._pool.acquire() as con:
            await con.execute(
                """INSERT INTO automerge_seen (repo_id, pr_number, head_sha, decision, reason)
                   VALUES ($1, $2, $3, $4, $5)
                   ON CONFLICT (repo_id, pr_number, head_sha)
                   DO UPDATE SET decision = EXCLUDED.decision, reason = EXCLUDED.reason,
                                 decided_at = now()""",
                repo_id, pr_number, head_sha, decision, reason)

    # ---- controller-loop ("the brain") cursor (DESIGN v0.2 §2) -------------
    # The proactive review scheduler's only state. Each verb is one status-guarded
    # CAS returning whether THIS caller won the transition — same discipline as the
    # job state machine above, so the single-instance lock is belt-and-suspenders,
    # not the sole guard against a double dispatch.
    async def get_cursor(self, repo_id: str, ref: str) -> Optional[dict]:
        """The (repo_id, ref) cursor row as a plain dict, or None if unseen.
        `updated_at` is left as a datetime so the controller can compare it to its
        stale-pending cutoff."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """SELECT repo_id, ref, baseline_sha, reviewed_sha, pending_sha,
                          state, attempts, updated_at
                     FROM review_cursor WHERE repo_id = $1 AND ref = $2""",
                repo_id, ref)
        return dict(row) if row is not None else None

    async def upsert_baseline(self, repo_id: str, ref: str, head: str) -> bool:
        """Seed the high-water mark at first sight: baseline=reviewed=head, NOT
        reviewed (B2 — avoids the whole-tree diff blow-up). INSERT ... ON CONFLICT
        DO NOTHING, so it only ever seeds an ABSENT cursor; returns True iff seeded."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """INSERT INTO review_cursor
                       (repo_id, ref, baseline_sha, reviewed_sha, pending_sha, state, attempts)
                   VALUES ($1, $2, $3, $3, NULL, 'baseline', 0)
                   ON CONFLICT (repo_id, ref) DO NOTHING
                   RETURNING repo_id""",
                repo_id, ref, head)
        return row is not None

    async def claim_dispatch(
        self, repo_id: str, ref: str, head: str, stale_before: _dt.datetime
    ) -> bool:
        """The dedup GATE: atomically claim `head` for dispatch. Returns True iff THIS
        caller won the slot (then it may invoke the review). The WHERE refuses to claim
        a head that is already reviewed, already blocked for that same head, or freshly
        in flight — but DOES re-claim a same-head pending older than `stale_before` (a
        dead dispatch) and always claims a NEW head. attempts resets to 1 on a new head,
        increments on a stale re-claim, so the blocked ceiling counts only the current
        head's tries."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """UPDATE review_cursor
                      SET pending_sha = $3,
                          state = 'dispatching',
                          attempts = CASE WHEN pending_sha IS DISTINCT FROM $3
                                          THEN 1 ELSE attempts + 1 END,
                          updated_at = now()
                    WHERE repo_id = $1 AND ref = $2
                      AND reviewed_sha <> $3
                      AND NOT (state = 'blocked' AND pending_sha = $3)
                      -- claim ONLY when idle, escaping a block, or re-claiming a dead
                      -- (stale) dispatch. A fresh in-flight head is NOT superseded — we
                      -- wait for it to deliver, then review the union next tick (Oracle
                      -- B2: superseding abandons the prior head's delivery + wastes a run).
                      AND (pending_sha IS NULL OR state = 'blocked' OR updated_at < $4)
                    RETURNING repo_id""",
                repo_id, ref, head, stale_before)
        return row is not None

    async def release_dispatch(self, repo_id: str, ref: str, head: str) -> bool:
        """Un-stick a dispatch whose trigger failed SYNCHRONOUSLY (missing/nonzero/timed-out
        play-review FRONT): force the pending row stale so the NEXT tick re-dispatches
        immediately instead of waiting out PENDING_STALE — while PRESERVING `attempts` so a
        persistently-failing trigger still climbs to the blocked ceiling. Guarded on the
        pending head + dispatching state. Returns True iff released."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """UPDATE review_cursor SET updated_at = to_timestamp(0)
                    WHERE repo_id = $1 AND ref = $2 AND pending_sha = $3
                      AND state = 'dispatching'
                    RETURNING repo_id""",
                repo_id, ref, head)
        return row is not None

    async def advance_cursor(self, repo_id: str, ref: str, head: str) -> bool:
        """Advance the cursor once a review of `head` DELIVERED (play-review's post-delivery
        done-<sha> marker). Guarded on pending_sha = head so it only ever advances the head
        we dispatched. Returns True iff advanced."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """UPDATE review_cursor
                      SET reviewed_sha = $3, pending_sha = NULL,
                          state = 'delivered', attempts = 0, updated_at = now()
                    WHERE repo_id = $1 AND ref = $2 AND pending_sha = $3
                    RETURNING repo_id""",
                repo_id, ref, head)
        return row is not None

    async def skip_to(self, repo_id: str, ref: str, head: str) -> bool:
        """Advance the cursor straight to `head` WITHOUT a review — used when base..head has
        no net diff (an empty/revert-net-zero commit), which play-review would abort on and
        never mark done, wedging the head to BLOCKED (workflow MAJOR). Clears any pending.
        Guarded on reviewed_sha <> head so it is idempotent. Returns True iff advanced."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """UPDATE review_cursor
                      SET reviewed_sha = $3, pending_sha = NULL,
                          state = 'delivered', attempts = 0, updated_at = now()
                    WHERE repo_id = $1 AND ref = $2 AND reviewed_sha <> $3
                    RETURNING repo_id""",
                repo_id, ref, head)
        return row is not None

    async def mark_blocked(self, repo_id: str, ref: str, head: str, max_attempts: int) -> bool:
        """Sticky-block the current pending head after `max_attempts` failed dispatches.
        A CAS (codex M5): guarded on pending_sha = head AND attempts >= max_attempts AND
        state = 'dispatching', so it can NEVER clobber a concurrent new-head claim into
        'blocked' off a stale read. A later NEW head escapes it (claim_dispatch resets on
        a distinct head). Returns True iff this caller blocked the row."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """UPDATE review_cursor SET state = 'blocked', updated_at = now()
                    WHERE repo_id = $1 AND ref = $2 AND pending_sha = $3
                      AND attempts >= $4 AND state = 'dispatching'
                    RETURNING repo_id""",
                repo_id, ref, head, max_attempts)
        return row is not None

    # ---- review skills ("+learning" rung) — DESIGN v0.3 governing sections --------------
    # Each verb mirrors the CAS/UPSERT discipline above. The pure matching + injection logic
    # lives in runtime.skillmatch (DB-free, unit-tested). The BODY lives in Postgres (the
    # indexer reads it from a trusted merged ref); selection NEVER rehashes disk (codex MAJOR).
    async def index_skills(self, repo_id: str, skills: list[dict]) -> dict:
        """UPSERT the per-repo skill cache from a trusted merged ref's skills/ contents (the
        controller parses + lint-validates each SKILL.md from the OWNED ref, never the worktree).
        One transaction. ON CONFLICT updates only when content_sha drifted (idempotent — a no-op
        tick changes nothing). A skill no longer present on the ref is ARCHIVED (reversible —
        archive-not-delete; re-adding via a PR upserts it back to active). The caller
        pre-validates (slug/desc/body/trigger/injection); the DB CHECKs are the fail-closed
        backstop (a violation raises -> the indexer alerts, never silently indexes a bad row).
        `skills` items: {name, description, body, body_sha, content_sha, path_trigger}."""
        present = [s["name"] for s in skills]
        up = 0
        async with self._pool.acquire() as con:
            async with con.transaction():
                for s in skills:
                    row = await con.fetchrow(
                        """INSERT INTO skill
                               (name, description, body, body_sha, content_sha,
                                repo_scope, path_trigger, provenance, state)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,'promoted','active')
                           ON CONFLICT (repo_scope, name) DO UPDATE
                               SET description = EXCLUDED.description, body = EXCLUDED.body,
                                   body_sha = EXCLUDED.body_sha, content_sha = EXCLUDED.content_sha,
                                   path_trigger = EXCLUDED.path_trigger,
                                   state = 'active'
                             WHERE skill.content_sha IS DISTINCT FROM EXCLUDED.content_sha
                           RETURNING name""",
                        s["name"], s["description"], s["body"], s["body_sha"],
                        s["content_sha"], repo_id, s["path_trigger"])
                    if row is not None:
                        up += 1
                arch = await con.fetch(
                    """UPDATE skill SET state = 'archived'
                        WHERE repo_scope = $1 AND state <> 'archived'
                          AND name <> ALL($2::text[])
                        RETURNING name""",
                    repo_id, present)
        return {"upserted": up, "archived_removed": len(arch), "total": len(skills)}

    async def select_skills(self, repo_id: str, changed_paths: list[str]) -> dict:
        """Pick <=2 ACTIVE skills for `repo_id` whose path_trigger matches any changed path
        (path-SEGMENT match), ordered new-first -> specificity desc -> recency desc. Matching +
        ordering are pure (runtime.skillmatch). A banned/broad trigger or a body_sha-drift row
        (tampered/half-written) is DROPPED here (fail-closed out of selection), drift surfaced
        for a jefe alert. Returns {"skills":[{name,body}], "drift":[name]}."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """SELECT name, body, body_sha, path_trigger, last_used_at
                     FROM skill WHERE repo_scope = $1 AND state = 'active'""",
                repo_id)
        cand, drift = [], []
        for r in rows:
            trig = r["path_trigger"]
            if skillmatch.is_banned_trigger(trig):
                continue  # belt: a banned trigger should never have been indexed
            if not any(skillmatch.seg_match(trig, p) for p in changed_paths):
                continue
            if hashlib.sha256(r["body"].encode()).hexdigest() != r["body_sha"]:
                drift.append(r["name"])
                continue
            cand.append(r)
        cand.sort(key=lambda r: (
            r["last_used_at"] is not None,                      # NULL (new) sorts first
            -skillmatch.specificity(r["path_trigger"]),         # more specific first
            -(r["last_used_at"].timestamp() if r["last_used_at"] else 0.0),  # more recent first
        ))
        return {"skills": [{"name": r["name"], "body": r["body"]} for r in cand[:2]],
                "drift": drift}

    async def record_skill_use(self, repo_id: str, review_play: str, used: list[dict]) -> None:
        """Debounced usage accounting: bump last_used_at (at most once/hour per skill, so it
        stays off the review hot path) + append an audit row per skill. The CALLER (skillselect)
        swallows any error — a DB hiccup must never block a review (selection fails OPEN).
        `used` items: {name, body_sha}."""
        if not used:
            return
        names = [u["name"] for u in used]
        async with self._pool.acquire() as con:
            async with con.transaction():
                await con.execute(
                    """UPDATE skill SET last_used_at = now()
                        WHERE repo_scope = $1 AND name = ANY($2::text[])
                          AND (last_used_at IS NULL OR last_used_at < now() - interval '1 hour')""",
                    repo_id, names)
                for u in used:
                    await con.execute(
                        """INSERT INTO skill_use (id, review_play, skill_name, body_sha, repo_scope)
                           VALUES ($1,$2,$3,$4,$5)""",
                        _new_id(), review_play, u["name"], u["body_sha"], repo_id)

    async def prune_skills(self) -> dict:
        """Deterministic, NO-LLM lifecycle prune — status-flip only (never deletes a row/file;
        reversible == fail-closed). active -> stale after SKILL_STALE_DAYS of no use; stale ->
        archived after SKILL_ARCHIVE_DAYS. NO reactivate-on-reuse (a stale skill is never
        selected, so reuse can't reach it — reactivation is human re-arm only, v0.3 #5). The
        `state` guard makes each transition a CAS (resolves a prune-vs-index race). Returns
        {staled, archived}."""
        async with self._pool.acquire() as con:
            async with con.transaction():
                staled = await con.fetch(
                    """UPDATE skill SET state = 'stale'
                        WHERE state = 'active'
                          AND COALESCE(last_used_at, created_at) < now() - $1::interval
                        RETURNING name""",
                    _dt.timedelta(days=self.SKILL_STALE_DAYS))
                archived = await con.fetch(
                    """UPDATE skill SET state = 'archived'
                        WHERE state = 'stale'
                          AND COALESCE(last_used_at, created_at) < now() - $1::interval
                        RETURNING name""",
                    _dt.timedelta(days=self.SKILL_ARCHIVE_DAYS))
        return {"staled": len(staled), "archived": len(archived)}
