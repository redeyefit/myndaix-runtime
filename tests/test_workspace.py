"""C5 workspace-isolation tests - prove the headline safety property: a
workspace-actor agent's mutations never touch the live repo, its diff is
captured as an artifact, and concurrent agents don't collide. Local git only.

Run: PYTHONPATH=src python3 tests/test_workspace.py
"""
import subprocess
import tempfile
from pathlib import Path

from runtime.workspace import WorkspaceManager


def _init_repo() -> str:
    d = tempfile.mkdtemp(prefix="mdx-testrepo-")
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=d, check=True)
    Path(d, "app.py").write_text("print('v1')\n")
    subprocess.run(["git", "add", "-A"], cwd=d, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=d, check=True)
    return d


def test_worktree_isolates_the_live_repo():
    repo = _init_repo()
    wm = WorkspaceManager()
    wt = wm.create(repo, "HEAD")

    # the "agent" mutates files in its worktree only
    Path(wt, "app.py").write_text("print('v2 - changed by the agent')\n")
    Path(wt, "new_file.txt").write_text("an artifact the agent produced\n")

    # the LIVE repo is untouched
    assert Path(repo, "app.py").read_text() == "print('v1')\n"
    assert not Path(repo, "new_file.txt").exists()

    # the change is captured as a reviewable artifact (not auto-merged)
    patch = wm.capture_diff(wt)
    assert patch and Path(patch).exists()
    diff = Path(patch).read_text()
    assert "v2 - changed by the agent" in diff
    assert "new_file.txt" in diff

    wm.cleanup(repo, wt)


def test_concurrent_worktrees_do_not_collide():
    repo = _init_repo()
    wm = WorkspaceManager()
    a = wm.create(repo, "HEAD")
    b = wm.create(repo, "HEAD")

    # two agents write conflicting content - in isolation
    Path(a, "app.py").write_text("AGENT-A\n")
    Path(b, "app.py").write_text("AGENT-B\n")

    assert Path(a, "app.py").read_text() == "AGENT-A\n"
    assert Path(b, "app.py").read_text() == "AGENT-B\n"      # no collision
    assert Path(repo, "app.py").read_text() == "print('v1')\n"  # live untouched

    wm.cleanup(repo, a)
    wm.cleanup(repo, b)


def test_no_changes_yields_no_artifact():
    repo = _init_repo()
    wm = WorkspaceManager()
    wt = wm.create(repo, "HEAD")
    assert wm.capture_diff(wt) is None     # nothing changed -> nothing to surface
    wm.cleanup(repo, wt)


if __name__ == "__main__":
    passed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print("PASS", _name)
            passed += 1
    print(f"ALL PASS ({passed})")
