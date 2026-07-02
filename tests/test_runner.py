"""C1 runner tests - deterministic, POSIX-only (printf/cat/false/sleep), no real
agents needed. Proves: arg channel, stdin channel, nonzero->terminal, timeout->killed.
Runnable with pytest OR standalone: `PYTHONPATH=src python3 tests/test_runner.py`.
"""
import asyncio
import os
import shutil
import tempfile
import uuid

from runtime import runner
from runtime.contracts import Authority, ErrorClass, Job, Profile, Reach, ResultStatus
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


# -- gate fix: a job WITHOUT a worktree must run in a fresh empty scratch cwd, NOT the serve
#    process's inherited cwd (the repo at base) — else a reviewer reads the wrong tree and calls
#    real diff-findings "phantom". The scratch cwd is removed after the run.

def test_cli_no_worktree_runs_in_fresh_scratch_cwd():
    serve_cwd = os.path.realpath(os.getcwd())
    r = asyncio.run(runner.invoke_cli(_spec(["/bin/pwd"], "stdin"), _job()))
    assert r.status is ResultStatus.OK, r.text
    ran_in = os.path.realpath(r.text.strip())
    assert ran_in != serve_cwd                                # NOT the inherited repo cwd
    # not even UNDER serve_cwd — guards the TMPDIR-under-repo case where git parent-discovery
    # from the scratch dir could still reach the base tree (kilabz: defense-in-depth).
    assert os.path.commonpath([ran_in, serve_cwd]) != serve_cwd
    assert os.path.basename(ran_in).startswith("mdx-cli-cwd.")
    assert not os.path.exists(ran_in)                         # scratch cwd cleaned up in finally


def test_cli_empty_worktree_path_still_gets_scratch_cwd():
    # an empty-string worktree_path must NOT slip through to cwd=None (which re-inherits the
    # serve cwd and defeats the fix) — the `or None` guard turns it into a fresh scratch cwd.
    serve_cwd = os.path.realpath(os.getcwd())
    job = _job()
    job.worktree_path = ""
    r = asyncio.run(runner.invoke_cli(_spec(["/bin/pwd"], "stdin"), job))
    assert r.status is ResultStatus.OK, r.text
    ran_in = os.path.realpath(r.text.strip())
    assert ran_in != serve_cwd
    assert os.path.basename(ran_in).startswith("mdx-cli-cwd.")


def test_cli_with_worktree_runs_there_and_is_preserved():
    wt = tempfile.mkdtemp(prefix="mdx-test-wt.")
    try:
        job = _job()
        job.worktree_path = wt
        r = asyncio.run(runner.invoke_cli(_spec(["/bin/pwd"], "stdin"), job))
        assert r.status is ResultStatus.OK, r.text
        assert os.path.realpath(r.text.strip()) == os.path.realpath(wt)
        assert os.path.isdir(wt)                              # invoke_cli must NOT remove a caller-owned worktree
    finally:
        shutil.rmtree(wt, ignore_errors=True)


# -- P2 env scrub: a CLI subprocess must NOT inherit sibling agents' secrets ----

def _printenv_run(spec):
    """Run `printenv` as the CLI agent and return its (scrubbed) environment dump."""
    r = asyncio.run(runner.invoke_cli(spec, _job(prompt="ignored")))
    assert r.status is ResultStatus.OK, r.text
    return r.text


def test_cli_env_scrubbed_of_inherited_secrets():
    """A CLI agent that declares no secret_ref sees the operational baseline but NONE
    of the pool's secrets (HF_KEY, PERPLEXITY_API_KEY) — they must not leak into codex."""
    import os
    os.environ["HF_KEY"] = "hf-secret"
    os.environ["PERPLEXITY_API_KEY"] = "perp-secret"
    try:
        env = _printenv_run(_spec(["printenv"], "stdin"))
        assert "HF_KEY" not in env and "hf-secret" not in env
        assert "PERPLEXITY_API_KEY" not in env and "perp-secret" not in env
        assert "PATH=" in env          # operational baseline still present so the agent runs
    finally:
        del os.environ["HF_KEY"]
        del os.environ["PERPLEXITY_API_KEY"]


def test_cli_env_declared_secret_ref_passes_through():
    """An agent's OWN declared secret_ref is allowed through; a sibling's is still dropped."""
    import os
    spec = AgentSpec(agent_id="t", reach=Reach.CLI, authority=Authority.RESPONDER,
                     model="none", role="test",
                     adapter={"kind": "cli", "argv": ["printenv"], "prompt_channel": "stdin",
                              "secret_ref": "T_DECLARED_KEY"})
    os.environ["T_DECLARED_KEY"] = "mine"
    os.environ["HF_KEY"] = "not-mine"
    try:
        env = _printenv_run(spec)
        assert "T_DECLARED_KEY=mine" in env       # declared -> allowed
        assert "HF_KEY" not in env                 # sibling secret -> still dropped
    finally:
        del os.environ["T_DECLARED_KEY"]
        del os.environ["HF_KEY"]


def test_cli_env_keeps_operational_vars():
    """Non-secret operational vars (proxy, CA bundle, relocated config dir) survive the
    scrub so a proxied / non-default-$HOME deploy doesn't brick the agents — while a
    secret set alongside them is still dropped."""
    import os
    ops = {"HTTPS_PROXY": "http://proxy:8080", "NODE_EXTRA_CA_CERTS": "/etc/ca.pem",
           "XDG_CONFIG_HOME": "/cfg", "CODEX_HOME": "/cfg/codex"}
    for k, v in ops.items():
        os.environ[k] = v
    os.environ["HF_KEY"] = "secret"
    try:
        env = _printenv_run(_spec(["printenv"], "stdin"))
        for k, v in ops.items():
            assert f"{k}={v}" in env, k
        assert "HF_KEY" not in env
    finally:
        for k in (*ops, "HF_KEY"):
            del os.environ[k]


def test_cli_env_per_agent_passthrough_list():
    """An agent may declare adapter['env_passthrough'] for non-secret vars it needs."""
    import os
    spec = AgentSpec(agent_id="t", reach=Reach.CLI, authority=Authority.RESPONDER,
                     model="none", role="test",
                     adapter={"kind": "cli", "argv": ["printenv"], "prompt_channel": "stdin",
                              "env_passthrough": ["AGENT_NEEDS_THIS"]})
    os.environ["AGENT_NEEDS_THIS"] = "ok"
    try:
        assert "AGENT_NEEDS_THIS=ok" in _printenv_run(spec)
    finally:
        del os.environ["AGENT_NEEDS_THIS"]


def test_scratch_home_seeds_auth_and_isolates(tmpdir=None):
    """A scratch_home agent (codex fix actor) runs under a throwaway HOME seeded with ONLY its
    auth dir — so it can't read the operator's ~/.ssh etc. (PR-4 fix-stage containment)."""
    import os, shutil, tempfile
    real = tempfile.mkdtemp()
    os.makedirs(os.path.join(real, ".codex"))
    with open(os.path.join(real, ".codex", "auth.json"), "w") as fh:
        fh.write("{}")
    spec = AgentSpec(agent_id="codex", reach=Reach.CLI, authority=Authority.WORKSPACE_ACTOR,
                     model="none", role="test",
                     adapter={"kind": "cli", "argv": ["codex"], "prompt_channel": "stdin",
                              "scratch_home": True})
    old = os.environ.get("CODEX_HOME")
    os.environ["CODEX_HOME"] = os.path.join(real, ".codex")
    scratch = None
    try:
        env, scratch = runner._make_scratch_home(spec, {"HOME": "/real/home", "CODEX_HOME": "/x"})
        assert scratch and env["HOME"] == scratch and env["HOME"] != "/real/home"
        assert os.path.isfile(os.path.join(scratch, ".codex", "auth.json"))   # auth seeded
        assert "CODEX_HOME" not in env                                        # host override dropped
    finally:
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)
        shutil.rmtree(real, ignore_errors=True)
        if old is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = old


def test_scratch_home_noop_without_flag():
    """No scratch_home flag -> env passes through unchanged, no temp dir created."""
    spec = AgentSpec(agent_id="t", reach=Reach.CLI, authority=Authority.RESPONDER,
                     model="none", role="test",
                     adapter={"kind": "cli", "argv": ["printenv"], "prompt_channel": "stdin"})
    env, scratch = runner._make_scratch_home(spec, {"HOME": "/real/home"})
    assert scratch is None and env == {"HOME": "/real/home"}


def test_cli_env_operator_passthrough_escape_hatch():
    """$MYNDAIX_CLI_ENV_PASSTHROUGH lets the operator open a hole at deploy time (e.g. an
    env-based CLI auth key) without a source edit — comma-separated, whitespace-tolerant."""
    import os
    os.environ["MYNDAIX_CLI_ENV_PASSTHROUGH"] = " EXTRA_ONE , EXTRA_TWO "
    os.environ["EXTRA_ONE"] = "1"
    os.environ["EXTRA_TWO"] = "2"
    os.environ["HF_KEY"] = "still-secret"
    try:
        env = _printenv_run(_spec(["printenv"], "stdin"))
        assert "EXTRA_ONE=1" in env and "EXTRA_TWO=2" in env
        assert "HF_KEY" not in env                 # the hatch is scoped, not a free-for-all
    finally:
        for k in ("MYNDAIX_CLI_ENV_PASSTHROUGH", "EXTRA_ONE", "EXTRA_TWO", "HF_KEY"):
            del os.environ[k]


def test_roster_cli_agents_get_only_their_own_key():
    """Each live CLI agent inherits its OWN auth key and NONE of the others' — the
    cross-agent containment + deploy-safety property of the P2 scrub. With every secret
    set in the pool's env, codex must see OPENAI_API_KEY but never ANTHROPIC/GEMINI/HF/etc."""
    import os
    from runtime.registry import REGISTRY
    keys = {"ANTHROPIC_API_KEY": "ak", "OPENAI_API_KEY": "ok", "GEMINI_API_KEY": "gk",
            "GOOGLE_API_KEY": "gok", "HF_KEY": "hf", "PERPLEXITY_API_KEY": "pk"}
    for k, v in keys.items():
        os.environ[k] = v
    try:
        expect = {"lobster": {"ANTHROPIC_API_KEY"}, "mack": {"ANTHROPIC_API_KEY"},
                  "mini": {"ANTHROPIC_API_KEY"}, "kilabz": {"OPENAI_API_KEY"},
                  "codex": {"OPENAI_API_KEY"}, "oracle": {"GEMINI_API_KEY", "GOOGLE_API_KEY"}}
        for aid, own in expect.items():
            env = runner._cli_env(REGISTRY[aid])
            leaked = set(keys) & set(env)         # which secret keys reached this agent
            assert leaked == own, (aid, leaked, own)
            assert "PATH" in env                   # operational baseline kept -> agent still runs
    finally:
        for k in keys:
            del os.environ[k]


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
    # invoke_higgsfield now derives the deadline from spec.profile.timeout_s (NOT job.timeout_s,
    # which the spine leaves at its dead 300s default). Use a short profile timeout so the
    # timeout/cancel tests trigger fast; completion tests finish instantly via MockTransport.
    return AgentSpec(agent_id="higgsfield", reach=Reach.API,
                     authority=Authority.RESPONDER, model="dop-lite", role="video",
                     profile=Profile(timeout_s=5), adapter=adapter)


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
    """401-on-submit -> terminal; 5xx-on-submit -> TERMINAL too (charge-ambiguous on a
    non-idempotent POST: a gateway 5xx can land AFTER the queue charged, so fail closed,
    never re-submit); terminal status 'failed' -> terminal (Higgsfield refunds)."""
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
        # 5xx submit is NOT safe to retry — it may have been received & charged.
        assert r.error_class is ErrorClass.TERMINAL

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


def _hf_run(spec, job, handler):
    import httpx
    return asyncio.run(runner.invoke_higgsfield(spec, job, transport=httpx.MockTransport(handler)))


def test_higgsfield_poll_retry_then_recovers():
    """§7b P0: a transient 5xx during polling retries (up to poll_retry_max) and then
    RECOVERS to a completed result - not just 'eventually gives up'."""
    import os

    import httpx
    spec, job = _hf_spec(), _hf_job()
    os.environ["HF_KEY"] = "id:secret"
    try:
        polls = {"n": 0}

        def handler(req):
            if req.method == "POST":
                return httpx.Response(200, json={"request_id": "req-r"})
            polls["n"] += 1
            if polls["n"] <= 2:                       # two transient blips...
                return httpx.Response(503, text="upstream")
            return httpx.Response(200, json={"status": "completed",         # ...then success
                                  "video": {"url": "https://cloud-cdn.higgsfield.ai/r.mp4"}})

        r = _hf_run(spec, job, handler)
        assert r.status is ResultStatus.OK, r.text
        assert r.artifact_ref == "https://cloud-cdn.higgsfield.ai/r.mp4"
        assert polls["n"] == 3                        # proves it retried, didn't give up at #1
    finally:
        del os.environ["HF_KEY"]


def test_higgsfield_malformed_cost_preserves_artifact():
    """A non-numeric upstream `cost` must NOT discard a paid result: keep artifact_ref,
    just drop the cost. (Passing it raw to Result would raise pydantic ValidationError.)"""
    import os

    import httpx
    spec = _hf_spec()
    os.environ["HF_KEY"] = "id:secret"
    try:
        for bad_cost in ("$0.13", {"amount": 0.13, "currency": "usd"}, "free", True):
            def handler(req, _c=bad_cost):
                if req.method == "POST":
                    return httpx.Response(200, json={"request_id": "req-c"})
                return httpx.Response(200, json={"status": "completed", "cost": _c,
                                      "video": {"url": "https://cloud-cdn.higgsfield.ai/c.mp4"}})
            r = _hf_run(spec, _hf_job(), handler)
            assert r.status is ResultStatus.OK, (bad_cost, r.text)
            assert r.artifact_ref == "https://cloud-cdn.higgsfield.ai/c.mp4"
            assert r.cost is None, bad_cost          # malformed cost dropped, not fatal
    finally:
        del os.environ["HF_KEY"]


def test_higgsfield_nonstring_status_does_not_crash():
    """A non-string poll `status` (e.g. an int code) must fail closed as TERMINAL,
    never raise out of invoke_higgsfield (which would escape the result-recording path)."""
    import os

    import httpx
    spec = _hf_spec()
    os.environ["HF_KEY"] = "id:secret"
    try:
        def handler(req):
            if req.method == "POST":
                return httpx.Response(200, json={"request_id": "req-i"})
            return httpx.Response(200, json={"status": 200})

        r = _hf_run(spec, _hf_job(), handler)
        assert r.status is ResultStatus.ERROR and r.error_class is ErrorClass.TERMINAL
    finally:
        del os.environ["HF_KEY"]


def test_higgsfield_accepts_202_submit():
    """An async queue may answer submit with 202 Accepted (not 200) carrying a
    request_id - that must be honored, not dropped as a submit error."""
    import os

    import httpx
    spec = _hf_spec()
    os.environ["HF_KEY"] = "id:secret"
    try:
        def handler(req):
            if req.method == "POST":
                return httpx.Response(202, json={"request_id": "req-202"})
            return httpx.Response(200, json={"status": "completed",
                                  "images": [{"url": "https://cloud-cdn.higgsfield.ai/a.png"}]})

        r = _hf_run(spec, _hf_job(), handler)
        assert r.status is ResultStatus.OK and r.artifact_ref.endswith("a.png")
    finally:
        del os.environ["HF_KEY"]


def test_higgsfield_unknown_and_nsfw_and_no_url_are_terminal():
    """Unknown status (design §7b), nsfw, and completed-without-url all -> TERMINAL."""
    import os

    import httpx
    spec = _hf_spec()
    os.environ["HF_KEY"] = "id:secret"
    try:
        cases = {
            "weird-state": {"status": "weird-state"},
            "nsfw": {"status": "nsfw"},
            "nourl": {"status": "completed"},                 # no video/images
        }
        for name, payload in cases.items():
            def handler(req, _p=payload):
                if req.method == "POST":
                    return httpx.Response(200, json={"request_id": "req-u"})
                return httpx.Response(200, json=_p)
            r = _hf_run(spec, _hf_job(), handler)
            assert r.status is ResultStatus.ERROR and r.error_class is ErrorClass.TERMINAL, name
        # the unknown-status case should surface the actual status for a human
        def unk(req):
            if req.method == "POST":
                return httpx.Response(200, json={"request_id": "req-u"})
            return httpx.Response(200, json={"status": "weird-state"})
        assert "weird-state" in _hf_run(spec, _hf_job(), unk).text
    finally:
        del os.environ["HF_KEY"]


def test_higgsfield_submit_ambiguous_terminal_connect_retryable():
    """Charge-correctness: a pre-send ConnectError is RETRYABLE (nothing charged), but
    an ambiguous ReadTimeout on the non-idempotent submit is TERMINAL (may have charged)."""
    import os

    import httpx
    spec = _hf_spec()
    os.environ["HF_KEY"] = "id:secret"
    try:
        def conn(req):
            raise httpx.ConnectError("refused")
        r = _hf_run(spec, _hf_job(), conn)
        assert r.status is ResultStatus.ERROR and r.error_class is ErrorClass.RETRYABLE

        def readto(req):
            raise httpx.ReadTimeout("slow")
        r = _hf_run(spec, _hf_job(), readto)
        assert r.status is ResultStatus.ERROR and r.error_class is ErrorClass.TERMINAL
    finally:
        del os.environ["HF_KEY"]


def test_higgsfield_cancel_on_timeout():
    """On poll timeout the runner best-effort POSTs Higgsfield's cancel_url so a still-
    queued job stops charging (design §7b P1)."""
    import os

    import httpx
    spec, job = _hf_spec(), _hf_job(timeout=1)
    os.environ["HF_KEY"] = "id:secret"
    try:
        hits = {"cancel": 0}

        def handler(req):
            if req.method == "POST" and req.url.path.endswith("/cancel"):
                hits["cancel"] += 1
                return httpx.Response(200, json={"ok": True})
            if req.method == "POST":
                return httpx.Response(200, json={"request_id": "req-x",
                                      "cancel_url": "https://platform.higgsfield.ai/requests/req-x/cancel"})
            return httpx.Response(200, json={"status": "queued"})

        r = _hf_run(spec, job, handler)
        assert r.status is ResultStatus.TIMEOUT and r.error_class is ErrorClass.TERMINAL
        assert hits["cancel"] == 1                 # cancel was attempted
    finally:
        del os.environ["HF_KEY"]


def test_higgsfield_ipv4_mapped_ssrf_rejected():
    """IPv4-mapped IPv6 (::ffff:127.0.0.1) must not bypass the loopback guard."""
    import os

    import uuid as _uuid

    from runtime.contracts import Job
    spec = _hf_spec()
    os.environ["HF_KEY"] = "id:secret"
    try:
        job = Job(id=_uuid.uuid4(), to_agent="higgsfield", prompt="x",
                  context={"image_url": "http://[::ffff:127.0.0.1]/x.png"})
        r = asyncio.run(runner.invoke_higgsfield(spec, job))
        assert r.status is ResultStatus.ERROR and r.error_class is ErrorClass.TERMINAL
        assert "rejected" in r.text
    finally:
        del os.environ["HF_KEY"]


def test_higgsfield_malformed_payload_never_raises_post_charge():
    """§5-A hardening (KilaBz re-review): after a request_id exists, malformed server
    payloads must map to a TERMINAL Result, never raise out of invoke_higgsfield -
    covers non-string status_url/cancel_url, non-string artifact url, bad poll_retry_max."""
    import os

    import httpx
    os.environ["HF_KEY"] = "id:secret"
    try:
        # (1) non-string status_url/cancel_url in the submit response: _hf_pin_url must
        # fall back to a constructed URL, not raise; the run still completes.
        def bad_urls(req):
            if req.method == "POST" and req.url.path.endswith("/cancel"):
                return httpx.Response(200, json={})
            if req.method == "POST":
                return httpx.Response(200, json={"request_id": "req-bu",
                                      "status_url": {"bad": "shape"}, "cancel_url": 123})
            return httpx.Response(200, json={"status": "completed",
                                  "video": {"url": "https://cloud-cdn.higgsfield.ai/ok.mp4"}})
        r = _hf_run(_hf_spec(), _hf_job(), bad_urls)
        assert r.status is ResultStatus.OK and r.artifact_ref.endswith("ok.mp4")

        # (2) non-string artifact url on a completed result -> TERMINAL, not a raise.
        def bad_artifact(req):
            if req.method == "POST":
                return httpx.Response(200, json={"request_id": "req-ba"})
            return httpx.Response(200, json={"status": "completed", "video": {"url": 123}})
        r = _hf_run(_hf_spec(), _hf_job(), bad_artifact)
        assert r.status is ResultStatus.ERROR and r.error_class is ErrorClass.TERMINAL

        # (3) non-numeric poll_retry_max must not raise at `fails > retry_max`; it falls
        # back to the default and still fails closed after exhausting retries.
        spec = _hf_spec(poll_retry_max="not-a-number")
        def always_503(req):
            if req.method == "POST":
                return httpx.Response(200, json={"request_id": "req-rm"})
            return httpx.Response(503, text="upstream")
        r = _hf_run(spec, _hf_job(), always_503)
        assert r.status is ResultStatus.ERROR and r.error_class is ErrorClass.TERMINAL
    finally:
        del os.environ["HF_KEY"]


def test_higgsfield_poll_loop_backstop_never_raises():
    """§5-A airtight (final KilaBz trace): post-charge, NOTHING escapes invoke_higgsfield.
    Covers nan sleep knobs and a generic non-HTTPError raised mid-poll."""
    import os

    import httpx
    os.environ["HF_KEY"] = "id:secret"
    try:
        # nan poll_interval_s must not blow up asyncio.sleep; run still completes.
        spec = _hf_spec(poll_interval_s="nan")
        def slow_then_done(req):
            if req.method == "POST":
                return httpx.Response(200, json={"request_id": "req-n"})
            return httpx.Response(200, json={"status": "completed",
                                  "video": {"url": "https://cloud-cdn.higgsfield.ai/n.mp4"}})
        r = _hf_run(spec, _hf_job(), slow_then_done)
        assert r.status is ResultStatus.OK, r.text

        # nan backoff with a transient blip first -> retry sleeps 0, recovers.
        spec = _hf_spec(poll_retry_backoff_s="nan")
        seq = {"n": 0}
        def blip(req):
            if req.method == "POST":
                return httpx.Response(200, json={"request_id": "req-nb"})
            seq["n"] += 1
            if seq["n"] == 1:
                return httpx.Response(503, text="x")
            return httpx.Response(200, json={"status": "completed",
                                  "video": {"url": "https://cloud-cdn.higgsfield.ai/nb.mp4"}})
        r = _hf_run(spec, _hf_job(), blip)
        assert r.status is ResultStatus.OK, r.text

        # a generic (non-HTTPError) exception mid-poll is trapped by the backstop -> TERMINAL.
        def boom(req):
            if req.method == "POST":
                return httpx.Response(200, json={"request_id": "req-b"})
            raise ValueError("surprise non-http error")
        r = _hf_run(_hf_spec(), _hf_job(), boom)
        assert r.status is ResultStatus.ERROR and r.error_class is ErrorClass.TERMINAL
        assert "post-charge" in r.text
    finally:
        del os.environ["HF_KEY"]


def test_higgsfield_missing_secret_ref_fails_closed():
    """An adapter with no secret_ref must fail closed (TERMINAL), never send 'Key None'."""
    spec = AgentSpec(agent_id="higgsfield", reach=Reach.API, authority=Authority.RESPONDER,
                     model="dop-lite", role="video",
                     adapter={"kind": "higgsfield", "base": "https://platform.higgsfield.ai",
                              "application": "/higgsfield-ai/dop/lite"})   # no secret_ref
    r = asyncio.run(runner.invoke_higgsfield(spec, _hf_job()))
    assert r.status is ResultStatus.ERROR and r.error_class is ErrorClass.TERMINAL
    assert "missing API key" in r.text


# -- Speak (talking-head) payload family: adapter.payload='speak' switches the request
# body to the nested /v1/speak shape; the submit/poll/charge contract is unchanged. --

def _speak_spec(**adapter_over):
    adapter = {"kind": "higgsfield", "base": "https://platform.higgsfield.ai",
               "secret_ref": "HF_KEY", "application": "/v1/speak/higgsfield",
               "payload": "speak",
               "poll_interval_s": 0, "poll_retry_backoff_s": 0}
    adapter.update(adapter_over)
    return AgentSpec(agent_id="speak", reach=Reach.API,
                     authority=Authority.RESPONDER, model="speak-v2", role="talking-head",
                     profile=Profile(timeout_s=5), adapter=adapter)


def _speak_job(prompt="warm, direct to camera", **ctx_over):
    ctx = {"image_url": "http://1.1.1.1/face.png",
           "audio_url": "http://1.1.1.1/speech.wav"}
    ctx.update(ctx_over)
    # None means "omit this key" so tests can drop required fields
    ctx = {k: v for k, v in ctx.items() if v is not None}
    return Job(id=uuid.uuid4(), to_agent="speak", prompt=prompt, timeout_s=5, context=ctx)


def test_speak_submit_poll_completed_nested_body():
    """The speak path must send the NESTED SDK body (input_image/input_audio/prompt +
    optional quality/duration) — not the flat DoP shape — and complete like any hf job."""
    import json as _json
    import os

    import httpx
    os.environ["HF_KEY"] = "id:secret"
    try:
        seen = {"posts": 0}

        def handler(req):
            if req.method == "POST":
                seen["posts"] += 1
                assert req.url.path == "/v1/speak/higgsfield"
                assert req.headers["Authorization"] == "Key id:secret"
                seen["body"] = _json.loads(req.content)
                return httpx.Response(200, json={"request_id": "spk-1"})
            return httpx.Response(200, json={"status": "completed",
                                  "video": {"url": "https://cloud-cdn.higgsfield.ai/talk.mp4"},
                                  "cost": 0.8})

        job = _speak_job(quality="high", duration=10)
        r = asyncio.run(runner.invoke_higgsfield(_speak_spec(), job,
                        transport=httpx.MockTransport(handler)))
        assert r.status is ResultStatus.OK, r.text
        assert seen["posts"] == 1   # exactly ONE charged submit, ever
        assert r.artifact_ref == "https://cloud-cdn.higgsfield.ai/talk.mp4" and r.cost == 0.8
        # `params` envelope confirmed by live 422 (2026-07-01): fields must be wrapped
        assert seen["body"] == {"params": {
            "input_image": {"type": "image_url", "image_url": "http://1.1.1.1/face.png"},
            "input_audio": {"type": "audio_url", "audio_url": "http://1.1.1.1/speech.wav"},
            "prompt": "warm, direct to camera",
            "quality": "high", "duration": 10,
        }}
    finally:
        del os.environ["HF_KEY"]


def test_speak_omitted_knobs_are_omitted_from_body():
    """quality/duration left unset must NOT appear in the body (API defaults apply);
    a string duration from a JSON context ('10') is accepted and sent as int."""
    import json as _json
    import os

    import httpx
    os.environ["HF_KEY"] = "id:secret"
    try:
        seen = {}

        def handler(req):
            if req.method == "POST":
                seen["body"] = _json.loads(req.content)
                return httpx.Response(200, json={"request_id": "spk-2"})
            return httpx.Response(200, json={"status": "completed",
                                  "video": {"url": "https://x.example/t.mp4"}})

        r = asyncio.run(runner.invoke_higgsfield(_speak_spec(), _speak_job(),
                        transport=httpx.MockTransport(handler)))
        assert r.status is ResultStatus.OK, r.text
        assert "quality" not in seen["body"]["params"] and "duration" not in seen["body"]["params"]

        r = asyncio.run(runner.invoke_higgsfield(_speak_spec(), _speak_job(duration="10"),
                        transport=httpx.MockTransport(handler)))
        assert r.status is ResultStatus.OK, r.text
        assert seen["body"]["params"]["duration"] == 10
    finally:
        del os.environ["HF_KEY"]


def test_speak_missing_audio_url_terminal_no_request():
    """audio_url is required for the speak payload; missing -> TERMINAL before any
    HTTP call (nothing can be charged)."""
    import os

    import httpx
    os.environ["HF_KEY"] = "id:secret"
    try:
        def handler(req):   # any request at all is a test failure
            raise AssertionError("no HTTP request may be made")
        r = asyncio.run(runner.invoke_higgsfield(_speak_spec(), _speak_job(audio_url=None),
                        transport=httpx.MockTransport(handler)))
        assert r.status is ResultStatus.ERROR and r.error_class is ErrorClass.TERMINAL
        assert "audio_url" in r.text
    finally:
        del os.environ["HF_KEY"]


def test_speak_invalid_knobs_terminal_no_request():
    """Invalid quality/duration are hard-rejected PRE-submit — silently defaulting a
    paid knob would bill a render the caller didn't ask for. bool must not pass the
    duration check (bool is an int subclass)."""
    import os

    import httpx
    os.environ["HF_KEY"] = "id:secret"
    try:
        def handler(req):
            raise AssertionError("no HTTP request may be made")
        for ctx in ({"quality": "ultra"}, {"quality": ["mid"]}, {"duration": 20},
                    {"duration": True}, {"duration": 7.5}):
            r = asyncio.run(runner.invoke_higgsfield(_speak_spec(), _speak_job(**ctx),
                            transport=httpx.MockTransport(handler)))
            assert r.status is ResultStatus.ERROR and r.error_class is ErrorClass.TERMINAL, ctx
            assert "invalid" in r.text, ctx
    finally:
        del os.environ["HF_KEY"]


def test_speak_rejects_unsafe_audio_url():
    """audio_url gets the same SSRF guard as image_url — it is an untrusted URL handed
    to a third party with our key attached. Parity cases mirror the image_url coverage:
    loopback, RFC-1918, link-local/metadata IP, IPv4-mapped IPv6, non-http scheme.
    (IP literals only — no DNS in tests.)"""
    import os
    os.environ["HF_KEY"] = "id:secret"
    try:
        for bad in ("http://127.0.0.1/steal.wav",          # loopback
                    "http://10.0.0.8/steal.wav",           # private (RFC-1918)
                    "http://169.254.169.254/latest/x.wav",  # link-local / AWS IMDS
                    "http://[::ffff:127.0.0.1]/steal.wav",  # IPv4-mapped IPv6 unwrap
                    "file:///etc/passwd"):                  # non-http scheme
            r = asyncio.run(runner.invoke_higgsfield(
                _speak_spec(), _speak_job(audio_url=bad)))
            assert r.status is ResultStatus.ERROR and r.error_class is ErrorClass.TERMINAL, bad
            assert "audio_url rejected" in r.text, bad
    finally:
        del os.environ["HF_KEY"]


def test_speak_dop_flat_body_regression():
    """A higgsfield spec WITHOUT payload='speak' must still build the flat DoP body —
    the seam may not change the default wire format."""
    import json as _json
    import os

    import httpx
    os.environ["HF_KEY"] = "id:secret"
    try:
        seen = {}

        def handler(req):
            if req.method == "POST":
                seen["body"] = _json.loads(req.content)
                return httpx.Response(200, json={"request_id": "req-flat"})
            return httpx.Response(200, json={"status": "completed",
                                  "video": {"url": "https://x.example/v.mp4"}})

        r = asyncio.run(runner.invoke_higgsfield(_hf_spec(), _hf_job(),
                        transport=httpx.MockTransport(handler)))
        assert r.status is ResultStatus.OK, r.text
        assert seen["body"] == {"prompt": "push-in", "image_url": "http://1.1.1.1/cat.png"}
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
