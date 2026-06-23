"""HTTP Command-API tests - the runtime as a service. httpx ASGITransport drives
the REAL FastAPI app (routing + Pydantic validation) in-process against real
Postgres; a worker pool processes the queue as the separate concern it is.

Setup:  brew services start postgresql@16 && createdb runtime_test
Run:    LEDGER_TEST_DSN=postgresql://localhost/runtime_test \\
            PYTHONPATH=src python3 tests/test_api.py
"""
import asyncio
import inspect
import os
import uuid

import httpx

from runtime.api import create_app
from runtime.contracts import Authority, Reach
from runtime.ledger.postgres_store import PostgresLedger
from runtime.pool import WorkerPool
from runtime.registry import REGISTRY, AgentSpec

DSN = os.environ.get("LEDGER_TEST_DSN", "postgresql://localhost/runtime_test")


def _register():
    REGISTRY["api-echo"] = AgentSpec(
        agent_id="api-echo", reach=Reach.CLI, authority=Authority.RESPONDER,
        model="none", role="echo",
        adapter={"kind": "cli", "prompt_channel": "arg", "argv": ["printf", "echo:%s"]})


async def _client_and_ledger():
    led = await PostgresLedger.connect(DSN)
    async with led._pool.acquire() as con:
        await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await led.init_schema()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(led)),
                               base_url="http://runtime")
    return client, led


async def test_health():
    _register()
    client, led = await _client_and_ledger()
    try:
        r = await client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok" and body["queued"] == 0
    finally:
        await client.aclose()
        await led.close()


async def test_submit_then_status():
    _register()
    client, led = await _client_and_ledger()
    try:
        r = await client.post("/jobs", json={"to_agent": "api-echo", "prompt": "hi"})
        assert r.status_code == 201, r.text
        jid = r.json()["job_id"]
        assert (await client.get(f"/jobs/{jid}")).json()["status"] == "queued"
        await WorkerPool(led, size=2).run_until_idle()
        assert (await client.get(f"/jobs/{jid}")).json()["status"] == "done"
    finally:
        await client.aclose()
        await led.close()


async def test_inbound_produces_a_reply():
    _register()
    client, led = await _client_and_ledger()
    try:
        r = await client.post("/inbound", json={
            "sender_id": "alice", "dedupe_key": str(uuid.uuid4()),
            "body": "ping", "to_agent": "api-echo"})
        assert r.status_code == 201, r.text
        jid = r.json()["job_id"]
        await WorkerPool(led, size=2).run_until_idle()
        job = (await client.get(f"/jobs/{jid}")).json()
        assert job["status"] == "done"
        replies = [o["body"] for o in (job.get("outbound") or [])]
        assert any("echo:ping" in b for b in replies), f"reply not in status: {replies}"
    finally:
        await client.aclose()
        await led.close()


async def test_unknown_job_404():
    _register()
    client, led = await _client_and_ledger()
    try:
        assert (await client.get(f"/jobs/{uuid.uuid4()}")).status_code == 404
    finally:
        await client.aclose()
        await led.close()


async def test_malformed_inbound_422():
    _register()
    client, led = await _client_and_ledger()
    try:
        # missing required sender_id / dedupe_key / to_agent -> validation error
        assert (await client.post("/inbound", json={"body": "x"})).status_code == 422
    finally:
        await client.aclose()
        await led.close()


async def test_input_validation():
    _register()
    client, led = await _client_and_ledger()
    try:
        # empty dedupe_key -> 422 (would otherwise collapse distinct messages)
        r1 = await client.post("/inbound", json={
            "sender_id": "a", "dedupe_key": "", "body": "x", "to_agent": "api-echo"})
        assert r1.status_code == 422, r1.text
        # oversized body -> 422 (DoS guard)
        r2 = await client.post("/inbound", json={
            "sender_id": "a", "dedupe_key": "k", "body": "x" * 200_000, "to_agent": "api-echo"})
        assert r2.status_code == 422
        # NUL byte -> a clean 422, not a Postgres 500
        r3 = await client.post("/jobs", json={"to_agent": "api-echo", "prompt": "bad\x00nul"})
        assert r3.status_code == 422
    finally:
        await client.aclose()
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
