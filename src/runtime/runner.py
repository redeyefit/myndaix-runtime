"""C1 runner - invoke a cli agent as a subprocess, with process-group isolation,
a hard timeout, and exit-code -> Result mapping. This is the piece that turns
'agents answer direct local shell calls' into the C1 contract.

No Postgres needed; testable standalone. Process-group kill (start_new_session +
killpg) is the bulletproof termination the design requires (a prior runtime
leaked orphaned children that kept burning tokens). API-reach agents (e.g. recon ->
Perplexity) go through invoke_api below; the key comes from the environment, never the roster.
"""
from __future__ import annotations

import asyncio
import ipaddress
import math
import os
import signal
import socket
import time
import urllib.parse
from typing import Optional

from runtime.contracts import ErrorClass, Job, Reach, Result, ResultStatus
from runtime.registry import AgentSpec
from runtime.registry import get as get_spec

_KILL_GRACE_S = 3

# Higgsfield polling constants
_HF_POLL_INTERVAL_S = 2
_HF_POLL_RETRY_MAX = 3
_HF_POLL_RETRY_BACKOFF_S = 2
_HF_REQ_TIMEOUT_CAP_S = 30   # ceiling on any single submit/poll request (overall deadline still bounds total)
_HF_CANCEL_TIMEOUT_S = 5     # best-effort cancel POST must not hang the timeout return
_HF_ACTIVE_STATES = ("queued", "in_progress")   # everything else nonempty -> terminal (design §7b)

# Private / link-local IP ranges — blocked as image_url targets (SSRF defence)
_PRIVATE_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / AWS IMDS
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


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


async def invoke_higgsfield(spec: AgentSpec, job: Job, *, transport=None) -> Result:
    """Invoke Higgsfield's async media queue (image/text -> video). Unlike invoke_api
    this is NOT OpenAI-shaped: auth is `Authorization: Key <key>`, submit returns a
    request_id, and the result is polled. The key comes from os.environ[secret_ref],
    never the adapter.

    Idempotency (DESIGN S5, decision A - fail-closed): only a *pre-send* network
    failure (couldn't connect) is RETRYABLE - nothing reached Higgsfield, nothing
    charged. The submit POST is NON-idempotent, so an AMBIGUOUS failure (read timeout,
    protocol error: the request may have been received & charged) is TERMINAL - we must
    never re-submit and double-charge. Once submit returns a request_id the job is
    charged, so EVERY later failure (poll error, timeout, bad payload, success-path
    crash) is TERMINAL. Idempotent resume is deferred to v2."""
    try:
        import httpx
    except ImportError:
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                      text="higgsfield agents need httpx (pip install httpx)")
    a = spec.adapter
    base = a.get("base")
    if not base:
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                      text="higgsfield adapter missing 'base'")
    application = job.context.get("application") or a.get("application")
    if not application:
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                      text="higgsfield job missing 'application' (no adapter default)")
    secret_ref = a.get("secret_ref")
    key = os.environ.get(secret_ref) if secret_ref else None
    if not key:
        # Auth is mandatory - fail closed whether secret_ref is unset OR the env var is
        # missing. (Never let `key=None` build a literal 'Key None' header.)
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                      text=f"missing API key in env: {secret_ref or '<secret_ref unset>'}")

    image_url = job.context.get("image_url")
    if not image_url:
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                      text="higgsfield job missing 'image_url' (required for image->video)")
    ssrf = await _reject_unsafe_url(image_url)
    if ssrf:
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                      text=f"image_url rejected: {ssrf}")

    headers = {"Content-Type": "application/json", "Authorization": f"Key {key}"}
    submit_url = base.rstrip("/") + "/" + application.lstrip("/")
    body = {"prompt": job.prompt, "image_url": image_url}
    # Coerce the tuning knobs up front: a malformed adapter value (e.g. "3") must
    # never surface as a TypeError mid-poll (post-charge), which would violate §5-A.
    poll_interval = _hf_float(a.get("poll_interval_s"), _HF_POLL_INTERVAL_S)
    retry_backoff = _hf_float(a.get("poll_retry_backoff_s"), _HF_POLL_RETRY_BACKOFF_S)
    retry_max = _hf_int(a.get("poll_retry_max"), _HF_POLL_RETRY_MAX)

    started = time.monotonic()
    deadline = started + job.timeout_s
    async with httpx.AsyncClient(transport=transport) as client:
        # -- submit (pre-charge) --
        try:
            resp = await client.post(submit_url, json=body, headers=headers,
                                     timeout=_hf_req_timeout(deadline))
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
            # never reached the server -> nothing charged -> safe to retry
            return Result(status=ResultStatus.ERROR, error_class=ErrorClass.RETRYABLE,
                          text=f"higgsfield submit unreachable: {e}", ms=_ms(started))
        except httpx.HTTPError as e:
            # ambiguous (read timeout / protocol): the POST may have landed & charged.
            # The submit is non-idempotent, so fail CLOSED rather than risk a re-submit.
            return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                          text=f"higgsfield submit ambiguous failure (fail-closed, no retry): {e}",
                          ms=_ms(started))
        if resp.status_code >= 300:   # accept any 2xx (200/201/202 async-queue 'Accepted')
            ec = ErrorClass.RETRYABLE if resp.status_code >= 500 else ErrorClass.TERMINAL
            return Result(status=ResultStatus.ERROR, error_class=ec, exit_code=resp.status_code,
                          text=f"higgsfield submit {resp.status_code}: {resp.text[:300]}",
                          ms=_ms(started))
        try:
            sub = resp.json()
            request_id = sub["request_id"]
        except (KeyError, ValueError, TypeError) as e:
            return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                          text=f"higgsfield submit: unexpected shape: {e}", ms=_ms(started))
        # -- poll (post-charge: per DESIGN S5-A, EVERY failure here is TERMINAL) --
        # §5-A structural guarantee: the job is now charged, so NO exception may escape
        # this block. EVERY post-request_id statement (incl. URL construction) lives
        # inside it. Targeted excepts below give precise messages; this outer catch-all
        # is the backstop so any unforeseen raise (a URL httpx rejects as InvalidURL, a
        # pathological adapter value, a future httpx quirk) becomes a TERMINAL Result
        # rather than escaping -> the worker can never reclaim & re-submit -> no double
        # charge. CancelledError is a BaseException, so cooperative cancellation still
        # propagates (we must not swallow it).
        try:
            # Only trust server-returned status/cancel URLs if they share base's origin;
            # otherwise build our own from request_id. Polling/cancelling carry the HF key,
            # so we must never send it to a host the submit response could redirect us to.
            status_url = _hf_pin_url(sub.get("status_url"), base, f"/requests/{request_id}/status")
            cancel_url = _hf_pin_url(sub.get("cancel_url"), base, f"/requests/{request_id}/cancel")
            fails = 0
            while True:
                if time.monotonic() >= deadline:
                    # best-effort cancel so a still-QUEUED job stops charging (live-verified:
                    # cancel only takes effect while queued, not mid-render).
                    await _hf_best_effort_cancel(client, cancel_url, headers)
                    return Result(status=ResultStatus.TIMEOUT, error_class=ErrorClass.TERMINAL,
                                  text=f"higgsfield poll timed out after {job.timeout_s}s "
                                       f"(request_id={request_id})", ms=_ms(started))
                err = None
                try:
                    pr = await client.get(status_url, headers=headers,
                                          timeout=_hf_req_timeout(deadline))
                    if pr.status_code != 200:
                        err = f"poll {pr.status_code}: {pr.text[:200]}"
                except httpx.HTTPError as e:
                    err = f"poll error: {e}"
                if err is not None:   # transient: retry up to retry_max, then fail closed
                    fails += 1
                    if fails > retry_max:
                        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                                      text=f"higgsfield {err} (charged, no retry)", ms=_ms(started))
                    await asyncio.sleep(_hf_sleep(retry_backoff, deadline))
                    continue
                fails = 0
                try:
                    data = pr.json()
                    status = str(data.get("status") or "").lower()
                except (ValueError, TypeError, AttributeError) as e:
                    # malformed-but-charged payload must NEVER raise - fail closed.
                    return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                                  text=f"higgsfield poll: bad payload (charged): {e}", ms=_ms(started))
                if status == "completed":
                    url = _hf_artifact_url(data)
                    if not url:
                        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                                      text="higgsfield completed but no video/image url",
                                      ms=_ms(started))
                    # cost is best-effort metadata: a malformed value must not discard the
                    # paid artifact (passing a non-number straight to Result would raise).
                    cost = data.get("cost")
                    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
                        cost = None
                    return Result(status=ResultStatus.OK, text=url, artifact_ref=url,
                                  cost=cost, ms=_ms(started))
                if status in ("failed", "nsfw"):
                    # Higgsfield refunds these - terminal, no retry.
                    return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                                  text=f"higgsfield {status} (request_id={request_id})",
                                  ms=_ms(started))
                if status and status not in _HF_ACTIVE_STATES:
                    # design §7b: an unrecognized status is TERMINAL with the status logged,
                    # not silently polled until timeout (which would lose the real reason).
                    return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                                  text=f"higgsfield unknown status {status!r} "
                                       f"(request_id={request_id})", ms=_ms(started))
                # active (queued / in_progress) or a momentarily-missing status -> keep waiting.
                await asyncio.sleep(_hf_sleep(poll_interval, deadline))
        except Exception as e:   # noqa: BLE001 - deliberate post-charge backstop (see above)
            return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                          text=f"higgsfield poll: unexpected post-charge error "
                               f"(charged, no retry): {type(e).__name__}: {e}", ms=_ms(started))


async def invoke(agent_id: str, job: Job) -> Result:
    spec = get_spec(agent_id)
    if spec is None:
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                      text=f"unknown agent: {agent_id}")
    if spec.reach is Reach.CLI:
        return await invoke_cli(spec, job)
    if spec.adapter.get("kind") == "higgsfield":
        return await invoke_higgsfield(spec, job)
    return await invoke_api(spec, job)


# -- helpers --
def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _hf_int(v, default: int) -> int:
    """Coerce an adapter knob to int, falling back on None/garbage (never raise)."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _hf_float(v, default: float) -> float:
    """Coerce an adapter knob to a FINITE float, falling back on None/garbage/nan/inf
    (never raise; a non-finite delay would later blow up asyncio.sleep)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _hf_sleep(delay: float, deadline: float) -> float:
    """A poll/backoff sleep that never overshoots the deadline and never feeds
    asyncio.sleep a negative or non-finite value (which would raise post-charge)."""
    d = min(delay, max(0.0, deadline - time.monotonic()))
    return d if math.isfinite(d) and d >= 0 else 0.0


def _hf_artifact_url(data: dict) -> Optional[str]:
    """Pull the result url from a completed Higgsfield status payload, ONLY if it is a
    non-empty string. A non-string url (e.g. a number) must yield None -> 'completed but
    no url' TERMINAL, never a non-string that crashes Result() post-charge (§5-A).
    image->video returns `video:{url}`; image paths return `images:[{url},...]`."""
    video = data.get("video")
    if isinstance(video, dict) and isinstance(video.get("url"), str) and video["url"]:
        return video["url"]
    images = data.get("images")
    if isinstance(images, list) and images and isinstance(images[0], dict) \
            and isinstance(images[0].get("url"), str) and images[0]["url"]:
        return images[0]["url"]
    return None


def _hf_req_timeout(deadline: float) -> float:
    """Per-request timeout for a single submit/poll call: never exceed the remaining
    overall budget, and cap it so one slow request can't run for the whole job.
    (Floored above 0 so httpx doesn't read it as 'fail immediately'.)"""
    return max(0.001, min(_HF_REQ_TIMEOUT_CAP_S, deadline - time.monotonic()))


def _hf_pin_url(returned: Optional[str], base: str, fallback_path: str) -> str:
    """Trust a server-returned status/cancel URL only if it shares base's origin;
    otherwise construct our own from base. We attach the HF key to these requests, so
    an attacker-influenced submit response must not be able to point them at its host."""
    if isinstance(returned, str) and returned:   # non-string (e.g. a dict) -> fall through, never raise
        try:
            r, b = urllib.parse.urlparse(returned), urllib.parse.urlparse(base)
            if r.scheme in ("http", "https") and r.scheme == b.scheme \
                    and r.hostname == b.hostname and r.port == b.port:
                return returned
        except (ValueError, TypeError):
            pass
    return base.rstrip("/") + fallback_path


async def _hf_best_effort_cancel(client, cancel_url: str, headers: dict) -> None:
    """Fire Higgsfield's cancel endpoint on timeout so a still-queued job stops
    charging. Best-effort: any failure here is swallowed - it must never mask or
    replace the timeout Result. (Cancel only takes effect while queued, not mid-render.)"""
    try:
        await client.post(cancel_url, headers=headers, timeout=_HF_CANCEL_TIMEOUT_S)
    except Exception:
        pass


async def _reject_unsafe_url(url: str) -> Optional[str]:
    """SSRF guard for image_url (untrusted, and sent to a third party). Returns a
    rejection reason, or None if the url is safe. Only http(s); the host must not
    resolve to a private/loopback/link-local/reserved address (blocks file://, the
    AWS metadata IP, internal services). DNS runs on the event loop's executor so a
    slow/hostile resolver can't stall every other concurrent job.

    Defense-in-depth, not airtight: we do NOT fetch the URL (Higgsfield does), and we
    don't pin the resolved IP, so DNS-rebinding / HTTP-redirect to an internal host is
    out of our control - that ultimately relies on Higgsfield's own egress filtering."""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as e:
        return f"unparseable url ({e})"
    if parsed.scheme not in ("http", "https"):
        return f"scheme {parsed.scheme!r} not allowed (http/https only)"
    host = parsed.hostname
    if not host:
        return "no host"
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except socket.gaierror as e:
        return f"host did not resolve ({e})"
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        # an IPv4-mapped IPv6 address (::ffff:127.0.0.1) is_private=False - unwrap it
        # to its v4 form before classifying, or the loopback/private check is bypassed.
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if any(ip in net for net in _PRIVATE_NETS) or ip.is_private \
                or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return f"host resolves to non-public address {addr}"
    return None


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
