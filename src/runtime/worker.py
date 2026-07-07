"""Worker loop - the heart of C3/C4/C5, ledger-agnostic.

A worker leases a queued job from the ledger, invokes its agent via the C1
runner, and records the result. Because it pulls from the ledger (and the
transport reads outbound from it *separately*), a slow or failed agent can never
starve the command center - the prior runtime's exact failure mode.

The worker depends ONLY on the small `WorkerLedger` interface below, which BOTH
the SQLite demo store and the Postgres production store satisfy - so the SAME
code drives either (`drain()` sequentially; `pool.WorkerPool` concurrently).

`process_attempt` is the shared core (used by run_one AND the pool). Its hard
guarantees:
  * a workspace-actor runs in an ephemeral git worktree (C5), and that worktree is
    ALWAYS cleaned up (try/finally) - even if the agent raises or is cancelled;
  * a heartbeat keeps a long job's lease alive; if the lease is LOST mid-run, the
    orphaned agent is CANCELLED (its process group killed) and the job is dropped -
    never left running to double-mutate a worktree another worker now owns;
  * finalization that didn't transition the attempt (LostLease) means another
    worker owns the job now -> return None (not counted), and any orphan diff
    artifact this attempt wrote is removed.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional, Protocol

from runtime import runner
from runtime.contracts import Authority, Job, LostLease, Result, ResultStatus
from runtime.registry import get as get_spec
from runtime.workspace import WorkspaceManager


class WorkerLedger(Protocol):
    """The minimal ledger surface a worker needs. Both stores implement it.
    (heartbeat_attempt is optional - used only if present + a heartbeat is asked.)"""
    async def lease_job(self, worker_id: str, capabilities: list[str]) -> Optional[Any]: ...
    async def get_attempt_job(self, attempt_id: Any) -> Optional[Job]: ...
    async def complete_attempt(self, attempt_id: Any, result: Result) -> None: ...
    async def fail_attempt(self, attempt_id: Any, result: Result) -> None: ...


def _unlink(path: Optional[str]) -> None:
    if path:
        try:
            Path(path).unlink()
        except OSError:
            pass


async def _invoke(ledger, attempt_id, job: Job, heartbeat_interval_s: Optional[float]) -> Result:
    """Run the agent. With a heartbeat (and a ledger that supports it), extend the
    lease periodically so a job longer than the lease isn't reclaimed. If a
    heartbeat finds the lease GONE, cancel the now-orphaned agent (the runner kills
    its process group) and raise LostLease - do NOT let it run to completion."""
    if not heartbeat_interval_s or not hasattr(ledger, "heartbeat_attempt"):
        return await runner.invoke(job.to_agent, job)
    task = asyncio.ensure_future(runner.invoke(job.to_agent, job))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=heartbeat_interval_s)
            if task in done:
                return task.result()
            # raises LostLease if the lease is gone (or any heartbeat error). The
            # finally below cancels + reaps the agent on EVERY exit path - LostLease,
            # outer cancellation (shutdown), or a transient heartbeat error - so no
            # subprocess is ever orphaned (and a worktree is never cleaned out from
            # under a still-running agent).
            await ledger.heartbeat_attempt(attempt_id)
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass


async def process_attempt(ledger: "WorkerLedger", attempt_id: Any,
                          wm: Optional[WorkspaceManager] = None,
                          heartbeat_interval_s: Optional[float] = None) -> Optional[ResultStatus]:
    """Run a leased attempt to completion and record the result. Returns the
    ResultStatus, or None if the lease was lost (another worker owns the job now)."""
    job = await ledger.get_attempt_job(attempt_id)
    if job is None:
        return None  # lease lost between lease and fetch

    spec = get_spec(job.to_agent)

    # C5: workspace-actor + a target repo -> run in an isolated worktree.
    worktree = None
    if spec and spec.authority is Authority.WORKSPACE_ACTOR and job.repo_id:
        wm = wm or WorkspaceManager()
        # name the worktree by attempt_id so the janitor sweep can correlate a leftover
        # dir to its (now-closed) attempt after a hard crash (PR-1c)
        # to_thread: workspace._git is a BLOCKING subprocess. Running it directly on the event loop
        # (as this did) meant a stalled/wedged git froze EVERY worker + the janitor + all heartbeats
        # until it returned — and expired leases could then be reclaimed and double-run (core-audit
        # HIGH). Offload to a thread so the loop keeps servicing others; _git's timeout frees the thread.
        worktree = await asyncio.to_thread(wm.create, job.repo_id, job.base_ref or "HEAD", str(attempt_id))
        job.worktree_path = worktree

    try:
        try:
            result = await _invoke(ledger, attempt_id, job, heartbeat_interval_s)
        except LostLease:
            return None  # reclaimed mid-run; agent cancelled, nothing to record

        if worktree is not None and result.status is ResultStatus.OK:
            result.artifact_ref = await asyncio.to_thread(wm.capture_diff, worktree)  # diff; never auto-merged

        try:
            if result.status is ResultStatus.OK:
                await ledger.complete_attempt(attempt_id, result)
            else:
                await ledger.fail_attempt(attempt_id, result)
        except LostLease:
            _unlink(result.artifact_ref)  # orphan diff for a job we no longer own
            return None
        return result.status
    finally:
        if worktree is not None:
            await asyncio.to_thread(wm.cleanup, job.repo_id, worktree)  # off-loop; live repo untouched


async def run_one(ledger: "WorkerLedger", worker_id: str = "w1",
                  wm: Optional[WorkspaceManager] = None) -> bool:
    """Lease one job and run it. Returns True if a job was processed, False if
    the queue was empty."""
    attempt_id = await ledger.lease_job(worker_id, [])
    if attempt_id is None:
        return False
    await process_attempt(ledger, attempt_id, wm)
    return True


async def drain(ledger: "WorkerLedger", worker_id: str = "w1",
                wm: Optional[WorkspaceManager] = None) -> int:
    """Process the whole queue sequentially (demo). Returns jobs handled.
    For concurrency + crash recovery, use pool.WorkerPool."""
    n = 0
    while await run_one(ledger, worker_id, wm):
        n += 1
    return n
