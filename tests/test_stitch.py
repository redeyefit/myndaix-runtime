"""Stitcher tests. The per-segment generation (_hf_generate) and ffmpeg calls are
mocked so these run fast and offline; a couple of tests exercise REAL ffmpeg against
tiny generated fixtures to prove the concat/last-frame helpers. Sync test functions
drive the async runner via asyncio.run (matches test_runner.py; no asyncio plugin needed).
"""
import asyncio
import os
import shutil
import subprocess
import uuid

import httpx
import pytest

from runtime.contracts import Authority, ErrorClass, Job, Reach, Result, ResultStatus
from runtime.registry import AgentSpec
from runtime import ffmpeg_util, runner_stitch


# -- fixtures / helpers ----------------------------------------------------
def _spec(max_segments=12):
    return AgentSpec(agent_id="stitcher", reach=Reach.API, authority=Authority.WORKSPACE_ACTOR,
                     model="dop-lite", role="stitch",
                     adapter={"kind": "stitch", "base": "https://platform.higgsfield.ai",
                              "secret_ref": "HF_KEY", "application": "/higgsfield-ai/dop/lite",
                              "max_segments": max_segments})


def _job(shots, workdir, **ctx):
    c = {"shotlist": shots}
    c.update(ctx)
    return Job(id=uuid.uuid4(), to_agent="stitcher", prompt="brand reveal",
               context=c, worktree_path=str(workdir), timeout_s=300)


def _mock_transport():
    """Handles the download (GET), upload-url POST, and presigned PUT. Each upload-url
    request returns a DISTINCT public_url so chaining + final can be asserted."""
    state = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        u = str(req.url)
        if u.endswith("/files/generate-upload-url"):
            state["n"] += 1
            n = state["n"]
            return httpx.Response(200, json={"public_url": f"https://cdn/up{n}.png",
                                             "upload_url": f"https://up.example/put{n}"})
        if req.method == "PUT":
            return httpx.Response(200)
        if req.method == "GET":
            return httpx.Response(200, content=b"FAKE_MP4_BYTES")
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _patch_ffmpeg(monkeypatch):
    """Replace the ffmpeg shellouts with file-creating stubs (no real ffmpeg)."""
    def fake_last_frame(video, out):
        with open(out, "wb") as f:
            f.write(b"PNG")
        return out

    def fake_concat(paths, out, **kw):
        with open(out, "wb") as f:
            f.write(b"MP4")
        return out

    def fake_probe(p):
        return {"width": "1080", "height": "1920", "codec_name": "h264",
                "pix_fmt": "yuv420p", "sample_aspect_ratio": "1:1", "time_base": "1/30"}

    def fake_img2clip(img, out, **kw):
        with open(out, "wb") as f:
            f.write(b"MP4")
        return out

    monkeypatch.setattr(ffmpeg_util, "last_frame_png", fake_last_frame)
    monkeypatch.setattr(ffmpeg_util, "concat", fake_concat)
    monkeypatch.setattr(ffmpeg_util, "probe", fake_probe)
    monkeypatch.setattr(ffmpeg_util, "image_to_clip", fake_img2clip)


def _ok_gen(calls, fail_at=None):
    """A fake _hf_generate that records each call and returns OK (or a failure at fail_at)."""
    async def gen(client, *, base, application, key, prompt, image_url, started, deadline, **kw):
        calls.append({"image_url": image_url, "motion_id": kw.get("motion_id"),
                      "application": application, "prompt": prompt})
        if fail_at is not None and len(calls) - 1 == fail_at:
            return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                          text="gen boom")
        return Result(status=ResultStatus.OK, text=f"https://cdn/clip{len(calls)}.mp4",
                      artifact_ref=f"https://cdn/clip{len(calls)}.mp4", cost=0.13)
    return gen


# -- tests -----------------------------------------------------------------
def test_stitch_happy_path_chains_and_uploads(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_KEY", "kid:secret")
    _patch_ffmpeg(monkeypatch)
    calls = []
    monkeypatch.setattr(runner_stitch, "_hf_generate", _ok_gen(calls))
    shots = [
        {"prompt": "open", "image_url": "https://seed/0.png", "motion_id": "M0"},
        {"prompt": "mid", "motion_id": "M1"},
        {"prompt": "close", "motion_id": "M2"},
    ]
    r = asyncio.run(runner_stitch.invoke_stitch(_spec(), _job(shots, tmp_path),
                                                transport=_mock_transport()))
    assert r.status is ResultStatus.OK, r.text
    assert len(calls) == 3
    # shot 0 uses its explicit seed; shots 1 & 2 chain off the uploaded last frame
    assert calls[0]["image_url"] == "https://seed/0.png"
    assert calls[1]["image_url"] == "https://cdn/up1.png"   # chained
    assert calls[2]["image_url"] == "https://cdn/up2.png"   # chained
    assert calls[0]["motion_id"] == "M0"
    # final = the last upload (video); cost summed across shots
    assert r.artifact_ref == "https://cdn/up3.png"
    assert r.cost == pytest.approx(0.39)


def test_stitch_partial_failure_returns_what_succeeded(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_KEY", "kid:secret")
    _patch_ffmpeg(monkeypatch)
    calls = []
    monkeypatch.setattr(runner_stitch, "_hf_generate", _ok_gen(calls, fail_at=1))  # 2nd shot fails
    shots = [{"prompt": "a", "image_url": "https://seed/0.png"},
             {"prompt": "b"}, {"prompt": "c"}]
    r = asyncio.run(runner_stitch.invoke_stitch(_spec(), _job(shots, tmp_path),
                                                transport=_mock_transport()))
    assert r.status is ResultStatus.ERROR
    assert "PARTIAL 1/3" in r.text
    assert r.artifact_ref is not None      # the 1 good shot was still concatenated + returned
    assert len(calls) == 2                 # stopped after the failure


def test_stitch_rejects_over_max_segments_before_spending(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_KEY", "kid:secret")
    calls = []
    monkeypatch.setattr(runner_stitch, "_hf_generate", _ok_gen(calls))
    shots = [{"prompt": str(i)} for i in range(5)]
    r = asyncio.run(runner_stitch.invoke_stitch(_spec(max_segments=3), _job(shots, tmp_path)))
    assert r.status is ResultStatus.ERROR
    assert "max_segments" in r.text
    assert calls == []                     # fail-closed: nothing generated, nothing charged


def test_stitch_missing_shotlist_is_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_KEY", "kid:secret")
    job = Job(id=uuid.uuid4(), to_agent="stitcher", prompt="x", context={},
              worktree_path=str(tmp_path), timeout_s=300)
    r = asyncio.run(runner_stitch.invoke_stitch(_spec(), job))
    assert r.status is ResultStatus.ERROR
    assert "shotlist" in r.text


def test_stitch_missing_key_is_terminal(tmp_path, monkeypatch):
    monkeypatch.delenv("HF_KEY", raising=False)
    r = asyncio.run(runner_stitch.invoke_stitch(_spec(), _job([{"prompt": "a"}], tmp_path)))
    assert r.status is ResultStatus.ERROR
    assert "API key" in r.text


def test_stitch_resume_skips_rendered_segments(tmp_path, monkeypatch):
    """A pre-existing manifest pointing at an on-disk clip is reused (not re-charged)."""
    monkeypatch.setenv("HF_KEY", "kid:secret")
    _patch_ffmpeg(monkeypatch)
    # seed a manifest + a fake existing clip for shot 0
    seg0 = os.path.join(str(tmp_path), "seg_000.mp4")
    with open(seg0, "wb") as f:
        f.write(b"EXISTING")
    import json
    with open(os.path.join(str(tmp_path), "stitch_manifest.json"), "w") as f:
        json.dump({"0": {"clip_path": seg0, "last_frame_url": "https://cdn/cached.png",
                         "cost": 0.13}}, f)
    calls = []
    monkeypatch.setattr(runner_stitch, "_hf_generate", _ok_gen(calls))
    shots = [{"prompt": "a", "image_url": "https://seed/0.png"}, {"prompt": "b"}]
    r = asyncio.run(runner_stitch.invoke_stitch(_spec(), _job(shots, tmp_path),
                                                transport=_mock_transport()))
    assert r.status is ResultStatus.OK, r.text
    assert len(calls) == 1                 # only shot 1 generated; shot 0 resumed
    assert calls[0]["image_url"] == "https://cdn/cached.png"   # chained off the cached last frame


# -- motion_id wiring (invoke_higgsfield through the shared helper) ---------
def test_motion_id_lands_in_submit_body(monkeypatch):
    from runtime import runner
    monkeypatch.setenv("HF_KEY", "kid:secret")
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and str(req.url).endswith("/dop/lite"):
            import json as _j
            captured.update(_j.loads(req.content))
            return httpx.Response(200, json={"request_id": "r1",
                                             "status_url": "https://platform.higgsfield.ai/requests/r1/status"})
        return httpx.Response(200, json={"status": "completed",
                                         "video": {"url": "https://cdn/out.mp4"}})

    spec = AgentSpec(agent_id="higgsfield", reach=Reach.API, authority=Authority.RESPONDER,
                     model="dop-lite", role="v",
                     adapter={"kind": "higgsfield", "base": "https://platform.higgsfield.ai",
                              "secret_ref": "HF_KEY", "application": "/higgsfield-ai/dop/lite"})
    job = Job(id=uuid.uuid4(), to_agent="higgsfield", prompt="reveal",
              context={"image_url": "https://example.com/x.png", "motion_id": "AGENT-REVEAL-UUID",
                       "motion_strength": 0.4}, timeout_s=60)
    r = asyncio.run(runner.invoke_higgsfield(spec, job, transport=httpx.MockTransport(handler)))
    assert r.status is ResultStatus.OK, r.text
    assert captured.get("motion_id") == "AGENT-REVEAL-UUID"
    assert captured.get("motion_strength") == 0.4


# -- real ffmpeg fixtures (skip if ffmpeg absent) --------------------------
_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _make_clip(path, color):
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                    "-i", f"color=c={color}:s=320x240:d=0.5:r=30",
                    "-pix_fmt", "yuv420p", "-an", path], capture_output=True, check=True)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
def test_ffmpeg_concat_and_last_frame_real(tmp_path):
    a, b = str(tmp_path / "a.mp4"), str(tmp_path / "b.mp4")
    _make_clip(a, "red")
    _make_clip(b, "blue")
    assert ffmpeg_util.clips_uniform([a, b]) is True
    out = ffmpeg_util.concat([a, b], str(tmp_path / "cat.mp4"))
    assert os.path.getsize(out) > 0
    png = ffmpeg_util.last_frame_png(a, str(tmp_path / "last.png"))
    assert os.path.getsize(png) > 0


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
def test_ffmpeg_image_to_clip_real(tmp_path):
    card = str(tmp_path / "card.png")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.1",
                    "-frames:v", "1", card], capture_output=True, check=True)
    clip = ffmpeg_util.image_to_clip(card, str(tmp_path / "card.mp4"), duration=1.0, size=(320, 240))
    assert os.path.getsize(clip) > 0
