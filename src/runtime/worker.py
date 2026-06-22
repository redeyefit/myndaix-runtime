"""Worker loop - the heart of C3/C4.

A worker leases a queued job from the ledger, invokes its agent via the C1
runner, and records the result + a reply. Because the worker pulls from the
ledger (and the transport reads outbound from it *separately*), a slow or failed
agent can never starve the command center - the prior runtime's exact failure
mode. Demo worker = a sequential drain; production = a pool with leases.
"""
from __future__ import annotations

import uuid

from runtime import runner
from runtime.contracts import Job, ResultStatus
from runtime.ledger.sqlite_store import Ledger


async def run_one(ledger: Ledger, worker_id: str = "w1") -> bool:
    """Lease one job, run it through the C1 runner, record result + reply.
    Returns True if a job was processed, False if the queue was empty."""
    row = ledger.lease_job(worker_id)
    if row is None:
        return False
    job = Job(id=uuid.UUID(row["id"]), to_agent=row["to_agent"], prompt=row["body"],
              base_ref=row["base_ref"])
    result = await runner.invoke(job.to_agent, job)
    if result.status is ResultStatus.OK:
        ledger.complete_attempt(row["id"], row["attempt_id"], result)
    else:
        ledger.fail_attempt(row["id"], row["attempt_id"], result)
    return True


async def drain(ledger: Ledger, worker_id: str = "w1") -> int:
    """Process the whole queue (demo). Returns the number of jobs handled."""
    n = 0
    while await run_one(ledger, worker_id):
        n += 1
    return n
