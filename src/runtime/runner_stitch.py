"""Stitcher (workspace-actor): build a long video from a shot-list.

Per shot: generate a clip via DoP (runner._hf_generate), chaining the previous shot's
last frame as the next shot's start image for continuity; download each clip; then
ffmpeg-concat and apply a DETERMINISTIC brand layer (the HYBRID rule — AI for the moving
b-roll, exact tools for the logo/end-card, because models can't render a logo pixel-exact).

One self-contained job (NOT N ledger sub-jobs) so the whole spend sits under one cost
ceiling. authority=WORKSPACE_ACTOR -> the worker NEVER auto-retries it, so a re-run can't
silently re-charge; a per-segment manifest in the workspace makes a manual re-run RESUME
(skip already-rendered shots) instead of regenerating. Partial failure returns the clips
that DID succeed, concatenated — spend on good shots is never thrown away.

Shot-list (job.context["shotlist"]) = ordered list of dicts:
  {prompt, motion_id?, motion_strength?, image_url?, end_image_url?, application?}
Job-level context: image_url (base/first seed), chain (bool, default True), end_card_url
(branded end card image to append), application (default model path via adapter).
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from typing import Optional

from runtime.contracts import ErrorClass, Job, Result, ResultStatus
from runtime.registry import AgentSpec
from runtime import ffmpeg_util as fu
from runtime.runner import (
    _hf_generate, _hf_float, _hf_int, _ms,
    _HF_POLL_INTERVAL_S, _HF_POLL_RETRY_BACKOFF_S, _HF_POLL_RETRY_MAX,
)

_STITCH_MAX_SEGMENTS = 12      # cost guardrail: N shots x ~$0.13 — a runaway N is a $ DoS
_HTTP_TIMEOUT_S = 120          # per clip download / frame+final upload


def _err(text: str, *, started: float, cost: Optional[float] = None,
         status: ResultStatus = ResultStatus.ERROR,
         ec: ErrorClass = ErrorClass.TERMINAL, artifact: Optional[str] = None) -> Result:
    return Result(status=status, error_class=ec, text=text, artifact_ref=artifact,
                  cost=cost or None, ms=_ms(started))


def _load_manifest(path: str) -> dict:
    try:
        with open(path) as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_manifest(path: str, manifest: dict) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(manifest, f)
        os.replace(tmp, path)          # atomic
    except OSError:
        pass                           # manifest is an optimization; never fail the job over it


async def _hf_upload(client, base: str, key: str, data: bytes, content_type: str) -> str:
    """Replicate Higgsfield's two-step upload with raw httpx (no SDK dep):
    POST /files/generate-upload-url {content_type} -> {public_url, upload_url};
    PUT the bytes to upload_url (presigned, no auth header); return public_url."""
    r = await client.post(base.rstrip("/") + "/files/generate-upload-url",
                          headers={"Authorization": f"Key {key}",
                                   "Content-Type": "application/json"},
                          json={"content_type": content_type}, timeout=_HTTP_TIMEOUT_S)
    r.raise_for_status()
    j = r.json()
    public_url, upload_url = j["public_url"], j["upload_url"]
    pr = await client.put(upload_url, content=data,
                          headers={"Content-Type": content_type}, timeout=_HTTP_TIMEOUT_S)
    pr.raise_for_status()
    return public_url


async def invoke_stitch(spec: AgentSpec, job: Job, *, transport=None) -> Result:
    try:
        import httpx
    except ImportError:
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                      text="stitcher needs httpx (pip install httpx)")
    started = time.monotonic()
    a = spec.adapter
    base = a.get("base")
    if not base:
        return _err("stitcher adapter missing 'base'", started=started)
    secret_ref = a.get("secret_ref")
    key = os.environ.get(secret_ref) if secret_ref else None
    if not key:
        return _err(f"missing API key in env: {secret_ref or '<secret_ref unset>'}", started=started)
    default_app = job.context.get("application") or a.get("application")

    shots = job.context.get("shotlist")
    if not isinstance(shots, list) or not shots:
        return _err("stitcher job missing non-empty 'shotlist' in context", started=started)
    if not all(isinstance(s, dict) for s in shots):
        return _err("stitcher shotlist entries must be objects", started=started)
    max_seg = _hf_int(a.get("max_segments"), _STITCH_MAX_SEGMENTS)
    if len(shots) > max_seg:
        # fail-closed BEFORE spending: a runaway shot count is a financial DoS.
        return _err(f"shotlist has {len(shots)} shots > max_segments {max_seg}", started=started)

    chain = job.context.get("chain", True)
    workdir = job.worktree_path or tempfile.mkdtemp(prefix="mdx-stitch-")
    os.makedirs(workdir, exist_ok=True)
    manifest_path = os.path.join(workdir, "stitch_manifest.json")
    manifest = _load_manifest(manifest_path)

    deadline = started + job.timeout_s
    poll_interval = _hf_float(a.get("poll_interval_s"), _HF_POLL_INTERVAL_S)
    retry_backoff = _hf_float(a.get("poll_retry_backoff_s"), _HF_POLL_RETRY_BACKOFF_S)
    retry_max = _hf_int(a.get("poll_retry_max"), _HF_POLL_RETRY_MAX)

    clips: list[str] = []
    total_cost = 0.0
    failed_at: Optional[int] = None
    fail_reason: Optional[str] = None
    prev_last_url: Optional[str] = None

    async with httpx.AsyncClient(transport=transport) as client:
        for i, shot in enumerate(shots):
            seg_path = os.path.join(workdir, f"seg_{i:03d}.mp4")
            # RESUME: a prior run already rendered this segment -> reuse, don't re-charge.
            cached = manifest.get(str(i))
            if cached and os.path.isfile(cached.get("clip_path", "")):
                clips.append(cached["clip_path"])
                total_cost += cached.get("cost") or 0.0
                prev_last_url = cached.get("last_frame_url") or prev_last_url
                continue

            start_img = (shot.get("image_url")
                         or (prev_last_url if (chain and i > 0) else None)
                         or job.context.get("image_url"))
            if not start_img:
                failed_at, fail_reason = i, "no image_url (and no chained/base seed)"
                break
            app = shot.get("application") or default_app
            if not app:
                failed_at, fail_reason = i, "no 'application' (model path) for shot"
                break

            res = await _hf_generate(
                client, base=base, application=app, key=key,
                prompt=shot.get("prompt") or job.prompt, image_url=start_img,
                started=time.monotonic(), deadline=deadline,
                motion_id=shot.get("motion_id"), motion_strength=shot.get("motion_strength"),
                end_image_url=shot.get("end_image_url"),
                poll_interval=poll_interval, retry_backoff=retry_backoff, retry_max=retry_max)
            if res.status is not ResultStatus.OK or not res.artifact_ref:
                failed_at, fail_reason = i, res.text
                break
            total_cost += res.cost or 0.0

            try:
                dl = await client.get(res.artifact_ref, timeout=_HTTP_TIMEOUT_S)
                dl.raise_for_status()
                with open(seg_path, "wb") as f:
                    f.write(dl.content)
            except Exception as e:   # noqa: BLE001 - any download failure -> partial, not a crash
                failed_at, fail_reason = i, f"clip download failed: {e}"
                break
            clips.append(seg_path)

            # CHAIN: extract this clip's last frame, upload it, feed it as the next start.
            # Skip if the next shot brings its own image_url, or this is the last shot.
            last_url = None
            if chain and i < len(shots) - 1 and not shots[i + 1].get("image_url"):
                try:
                    frame_png = os.path.join(workdir, f"frame_{i:03d}.png")
                    await asyncio.to_thread(fu.last_frame_png, seg_path, frame_png)
                    with open(frame_png, "rb") as f:
                        last_url = await _hf_upload(client, base, key, f.read(), "image/png")
                except Exception:   # noqa: BLE001 - chaining is best-effort; next shot falls back
                    last_url = None
            prev_last_url = last_url or prev_last_url
            manifest[str(i)] = {"clip_path": seg_path, "last_frame_url": last_url,
                                "cost": res.cost or 0.0}
            _save_manifest(manifest_path, manifest)

        # -- assemble whatever succeeded --
        if not clips:
            return _err(f"stitch produced no clips (failed at shot {failed_at}: {fail_reason})",
                        started=started, cost=total_cost)
        final_path = os.path.join(workdir, "final.mp4")
        try:
            seq = list(clips)
            # HYBRID brand layer: append a deterministic end card (exact logo) if given.
            end_card = await _resolve_end_card(client, job, workdir, clips[0])
            if end_card:
                seq.append(end_card)
            await asyncio.to_thread(fu.concat, seq, final_path)
            with open(final_path, "rb") as f:
                final_url = await _hf_upload(client, base, key, f.read(), "video/mp4")
        except fu.FfmpegError as e:
            return _err(f"stitch assembly (ffmpeg) failed: {e}", started=started, cost=total_cost)
        except Exception as e:   # noqa: BLE001
            return _err(f"stitch assembly failed: {type(e).__name__}: {e}",
                        started=started, cost=total_cost)

    if failed_at is not None:
        # partial success: hand back the concatenated good shots + the failure reason.
        return _err(f"stitch PARTIAL {len(clips)}/{len(shots)} shots "
                    f"(failed at {failed_at}: {fail_reason})",
                    started=started, cost=total_cost, artifact=final_url)
    return Result(status=ResultStatus.OK, text=final_url, artifact_ref=final_url,
                  cost=total_cost or None, ms=_ms(started))


async def _resolve_end_card(client, job: Job, workdir: str, ref_clip: str) -> Optional[str]:
    """If the job supplies a branded end-card image (end_card_url to download, or
    end_card_path local), render it into a static clip sized to match the first clip so it
    concats uniformly. Returns the clip path, or None if no end card / on any failure
    (the end card is optional polish, never a reason to fail the whole render)."""
    url = job.context.get("end_card_url")
    path = job.context.get("end_card_path")
    img = os.path.join(workdir, "endcard_src")
    try:
        if url:
            r = await client.get(url, timeout=_HTTP_TIMEOUT_S)
            r.raise_for_status()
            with open(img, "wb") as f:
                f.write(r.content)
        elif path and os.path.isfile(path):
            img = path
        else:
            return None
        s = await asyncio.to_thread(fu.probe, ref_clip)
        size = (int(s["width"]), int(s["height"]))
        dur = _hf_float(job.context.get("end_card_seconds"), 2.0)
        out = os.path.join(workdir, "endcard.mp4")
        await asyncio.to_thread(lambda: fu.image_to_clip(img, out, duration=dur, size=size))
        return out
    except Exception:   # noqa: BLE001 - end card is optional; never break the render
        return None
