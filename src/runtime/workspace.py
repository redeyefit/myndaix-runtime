"""C5 - workspace isolation.

A `workspace-actor` job runs in an **ephemeral git worktree** created from a
pinned ref. It mutates ONLY that tree; its changes are captured as a diff
artifact (never auto-merged); the live repo is never touched mid-flight. Two
concurrent actors get separate worktrees, so they can't corrupt a shared repo -
the failure a single-threaded runtime only prevented by accident.

The runner enforces cwd=worktree (C5); this class manages the worktree lifecycle.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Optional


def _git(args: list[str], cwd: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout


class WorkspaceManager:
    """Creates, captures, and tears down ephemeral git worktrees per job."""

    def __init__(self, worktree_root: Optional[str] = None):
        self.root = Path(worktree_root or tempfile.mkdtemp(prefix="mdx-worktrees-"))
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, repo_path: str, base_ref: str = "HEAD") -> str:
        """git worktree add a fresh, isolated, detached checkout at base_ref.
        Returns the worktree path. The agent mutates only this directory."""
        wt = self.root / f"wt-{uuid.uuid4().hex[:12]}"
        _git(["worktree", "add", "--detach", str(wt), base_ref], cwd=repo_path)
        return str(wt)

    def capture_diff(self, worktree_path: str) -> Optional[str]:
        """Capture the worktree's changes (including new files) as a .patch
        artifact relative to its base ref. Returns the patch path, or None if
        nothing changed. This is the artifact_ref surfaced for review - it is
        NEVER auto-merged into the live tree."""
        _git(["add", "-A"], cwd=worktree_path)            # stage incl. untracked
        diff = _git(["diff", "--cached"], cwd=worktree_path)
        if not diff.strip():
            return None
        patch = self.root / f"{Path(worktree_path).name}.patch"
        patch.write_text(diff)
        return str(patch)

    def cleanup(self, repo_path: str, worktree_path: str) -> None:
        """Remove the worktree. Failure to deregister falls back to a hard rm -
        the live repo is never affected."""
        try:
            _git(["worktree", "remove", "--force", worktree_path], cwd=repo_path)
        except subprocess.CalledProcessError:
            shutil.rmtree(worktree_path, ignore_errors=True)
