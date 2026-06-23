"""Concurrent worker pool proofs (against real Postgres). This is what the
lease/reclaim machinery was built for:

  1. N workers drain a queue with NO double-processing (one OK attempt per job).
  2. A crashed worker's job is recovered by the janitor and finished by another.
  3. A healthy long job survives via heartbeats (NOT reclaimed).

Setup:  brew services start postgresql@16 && createdb runtime_test
Run:    LEDGER_TEST_DSN=postgresql://localhost/runtime_test \\
            PYTHONPATH=src python3 tests/test_pool.py
"""
import asyncio
import inspect
import os
import subprocess
import tempfile
from pathlib import Path

from runtime.contracts import Authority, Reach
from runtime.ledger.postgres_store import PostgresLedger
from runtime.pool import WorkerPool
from runtime.registry import REGISTRY, AgentSpec

DSN = os.environ.get("LEDGER_TEST_DSN", "postgresql://localhost/runtime_test")


def _register():
    REGISTRY["pool-fast"] = AgentSpec(
        agent_id="pool-fast", reach=Reach.CLI, authority=Authority.RESPONDER,
        model="none", role="fast responder",
        adapter={"kind": "cli", "prompt_channel": "arg", "argv": ["printf", "done %s"]})
    REGISTRY["pool-slow"] = AgentSpec(
        agent_id="pool-slow", reach=Reach.CLI, authority=Authority.RESPONDER,
        model="none", role="slow responder",
        adapter={"kind": "cli", "prompt_channel": "stdin",
                 "argv": ["sh", "-c", "sleep 0.8; printf done"]})


async def _fresh() -> PostgresLedger:
    led = await PostgresLedger.connect(DSN)
    async with led._pool.acquire() as con:
        await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await led.init_schema()
    return led


async def test_pool_concurrent_exactly_once():
    _register()
    led = await _fresh()
    try:
        n = 40
        for i in range(n):
            await led.submit_job(to_agent="pool-fast", prompt=f"job {i}")
        pool = WorkerPool(led, size=8)
        processed = await pool.run_until_idle()
        assert processed == n, f"processed {processed}, expected {n}"
        async with led._pool.acquire() as con:
            done = await con.fetchval("SELECT count(*) FROM job WHERE status='done'")
            ok = await con.fetchval("SELECT count(*) FROM attempt WHERE status='ok'")
            workers = await con.fetchval("SELECT count(DISTINCT worker_id) FROM attempt")
        assert done == n, f"done={done}, expected {n}"
        assert ok == n, f"ok attempts={ok}, expected {n} (no double-processing)"
        assert workers > 1, f"expected concurrent workers, only {workers} did work"
    finally:
        await led.close()


async def test_pool_recovers_crashed_worker():
    _register()
    led = await _fresh()
    try:
        jid = await led.submit_job(to_agent="pool-fast", prompt="recover me")
        # a worker leases it then 'crashes' (never completes); force its lease expired
        att = await led.lease_job("crasher", [])
        assert att is not None
        async with led._pool.acquire() as con:
            await con.execute(
                "UPDATE attempt SET lease_expires_at = statement_timestamp() - interval '1 second' "
                "WHERE id=$1", att)
        # the pool's janitor reclaims it -> requeues -> a healthy worker finishes it
        pool = WorkerPool(led, size=2, janitor_interval_s=0.1)
        await pool.run_until_idle(quiet_s=0.6)
        st = await led.get_status(jid)
        assert st["status"] == "done", f"crashed job not recovered: {st['status']}"
        assert pool.reclaimed >= 1, f"janitor should have reclaimed >=1, got {pool.reclaimed}"
    finally:
        await led.close()


async def test_pool_heartbeat_keeps_long_job():
    _register()
    led = await _fresh()
    led.LEASE_SECONDS = 0.4         # short lease: a 0.8s job WOULD be reclaimed...
    led.HEARTBEAT_SECONDS = 0.6     # ...but each heartbeat extends it
    try:
        jid = await led.submit_job(to_agent="pool-slow", prompt="long job")
        pool = WorkerPool(led, size=1, janitor_interval_s=0.1, heartbeat_interval_s=0.12)
        await pool.run_until_idle(quiet_s=0.5)
        st = await led.get_status(jid)
        assert st["status"] == "done", f"long job status={st['status']}"
        async with led._pool.acquire() as con:
            attempts = await con.fetchval("SELECT count(*) FROM attempt WHERE job_id=$1", jid)
        assert attempts == 1, f"heartbeat should keep ONE attempt, got {attempts}"
        assert pool.reclaimed == 0, f"heartbeat should prevent reclaim, got {pool.reclaimed}"
    finally:
        await led.close()


def _init_repo() -> str:
    d = tempfile.mkdtemp(prefix="mdx-poolrepo-")
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=d, check=True)
    Path(d, "app.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=d, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=d, check=True)
    return d


# -- a poison job (bad binary) must NOT kill the fleet (the P0 the suite missed) --
async def test_pool_survives_bad_argv():
    _register()
    REGISTRY["pool-badbin"] = AgentSpec(
        agent_id="pool-badbin", reach=Reach.CLI, authority=Authority.RESPONDER,
        model="none", role="bad binary",
        adapter={"kind": "cli", "prompt_channel": "arg",
                 "argv": ["mdx-nonexistent-binary-zzz"]})
    led = await _fresh()
    try:
        good = 5
        for i in range(good):
            await led.submit_job(to_agent="pool-fast", prompt=f"good {i}")
        poison = await led.submit_job(to_agent="pool-badbin", prompt="boom")
        pool = WorkerPool(led, size=4)
        await pool.run_until_idle()
        async with led._pool.acquire() as con:
            done = await con.fetchval("SELECT count(*) FROM job WHERE status='done'")
        assert done == good, f"good jobs not all done ({done}/{good}) - workers died?"
        assert (await led.get_status(poison))["status"] == "failed"
        assert pool.worker_faults == 0, "a bad binary is the runner's job, not the backstop's"
    finally:
        await led.close()


# -- an exception INSIDE process_attempt (poison adapter) hits the worker backstop --
async def test_pool_survives_poison_adapter():
    _register()
    REGISTRY["pool-poison"] = AgentSpec(
        agent_id="pool-poison", reach=Reach.CLI, authority=Authority.RESPONDER,
        model="none", role="malformed adapter",
        adapter={"kind": "cli", "prompt_channel": "arg"})  # no 'argv' -> KeyError
    led = await _fresh()
    try:
        good = 5
        for i in range(good):
            await led.submit_job(to_agent="pool-fast", prompt=f"good {i}")
        poison = await led.submit_job(to_agent="pool-poison", prompt="boom")
        pool = WorkerPool(led, size=4)
        await pool.run_until_idle()
        async with led._pool.acquire() as con:
            done = await con.fetchval("SELECT count(*) FROM job WHERE status='done'")
        assert done == good, f"workers died on a poison adapter ({done}/{good})"
        assert (await led.get_status(poison))["status"] == "failed"
        assert pool.worker_faults >= 1, "the backstop should have caught the KeyError"
    finally:
        await led.close()


# -- a workspace-actor that raises after worktree creation must NOT leak the worktree --
async def test_pool_worktree_cleaned_on_failure():
    REGISTRY["pool-bad-ws"] = AgentSpec(
        agent_id="pool-bad-ws", reach=Reach.CLI, authority=Authority.WORKSPACE_ACTOR,
        model="none", role="broken workspace actor",
        adapter={"kind": "cli", "prompt_channel": "stdin"})  # no argv -> KeyError post-worktree
    led = await _fresh()
    repo = _init_repo()
    try:
        jid = await led.submit_job(to_agent="pool-bad-ws", prompt="edit", repo_id=repo)
        pool = WorkerPool(led, size=2)
        await pool.run_until_idle()
        assert (await led.get_status(jid))["status"] == "failed"
        out = subprocess.run(["git", "worktree", "list"], cwd=repo,
                             capture_output=True, text=True)
        lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
        assert len(lines) == 1, f"orphan worktree leaked into the live repo: {out.stdout!r}"
    finally:
        await led.close()


# -- misconfig is rejected loudly, not silently defeated --
async def test_pool_rejects_bad_config():
    led = await _fresh()
    try:
        led.LEASE_SECONDS = 1.0
        bad_hb = False
        try:
            WorkerPool(led, heartbeat_interval_s=0.9)  # > lease/2
        except ValueError:
            bad_hb = True
        assert bad_hb, "should reject heartbeat_interval_s > LEASE_SECONDS/2"
        bad_poll = False
        try:
            await WorkerPool(led, poll_s=0.5).run_until_idle(quiet_s=0.4)  # poll_s >= quiet_s
        except ValueError:
            bad_poll = True
        assert bad_poll, "should reject poll_s >= quiet_s"
    finally:
        await led.close()


async def _main():
    passed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and inspect.iscoroutinefunction(_fn):
            await _fn()
            print("PASS", _name)
            passed += 1
    print(f"ALL PASS ({passed})")


if __name__ == "__main__":
    asyncio.run(_main())
