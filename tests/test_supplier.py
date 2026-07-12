"""No-spend supplier-GATEWAY CI — exercises the REAL invoke_supplier against an
httpx.MockTransport (the transport= kwarg): request-build, poll-parse, charge-safety,
fail-closed pricing, SSRF — no network, no dollars spent (mirrors
tests/test_orchestrator_supplier.py's mock-transport pattern).

SSRF (_reject_unsafe_url) is stubbed to a no-op ONLY where the test needs offline
determinism; the reject path is exercised with an explicit stub that rejects.

Run: PYTHONPATH=src python3 tests/test_supplier.py
"""
import asyncio
import os
import uuid

import httpx

from runtime import runner, supplier
from runtime.contracts import ErrorClass, Job, ResultStatus
from runtime.registry import get as get_spec

PASS = [0]
FAIL = [0]


def ok(cond, label):
    if cond:
        PASS[0] += 1
    else:
        FAIL[0] += 1
        print("  FAIL:", label)


VIDEO = "https://v3.fal.media/files/x/plate.mp4"
IMAGE = "https://v3.fal.media/files/x/still.png"
SEED = "https://res.cloudinary.com/x/seed.png"


# ---- transports -------------------------------------------------------------------------
def _fal_transport(*, submit_code=200, statuses=("IN_QUEUE", "IN_PROGRESS", "COMPLETED"),
                   error=None, result=None, evil_urls=False):
    """A fal queue mock: POST submit -> status/response/cancel urls; GET status walks
    `statuses`; GET response returns `result`. evil_urls returns off-origin urls (the
    pin must ignore them and rebuild from queue.fal.run)."""
    calls = {"post": 0, "status": 0, "response": 0, "cancel": 0}
    seen = {"submit_body": None, "auth": None}
    state = {"i": 0}
    origin = "https://evil.example" if evil_urls else supplier.FAL_BASE

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "POST":
            calls["post"] += 1
            seen["submit_body"] = req.content
            seen["auth"] = req.headers.get("authorization")
            if submit_code >= 300:
                return httpx.Response(submit_code, text="upstream boom")
            rid = "req-1"
            return httpx.Response(submit_code, json={
                "request_id": rid,
                "status_url": f"{origin}/m/requests/{rid}/status",
                "response_url": f"{origin}/m/requests/{rid}",
                "cancel_url": f"{origin}/m/requests/{rid}/cancel"})
        if req.method == "PUT":
            calls["cancel"] += 1
            return httpx.Response(202, json={"status": "CANCELLATION_REQUESTED"})
        if req.method == "GET" and url.endswith("/status"):
            calls["status"] += 1
            st = statuses[min(state["i"], len(statuses) - 1)]
            state["i"] += 1
            body = {"status": st}
            if st == "COMPLETED" and error:
                body["error"] = error
                body["error_type"] = "GenerationError"
            return httpx.Response(200, json=body)
        if req.method == "GET":
            calls["response"] += 1
            return httpx.Response(200, json=result if result is not None
                                   else {"video": {"url": VIDEO}})
        return httpx.Response(404)

    return httpx.MockTransport(handler), calls, seen


def _rep_transport(*, submit_code=201, statuses=("starting", "processing", "succeeded"),
                   output=VIDEO, error=None):
    calls = {"post": 0, "get": 0, "cancel": 0}
    state = {"i": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "POST" and url.endswith("/cancel"):
            calls["cancel"] += 1
            return httpx.Response(200, json={})
        if req.method == "POST":
            calls["post"] += 1
            if submit_code >= 300:
                return httpx.Response(submit_code, text="upstream boom")
            return httpx.Response(submit_code, json={
                "id": "pred-1", "status": statuses[0],
                "urls": {"get": f"{supplier.REPLICATE_BASE}/v1/predictions/pred-1",
                         "cancel": f"{supplier.REPLICATE_BASE}/v1/predictions/pred-1/cancel"}})
        if req.method == "GET":
            calls["get"] += 1
            state["i"] += 1
            st = statuses[min(state["i"], len(statuses) - 1)]
            body = {"id": "pred-1", "status": st}
            if st == "succeeded":
                body["output"] = output
            if st in ("failed", "canceled"):
                body["error"] = error or "boom"
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    return httpx.MockTransport(handler), calls


def _job(ctx):
    return Job(id=uuid.uuid4(), to_agent="supplier", prompt="cinematic plate",
               context=ctx, timeout_s=300)


def _run(transport, job):
    return asyncio.run(supplier.invoke_supplier(get_spec("supplier"), job, transport=transport))


# ---- catalog / pricing (pure, pre-spend) --------------------------------------------------
def test_resolve_and_pricing():
    e = supplier.resolve("i2v", "fal")
    ok(e["model"] == "bytedance/seedance-2.0/image-to-video", "fal i2v model pinned")
    ok(supplier.estimate_cost("edit", "fal") == 0.08, "fal edit flat price")
    ok(supplier.estimate_cost("i2v", "fal", {"duration": 5}) == round(0.19 * 5, 4),
       "i2v price scales per second")
    ok(supplier.estimate_cost("i2v", "fal", {"duration": 999}) == round(0.19 * 15, 4),
       "duration clamped to 15s for the estimate")
    try:
        supplier.resolve("i2v", "runway")
        ok(False, "unknown backend must raise")
    except ValueError:
        ok(True, "unknown backend fail-closed")
    try:
        supplier.resolve("t2i", "fal", {"catalog": {"fal": {"t2i": {"price": None}}}})
        ok(False, "unpriced override must raise")
    except ValueError:
        ok(True, "UNPRICED entry fail-closed (never spend unpriced)")
    try:
        supplier.resolve("t2i", "fal", {"catalog": {"fal": {"t2i":
            {"model": "../../etc", "price": {"usd": 1}}}}})
        ok(False, "unsafe model id must raise")
    except ValueError:
        ok(True, "path-unsafe model id fail-closed")
    try:
        supplier.resolve("i2v", "replicate", {"catalog": {"replicate": {"i2v":
            {"model": "a/b/c", "price": {"usd": 1}}}}})
        ok(False, "replicate model must be owner/name")
    except ValueError:
        ok(True, "replicate 3-segment model fail-closed")


# ---- fal backend --------------------------------------------------------------------------
def test_fal_i2v_happy_path():
    t, calls, seen = _fal_transport()
    r = _run(t, _job({"op": "i2v", "backend": "fal", "image_url": SEED, "duration": 5}))
    ok(r.status is ResultStatus.OK, f"fal i2v OK (got {r.status}: {r.text[:120]})")
    ok(r.artifact_ref == VIDEO, "artifact_ref = video url")
    ok(r.cost == round(0.19 * 5, 4), "Result.cost logs the list-price estimate")
    ok(calls["post"] == 1 and calls["response"] == 1, "one submit, one result fetch")
    ok(b'"generate_audio": false' in (seen["submit_body"] or b"").replace(b"'", b'"')
       or b'"generate_audio":false' in (seen["submit_body"] or b""),
       "i2v body disables supplier audio (mx-engine owns the bed)")
    ok((seen["auth"] or "").startswith("Key "), "fal auth header is `Key <FAL_KEY>`")


def test_fal_t2i_and_edit():
    t, _, seen = _fal_transport(result={"images": [{"url": IMAGE}]})
    r = _run(t, _job({"op": "t2i", "backend": "fal"}))
    ok(r.status is ResultStatus.OK and r.artifact_ref == IMAGE, "t2i returns images[0].url")
    ok(r.cost == 0.08, "t2i cost logged")
    t, _, seen = _fal_transport(result={"images": [{"url": IMAGE}]})
    r = _run(t, _job({"op": "edit", "backend": "fal", "image_urls": [SEED, IMAGE]}))
    ok(r.status is ResultStatus.OK, "multi-ref edit OK")
    ok(b"image_urls" in (seen["submit_body"] or b""), "edit body carries image_urls")


def test_fal_url_pinning():
    # server returns evil-origin status/response urls -> the pin must rebuild them on
    # queue.fal.run (the mock only answers those paths on any host, so success here plus
    # asserting no request carried the key to evil.example proves the pin).
    hit_evil = []

    def spy(req: httpx.Request) -> httpx.Response:
        if req.url.host == "evil.example":
            hit_evil.append(str(req.url))
            return httpx.Response(200, json={"status": "COMPLETED"})
        if req.method == "POST":
            return httpx.Response(200, json={
                "request_id": "r", "status_url": "https://evil.example/s",
                "response_url": "https://evil.example/r",
                "cancel_url": "https://evil.example/c"})
        if str(req.url).endswith("/status"):
            return httpx.Response(200, json={"status": "COMPLETED"})
        return httpx.Response(200, json={"video": {"url": VIDEO}})

    r = _run(httpx.MockTransport(spy), _job({"op": "i2v", "backend": "fal", "image_url": SEED}))
    ok(r.status is ResultStatus.OK and not hit_evil,
       f"status/response urls pinned to queue.fal.run (evil hits: {hit_evil})")


def test_fal_charge_safety():
    # pre-send connect failure -> RETRYABLE (nothing charged)
    def refuse(req):
        raise httpx.ConnectError("refused")
    r = _run(httpx.MockTransport(refuse), _job({"op": "t2i", "backend": "fal"}))
    ok(r.status is ResultStatus.ERROR and r.error_class is ErrorClass.RETRYABLE,
       "connect error pre-send -> RETRYABLE")
    # non-2xx submit -> TERMINAL fail-closed (charge-ambiguous)
    t, _, _ = _fal_transport(submit_code=503)
    r = _run(t, _job({"op": "t2i", "backend": "fal"}))
    ok(r.status is ResultStatus.ERROR and r.error_class is ErrorClass.TERMINAL,
       "submit 5xx -> TERMINAL (never re-POST a paid submit)")
    # COMPLETED with error set -> TERMINAL (fal reports failures as COMPLETED+error)
    t, _, _ = _fal_transport(statuses=("COMPLETED",), error="nsfw content")
    r = _run(t, _job({"op": "t2i", "backend": "fal"}))
    ok(r.status is ResultStatus.ERROR and r.error_class is ErrorClass.TERMINAL
       and "nsfw" in r.text, "COMPLETED+error -> TERMINAL with the reason")
    # unknown status -> TERMINAL post-charge
    t, _, _ = _fal_transport(statuses=("EXPLODED",))
    r = _run(t, _job({"op": "t2i", "backend": "fal"}))
    ok(r.status is ResultStatus.ERROR and r.error_class is ErrorClass.TERMINAL,
       "unknown fal status -> TERMINAL")
    # completed but no url -> TERMINAL
    t, _, _ = _fal_transport(result={"weird": True})
    r = _run(t, _job({"op": "t2i", "backend": "fal"}))
    ok(r.status is ResultStatus.ERROR and "no video/image url" in r.text,
       "completed-without-url -> TERMINAL")


# ---- replicate backend ---------------------------------------------------------------------
def test_replicate_happy_and_failure():
    t, calls = _rep_transport()
    r = _run(t, _job({"op": "i2v", "backend": "replicate", "image_url": SEED, "duration": 4}))
    ok(r.status is ResultStatus.OK and r.artifact_ref == VIDEO,
       f"replicate i2v OK (got {r.status}: {r.text[:120]})")
    ok(r.cost == round(0.19 * 4, 4), "replicate cost logged from the table")
    t, calls = _rep_transport(output=[{"url": IMAGE}], statuses=("starting", "succeeded"))
    r = _run(t, _job({"op": "t2i", "backend": "replicate"}))
    ok(r.status is ResultStatus.OK and r.artifact_ref == IMAGE,
       "replicate list-of-objects output parsed")
    t, calls = _rep_transport(statuses=("starting", "failed"), error="NSFW")
    r = _run(t, _job({"op": "t2i", "backend": "replicate"}))
    ok(r.status is ResultStatus.ERROR and r.error_class is ErrorClass.TERMINAL
       and "NSFW" in r.text, "replicate failed -> TERMINAL with reason")
    t, calls = _rep_transport(submit_code=500)
    r = _run(t, _job({"op": "t2i", "backend": "replicate"}))
    ok(r.status is ResultStatus.ERROR and r.error_class is ErrorClass.TERMINAL,
       "replicate submit 5xx -> TERMINAL fail-closed")


# ---- input validation / SSRF (all pre-spend) -----------------------------------------------
def test_pre_spend_validation():
    t, calls, _ = _fal_transport()
    r = _run(t, _job({"op": "nope", "backend": "fal"}))
    ok(r.status is ResultStatus.ERROR and calls["post"] == 0, "unknown op fails pre-spend")
    r = _run(t, _job({"op": "i2v", "backend": "fal"}))
    ok(r.status is ResultStatus.ERROR and "image_url" in r.text and calls["post"] == 0,
       "i2v without image_url fails pre-spend")
    r = _run(t, _job({"op": "edit", "backend": "fal", "image_urls": []}))
    ok(r.status is ResultStatus.ERROR and calls["post"] == 0, "edit empty refs fails pre-spend")
    r = _run(t, _job({"op": "edit", "backend": "fal",
                      "image_urls": [SEED] * (supplier.MAX_REF_IMAGES + 1)}))
    ok(r.status is ResultStatus.ERROR and calls["post"] == 0, "too many refs fails pre-spend")
    old = os.environ.pop("FAL_KEY", None)
    try:
        r = _run(t, _job({"op": "t2i", "backend": "fal"}))
        ok(r.status is ResultStatus.ERROR and "FAL_KEY" in r.text and calls["post"] == 0,
           "missing key fail-closed pre-spend")
    finally:
        if old is not None:
            os.environ["FAL_KEY"] = old


def test_ssrf_reject_pre_spend():
    orig = runner._reject_unsafe_url

    async def reject(_u):
        return "host resolves to non-public address 127.0.0.1"
    runner._reject_unsafe_url = reject
    try:
        t, calls, _ = _fal_transport()
        r = _run(t, _job({"op": "i2v", "backend": "fal", "image_url": "https://internal/x.png"}))
        ok(r.status is ResultStatus.ERROR and "rejected" in r.text and calls["post"] == 0,
           "SSRF-rejected image_url never reaches the supplier")
    finally:
        runner._reject_unsafe_url = orig


# ---- runner routing + roster safety ---------------------------------------------------------
def test_registry_and_routing():
    spec = get_spec("supplier")
    ok(spec is not None and spec.adapter.get("kind") == "supplier", "supplier row registered")
    ok(spec.adapter.get("non_idempotent") is True, "paid gateway flagged non_idempotent")
    ok(spec.profile.timeout_s >= 600, "gateway profile timeout covers long renders")
    from runtime.ledger.postgres_store import PostgresLedger
    ok(PostgresLedger._requeue_safe("supplier") is False,
       "paid supplier never auto-requeues (no double-charge on worker crash)")


def main():
    os.environ.setdefault("FAL_KEY", "test:key")
    os.environ.setdefault("REPLICATE_API_TOKEN", "test-token")
    # offline determinism: the SSRF guard does real DNS; no-op it except in the SSRF test
    orig = runner._reject_unsafe_url

    async def _noop(_u):
        return None
    runner._reject_unsafe_url = _noop
    # fast polls so multi-status walks don't sleep 3s each
    supplier._POLL_INTERVAL_S = 0.01
    supplier._POLL_RETRY_BACKOFF_S = 0.01
    try:
        test_resolve_and_pricing()
        test_fal_i2v_happy_path()
        test_fal_t2i_and_edit()
        test_fal_url_pinning()
        test_fal_charge_safety()
        test_replicate_happy_and_failure()
        test_pre_spend_validation()
        test_ssrf_reject_pre_spend()
        test_registry_and_routing()
    finally:
        runner._reject_unsafe_url = orig
    print(f"supplier gateway: {PASS[0]} passed, {FAIL[0]} failed")
    raise SystemExit(1 if FAIL[0] else 0)


if __name__ == "__main__":
    main()
