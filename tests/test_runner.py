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


if __name__ == "__main__":
    passed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print("PASS", _name)
            passed += 1
    print(f"ALL PASS ({passed})")
