"""mx - submit a task to the running MyndAIX runtime and print the agent's reply.

    PYTHONPATH=src python3 -m runtime.cli <agent> "<task>"
    # or alias:  mx() { MYNDAIX_DSN=... PYTHONPATH=.../src python3 -m runtime.cli "$@"; }

Needs the worker-pool service running (`python3 -m runtime.serve`) and $MYNDAIX_DSN.
This is direct ops: you name the agent, the runtime dispatches it durably and hands
back the real reply. No orchestrator in the loop.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid

from runtime.contracts import TransportEnvelope
from runtime.ledger.postgres_store import PostgresLedger
from runtime.registry import REGISTRY

DSN = os.environ.get("MYNDAIX_DSN", "postgresql://localhost/runtime")


async def submit(agent: str, task: str, *, timeout_s: float = 180.0) -> int:
    if agent not in REGISTRY:
        roster = ", ".join(sorted(REGISTRY))
        print(f"unknown agent '{agent}'. roster: {roster}", file=sys.stderr)
        return 2

    led = await PostgresLedger.connect(DSN)
    try:
        # the CLI is a transport: ingest -> submit, so completion auto-queues the reply
        env = TransportEnvelope(transport="cli", account="cli", sender_id="operator",
                                reply_target="cli:operator", dedupe_key=str(uuid.uuid4()))
        event_id = await led.ingest_inbound(env, task)
        jid = await led.submit_job(to_agent=agent, prompt=task,
                                   inbound_event_id=event_id, created_by="operator")
        print(f"-> {agent}  (job {str(jid)[:8]})", file=sys.stderr, flush=True)

        deadline = time.monotonic() + timeout_s
        st = None
        while time.monotonic() < deadline:
            st = await led.get_status(jid)
            if st and st["status"] in ("done", "failed", "dead"):
                break
            await asyncio.sleep(0.3)
        else:
            print("timed out (is the pool running? `python3 -m runtime.serve`)", file=sys.stderr)
            return 1

        reply = next((o["body"] for o in (st.get("outbound") or [])), None)
        if reply is not None:
            print(reply)
        for o in (st.get("outbound") or []):           # mark delivered so it doesn't linger
            if o["status"] == "pending":
                await led.mark_outbound_sent(o["id"], f"cli-{o['id']}")

        if st["status"] != "done":
            print(f"(job {st['status']})", file=sys.stderr)
            return 1
        return 0
    finally:
        await led.close()


def main() -> int:
    if len(sys.argv) < 3:
        print('usage: mx <agent> "<task>"', file=sys.stderr)
        return 2
    return asyncio.run(submit(sys.argv[1], sys.argv[2]))


if __name__ == "__main__":
    raise SystemExit(main())
