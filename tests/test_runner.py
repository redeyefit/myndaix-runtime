"""C1 runner tests - deterministic, POSIX-only (printf/cat/false/sleep), no real
agents needed. Proves: arg channel, stdin channel, nonzero->terminal, timeout->killed.
Runnable with pytest OR standalone: `PYTHONPATH=src python3 tests/test_runner.py`.
"""
import asyncio
import uuid

from runtime import runner
from runtime.contracts import Authority, ErrorClass, Job, Reach, ResultStatus
from runtime.registry import AgentSpec


def _spec(argv, channel="arg"):
    return AgentSpec(
        agent_id="t", reach=Reach.CLI, authority=Authority.RESPONDER,
        model="none", role="test",
        adapter={"kind": "cli", "argv": argv, "prompt_channel": channel},
    )


def _job(prompt="", timeout=5):
    return Job(id=uuid.uuid4(), to_agent="t", prompt=prompt, timeout_s=timeout)


def test_cli_arg_channel_ok():
    r = asyncio.run(runner.invoke_cli(_spec(["printf", "%s"], "arg"), _job("hello-spine")))
    assert r.status is ResultStatus.OK
    assert r.text == "hello-spine"
    assert r.exit_code == 0


def test_cli_stdin_channel_ok():
    r = asyncio.run(runner.invoke_cli(_spec(["cat"], "stdin"), _job("piped-in")))
    assert r.status is ResultStatus.OK
    assert r.text == "piped-in"


def test_cli_nonzero_is_terminal():
    r = asyncio.run(runner.invoke_cli(_spec(["false"], "arg"), _job()))
    assert r.status is ResultStatus.ERROR
    assert r.error_class is ErrorClass.TERMINAL
    assert r.exit_code != 0


def test_cli_timeout_is_killed():
    # sleep ignores stdin; 1s timeout fires well before the 30s sleep finishes.
    r = asyncio.run(runner.invoke_cli(_spec(["sleep", "30"], "stdin"), _job(prompt="x", timeout=1)))
    assert r.status is ResultStatus.TIMEOUT


def test_api_agent_via_mock_transport():
    """invoke_api (OpenAI-compatible) with no live API: a mock transport proves it
    parses the reply, maps 401->terminal / 5xx->retryable, and a missing key fails clean."""
    import os

    import httpx

    spec = AgentSpec(
        agent_id="t-api", reach=Reach.API, authority=Authority.RESPONDER,
        model="test", role="test",
        adapter={"kind": "api", "endpoint": "https://x/chat/completions",
                 "secret_ref": "T_API_KEY", "model": "test-model"})
    job = _job(prompt="hi")

    # missing key -> terminal, no request made
    r = asyncio.run(runner.invoke_api(spec, job))
    assert r.status is ResultStatus.ERROR and "missing API key" in r.text

    os.environ["T_API_KEY"] = "secret"
    try:
        ok = httpx.MockTransport(
            lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "  4  "}}]}))
        r = asyncio.run(runner.invoke_api(spec, job, transport=ok))
        assert r.status is ResultStatus.OK and r.text == "4"   # parsed + stripped

        r = asyncio.run(runner.invoke_api(spec, job, transport=httpx.MockTransport(
            lambda req: httpx.Response(401, text="bad key"))))
        assert r.status is ResultStatus.ERROR and r.error_class is ErrorClass.TERMINAL

        r = asyncio.run(runner.invoke_api(spec, job, transport=httpx.MockTransport(
            lambda req: httpx.Response(500, text="boom"))))
        assert r.error_class is ErrorClass.RETRYABLE
    finally:
        del os.environ["T_API_KEY"]


def _hf_spec(**adapter_over):
    adapter = {"kind": "higgsfield", "base": "https://platform.higgsfield.ai",
               "secret_ref": "HF_KEY", "application": "/higgsfield-ai/dop/lite",
               # 0 keeps the poll loop instant in tests (real default is 2s)
               "poll_interval_s": 0, "poll_retry_backoff_s": 0}
    adapter.update(adapter_over)
    return AgentSpec(agent_id="higgsfield", reach=Reach.API,
                     authority=Authority.RESPONDER, model="dop-lite", role="video",
                     adapter=adapter)


# image_url is a PUBLIC IP literal so the SSRF check passes without a DNS lookup.
_HF_JOB_CTX = {"image_url": "http://1.1.1.1/cat.png"}


def _hf_job(prompt="push-in", timeout=5):
    return Job(id=uuid.uuid4(), to_agent="higgsfield", prompt=prompt,
               timeout_s=timeout, context=dict(_HF_JOB_CTX))


def test_higgsfield_submit_poll_completed():
    """Submit -> queued -> in_progress -> completed maps to OK with the mp4 as
    artifact_ref; missing key fails clean before any request."""
    import os

    import httpx

    spec = _hf_spec()
    job = _hf_job()

    # missing key -> terminal, no request made
    r = asyncio.run(runner.invoke_higgsfield(spec, job))
    assert r.status is ResultStatus.ERROR and "missing API key" in r.text

    os.environ["HF_KEY"] = "id:secret"
    try:
        polls = {"n": 0}

        def handler(req):
            if req.method == "POST":
                assert req.headers["Authorization"] == "Key id:secret"
                return httpx.Response(200, json={"request_id": "req-1",
                                      "status_url": "https://platform.higgsfield.ai/requests/req-1/status"})
            polls["n"] += 1
            if polls["n"] == 1:
                return httpx.Response(200, json={"status": "queued"})
            if polls["n"] == 2:
                return httpx.Response(200, json={"status": "in_progress"})
            return httpx.Response(200, json={"status": "completed",
                                  "video": {"url": "https://cloud-cdn.higgsfield.ai/out.mp4"},
                                  "cost": 0.13})

        r = asyncio.run(runner.invoke_higgsfield(spec, job, transport=httpx.MockTransport(handler)))
        assert r.status is ResultStatus.OK, r.text
        assert r.artifact_ref == "https://cloud-cdn.higgsfield.ai/out.mp4"
        assert r.text == r.artifact_ref and r.cost == 0.13
    finally:
        del os.environ["HF_KEY"]


def test_higgsfield_submit_errors_and_terminal_states():
    """401-on-submit -> terminal (no charge); 5xx-on-submit -> retryable (pre-charge,
    safe); terminal status 'failed' -> terminal (Higgsfield refunds)."""
    import os

    import httpx

    spec, job = _hf_spec(), _hf_job()
    os.environ["HF_KEY"] = "id:secret"
    try:
        r = asyncio.run(runner.invoke_higgsfield(spec, job, transport=httpx.MockTransport(
            lambda req: httpx.Response(401, text="bad key"))))
        assert r.status is ResultStatus.ERROR and r.error_class is ErrorClass.TERMINAL

        r = asyncio.run(runner.invoke_higgsfield(spec, job, transport=httpx.MockTransport(
            lambda req: httpx.Response(503, text="upstream"))))
        assert r.error_class is ErrorClass.RETRYABLE  # nothing charged yet -> safe to retry

        def failed(req):
            if req.method == "POST":
                return httpx.Response(200, json={"request_id": "req-2"})
            return httpx.Response(200, json={"status": "failed"})

        r = asyncio.run(runner.invoke_higgsfield(spec, job, transport=httpx.MockTransport(failed)))
        assert r.status is ResultStatus.ERROR and r.error_class is ErrorClass.TERMINAL
        assert "failed" in r.text
    finally:
        del os.environ["HF_KEY"]


def test_higgsfield_post_submit_failure_is_terminal():
    """DESIGN S5-A fail-closed: once we hold a request_id (charged), a poll that keeps
    erroring is TERMINAL - never RETRYABLE - so the worker can't re-submit & double-charge."""
    import os

    import httpx

    spec, job = _hf_spec(), _hf_job()
    os.environ["HF_KEY"] = "id:secret"
    try:
        def submit_then_die(req):
            if req.method == "POST":
                return httpx.Response(200, json={"request_id": "req-3"})
            raise httpx.ConnectError("network gone")

        r = asyncio.run(runner.invoke_higgsfield(spec, job,
                        transport=httpx.MockTransport(submit_then_die)))
        assert r.status is ResultStatus.ERROR
        assert r.error_class is ErrorClass.TERMINAL  # NOT retryable - already charged
    finally:
        del os.environ["HF_KEY"]


def test_higgsfield_poll_timeout_is_terminal():
    """Stuck in 'queued' past job.timeout_s -> TIMEOUT/TERMINAL (charged, no retry)."""
    import os

    import httpx

    spec, job = _hf_spec(), _hf_job(timeout=1)
    os.environ["HF_KEY"] = "id:secret"
    try:
        def never_done(req):
            if req.method == "POST":
                return httpx.Response(200, json={"request_id": "req-4"})
            return httpx.Response(200, json={"status": "queued"})

        r = asyncio.run(runner.invoke_higgsfield(spec, job,
                        transport=httpx.MockTransport(never_done)))
        assert r.status is ResultStatus.TIMEOUT and r.error_class is ErrorClass.TERMINAL
    finally:
        del os.environ["HF_KEY"]


def test_higgsfield_rejects_unsafe_image_url():
    """SSRF guard: file:// and private/loopback targets are terminal BEFORE any request."""
    import os

    spec = _hf_spec()
    os.environ["HF_KEY"] = "id:secret"
    try:
        for bad in ("file:///etc/passwd", "http://127.0.0.1/x", "http://169.254.169.254/latest/meta-data"):
            job = Job(id=uuid.uuid4(), to_agent="higgsfield", prompt="x",
                      context={"image_url": bad})
            r = asyncio.run(runner.invoke_higgsfield(spec, job))
            assert r.status is ResultStatus.ERROR and r.error_class is ErrorClass.TERMINAL
            assert "rejected" in r.text, bad
        # missing image_url -> clean terminal, not a crash
        job = Job(id=uuid.uuid4(), to_agent="higgsfield", prompt="x", context={})
        r = asyncio.run(runner.invoke_higgsfield(spec, job))
        assert r.status is ResultStatus.ERROR and "image_url" in r.text
    finally:
        del os.environ["HF_KEY"]


if __name__ == "__main__":
    passed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print("PASS", _name)
            passed += 1
    print(f"ALL PASS ({passed})")
