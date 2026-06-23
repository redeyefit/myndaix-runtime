"""End-to-end demo - work routed through the spine to an agent and back.

    PYTHONPATH=src python3 demo.py            # fast, deterministic (demo-echo agent)
    PYTHONPATH=src python3 demo.py kilabz     # route to a REAL agent (Codex / GPT-5.5)
    PYTHONPATH=src python3 demo.py --isolate   # an agent edits code in an isolated worktree
    PYTHONPATH=src python3 demo.py --postgres  # the SAME flow, but state lives in Postgres
                                               # (needs: brew services start postgresql@16
                                               #  && createdb runtime_test)

The `--isolate` and `--postgres` runs call the SAME worker.drain() - only the
ledger differs. That is the whole thesis: persistence swaps behind the contract.
"""
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from runtime import worker
from runtime.contracts import Authority, Reach
from runtime.ledger.sqlite_store import Ledger
from runtime.registry import REGISTRY, AgentSpec


def register_demo_agent() -> None:
    # Adding an agent is ONE registry row, never a spine edit (the principle, live).
    REGISTRY["demo-echo"] = AgentSpec(
        agent_id="demo-echo", reach=Reach.CLI, authority=Authority.RESPONDER,
        model="none", role="demo echo",
        adapter={"kind": "cli", "argv": ["printf", "[demo-echo replied] %s"],
                 "prompt_channel": "arg"})


def register_fixer(agent_id: str = "fixer") -> None:
    # a deterministic workspace-actor that fixes the bug in its own worktree
    REGISTRY[agent_id] = AgentSpec(
        agent_id=agent_id, reach=Reach.CLI, authority=Authority.WORKSPACE_ACTOR,
        model="none", role="demo code fixer",
        adapter={"kind": "cli", "prompt_channel": "stdin", "argv": [
            "python3", "-c",
            "open('app.py','w').write('def add(a, b):\\n    return a + b\\n')"]})


def _make_repo_with_bug() -> str:
    repo = tempfile.mkdtemp(prefix="mdx-demo-repo-")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "demo@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "demo"], cwd=repo, check=True)
    Path(repo, "app.py").write_text("def add(a, b):\n    return a - b  # bug\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _print_diff(artifact_ref) -> None:
    if not artifact_ref:
        return
    for line in Path(artifact_ref).read_text().splitlines():
        if line[:1] in "+-" and line[:3] not in ("+++", "---"):
            print(f"    {line}")


async def demo_message(agent: str) -> None:
    if agent == "demo-echo":
        register_demo_agent()
    print(f"== MyndAIX Team Runtime - message demo (agent: {agent}) ==\n")
    ledger = Ledger()
    jid = await ledger.submit_job(agent, "Hello from the MyndAIX runtime - confirm you ran.",
                                  reply_target="terminal:demo")
    print(f"  submit_job  -> {jid[:8]}")
    processed = await worker.drain(ledger)
    print(f"  worker      -> processed {processed} job(s)")
    print(f"  job {jid[:8]} -> status={(await ledger.status(jid))['status']}")
    print("\n  delivered replies:")
    for o in await ledger.pending_outbound():
        print(f"    -> {o['reply_target']}: {o['body']!r}")
        await ledger.mark_sent(o["id"])
    final = (await ledger.status(jid))["status"]
    print(f"\n{'OK' if final == 'done' else 'FAILED'} - the spine routed a message "
          f"to an agent and returned a reply (job {final}).")


async def demo_isolated() -> None:
    register_fixer("fixer")
    repo = _make_repo_with_bug()
    print("== MyndAIX Team Runtime - workspace-isolation demo (SQLite store) ==\n")
    print(f"  target repo   : {repo}")
    print(f"  app.py before : {Path(repo, 'app.py').read_text().strip()!r}")

    ledger = Ledger()
    jid = await ledger.submit_job("fixer", "fix the bug in add()", repo_id=repo)
    await worker.drain(ledger)
    job = await ledger.status(jid)

    print(f"\n  job {jid[:8]} -> {job['status']} (ran in an isolated git worktree)")
    print(f"  app.py AFTER  : {Path(repo, 'app.py').read_text().strip()!r}   <- LIVE REPO UNTOUCHED")
    print("\n  the agent's change, captured as a reviewable artifact (NOT auto-merged):")
    _print_diff(job["artifact_ref"])
    print("\nOK - the agent edited code in isolation; the live repo is untouched.")


async def demo_isolated_postgres() -> None:
    from runtime.ledger.postgres_store import PostgresLedger
    dsn = os.environ.get("LEDGER_TEST_DSN", "postgresql://localhost/runtime_test")
    register_fixer("fixer")
    repo = _make_repo_with_bug()

    led = await PostgresLedger.connect(dsn)
    async with led._pool.acquire() as con:
        await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await led.init_schema()
    try:
        print("== MyndAIX Team Runtime - workspace isolation through POSTGRES ==\n")
        print(f"  store         : {dsn}")
        print(f"  target repo   : {repo}")
        print(f"  app.py before : {Path(repo, 'app.py').read_text().strip()!r}")

        jid = await led.submit_job(to_agent="fixer", prompt="fix the bug in add()", repo_id=repo)
        await worker.drain(led)                        # <- the SAME worker as the SQLite demo
        st = await led.get_status(jid)

        print(f"\n  job {st['id'][:8]} -> {st['status']} (state in Postgres, ran in an isolated worktree)")
        print(f"  app.py AFTER  : {Path(repo, 'app.py').read_text().strip()!r}   <- LIVE REPO UNTOUCHED")
        print("\n  the agent's change, captured as a reviewable artifact (NOT auto-merged):")
        _print_diff(st["artifact_ref"])
        print("\nOK - same worker, Postgres-backed. Persistence swapped behind the contract.")
    finally:
        await led.close()


async def demo_pool() -> None:
    import time

    from runtime.ledger.postgres_store import PostgresLedger
    from runtime.pool import WorkerPool

    dsn = os.environ.get("LEDGER_TEST_DSN", "postgresql://localhost/runtime_test")
    # a slow responder so concurrency is visible in wall-clock
    REGISTRY["pool-demo"] = AgentSpec(
        agent_id="pool-demo", reach=Reach.CLI, authority=Authority.RESPONDER,
        model="none", role="slow responder",
        adapter={"kind": "cli", "prompt_channel": "stdin",
                 "argv": ["sh", "-c", "sleep 0.3; printf done"]})

    led = await PostgresLedger.connect(dsn)
    async with led._pool.acquire() as con:
        await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await led.init_schema()
    try:
        n, size = 13, 6
        print("== MyndAIX Team Runtime - concurrent worker pool (Postgres) ==\n")
        print(f"  {n} jobs (~0.3s each) | {size} workers | 1 simulated crashed worker\n")
        for i in range(n):
            await led.submit_job(to_agent="pool-demo", prompt=f"job {i}")

        # simulate a crashed worker: lease one job, expire its lease, never complete it
        crashed = await led.lease_job("crashed-worker", [])
        async with led._pool.acquire() as con:
            await con.execute("UPDATE attempt SET lease_expires_at = "
                              "statement_timestamp() - interval '1 second' WHERE id=$1", crashed)
        print("  a worker leased a job then 'crashed' (lease expired, never completed)")

        pool = WorkerPool(led, size=size, janitor_interval_s=0.1)
        t0 = time.monotonic()
        processed = await pool.run_until_idle(quiet_s=0.5)
        elapsed = time.monotonic() - t0

        async with led._pool.acquire() as con:
            done = await con.fetchval("SELECT count(*) FROM job WHERE status='done'")
        print(f"\n  processed {processed} jobs in {elapsed:.2f}s "
              f"(sequential would be ~{n * 0.3:.1f}s)")
        print(f"  jobs done : {done}/{n}")
        print(f"  reclaimed : {pool.reclaimed} (the crashed worker's job, recovered by the janitor)")
        print("\nOK - workers drained the queue concurrently with no double-processing, and "
              "the crashed worker's job was recovered. The ledger is the only shared state.")
    finally:
        await led.close()


async def demo_terminal() -> None:
    import time

    from runtime.ledger.postgres_store import PostgresLedger
    from runtime.pool import WorkerPool
    from runtime.transport.terminal import TerminalTransport

    dsn = os.environ.get("LEDGER_TEST_DSN", "postgresql://localhost/runtime_test")
    REGISTRY["term-fast"] = AgentSpec(
        agent_id="term-fast", reach=Reach.CLI, authority=Authority.RESPONDER,
        model="none", role="fast responder",
        adapter={"kind": "cli", "prompt_channel": "arg", "argv": ["printf", "you said: %s"]})
    REGISTRY["term-slow"] = AgentSpec(
        agent_id="term-slow", reach=Reach.CLI, authority=Authority.RESPONDER,
        model="none", role="slow responder",
        adapter={"kind": "cli", "prompt_channel": "stdin",
                 "argv": ["sh", "-c", "sleep 0.6; printf '(slow agent, 0.6s) done'"]})

    led = await PostgresLedger.connect(dsn)
    async with led._pool.acquire() as con:
        await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await led.init_schema()
    try:
        print("== MyndAIX Team Runtime - terminal transport (C3 dumb pipe over Postgres) ==\n")
        pool = WorkerPool(led, size=4)
        transport = TerminalTransport(led)

        print("  inbound - each line is ingested + queued instantly; the pipe NEVER waits on an agent:")
        t0 = time.monotonic()
        await transport.ingest("slow one", to_agent="term-slow")
        await transport.ingest("hello", to_agent="term-fast")
        await transport.ingest("world", to_agent="term-fast")
        print(f"    3 messages queued in {(time.monotonic() - t0) * 1000:.0f}ms\n")

        print("  outbound - replies stream back as each job finishes (fast ones before the slow one):")
        stop = asyncio.Event()
        delivery = asyncio.ensure_future(transport.run_delivery(stop, poll_s=0.02))
        await pool.run_until_idle()
        stop.set()
        await delivery
        print("\nOK - inbound and outbound are fully decoupled through the ledger; "
              "a slow agent never blocks the pipe.")
    finally:
        await led.close()


async def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg == "--postgres":
        await demo_isolated_postgres()
    elif arg == "--pool":
        await demo_pool()
    elif arg == "--terminal":
        await demo_terminal()
    elif arg == "--isolate":
        await demo_isolated()
    else:
        await demo_message(arg or "demo-echo")


if __name__ == "__main__":
    asyncio.run(main())
