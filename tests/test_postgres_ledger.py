"""PostgresLedger concurrency proofs - the production ledger under REAL contention.

This is where the design's thesis is VERIFIED, not asserted: row-locking + a
status-guarded state machine end the file-IPC bug classes. Each test races real
asyncpg connections against a real local Postgres. The naive (no-lock) version of
each verb would FAIL the matching test - that's the regression guard.

Setup (once):
    brew services start postgresql@16
    createdb runtime_test
Run:
    LEDGER_TEST_DSN=postgresql://localhost/runtime_test \\
        PYTHONPATH=src python3 tests/test_postgres_ledger.py
"""
import asyncio
import inspect
import os

import asyncpg

from runtime.contracts import ErrorClass, Result, ResultStatus, TransportEnvelope
from runtime.ledger.postgres_store import LostLease, PostgresLedger

DSN = os.environ.get("LEDGER_TEST_DSN", "postgresql://localhost/runtime_test")

# real roster agents drive the authority paths:
#   kilabz  -> RESPONDER       (retry-safe; requeues)
#   mack    -> WORKSPACE_ACTOR (never auto-retried)
#   lobster -> CONTROLLER      (retry-safe)


def _env(dedupe: str) -> TransportEnvelope:
    return TransportEnvelope(transport="terminal", account="acct", sender_id="user1",
                             reply_target="terminal:demo", dedupe_key=dedupe)


def _ok(text="done", artifact=None) -> Result:
    return Result(status=ResultStatus.OK, text=text, artifact_ref=artifact)


def _retryable() -> Result:
    return Result(status=ResultStatus.ERROR, text="boom", error_class=ErrorClass.RETRYABLE)


async def _truncate(led: PostgresLedger) -> None:
    async with led._pool.acquire() as con:
        await con.execute(
            "TRUNCATE inbound_event, job, attempt, attempt_log, outbound, dead_letter "
            "RESTART IDENTITY CASCADE")


async def _expire_lease(led: PostgresLedger, attempt_id) -> None:
    async with led._pool.acquire() as con:
        await con.execute(
            "UPDATE attempt SET lease_expires_at = statement_timestamp() - interval '1 second' "
            "WHERE id = $1", attempt_id)


# -- invariant 1: no double-lease ----------------------------------------------
async def test_no_double_lease(led: PostgresLedger) -> None:
    await _truncate(led)
    jid = await led.submit_job(to_agent="kilabz", prompt="hi")
    results = await asyncio.gather(*[led.lease_job(f"w{i}", []) for i in range(50)])
    leased = [r for r in results if r is not None]
    assert len(leased) == 1, f"expected exactly 1 lease, got {len(leased)}"
    async with led._pool.acquire() as con:
        n = await con.fetchval(
            "SELECT count(*) FROM attempt WHERE job_id=$1 AND status='open'", jid)
    assert n == 1, f"expected 1 open attempt, got {n}"


# -- invariant 2: reclaim vs complete are mutually exclusive (the crown jewel) --
async def test_reclaim_vs_complete(led: PostgresLedger) -> None:
    # (A) 200 concurrent races: under real contention EXACTLY one writer acts on
    # the attempt and the job ends in a single consistent state (mutual exclusion).
    for trial in range(200):
        await _truncate(led)
        jid = await led.submit_job(to_agent="kilabz", prompt="hi")  # responder -> requeues on reclaim
        att = await led.lease_job("w1", [])
        await _expire_lease(led, att)

        async def _complete():
            try:
                await led.complete_attempt(att, _ok())
                return "completed"
            except LostLease:
                return "lost"

        reclaimed, outcome = await asyncio.gather(led.reclaim_expired(), _complete())
        st = await led.get_status(jid)
        assert (outcome == "completed" and reclaimed == 0) or \
               (outcome == "lost" and reclaimed == 1), \
            f"trial {trial}: outcome={outcome} reclaimed={reclaimed} (not mutually exclusive)"
        assert st["status"] == ("done" if outcome == "completed" else "queued"), \
            f"trial {trial}: outcome={outcome} but job={st['status']}"
        async with led._pool.acquire() as con:
            opn = await con.fetchval(
                "SELECT count(*) FROM attempt WHERE job_id=$1 AND status='open'", jid)
        assert opn <= 1, f"trial {trial}: {opn} open attempts (double state)"

    # (B) both outcomes proven DETERMINISTICALLY (timing-independent coverage):
    # complete-wins: a worker finishing after expiry but before the janitor runs still wins
    await _truncate(led)
    jid = await led.submit_job(to_agent="kilabz", prompt="hi")
    att = await led.lease_job("w1", [])
    await _expire_lease(led, att)
    await led.complete_attempt(att, _ok())
    assert (await led.get_status(jid))["status"] == "done"
    assert await led.reclaim_expired() == 0, "janitor must find nothing once the job is done"

    # reclaim-wins: the janitor reclaims first; the late complete loses (LostLease)
    await _truncate(led)
    jid = await led.submit_job(to_agent="kilabz", prompt="hi")
    att = await led.lease_job("w1", [])
    await _expire_lease(led, att)
    assert await led.reclaim_expired() == 1
    lost = False
    try:
        await led.complete_attempt(att, _ok())
    except LostLease:
        lost = True
    assert lost, "a complete after reclaim must raise LostLease"
    assert (await led.get_status(jid))["status"] == "queued"


# -- invariant 3: ingest dedupe is exactly-once --------------------------------
async def test_ingest_dedupe(led: PostgresLedger) -> None:
    await _truncate(led)
    env = _env("dupe-key-1")
    ids = await asyncio.gather(*[led.ingest_inbound(env, "body") for _ in range(20)])
    assert len({str(i) for i in ids}) == 1, f"expected 1 unique id, got {len(set(ids))}"
    async with led._pool.acquire() as con:
        n = await con.fetchval(
            "SELECT count(*) FROM inbound_event WHERE dedupe_key=$1", "dupe-key-1")
    assert n == 1, f"expected 1 inbound_event, got {n}"


# -- invariant 4a: each outbound row is claimed by at most one sender -----------
async def test_outbound_single_claim(led: PostgresLedger) -> None:
    await _truncate(led)
    ev = await led.ingest_inbound(_env("ob-1"), "hi")
    jid = await led.submit_job(to_agent="kilabz", prompt="hi", inbound_event_id=ev)
    for k in range(3):
        await led.enqueue_outbound(jid, f"reply {k}")
    claims = await asyncio.gather(*[led.claim_outbound("terminal") for _ in range(20)])
    got = [c for c in claims if c is not None]
    assert len(got) == 3, f"expected 3 claims, got {len(got)}"
    assert len({c["id"] for c in got}) == 3, "a row was claimed by two senders"


# -- invariant 4b: a send is recorded exactly once (no double delivery) ---------
async def test_outbound_send_exactly_once(led: PostgresLedger) -> None:
    await _truncate(led)
    ev = await led.ingest_inbound(_env("ob-2"), "hi")
    jid = await led.submit_job(to_agent="kilabz", prompt="hi", inbound_event_id=ev)
    ob = await led.enqueue_outbound(jid, "reply")
    claimed = await led.claim_outbound("terminal")
    await led.mark_outbound_sent(claimed["id"], "provider-xyz")
    await led.mark_outbound_sent(claimed["id"], "provider-xyz")  # duplicate: must not raise/re-deliver
    async with led._pool.acquire() as con:
        st = await con.fetchval("SELECT status FROM outbound WHERE id=$1", ob)
        n = await con.fetchval(
            "SELECT count(*) FROM outbound WHERE provider_msg_id=$1", "provider-xyz")
    assert st == "sent", f"outbound status={st}"
    assert n == 1, f"provider_msg_id appears {n} times (double delivery)"


# -- invariant 5: a workspace_actor is NEVER auto-retried ----------------------
async def test_workspace_actor_never_requeued(led: PostgresLedger) -> None:
    for trial in range(50):
        await _truncate(led)
        jid = await led.submit_job(to_agent="mack", prompt="edit code")  # workspace_actor
        att = await led.lease_job("w1", [])
        await _expire_lease(led, att)

        async def _fail():
            try:
                await led.fail_attempt(att, _retryable())  # retryable, but agent is unsafe
            except LostLease:
                pass

        await asyncio.gather(_fail(), led.reclaim_expired())
        st = await led.get_status(jid)
        assert st["status"] in ("failed", "dead"), \
            f"trial {trial}: workspace_actor was requeued! status={st['status']}"
        async with led._pool.acquire() as con:
            natt = await con.fetchval("SELECT count(*) FROM attempt WHERE job_id=$1", jid)
        assert natt == 1, f"trial {trial}: {natt} attempts (an unsafe retry happened)"


# -- invariant 6: heartbeat cannot resurrect a reclaimed attempt ---------------
async def test_heartbeat_lost_lease(led: PostgresLedger) -> None:
    await _truncate(led)
    jid = await led.submit_job(to_agent="kilabz", prompt="hi")
    att = await led.lease_job("w1", [])
    await _expire_lease(led, att)
    assert await led.reclaim_expired() == 1
    raised = False
    try:
        await led.heartbeat_attempt(att)
    except LostLease:
        raised = True
    assert raised, "heartbeat on a reclaimed attempt must raise LostLease"
    async with led._pool.acquire() as con:
        stt = await con.fetchval("SELECT status FROM attempt WHERE id=$1", att)
    assert stt == "failed", f"reclaimed attempt status={stt}"


# -- invariant 7: admission limits hold under concurrent submit ----------------
async def test_admission_limits_concurrent(led: PostgresLedger) -> None:
    await _truncate(led)
    parent = await led.submit_job(to_agent="lobster", prompt="root")
    await asyncio.gather(*[
        led.submit_job(to_agent="kilabz", prompt=f"c{i}", parent_id=parent)
        for i in range(50)])
    async with led._pool.acquire() as con:
        live = await con.fetchval(
            "SELECT count(*) FROM job WHERE parent_id=$1 AND status<>'dead'", parent)
        dead = await con.fetchval(
            "SELECT count(*) FROM job WHERE parent_id=$1 AND status='dead'", parent)
    assert live == led.MAX_CHILDREN, f"admission overshoot/undershoot: {live} live (max {led.MAX_CHILDREN})"
    assert live + dead == 50, f"expected 50 children, got {live} live + {dead} dead"


# -- responder DOES requeue on a retryable failure -----------------------------
async def test_responder_requeues_on_retryable(led: PostgresLedger) -> None:
    await _truncate(led)
    jid = await led.submit_job(to_agent="kilabz", prompt="hi")  # responder
    att = await led.lease_job("w1", [])
    await led.fail_attempt(att, _retryable())
    st = await led.get_status(jid)
    assert st["status"] == "queued", f"responder retryable should requeue, got {st['status']}"
    att2 = await led.lease_job("w2", [])
    assert att2 is not None, "requeued job should be re-leasable"


# -- the DB itself rejects an illegal status (CHECK enforcement) ---------------
async def test_db_rejects_illegal_status(led: PostgresLedger) -> None:
    await _truncate(led)
    jid = await led.submit_job(to_agent="kilabz", prompt="hi")
    raised = False
    try:
        async with led._pool.acquire() as con:
            await con.execute("UPDATE job SET status='bogus' WHERE id=$1", jid)
    except asyncpg.CheckViolationError:
        raised = True
    assert raised, "DB must reject an illegal status via CHECK constraint"


# -- happy path: every verb once, end to end ----------------------------------
async def test_happy_path_all_verbs(led: PostgresLedger) -> None:
    await _truncate(led)
    ev = await led.ingest_inbound(_env("hp-1"), "hello")
    jid = await led.submit_job(to_agent="kilabz", prompt="do it", inbound_event_id=ev)
    att = await led.lease_job("w1", [])
    assert att is not None
    job = await led.get_attempt_job(att)
    assert job.prompt == "do it"
    await led.heartbeat_attempt(att)
    await led.append_log(att, "stdout", "working...")
    await led.complete_attempt(att, _ok(text="result", artifact="patch.diff"))
    # complete auto-queued the reply (transactional outbox); claim + deliver it
    claimed = await led.claim_outbound("terminal")
    assert claimed is not None and claimed["body"] == "result"
    await led.mark_outbound_sent(claimed["id"], "msg-1")
    st = await led.get_status(jid)
    assert st["status"] == "done", f"job status={st['status']}"
    assert st["artifact_ref"] == "patch.diff"
    assert any(o["status"] == "sent" for o in (st["outbound"] or [])), "reply not sent"


async def test_context_round_trips(led: PostgresLedger) -> None:
    """Job.context persists through submit -> jsonb -> lease -> get_attempt_job (the
    plumbing the higgsfield runner relies on to read job.context['image_url'])."""
    await _truncate(led)
    ctx = {"image_url": "http://example.com/cat.png", "application": "/higgsfield-ai/dop/lite"}
    await led.submit_job(to_agent="higgsfield", prompt="gen", context=ctx)
    att = await led.lease_job("w1", [])
    job = await led.get_attempt_job(att)
    assert job is not None and job.context == ctx, f"context lost: {job.context if job else None}"

    # omitted context -> the jsonb DEFAULT '{}' -> an empty dict (never None), so the
    # runner's job.context.get("image_url") is a clean None, not an AttributeError.
    await _truncate(led)
    await led.submit_job(to_agent="kilabz", prompt="no ctx")
    att2 = await led.lease_job("w1", [])
    job2 = await led.get_attempt_job(att2)
    assert job2 is not None and job2.context == {}


async def test_record_job_context_merges_and_status_guarded(led: PostgresLedger) -> None:
    """v2 idempotent-resume seam: record_job_context merges a delta into a LIVE job's
    context (jsonb ||), re-persist replaces the token, and it no-ops once the job is no
    longer leased/running — so a lost lease can never let the runner re-submit."""
    await _truncate(led)
    jid = await led.submit_job(to_agent="higgsfield", prompt="clip",
                               context={"image_url": "http://example.com/c.png"})
    att = await led.lease_job("w1", [])

    await led.record_job_context(jid, {"_hf_resume": {"request_id": "req-1", "attempts": 0}})
    job = await led.get_attempt_job(att)
    assert job.context["image_url"] == "http://example.com/c.png"          # original preserved
    assert job.context["_hf_resume"] == {"request_id": "req-1", "attempts": 0}  # delta merged

    await led.record_job_context(jid, {"_hf_resume": {"request_id": "req-1", "attempts": 3}})
    job = await led.get_attempt_job(att)
    assert job.context["_hf_resume"]["attempts"] == 3                      # token replaced wholesale

    # complete the job -> status 'done'; a later record is a no-op (status-guarded)
    await led.complete_attempt(att, _ok())
    await led.record_job_context(jid, {"_hf_resume": {"request_id": "LATE"}})
    async with led._pool.acquire() as con:
        ctx = await con.fetchval("SELECT context FROM job WHERE id=$1", jid)
    assert ctx["_hf_resume"]["request_id"] == "req-1", "no-op after the job left leased/running"


# -- regression: cancel must NOT deadlock against complete/fail (the P0) --------
# Before the lock-order fix this failed ~99% of trials with DeadlockDetectedError;
# it is the test the green suite was missing (cancel had zero coverage).
async def test_cancel_vs_finish_no_deadlock(led: PostgresLedger) -> None:
    for trial in range(150):
        await _truncate(led)
        jid = await led.submit_job(to_agent="kilabz", prompt="hi")
        att = await led.lease_job("w1", [])
        errors: list = []

        async def _cancel():
            try:
                await led.cancel(jid)
            except Exception as e:  # any DB error here is a failure
                errors.append(("cancel", type(e).__name__))

        async def _finish():
            try:
                if trial % 2 == 0:
                    await led.complete_attempt(att, _ok())
                else:
                    await led.fail_attempt(att, _retryable())
            except LostLease:
                pass  # expected when cancel won the race
            except Exception as e:
                errors.append(("finish", type(e).__name__))

        await asyncio.gather(_cancel(), _finish())
        assert not errors, f"trial {trial}: unhandled DB error(s) {errors}"
        st = await led.get_status(jid)
        assert st["status"] in ("dead", "done"), f"trial {trial}: job ended {st['status']}"
        async with led._pool.acquire() as con:
            opn = await con.fetchval(
                "SELECT count(*) FROM attempt WHERE job_id=$1 AND status='open'", jid)
        assert opn == 0, f"trial {trial}: stuck open attempt after cancel race"


# -- regression: no live child under a terminal (cancelled) parent -------------
async def test_no_live_child_under_terminal_parent(led: PostgresLedger) -> None:
    await _truncate(led)
    parent = await led.submit_job(to_agent="lobster", prompt="root")
    await led.cancel(parent)  # parent -> dead
    child = await led.submit_job(to_agent="kilabz", prompt="child", parent_id=parent)
    st = await led.get_status(child)
    assert st["status"] == "dead", \
        f"child under a cancelled parent must be dead, got {st['status']}"
    assert await led.lease_job("w1", []) is None, "a dead child must not be leasable"


# -- regression: a cross-row provider_msg_id collision is dead-lettered ---------
# (this actually exercises the savepoint branch the prior test only claimed to)
async def test_outbound_duplicate_provider_id_dead_letters(led: PostgresLedger) -> None:
    await _truncate(led)
    ev = await led.ingest_inbound(_env("ob-dup"), "hi")
    jid = await led.submit_job(to_agent="kilabz", prompt="hi", inbound_event_id=ev)
    await led.enqueue_outbound(jid, "reply 1")
    await led.enqueue_outbound(jid, "reply 2")
    c1 = await led.claim_outbound("terminal")
    c2 = await led.claim_outbound("terminal")
    await led.mark_outbound_sent(c1["id"], "same-id")
    await led.mark_outbound_sent(c2["id"], "same-id")  # collision: must NOT silently mark sent
    async with led._pool.acquire() as con:
        sent = await con.fetchval(
            "SELECT count(*) FROM outbound WHERE status='sent' AND provider_msg_id='same-id'")
        failed = await con.fetchval("SELECT count(*) FROM outbound WHERE status='failed'")
        dl = await con.fetchval(
            "SELECT count(*) FROM dead_letter WHERE reason LIKE 'duplicate provider_msg_id%'")
    assert sent == 1, f"exactly one row should own the provider id, got {sent}"
    assert failed == 1, f"the duplicate should be failed, got {failed}"
    assert dl == 1, f"the duplicate should be dead-lettered, got {dl}"


async def test_submit_idempotent_on_inbound_event(led: PostgresLedger) -> None:
    await _truncate(led)
    ev = await led.ingest_inbound(_env("idem-1"), "hi")
    j1 = await led.submit_job(to_agent="kilabz", prompt="hi", inbound_event_id=ev)
    j2 = await led.submit_job(to_agent="kilabz", prompt="hi again", inbound_event_id=ev)
    assert str(j1) == str(j2), "a second submit for the same inbound_event must return the SAME job"
    async with led._pool.acquire() as con:
        n = await con.fetchval("SELECT count(*) FROM job WHERE inbound_event_id=$1", ev)
    assert n == 1, f"expected exactly 1 job per inbound_event, got {n}"


async def main() -> None:
    led = await PostgresLedger.connect(DSN)
    # fresh schema for the run (schema.sql is plain CREATE, not IF NOT EXISTS)
    async with led._pool.acquire() as con:
        await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await led.init_schema()

    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and inspect.iscoroutinefunction(v)]
    passed = 0
    try:
        for t in tests:
            await t(led)
            print("PASS", t.__name__)
            passed += 1
    finally:
        await led.close()
    print(f"ALL PASS ({passed})")


if __name__ == "__main__":
    asyncio.run(main())
