"""Worker loop - the heart of C3/C4/C5, ledger-agnostic.

A worker leases a queued job from the ledger, invokes its agent via the C1
runner, and records the result. Because it pulls from the ledger (and the
transport reads outbound from it *separately*), a slow or failed agent can never
starve the command center - the prior runtime's exact failure mode.

The worker depends ONLY on the small `WorkerLedger` interface below, which BOTH
the SQLite demo store and the Postgres production store satisfy - so the SAME
worker drives either. That is the 'swap persistence behind the contract' claim,
made literal: `drain(sqlite_ledger)` and `drain(postgres_ledger)` are the same code.

C5: a `workspace-actor` job targeting a repo runs in an ephemeral git worktree.
Its diff is captured as an artifact (never auto-merged); the live repo is never
touched. The runner enforces cwd=worktree; this loop manages the lifecycle.
"""
from __future__ import annotations

from typing import Any, Optional, Protocol

from runtime import runner
from runtime.contracts import Authority, Job, Result, ResultStatus
from runtime.registry import get as get_spec
from runtime.workspace import WorkspaceManager


class WorkerLedger(Protocol):
    """The minimal ledger surface a worker needs. Both stores implement it."""
    async def lease_job(self, worker_id: str, capabilities: list[str]) -> Optional[Any]: ...
    async def get_attempt_job(self, attempt_id: Any) -> Optional[Job]: ...
    async def complete_attempt(self, attempt_id: Any, result: Result) -> None: ...
    async def fail_attempt(self, attempt_id: Any, result: Result) -> None: ...


async def run_one(ledger: WorkerLedger, worker_id: str = "w1",
                  wm: Optional[WorkspaceManager] = None) -> bool:
    """Lease one job, run it (isolated if it's a workspace-actor targeting a repo),
    record result. Returns True if a job was processed, False if the queue was empty."""
    attempt_id = await ledger.lease_job(worker_id, [])
    if attempt_id is None:
        return False

    job = await ledger.get_attempt_job(attempt_id)
    if job is None:
        return True  # lease lost between lease and fetch (cancelled/reclaimed); skip

    spec = get_spec(job.to_agent)

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
        await ledger.complete_attempt(attempt_id, result)
    else:
        await ledger.fail_attempt(attempt_id, result)
    return True


async def drain(ledger: WorkerLedger, worker_id: str = "w1",
                wm: Optional[WorkspaceManager] = None) -> int:
    """Process the whole queue (demo). Returns the number of jobs handled."""
    n = 0
    while await run_one(ledger, worker_id, wm):
        n += 1
    return n
