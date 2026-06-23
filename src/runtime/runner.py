"""C1 runner - invoke a cli agent as a subprocess, with process-group isolation,
a hard timeout, and exit-code -> Result mapping. This is the piece that turns
'agents answer direct local shell calls' into the C1 contract.

No Postgres needed; testable standalone. Process-group kill (start_new_session +
killpg) is the bulletproof termination the design requires (a prior runtime
leaked orphaned children that kept burning tokens). The api adapter is the next phase
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
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=job.worktree_path or None,
            start_new_session=True,  # own process group -> killpg reaches every child
        )
    except (FileNotFoundError, PermissionError, NotADirectoryError, OSError) as e:
        # a misconfigured argv/cwd is a TERMINAL failure, NOT a crash - the worker
        # must record it and stay alive (a poison job can't take down the fleet).
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                      text=f"spawn failed: {e}", ms=_ms(started))

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
    except asyncio.CancelledError:
        # cancelled (lease lost mid-run / pool shutdown) -> kill the process group
        # so no orphaned child keeps burning, then propagate. shield the reap so a
        # SECOND cancellation can't interrupt the SIGKILL escalation in _reap.
        _kill_group(proc.pid)
        await asyncio.shield(_reap(proc))
        raise

    code = proc.returncode
    if code == 0:
        return Result(status=ResultStatus.OK, text=_decode(out).strip(),
                      exit_code=0, ms=_ms(started))
    return Result(
        status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
        text=(_decode(err) or _decode(out)).strip(), exit_code=code, ms=_ms(started),
    )


async def invoke_api(spec: AgentSpec, job: Job, *, transport=None) -> Result:
    """Invoke an OpenAI-compatible chat-completions API agent (e.g. recon -> Perplexity).
    The key comes from os.environ[secret_ref] - never the adapter - so secrets stay out
    of the roster/config. A missing key, missing httpx, or a non-200 is a clean Result,
    never a crash (4xx -> terminal, 5xx/network -> retryable)."""
    try:
        import httpx
    except ImportError:
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                      text="api agents need httpx (pip install httpx)")
    a = spec.adapter
    endpoint = a.get("endpoint")
    if not endpoint:
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                      text="api adapter missing 'endpoint'")
    secret_ref = a.get("secret_ref")
    key = os.environ.get(secret_ref) if secret_ref else None
    if secret_ref and not key:
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                      text=f"missing API key in env: {secret_ref}")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {"model": a.get("model"),
               "messages": [{"role": "user", "content": job.prompt}]}
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=job.timeout_s, transport=transport) as client:
            resp = await client.post(endpoint, json=payload, headers=headers)
    except httpx.HTTPError as e:
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.RETRYABLE,
                      text=f"api request failed: {e}", ms=_ms(started))
    if resp.status_code != 200:
        ec = ErrorClass.RETRYABLE if resp.status_code >= 500 else ErrorClass.TERMINAL
        return Result(status=ResultStatus.ERROR, error_class=ec, exit_code=resp.status_code,
                      text=f"api {resp.status_code}: {resp.text[:300]}", ms=_ms(started))
    try:
        text = resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as e:
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                      text=f"unexpected api response shape: {e}", ms=_ms(started))
    return Result(status=ResultStatus.OK, text=str(text).strip(), ms=_ms(started))


async def invoke(agent_id: str, job: Job) -> Result:
    spec = get_spec(agent_id)
    if spec is None:
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                      text=f"unknown agent: {agent_id}")
    if spec.reach is Reach.CLI:
        return await invoke_cli(spec, job)
    return await invoke_api(spec, job)


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
