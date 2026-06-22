"""End-to-end demo - a message routed through the spine to an agent and back.

    PYTHONPATH=src python3 demo.py            # fast, deterministic (demo-echo agent)
    PYTHONPATH=src python3 demo.py codex      # route to a REAL agent (codex), if installed

Zero external dependencies (SQLite in-memory). This proves the whole loop:
submit_job -> ledger -> worker leases -> C1 runner invokes the agent -> result
captured -> reply delivered. Production swaps SQLite for Postgres behind the same
Command-API contract; nothing else changes.
"""
import asyncio
import sys

from runtime import worker
from runtime.contracts import Authority, Reach
from runtime.ledger.sqlite_store import Ledger
from runtime.registry import REGISTRY, AgentSpec


def register_demo_agent() -> None:
    # Adding an agent is ONE registry row, never a spine edit (the non-negotiable
    # principle, demonstrated live).
    REGISTRY["demo-echo"] = AgentSpec(
        agent_id="demo-echo", reach=Reach.CLI, authority=Authority.RESPONDER,
        model="none", role="demo echo",
        adapter={"kind": "cli", "argv": ["printf", "[demo-echo replied] %s"],
                 "prompt_channel": "arg"})


async def main() -> None:
    agent = sys.argv[1] if len(sys.argv) > 1 else "demo-echo"
    if agent == "demo-echo":
        register_demo_agent()

    print(f"== MyndAIX Team Runtime - end-to-end demo (agent: {agent}) ==\n")
    ledger = Ledger()  # in-memory SQLite

    jid = ledger.submit_job(agent, "Hello from the MyndAIX runtime - confirm you ran.",
                            reply_target="terminal:demo")
    print(f"  submit_job  -> {jid[:8]}  status={ledger.status(jid)['status']}")

    processed = await worker.drain(ledger)
    print(f"  worker      -> processed {processed} job(s)")
    print(f"  job {jid[:8]} -> status={ledger.status(jid)['status']}")

    print("\n  delivered replies:")
    for o in ledger.pending_outbound():
        print(f"    -> {o['reply_target']}: {o['body']!r}")
        ledger.mark_sent(o["id"])

    final = ledger.status(jid)["status"]
    print(f"\n{'OK' if final == 'done' else 'FAILED'} - the spine routed a message "
          f"to an agent and returned a reply (job {final}).")


if __name__ == "__main__":
    asyncio.run(main())
