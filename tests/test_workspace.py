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
    wm = WorkspaceManager(tempfile.mkdtemp(prefix="mdx-wt-root-"))
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
    wm = WorkspaceManager(tempfile.mkdtemp(prefix="mdx-wt-root-"))
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
    wm = WorkspaceManager(tempfile.mkdtemp(prefix="mdx-wt-root-"))
    wt = wm.create(repo, "HEAD")
    assert wm.capture_diff(wt) is None     # nothing changed -> nothing to surface
    wm.cleanup(repo, wt)


def test_sweep_removes_orphan_keeps_live():
    """PR-1c gate: a worktree whose attempt is NOT open (its worker crashed) is GC'd;
    one whose attempt is still open is kept. Live repo untouched."""
    repo = _init_repo()
    wm = WorkspaceManager(tempfile.mkdtemp(prefix="mdx-wt-root-"))
    orphan = wm.create(repo, "HEAD", attempt_id="orphan-1")
    live = wm.create(repo, "HEAD", attempt_id="live-1")
    removed = wm.sweep(open_attempt_ids={"live-1"}, min_age_s=0.0)   # age never protects here
    assert removed == 1
    assert not Path(orphan).exists(), "orphan worktree should be swept"
    assert Path(live).exists(), "live (open-attempt) worktree must be kept"
    assert Path(repo, "app.py").read_text() == "print('v1')\n"      # live repo untouched
    # the swept worktree is also deregistered (not left dangling in `git worktree list`)
    listing = subprocess.run(["git", "worktree", "list"], cwd=repo,
                             capture_output=True, text=True, check=True).stdout
    assert "orphan-1" not in listing and "live-1" in listing


def test_sweep_keeps_too_young():
    """A worktree younger than min_age_s is never reaped, even if its attempt isn't in
    the open set — it may be a just-leased attempt whose open status the snapshot raced."""
    repo = _init_repo()
    wm = WorkspaceManager(tempfile.mkdtemp(prefix="mdx-wt-root-"))
    fresh = wm.create(repo, "HEAD", attempt_id="fresh-1")
    removed = wm.sweep(open_attempt_ids=set(), min_age_s=3600.0)     # 1h floor; dir is seconds old
    assert removed == 0 and Path(fresh).exists()


def test_sweep_fallback_hard_removes_when_repo_unrecoverable():
    """If a worktree's .git back-pointer is gone, sweep still hard-removes the orphan dir."""
    repo = _init_repo()
    wm = WorkspaceManager(tempfile.mkdtemp(prefix="mdx-wt-root-"))
    wt = wm.create(repo, "HEAD", attempt_id="broken-1")
    (Path(wt) / ".git").unlink()                                     # break the back-pointer
    removed = wm.sweep(open_attempt_ids=set(), min_age_s=0.0)
    assert removed == 1 and not Path(wt).exists()


if __name__ == "__main__":
    passed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print("PASS", _name)
            passed += 1
    print(f"ALL PASS ({passed})")
