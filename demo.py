"""End-to-end demo - work routed through the spine to an agent and back.

    PYTHONPATH=src python3 demo.py            # fast, deterministic (demo-echo agent)
    PYTHONPATH=src python3 demo.py kilabz     # route to a REAL agent (codex / GPT-5.5)
    PYTHONPATH=src python3 demo.py --isolate  # a workspace-actor edits code in an
                                              # isolated git worktree; the diff comes
                                              # back as an artifact, live repo untouched

Zero external dependencies (SQLite in-memory). Production swaps SQLite for
Postgres behind the same Command-API contract; nothing else changes.
"""
import asyncio
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


async def demo_message(agent: str) -> None:
    if agent == "demo-echo":
        register_demo_agent()
    print(f"== MyndAIX Team Runtime - message demo (agent: {agent}) ==\n")
    ledger = Ledger()
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


def _make_repo_with_bug() -> str:
    repo = tempfile.mkdtemp(prefix="mdx-demo-repo-")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "demo@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "demo"], cwd=repo, check=True)
    Path(repo, "app.py").write_text("def add(a, b):\n    return a - b  # bug\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


async def demo_isolated() -> None:
    # a deterministic workspace-actor that fixes the bug in its own worktree
    REGISTRY["fixer"] = AgentSpec(
        agent_id="fixer", reach=Reach.CLI, authority=Authority.WORKSPACE_ACTOR,
        model="none", role="demo code fixer",
        adapter={"kind": "cli", "prompt_channel": "stdin", "argv": [
            "python3", "-c",
            "open('app.py','w').write('def add(a, b):\\n    return a + b\\n')"]})

    repo = _make_repo_with_bug()
    print("== MyndAIX Team Runtime - workspace-isolation demo ==\n")
    print(f"  target repo   : {repo}")
    print(f"  app.py before : {Path(repo, 'app.py').read_text().strip()!r}")

    ledger = Ledger()
    jid = ledger.submit_job("fixer", "fix the bug in add()", repo_id=repo)
    await worker.drain(ledger)
    job = ledger.status(jid)

    print(f"\n  job {jid[:8]} -> {job['status']} (ran in an isolated git worktree)")
    print(f"  app.py AFTER  : {Path(repo, 'app.py').read_text().strip()!r}   <- LIVE REPO UNTOUCHED")
    print("\n  the agent's change, captured as a reviewable artifact (NOT auto-merged):")
    for line in Path(job["artifact_ref"]).read_text().splitlines():
        if line[:1] in "+-" and line[:3] not in ("+++", "---"):
            print(f"    {line}")
    print("\nOK - the agent edited code in isolation; the live repo is untouched.")


async def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--isolate":
        await demo_isolated()
    else:
        await demo_message(sys.argv[1] if len(sys.argv) > 1 else "demo-echo")


if __name__ == "__main__":
    asyncio.run(main())
