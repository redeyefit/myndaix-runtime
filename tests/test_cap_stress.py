"""PR-2 per-repo concurrency cap — THE STRESS HARNESS (design §4 a–j). This is the
empirical merge gate: the cap is a spine edit, so its correctness is PROVEN here under
real Postgres contention, not asserted. Each test sets a small MAX_PER_REPO on the
ledger instance and hammers concurrently.

Asserts ALL of:
  (a) cap never exceeded per repo under N>cap hammer — drift-proof via the hard COUNT
  (b) cold-repo isolation: a capped hot repo never starves a cold repo's job
  (c) no spin when a repo is capped — lease_job returns None promptly, never hangs
  (d) reconcile-OFF: active reconverges to EXACTLY 0 at quiescence (per-close decrements)
  (e) close-race: complete vs fail vs reclaim vs cancel on one attempt -> exactly one decrement
  (f) queued / terminal / duplicate cancel -> zero decrement (never negative)
  (g) missing rc row never over-admits (hard COUNT gates even with no/zero cache)
  (h) zero 40P01 (deadlock) over a mixed concurrent workload
  (i) a brand-new repo's first job leases (seeds the rc row)
  (j) the hard-count + PICK queries are index-usable (EXPLAIN with seqscan off)

Setup (once):
    brew services start postgresql@16 && createdb runtime_test
Run:
    LEDGER_TEST_DSN=postgresql://localhost/runtime_test \\
        PYTHONPATH=src python3 tests/test_cap_stress.py
"""
import asyncio
import inspect
import os
import time

import asyncpg

from runtime.contracts import ErrorClass, LostLease, Result, ResultStatus
from runtime.ledger.postgres_store import PostgresLedger

DSN = os.environ.get("LEDGER_TEST_DSN", "postgresql://localhost/runtime_test")


def _ok() -> Result:
    return Result(status=ResultStatus.OK, text="done")


def _retryable() -> Result:
    return Result(status=ResultStatus.ERROR, text="boom", error_class=ErrorClass.RETRYABLE)


async def _truncate(led: PostgresLedger) -> None:
    async with led._pool.acquire() as con:
        await con.execute(
            "TRUNCATE inbound_event, job, attempt, attempt_log, outbound, dead_letter, "
            "repo_concurrency RESTART IDENTITY CASCADE")


async def _submit_n(led: PostgresLedger, repo, n, agent="kilabz") -> list:
    """Submit n queued jobs for `repo` (kilabz = responder, requeue-safe). repo=None -> cap-exempt."""
    return [await led.submit_job(to_agent=agent, prompt=f"{repo}-{i}", repo_id=repo)
            for i in range(n)]


async def _open_count(led: PostgresLedger, repo) -> int:
    """Open attempts on leased/running jobs for `repo` — the quantity the cap bounds
    (matches the hard COUNT; an orphan open attempt on a dead job is intentionally excluded)."""
    async with led._pool.acquire() as con:
        return await con.fetchval(
            """SELECT count(*) FROM attempt a JOIN job j ON j.id = a.job_id
                WHERE a.status='open' AND j.repo_id=$1 AND j.status IN ('leased','running')""",
            repo)


async def _active(led: PostgresLedger, repo) -> int:
    async with led._pool.acquire() as con:
        v = await con.fetchval("SELECT active FROM repo_concurrency WHERE repo_id=$1", repo)
    return v if v is not None else 0


async def _expire_lease(led: PostgresLedger, attempt_id) -> None:
    async with led._pool.acquire() as con:
        await con.execute(
            "UPDATE attempt SET lease_expires_at = statement_timestamp() - interval '1 hour' "
            "WHERE id = $1", attempt_id)


# -- (a) cap never exceeded under N>cap hammer, drift-proof ---------------------
async def test_a_cap_never_exceeded_under_hammer(led: PostgresLedger) -> None:
    led.MAX_PER_REPO = 2
    await _truncate(led)
    await _submit_n(led, "hot", 12)
    attempts = await asyncio.gather(*[led.lease_job(f"w{i}", []) for i in range(16)])
    leased = [a for a in attempts if a is not None]
    assert await _open_count(led, "hot") <= 2, "cap breached under hammer"
    assert len(leased) == 2, f"expected exactly cap(2) leased, got {len(leased)}"
    # DRIFT: corrupt the cached active stale-low and hammer again — the hard COUNT
    # under the rc lock must still hold the cap (the cache is never the authority).
    async with led._pool.acquire() as con:
        await con.execute("UPDATE repo_concurrency SET active=0 WHERE repo_id='hot'")
    await asyncio.gather(*[led.lease_job(f"x{i}", []) for i in range(8)])
    assert await _open_count(led, "hot") <= 2, "cap breached under counter drift"


# -- (b) cold-repo isolation: a capped hot repo doesn't starve a cold one -------
async def test_b_cold_repo_not_starved(led: PostgresLedger) -> None:
    led.MAX_PER_REPO = 2
    await _truncate(led)
    await _submit_n(led, "hot", 10)
    await _submit_n(led, "cold", 1)
    await asyncio.gather(*[led.lease_job(f"w{i}", []) for i in range(16)])
    assert await _open_count(led, "cold") == 1, "cold repo starved by a capped hot repo"
    assert await _open_count(led, "hot") <= 2, "hot cap breached"


# -- (c) no spin when capped: lease_job returns None promptly ------------------
async def test_c_no_spin_when_capped(led: PostgresLedger) -> None:
    led.MAX_PER_REPO = 2
    await _truncate(led)
    await _submit_n(led, "hot", 5)
    assert await led.lease_job("w1", []) is not None
    assert await led.lease_job("w2", []) is not None     # cap now full (2 open)
    start = time.monotonic()
    a3 = await led.lease_job("w3", [])                    # all eligible repos capped
    elapsed = time.monotonic() - start
    assert a3 is None, "leased beyond the cap"
    assert elapsed < 2.0, f"lease_job spun {elapsed:.2f}s instead of returning None"


# -- (d) reconciler OFF: active reconverges to EXACTLY 0 per close --------------
async def test_d_active_reconverges_to_zero_without_reconciler(led: PostgresLedger) -> None:
    led.MAX_PER_REPO = 4
    await _truncate(led)
    await _submit_n(led, "R", 3)
    atts = [a for a in [await led.lease_job(f"w{i}", []) for i in range(3)] if a]
    assert await _active(led, "R") == len(atts) == 3
    for i, a in enumerate(atts):
        await led.complete_attempt(a, _ok())
        assert await _active(led, "R") == 3 - (i + 1), "active didn't track the close (no reconciler)"
    assert await _active(led, "R") == 0 and await _open_count(led, "R") == 0


# -- (e) close-race -> exactly one decrement -----------------------------------
async def test_e_close_race_exactly_one_decrement(led: PostgresLedger) -> None:
    led.MAX_PER_REPO = 4
    for _ in range(6):
        await _truncate(led)
        jids = await _submit_n(led, "R", 1)
        a = await led.lease_job("w1", [])
        assert await _active(led, "R") == 1
        await _expire_lease(led, a)                       # let reclaim contend too
        # race all four close paths on the SAME attempt/job
        await asyncio.gather(
            led.complete_attempt(a, _ok()),
            led.fail_attempt(a, _retryable()),
            led.cancel(jids[0]),
            led.reclaim_expired(),
            return_exceptions=True)                       # losers raise LostLease / no-op
        assert await _open_count(led, "R") == 0, "attempt left open after a close race"
        assert await _active(led, "R") == 0, "close race did not decrement EXACTLY once"


# -- (f) queued / terminal / duplicate cancel -> zero decrement ----------------
async def test_f_cancel_zero_decrement_cases(led: PostgresLedger) -> None:
    led.MAX_PER_REPO = 4
    await _truncate(led)
    # queued cancel: no open attempt, no rc row -> decrement nothing, no underflow
    jq = await _submit_n(led, "R", 1)
    await led.cancel(jq[0])
    assert await _active(led, "R") == 0
    # leased cancel decrements once; a duplicate + a terminal cancel decrement zero more
    jl = await _submit_n(led, "R", 1)
    await led.lease_job("w1", [])
    assert await _active(led, "R") == 1
    await led.cancel(jl[0])
    assert await _active(led, "R") == 0
    await led.cancel(jl[0])                               # duplicate
    await led.cancel(jl[0])                               # terminal
    assert await _active(led, "R") == 0, "duplicate/terminal cancel drove active negative"


# -- (g) missing rc row never over-admits --------------------------------------
async def test_g_missing_rc_row_no_over_admit(led: PostgresLedger) -> None:
    led.MAX_PER_REPO = 2
    await _truncate(led)
    await _submit_n(led, "R", 5)
    assert await led.lease_job("w1", []) is not None
    assert await led.lease_job("w2", []) is not None     # 2 open, rc.active=2
    async with led._pool.acquire() as con:               # simulate a lost/missing rc row
        await con.execute("DELETE FROM repo_concurrency WHERE repo_id='R'")
    await asyncio.gather(*[led.lease_job(f"x{i}", []) for i in range(8)])
    assert await _open_count(led, "R") <= 2, "missing rc row over-admitted past the cap"


# -- (h) zero 40P01 deadlocks under a mixed concurrent workload ----------------
async def test_h_zero_deadlocks_under_mixed_load(led: PostgresLedger) -> None:
    led.MAX_PER_REPO = 3
    await _truncate(led)
    repos = ["a", "b", "c"]
    jids = {r: await _submit_n(led, r, 12) for r in repos}
    deadlocks: list = []

    async def guarded(coro_fn):
        try:
            await coro_fn()
        except asyncpg.DeadlockDetectedError as e:   # 40P01 — the thing we must never see
            deadlocks.append(e)
        except (LostLease, Exception):
            pass                                     # other outcomes aren't this test's concern

    # wave 1: hammer leases across all repos
    atts = await asyncio.gather(*[led.lease_job(f"w{i}", []) for i in range(24)])
    atts = [a for a in atts if a]
    # wave 2: every multi-row locker concurrently on overlapping repos/rows
    tasks = []
    for i, a in enumerate(atts):
        tasks.append(guarded((lambda a=a: led.complete_attempt(a, _ok())) if i % 2
                             else (lambda a=a: led.fail_attempt(a, _retryable()))))
    for r in repos:
        for jid in jids[r][:4]:
            tasks.append(guarded(lambda jid=jid: led.cancel(jid)))
    for _ in range(6):
        tasks.append(guarded(led.reclaim_expired))
    for _ in range(4):
        tasks.append(guarded(led.reconcile_repo_concurrency))
    for i in range(12):
        tasks.append(guarded(lambda i=i: led.lease_job(f"v{i}", [])))
    await asyncio.gather(*tasks)
    assert not deadlocks, f"{len(deadlocks)} x 40P01 deadlock under mixed load (lock-order bug)"
    for r in repos:                                  # cap still held throughout
        assert await _open_count(led, r) <= 3, f"cap breached on {r} during mixed load"


# -- (i) a brand-new repo's first job leases (seeds the rc row) ----------------
async def test_i_new_repo_first_job_leases(led: PostgresLedger) -> None:
    led.MAX_PER_REPO = 2
    await _truncate(led)
    await _submit_n(led, "brand-new", 1)
    assert await led.lease_job("w1", []) is not None, "first job of a new repo must lease"
    assert await _active(led, "brand-new") == 1 and await _open_count(led, "brand-new") == 1


# -- (j) the hard-count + PICK queries are index-usable ------------------------
async def test_j_queries_are_index_usable(led: PostgresLedger) -> None:
    await _truncate(led)
    await _submit_n(led, "R", 3)
    async with led._pool.acquire() as con:
        async with con.transaction():
            # with seqscan off, the planner uses an index IFF the query shape supports one.
            await con.execute("SET LOCAL enable_seqscan = off")
            hard = await con.fetch(
                """EXPLAIN SELECT count(*) FROM attempt a JOIN job j2 ON j2.id=a.job_id
                    WHERE a.status='open' AND j2.repo_id=$1
                      AND j2.status IN ('leased','running')""", "R")
            htxt = "\n".join(r["QUERY PLAN"] for r in hard)
            assert "Index" in htxt or "Bitmap" in htxt, f"hard COUNT not index-backed:\n{htxt}"
            pick = await con.fetch(
                """EXPLAIN SELECT j.id, j.repo_id FROM job j
                     LEFT JOIN repo_concurrency rc ON rc.repo_id = j.repo_id
                    WHERE j.status='queued'
                      AND (j.repo_id IS NULL OR COALESCE(rc.active,0) < $1)
                    ORDER BY j.priority DESC, j.created_at, j.id LIMIT 1""", 2)
            ptxt = "\n".join(r["QUERY PLAN"] for r in pick)
            assert "Index" in ptxt or "Bitmap" in ptxt, f"PICK not index-backed:\n{ptxt}"


async def _main() -> None:
    led = await PostgresLedger.connect(DSN)
    async with led._pool.acquire() as con:
        await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await led.init_schema()
    passed = 0
    try:
        for _name, _fn in sorted(globals().items()):
            if _name.startswith("test_") and inspect.iscoroutinefunction(_fn):
                led.MAX_PER_REPO = 4              # reset the feature flag between tests
                await _fn(led)
                print("PASS", _name)
                passed += 1
    finally:
        await led.close()
    print(f"ALL PASS ({passed})")


if __name__ == "__main__":
    asyncio.run(_main())
