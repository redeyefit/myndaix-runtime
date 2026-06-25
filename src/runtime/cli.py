"""mxr - submit a task to the running MyndAIX runtime and print the agent's reply.

    PYTHONPATH=src python3 -m runtime.cli <agent> "<task>"
    # or a wrapper on PATH (see docs/OPERATING.md):  mxr <agent> "<task>"

Needs the worker-pool service running (`python3 -m runtime.serve`) and $MYNDAIX_DSN.
This is direct ops: you name the agent, the runtime dispatches it durably and hands
back the real reply. No orchestrator in the loop.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import uuid
from typing import Optional

from runtime.contracts import TransportEnvelope
from runtime.ledger.postgres_store import PostgresLedger
from runtime.registry import REGISTRY

DSN = os.environ.get("MYNDAIX_DSN", "postgresql://localhost/runtime")


async def submit(agent: str, task: str, *, context: Optional[dict] = None,
                 repo_id: Optional[str] = None, base_ref: Optional[str] = None,
                 timeout_s: float = 180.0) -> int:
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
        # repo_id/base_ref scope the job to a repo bucket (omitted -> NULL -> cap-exempt)
        jid = await led.submit_job(to_agent=agent, prompt=task, context=context,
                                   inbound_event_id=event_id, created_by="operator",
                                   repo_id=repo_id, base_ref=base_ref)
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

        if st["status"] == "done":
            reply = next((o["body"] for o in (st.get("outbound") or [])), None)
            if reply is not None:
                print(reply)
            for o in (st.get("outbound") or []):       # mark delivered so it doesn't linger
                if o["status"] == "pending":
                    await led.mark_outbound_sent(o["id"], f"cli-{o['id']}")
            return 0

        # failed/dead: surface WHY (the agent's error output, from the attempt)
        err = next((a.get("text") for a in (st.get("attempts") or [])
                    if a.get("status") == "failed" and a.get("text")), None)
        if err:
            print(err.strip(), file=sys.stderr)
        print(f"(job {st['status']})", file=sys.stderr)
        return 1
    finally:
        await led.close()


def _build_context(args: argparse.Namespace) -> dict:
    """Pack the optional media flags into Job.context (free-form dict, no contract
    change). Only set keys the operator actually passed."""
    ctx: dict = {}
    if args.image is not None:
        ctx["image_url"] = args.image
    if args.application is not None:
        ctx["application"] = args.application
    return ctx


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="mxr", description='submit a task to the MyndAIX runtime',
        epilog='for a task that starts with a dash, use --:  mxr recon -- "-v explain"')
    p.add_argument("agent", help="roster agent id (e.g. recon, higgsfield)")
    p.add_argument("task", help="the prompt / task text")
    p.add_argument("--image", metavar="URL",
                   help="input image url (media agents, e.g. higgsfield image->video)")
    p.add_argument("--application", metavar="PATH",
                   help="override the agent's media application/model path")
    p.add_argument("--repo", metavar="ID", dest="repo_id",
                   help="repo bucket id for per-repo concurrency (omitted -> cap-exempt)")
    p.add_argument("--base-ref", metavar="REF", dest="base_ref",
                   help="base git ref/SHA the work is anchored to (e.g. the reviewed tip)")
    args = p.parse_args(argv)
    return asyncio.run(submit(args.agent, args.task, context=_build_context(args),
                              repo_id=args.repo_id, base_ref=args.base_ref))


if __name__ == "__main__":
    raise SystemExit(main())
