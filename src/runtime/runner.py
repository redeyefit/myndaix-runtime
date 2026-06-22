"""C1 runner - invoke a cli agent as a subprocess, with process-group isolation,
a hard timeout, and exit-code -> Result mapping. This is the piece that turns
'agents answer direct local shell calls' into the C1 contract.

No Postgres needed; testable standalone. Process-group kill (start_new_session +
killpg) is the bulletproof termination the design requires (openclaw leaked
orphaned children that kept spending tokens). The api adapter is the next phase
(recon is the only api agent).
"""
from __future__ import annotations

import asyncio
import os
import signal
import time
from typing import Optional

from runtime.contracts import ErrorClass, Job, Reach, Result, ResultStatus
from runtime.registry import AgentSpec
from runtime.registry import get as get_spec

_KILL_GRACE_S = 3


async def invoke_cli(spec: AgentSpec, job: Job) -> Result:
    adapter = spec.adapter
    argv = list(adapter["argv"])
    channel = adapter.get("prompt_channel", "stdin")
    stdin_data: Optional[bytes] = None
    if channel == "arg":
        argv = argv + [job.prompt]
    else:
        stdin_data = job.prompt.encode()

    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=job.worktree_path or None,
        start_new_session=True,  # own process group -> killpg reaches every child
    )

    comm = proc.communicate(stdin_data) if stdin_data is not None else proc.communicate()
    try:
        out, err = await asyncio.wait_for(comm, timeout=job.timeout_s)
    except asyncio.TimeoutError:
        _kill_group(proc.pid)
        await _reap(proc)
        return Result(
            status=ResultStatus.TIMEOUT, error_class=ErrorClass.RETRYABLE,
            text=f"timeout after {job.timeout_s}s", ms=_ms(started),
        )

    code = proc.returncode
    if code == 0:
        return Result(status=ResultStatus.OK, text=_decode(out).strip(),
                      exit_code=0, ms=_ms(started))
    return Result(
        status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
        text=(_decode(err) or _decode(out)).strip(), exit_code=code, ms=_ms(started),
    )


async def invoke(agent_id: str, job: Job) -> Result:
    spec = get_spec(agent_id)
    if spec is None:
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                      text=f"unknown agent: {agent_id}")
    if spec.reach is Reach.CLI:
        return await invoke_cli(spec, job)
    raise NotImplementedError("api adapter is the next build phase")


# -- helpers --
def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _decode(b: bytes) -> str:
    return b.decode(errors="replace")


def _kill_group(pid: int) -> None:
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


async def _reap(proc: asyncio.subprocess.Process) -> None:
    try:
        await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_S)
    except asyncio.TimeoutError:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        await proc.wait()
