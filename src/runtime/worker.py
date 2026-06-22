"""Worker loop - the heart of C3/C4/C5.

A worker leases a queued job from the ledger, invokes its agent via the C1
runner, and records the result + a reply. Because the worker pulls from the
ledger (and the transport reads outbound from it *separately*), a slow or failed
agent can never starve the command center - the prior runtime's exact failure
mode. Demo worker = a sequential drain; production = a pool with leases.

C5: a `workspace-actor` job targeting a repo runs in an ephemeral git worktree.
Its diff is captured as an artifact (never auto-merged); the live repo is never
touched. The runner enforces cwd=worktree; this loop manages the lifecycle.
"""
from __future__ import annotations

import uuid
from typing import Optional

from runtime import runner
from runtime.contracts import Authority, Job, ResultStatus
from runtime.ledger.sqlite_store import Ledger
from runtime.registry import get as get_spec
from runtime.workspace import WorkspaceManager


async def run_one(ledger: Ledger, worker_id: str = "w1",
                  wm: Optional[WorkspaceManager] = None) -> bool:
    """Lease one job, run it through the C1 runner (isolated if it's a
    workspace-actor targeting a repo), record result + reply. Returns True if a
    job was processed, False if the queue was empty."""
    row = ledger.lease_job(worker_id)
    if row is None:
        return False

    spec = get_spec(row["to_agent"])
    job = Job(id=uuid.UUID(row["id"]), to_agent=row["to_agent"], prompt=row["body"],
              repo_id=row["repo_id"], base_ref=row["base_ref"])

    # C5: workspace-actor + a target repo -> run in an isolated worktree.
    worktree = None
    if spec and spec.authority is Authority.WORKSPACE_ACTOR and job.repo_id:
        wm = wm or WorkspaceManager()
        worktree = wm.create(job.repo_id, job.base_ref or "HEAD")
        job.worktree_path = worktree

    result = await runner.invoke(job.to_agent, job)

    if worktree:
        if result.status is ResultStatus.OK:
            result.artifact_ref = wm.capture_diff(worktree)  # diff; never auto-merged
        wm.cleanup(job.repo_id, worktree)                    # live repo untouched

    if result.status is ResultStatus.OK:
        ledger.complete_attempt(row["id"], row["attempt_id"], result)
    else:
        ledger.fail_attempt(row["id"], row["attempt_id"], result)
    return True


async def drain(ledger: Ledger, worker_id: str = "w1",
                wm: Optional[WorkspaceManager] = None) -> int:
    """Process the whole queue (demo). Returns the number of jobs handled."""
    n = 0
    while await run_one(ledger, worker_id, wm):
        n += 1
    return n
