"""Worker + C5 isolation, end-to-end - a workspace-actor job runs in an isolated
worktree, its file change is captured as the job's artifact_ref, and the LIVE
repo is untouched. Deterministic (a python-writer agent), no real LLM.

Run: PYTHONPATH=src python3 tests/test_worker.py
"""
import asyncio
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
    # a deterministic workspace-actor: rewrites app.py in its cwd (the worktree)
    REGISTRY["test-fixer"] = AgentSpec(
        agent_id="test-fixer", reach=Reach.CLI, authority=Authority.WORKSPACE_ACTOR,
        model="none", role="deterministic code fixer",
        adapter={"kind": "cli", "prompt_channel": "stdin", "argv": [
            "python3", "-c",
            "open('app.py','w').write('def add(a, b):\\n    return a + b\\n')"]})


def test_worker_isolates_a_workspace_actor():
    _register_fixer()
    repo = _init_repo()
    ledger = Ledger()

    jid = ledger.submit_job("test-fixer", "fix the bug in add()", repo_id=repo)
    processed = asyncio.run(worker.drain(ledger))
    assert processed == 1

    job = ledger.status(jid)
    assert job["status"] == "done"

    # the agent's change is captured as a diff artifact (NOT merged)
    artifact = job["artifact_ref"]
    assert artifact and Path(artifact).exists()
    assert "return a + b" in Path(artifact).read_text()

    # the LIVE repo is untouched
    assert "return a - b" in Path(repo, "app.py").read_text()


if __name__ == "__main__":
    passed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print("PASS", _name)
            passed += 1
    print(f"ALL PASS ({passed})")
