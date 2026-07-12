"""supplier.py — the thin PRICED gateway to per-call media suppliers (fal.ai primary,
Replicate secondary). ONE interface, three ops:

    t2i   text -> image           (prompt)
    edit  multi-ref image edit    (prompt + image_urls[])
    i2v   image -> video          (prompt + image_url [+ end_image_url/duration/resolution])

Every call runs as a LEDGER JOB (a submit to the `supplier` registry agent) so the spend is
durable: the Result carries `cost` — a LIST-PRICE ESTIMATE from the pinned catalog, because
neither fal nor Replicate returns the charged amount in-band — and the worker persists it in
attempt.result. The submit POST charges money and is NOT deduplicated, so the registry row is
non_idempotent (never auto-requeued) and this module follows runner._hf_generate's
charge-correctness contract exactly:

  * pre-send connect failure -> RETRYABLE (nothing reached the supplier, nothing charged)
  * ambiguous submit failure -> TERMINAL fail-closed (may have charged; never re-POST)
  * ANY post-submit failure  -> TERMINAL (charged; no retry; no exception may escape)
  * deadline                 -> best-effort cancel + TIMEOUT (TERMINAL)

Higgsfield (adapter kinds "higgsfield"/"stitch", plus the mcp.higgsfield.ai connector) stays
as the ALTERNATE backend — this gateway does not replace it; the orchestrator routes per shot.
A supplier renders motion/background pixels ONLY: brand text/logos/captions are composited
downstream by mx-engine (it never receives a brand pixel to render).

Verified externals (2026-07-12, fal docs + model pages):
  * fal queue API: POST https://queue.fal.run/{model} with `Authorization: Key $FAL_KEY`,
    body = the model input JSON -> {request_id, status_url, response_url, cancel_url}.
    GET status_url -> {status: IN_QUEUE|IN_PROGRESS|COMPLETED, error?, error_type?};
    a FAILED request still reports COMPLETED with `error` set. GET response_url -> model
    output (video models: {video:{url}}, image models: {images:[{url}]}). Cancel: PUT.
  * fal i2v `bytedance/seedance-2.0/image-to-video`: required prompt+image_url; optional
    end_image_url, resolution, duration, aspect_ratio, generate_audio. Output video.url.
  * fal edit `fal-ai/nano-banana-2/edit`: $0.08/image (2K/4K billed 1.5x/2x — the estimate
    logs the BASE price; measure real spend on the dashboard before any cost LOGIC).
  * Replicate: POST https://api.replicate.com/v1/models/{owner}/{name}/predictions with
    `Authorization: Bearer $REPLICATE_API_TOKEN`, {"input": {...}} -> prediction
    {id, status, urls:{get,cancel}}; poll urls.get until succeeded|failed|canceled.
    Replicate INPUT PARAM NAMES for edit/i2v are pinned from memory, UNVERIFIED live —
    verify with one $0.08-bounded call before relying on the replicate backend.
"""
from __future__ import annotations

import asyncio
import math
import os
import re
import time
from typing import Any, Optional

from runtime.contracts import ErrorClass, Job, Result, ResultStatus
from runtime.registry import AgentSpec

OPS = ("t2i", "edit", "i2v")
BACKENDS = ("fal", "replicate")
FAL_BASE = "https://queue.fal.run"
REPLICATE_BASE = "https://api.replicate.com"

# env var per backend; keys live in ~/.myndaix/.secrets, never in the adapter/roster.
SECRET_REFS = {"fal": "FAL_KEY", "replicate": "REPLICATE_API_TOKEN"}

# ---- the pinned catalog: ONE model per (backend, op), price as DATA -----------------------
# Prices are LIST-PRICE ESTIMATES (fal model pages / provider comparisons, 2026-07-12); the
# APIs do not return spend in-band. An entry with no price is UNPRICED and fails closed —
# never spend unpriced. Override per-row via adapter["catalog"] (same shape, deep-merged).
DEFAULT_CATALOG: dict[str, dict[str, dict[str, Any]]] = {
    "fal": {
        "t2i":  {"model": "fal-ai/nano-banana-2", "price": {"usd": 0.08}},
        "edit": {"model": "fal-ai/nano-banana-2/edit", "price": {"usd": 0.08}},
        "i2v":  {"model": "bytedance/seedance-2.0/image-to-video",
                 "price": {"usd_per_second": 0.19, "default_seconds": 5.0}},
    },
    "replicate": {
        "t2i":  {"model": "black-forest-labs/flux-dev", "price": {"usd": 0.025}},
        "edit": {"model": "google/nano-banana-2", "price": {"usd": 0.08}},
        "i2v":  {"model": "bytedance/seedance-2.0",
                 "price": {"usd_per_second": 0.19, "default_seconds": 5.0}},
    },
}

# model ids land in a URL path — allow only owner/name(/subpath) segments, no dots-only
# segments, no traversal. Replicate needs exactly owner/name (its predictions route).
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]*(/[A-Za-z0-9][A-Za-z0-9._\-]*){1,3}")
_ASPECT_RE = re.compile(r"\d{1,2}:\d{1,2}")
_RESOLUTIONS = ("480p", "720p", "1080p")
MAX_REF_IMAGES = 8
DUR_MIN_S, DUR_MAX_S = 1.0, 15.0

_POLL_INTERVAL_S = 3.0
_POLL_RETRY_BACKOFF_S = 5.0
_POLL_RETRY_MAX = 5
_FAL_ACTIVE = ("in_queue", "in_progress")
_REP_ACTIVE = ("starting", "processing", "queued")


def resolve(op: str, backend: str, adapter: Optional[dict] = None) -> dict:
    """Resolve the (backend, op) catalog entry, adapter overrides merged over the defaults.
    FAIL-CLOSED (ValueError) on an unknown op/backend, a missing/unsafe model id, a missing
    price, or a replicate model that isn't exactly owner/name — all pre-spend."""
    if op not in OPS:
        raise ValueError(f"unknown op {op!r}; one of {OPS}")
    if backend not in BACKENDS:
        raise ValueError(f"unknown backend {backend!r}; one of {BACKENDS}")
    entry = dict(DEFAULT_CATALOG[backend].get(op) or {})
    over = ((adapter or {}).get("catalog") or {}).get(backend, {}).get(op)
    if isinstance(over, dict):
        entry.update(over)
    model = entry.get("model")
    if not isinstance(model, str) or not _MODEL_RE.fullmatch(model) or ".." in model:
        raise ValueError(f"no safe model pinned for {backend}/{op}: {model!r}")
    if backend == "replicate" and model.count("/") != 1:
        raise ValueError(f"replicate model must be owner/name: {model!r}")
    price = entry.get("price")
    if not isinstance(price, dict) or not any(
            isinstance(price.get(k), (int, float)) and not isinstance(price.get(k), bool)
            and math.isfinite(price[k]) and price[k] >= 0
            for k in ("usd", "usd_per_second")):
        raise ValueError(f"{backend}/{op} ({model}) is UNPRICED — refusing to spend")
    return entry


def _duration_s(context: dict, entry: dict) -> float:
    """The i2v duration used for the request AND the estimate: context.duration clamped to
    [1, 15]s, else the catalog default."""
    price = entry.get("price") or {}
    try:
        d = float(context.get("duration"))
    except (TypeError, ValueError):
        d = float(price.get("default_seconds", 5.0))
    if not math.isfinite(d):
        d = float(price.get("default_seconds", 5.0))
    return min(DUR_MAX_S, max(DUR_MIN_S, d))


def estimate_cost(op: str, backend: str, context: Optional[dict] = None,
                  adapter: Optional[dict] = None) -> float:
    """Pre-spend list-price estimate in USD (what the human cost gate shows and what the
    Result logs). Flat `usd` for images; `usd_per_second * duration` for video. Raises
    (fail-closed) if the entry is missing or unpriced."""
    entry = resolve(op, backend, adapter)
    price = entry["price"]
    if "usd_per_second" in price:
        return round(float(price["usd_per_second"]) * _duration_s(context or {}, entry), 4)
    return round(float(price["usd"]), 4)


# ---- request building (explicit whitelists — never splat untrusted context into a body) ---
def _collect_urls(context: dict, op: str) -> list[str]:
    """The image URL(s) this op sends to the supplier (all SSRF-checked by the caller)."""
    if op == "t2i":
        return []
    if op == "edit":
        urls = context.get("image_urls")
        if not isinstance(urls, list) or not urls or len(urls) > MAX_REF_IMAGES \
                or not all(isinstance(u, str) and u for u in urls):
            raise ValueError(f"edit needs image_urls: a list of 1..{MAX_REF_IMAGES} urls")
        return list(urls)
    u = context.get("image_url")
    if not isinstance(u, str) or not u:
        raise ValueError("i2v needs image_url")
    end = context.get("end_image_url")
    return [u] + ([end] if isinstance(end, str) and end else [])


def _build_inputs(op: str, backend: str, prompt: str, context: dict, entry: dict) -> dict:
    """The model input body: prompt + a whitelisted, coerced subset of job.context. Unknown
    context keys are DROPPED (sanitize-before-inject), optional knobs are omitted unless
    they coerce cleanly."""
    inputs: dict[str, Any] = {"prompt": prompt}
    if op == "edit":
        # fal nano-banana-2/edit takes `image_urls`; replicate nano-banana-2 takes
        # `image_input` (UNVERIFIED live — see module docstring).
        inputs["image_urls" if backend == "fal" else "image_input"] = context["image_urls"]
    elif op == "i2v":
        # fal seedance-2.0 i2v takes `image_url`; replicate seedance takes `image`
        # (UNVERIFIED live). Plates need no supplier audio — mx-engine owns the bed.
        inputs["image_url" if backend == "fal" else "image"] = context["image_url"]
        d = _duration_s(context, entry)
        inputs["duration"] = int(d) if float(d).is_integer() else d
        if backend == "fal":
            inputs["generate_audio"] = False
            if isinstance(context.get("end_image_url"), str) and context["end_image_url"]:
                inputs["end_image_url"] = context["end_image_url"]
        res = context.get("resolution")
        if isinstance(res, str) and res in _RESOLUTIONS:
            inputs["resolution"] = res
        ar = context.get("aspect_ratio")
        if isinstance(ar, str) and _ASPECT_RE.fullmatch(ar):
            inputs["aspect_ratio"] = ar
    return inputs


# ---- the invoke handler (routed by runner.invoke on adapter.kind == "supplier") -----------
async def invoke_supplier(spec: AgentSpec, job: Job, *, transport=None) -> Result:
    """One priced supplier call as a ledger job. job.context: {op, backend?, image_url? |
    image_urls?, end_image_url?, duration?, resolution?, aspect_ratio?}; the prompt is
    job.prompt. Everything that can fail for free fails BEFORE the submit."""
    try:
        import httpx
    except ImportError:
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                      text="supplier agents need httpx (pip install httpx)")
    started = time.monotonic()
    a = spec.adapter
    op = job.context.get("op")
    backend = job.context.get("backend") or a.get("default_backend") or "fal"
    try:
        entry = resolve(op, backend, a)
        urls = _collect_urls(job.context, op)
    except ValueError as e:
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                      text=f"supplier: {e}", ms=_ms(started))
    prompt = (job.prompt or "").strip()
    if not prompt:
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                      text="supplier: empty prompt", ms=_ms(started))
    secret_ref = (a.get("secret_refs") or SECRET_REFS).get(backend)
    key = os.environ.get(secret_ref) if secret_ref else None
    if not key:
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                      text=f"missing API key in env: {secret_ref or '<secret_ref unset>'}",
                      ms=_ms(started))
    # SSRF guard on every URL we forward to a third party (same guard as the HF path).
    from runtime.runner import _reject_unsafe_url
    for u in urls:
        reason = await _reject_unsafe_url(u)
        if reason:
            return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                          text=f"image url rejected: {reason}", ms=_ms(started))
    cost_est = estimate_cost(op, backend, job.context, a)
    inputs = _build_inputs(op, backend, prompt, job.context, entry)
    # PROFILE timeout, not job.timeout_s (the spine leaves job.timeout_s at the dead 300s
    # default) — identical to invoke_higgsfield.
    deadline = started + spec.profile.timeout_s
    from runtime.runner import _hf_float, _hf_int
    poll_interval = _hf_float(a.get("poll_interval_s"), _POLL_INTERVAL_S)
    retry_backoff = _hf_float(a.get("poll_retry_backoff_s"), _POLL_RETRY_BACKOFF_S)
    retry_max = _hf_int(a.get("poll_retry_max"), _POLL_RETRY_MAX)
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
        if backend == "fal":
            return await _fal_call(client, model=entry["model"], key=key, inputs=inputs,
                                   started=started, deadline=deadline, cost=cost_est,
                                   poll_interval=poll_interval, retry_backoff=retry_backoff,
                                   retry_max=retry_max)
        return await _replicate_call(client, model=entry["model"], key=key, inputs=inputs,
                                     started=started, deadline=deadline, cost=cost_est,
                                     poll_interval=poll_interval, retry_backoff=retry_backoff,
                                     retry_max=retry_max)


# ---- fal.ai queue backend -----------------------------------------------------------------
async def _fal_call(client, *, model: str, key: str, inputs: dict, started: float,
                    deadline: float, cost: float, poll_interval: float,
                    retry_backoff: float, retry_max: int) -> Result:
    """Submit one fal queue request and poll to a terminal Result (charge contract in the
    module docstring). status/response/cancel URLs are pinned to queue.fal.run's origin —
    we attach the key to them, so a server-returned URL must not redirect it elsewhere."""
    import httpx

    from runtime.runner import _hf_artifact_url, _hf_pin_url, _hf_req_timeout, _hf_sleep
    headers = {"Content-Type": "application/json", "Authorization": f"Key {key}"}
    try:
        resp = await client.post(f"{FAL_BASE}/{model}", json=inputs, headers=headers,
                                 timeout=_hf_req_timeout(deadline))
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.RETRYABLE,
                      text=f"fal submit unreachable: {e}", ms=_ms(started))
    except httpx.HTTPError as e:
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                      text=f"fal submit ambiguous failure (fail-closed, no retry): {e}",
                      ms=_ms(started))
    if resp.status_code >= 300:
        # a gateway 5xx can land AFTER the queue accepted & charged — charge-ambiguous,
        # fail closed for every non-2xx (only the connect branch above is pre-send).
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                      exit_code=resp.status_code,
                      text=f"fal submit {resp.status_code} (fail-closed, no retry): "
                           f"{resp.text[:300]}", ms=_ms(started))
    try:
        sub = resp.json()
        request_id = sub["request_id"]
    except (KeyError, ValueError, TypeError) as e:
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                      text=f"fal submit: unexpected shape: {e}", ms=_ms(started))
    # -- post-charge: NO exception may escape this block (worker must never re-submit) --
    try:
        status_url = _hf_pin_url(sub.get("status_url"), FAL_BASE,
                                 f"/{model}/requests/{request_id}/status")
        response_url = _hf_pin_url(sub.get("response_url"), FAL_BASE,
                                   f"/{model}/requests/{request_id}")
        cancel_url = _hf_pin_url(sub.get("cancel_url"), FAL_BASE,
                                 f"/{model}/requests/{request_id}/cancel")
        fails = 0
        while True:
            if time.monotonic() >= deadline:
                await _fal_best_effort_cancel(client, cancel_url, headers)
                return Result(status=ResultStatus.TIMEOUT, error_class=ErrorClass.TERMINAL,
                              text=f"fal poll timed out (request_id={request_id})",
                              ms=_ms(started))
            err = None
            try:
                pr = await client.get(status_url, headers=headers,
                                      timeout=_hf_req_timeout(deadline))
                if pr.status_code != 200:
                    err = f"poll {pr.status_code}: {pr.text[:200]}"
            except httpx.HTTPError as e:
                err = f"poll error: {e}"
            if err is not None:
                fails += 1
                if fails > retry_max:
                    return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                                  text=f"fal {err} (charged, no retry)", ms=_ms(started))
                await asyncio.sleep(_hf_sleep(retry_backoff, deadline))
                continue
            fails = 0
            try:
                data = pr.json()
                status = str(data.get("status") or "").lower()
            except (ValueError, TypeError, AttributeError) as e:
                return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                              text=f"fal poll: bad payload (charged): {e}", ms=_ms(started))
            if status == "completed":
                # a FAILED fal request still reports COMPLETED, with `error` set.
                if data.get("error"):
                    return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                                  text=f"fal request failed ({data.get('error_type')}): "
                                       f"{str(data.get('error'))[:300]}", ms=_ms(started))
                try:
                    rr = await client.get(response_url, headers=headers,
                                          timeout=_hf_req_timeout(deadline))
                    out = rr.json() if rr.status_code == 200 else None
                except (httpx.HTTPError, ValueError) as e:
                    return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                                  text=f"fal result fetch failed (charged): {e}",
                                  ms=_ms(started))
                url = _hf_artifact_url(out) if isinstance(out, dict) else None
                if not url:
                    return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                                  text="fal completed but no video/image url", ms=_ms(started))
                return Result(status=ResultStatus.OK, text=url, artifact_ref=url,
                              cost=cost, ms=_ms(started))
            if status not in _FAL_ACTIVE:
                return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                              text=f"fal unknown status {status!r} "
                                   f"(request_id={request_id})", ms=_ms(started))
            await asyncio.sleep(_hf_sleep(poll_interval, deadline))
    except Exception as e:   # noqa: BLE001 — deliberate post-charge backstop
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                      text=f"fal poll: unexpected post-charge error (charged, no retry): "
                           f"{type(e).__name__}: {e}", ms=_ms(started))


async def _fal_best_effort_cancel(client, cancel_url: str, headers: dict) -> None:
    """fal cancel is a PUT; only a still-queued request is reliably stopped. Best-effort:
    never mask the timeout Result."""
    try:
        await client.put(cancel_url, headers=headers, timeout=10.0)
    except Exception:   # noqa: BLE001
        pass


# ---- Replicate backend ----------------------------------------------------------------------
def _rep_output_url(output) -> Optional[str]:
    """Replicate output is model-specific: a url string, a list of url strings, or a list
    of {url} objects. Non-string/empty -> None (TERMINAL upstream), never a crash."""
    if isinstance(output, str) and output:
        return output
    if isinstance(output, list) and output:
        first = output[0]
        if isinstance(first, str) and first:
            return first
        if isinstance(first, dict) and isinstance(first.get("url"), str) and first["url"]:
            return first["url"]
    if isinstance(output, dict) and isinstance(output.get("url"), str) and output["url"]:
        return output["url"]
    return None


async def _replicate_call(client, *, model: str, key: str, inputs: dict, started: float,
                          deadline: float, cost: float, poll_interval: float,
                          retry_backoff: float, retry_max: int) -> Result:
    """Submit one Replicate prediction and poll to a terminal Result. Same charge contract
    as the fal path; the poll/cancel URLs are pinned to api.replicate.com's origin."""
    import httpx

    from runtime.runner import _hf_pin_url, _hf_req_timeout, _hf_sleep
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    try:
        resp = await client.post(f"{REPLICATE_BASE}/v1/models/{model}/predictions",
                                 json={"input": inputs}, headers=headers,
                                 timeout=_hf_req_timeout(deadline))
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.RETRYABLE,
                      text=f"replicate submit unreachable: {e}", ms=_ms(started))
    except httpx.HTTPError as e:
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                      text=f"replicate submit ambiguous failure (fail-closed, no retry): {e}",
                      ms=_ms(started))
    if resp.status_code >= 300:
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                      exit_code=resp.status_code,
                      text=f"replicate submit {resp.status_code} (fail-closed, no retry): "
                           f"{resp.text[:300]}", ms=_ms(started))
    try:
        pred = resp.json()
        pred_id = pred["id"]
    except (KeyError, ValueError, TypeError) as e:
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                      text=f"replicate submit: unexpected shape: {e}", ms=_ms(started))
    # -- post-charge: NO exception may escape this block --
    try:
        rurls = pred.get("urls") if isinstance(pred.get("urls"), dict) else {}
        get_url = _hf_pin_url(rurls.get("get"), REPLICATE_BASE,
                              f"/v1/predictions/{pred_id}")
        cancel_url = _hf_pin_url(rurls.get("cancel"), REPLICATE_BASE,
                                 f"/v1/predictions/{pred_id}/cancel")
        fails = 0
        data = pred
        while True:
            status = str(data.get("status") or "").lower()
            if status == "succeeded":
                url = _rep_output_url(data.get("output"))
                if not url:
                    return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                                  text="replicate succeeded but no output url",
                                  ms=_ms(started))
                return Result(status=ResultStatus.OK, text=url, artifact_ref=url,
                              cost=cost, ms=_ms(started))
            if status in ("failed", "canceled"):
                return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                              text=f"replicate {status}: {str(data.get('error'))[:300]} "
                                   f"(id={pred_id})", ms=_ms(started))
            if status not in _REP_ACTIVE:
                return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                              text=f"replicate unknown status {status!r} (id={pred_id})",
                              ms=_ms(started))
            if time.monotonic() >= deadline:
                try:
                    await client.post(cancel_url, headers=headers, timeout=10.0)
                except Exception:   # noqa: BLE001 — best-effort, never mask the timeout
                    pass
                return Result(status=ResultStatus.TIMEOUT, error_class=ErrorClass.TERMINAL,
                              text=f"replicate poll timed out (id={pred_id})", ms=_ms(started))
            await asyncio.sleep(_hf_sleep(poll_interval, deadline))
            try:
                pr = await client.get(get_url, headers=headers,
                                      timeout=_hf_req_timeout(deadline))
                if pr.status_code != 200:
                    raise ValueError(f"poll {pr.status_code}: {pr.text[:200]}")
                data = pr.json()
                fails = 0
            except (httpx.HTTPError, ValueError) as e:
                fails += 1
                if fails > retry_max:
                    return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                                  text=f"replicate poll error (charged, no retry): {e}",
                                  ms=_ms(started))
                data = {"status": _REP_ACTIVE[0]}   # keep polling after a transient
                await asyncio.sleep(_hf_sleep(retry_backoff, deadline))
    except Exception as e:   # noqa: BLE001 — deliberate post-charge backstop
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                      text=f"replicate poll: unexpected post-charge error (charged, no "
                           f"retry): {type(e).__name__}: {e}", ms=_ms(started))


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
