"""Worker + C5 isolation over the SQLite store - a workspace-actor job runs in an
isolated worktree, its change is captured as the job's artifact, and the LIVE repo
is untouched. Deterministic (a python-writer agent), no real LLM. Uses the SAME
worker.drain() the Postgres e2e test uses (test_postgres_e2e.py).

Run: PYTHONPATH=src python3 tests/test_worker.py
"""
import asyncio
import inspect
import subprocess
import tempfile
from pathlib import Path

from runtime import worker
from runtime.contracts import Authority, Reach
from runtime.ledger.sqlite_store import Ledger
from runtime.registry import REGISTRY, AgentSpec


def _init_repo() -> str:
    d = tempfile.mkdtemp(prefix="mdx-workrepo-")
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=d, check=True)
    Path(d, "app.py").write_text("def add(a, b):\n    return a - b  # bug\n")
    subprocess.run(["git", "add", "-A"], cwd=d, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=d, check=True)
    return d


def _register_fixer():
    REGISTRY["test-fixer"] = AgentSpec(
        agent_id="test-fixer", reach=Reach.CLI, authority=Authority.WORKSPACE_ACTOR,
        model="none", role="deterministic code fixer",
        adapter={"kind": "cli", "prompt_channel": "stdin", "argv": [
            "python3", "-c",
            "open('app.py','w').write('def add(a, b):\\n    return a + b\\n')"]})


async def test_worker_isolates_a_workspace_actor():
    _register_fixer()
    repo = _init_repo()
    ledger = Ledger()

    jid = await ledger.submit_job("test-fixer", "fix the bug in add()", repo_id=repo)
    processed = await worker.drain(ledger)
    assert processed == 1

    job = await ledger.status(jid)
    assert job["status"] == "done"

    artifact = job["artifact_ref"]
    assert artifact and Path(artifact).exists()
    assert "return a + b" in Path(artifact).read_text()           # the agent's change, captured
    assert "return a - b" in Path(repo, "app.py").read_text()      # live repo untouched


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
