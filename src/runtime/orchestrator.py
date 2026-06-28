"""MX Quality Orchestrator v1 — a STANDALONE driver (not a registry agent).

Turns a one-line brand brief into a generated motion plate by replicating Higgsfield's
quality pattern on our own stack: (1) prompt-director expands the brief into a rule-bound
labeled-block prompt with verbatim LOCKS + NEGATIVES, (2) model-router picks a named camera
motion + cost estimate, (HUMAN COST GATE), (3) the supplier generates motion+background, (4)
a measured critic gates the plate. Output = a manifest mx-engine ingests (it composites; the
orchestrator never renders a brand pixel).

Per the build corollaries (docs/MX_QUALITY_ORCHESTRATOR.md), v1:
  * is a standalone module run as `PYTHONPATH=src python3 -m runtime.orchestrator "<brief>"`
    (mirrors cli.py / controller.py) — it holds its OWN PostgresLedger; it is NOT a worker-
    invoked agent (those have no ledger handle and can't submit child jobs);
  * collapses stages 1/2/4 into IN-PROCESS functions; ONLY stage 3 (the supplier — it spends
    money) is a ledger job, to the existing `higgsfield` agent;
  * enforces a HUMAN COST GATE before any spend (the ledger enforces no $ ceiling);
  * sets the stage-3 poll deadline >= the supplier Profile.timeout_s (>=600s) so it never
    false-times-out a render that keeps running AND charging (the cli.py 180s default would).

motion_id is the live `/v1/motions` UUID (verified 2026-06-28: the API field takes the UUID;
"Dolly In" etc. are human labels). image_url is rendered+uploaded UPSTREAM by mx-engine and
handed in (a public Cloudinary secure_url that passes the runner's SSRF guard).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
from typing import Awaitable, Callable, Optional

from runtime import critic
from runtime.registry import get as get_spec

_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_WS = re.compile(r"\s+")
BRIEF_MAX = 500
MAX_RETRIES_CAP = 2          # HARD ceiling on bounded charged retries (design §6)
FRAME_GRAB_TIMEOUT_S = 45.0  # wall clock for the ffmpeg frame grab (anti-hang on a dead URL)
QUEUE_GRACE_S = 180.0        # how long a stage-3 job may sit QUEUED before the render window starts


def _sanitize_brief(brief: str) -> str:
    """Collapse control chars / newlines and cap length on the free-text brief BEFORE it is
    interpolated into the labeled-block prompt. A newline would otherwise let the brief forge a
    new labeled section (e.g. 'STYLE: anime' / 'NEGATIVES: none'), overriding the brand LOCKS
    (cross-family review). Brand LOCKS/NEGATIVES stay in non-user-controlled lines."""
    return _WS.sub(" ", _CTRL.sub(" ", brief or "")).strip()[:BRIEF_MAX].strip()

# ---- pinned motion catalog (live /v1/motions UUIDs, verified 2026-06-28) ------------------
# The API forwards `motion_id` verbatim (runner.py:330) and it must be the UUID, not the name.
MOTION_CATALOG = {
    "Dolly In":      "81ca2cd2-05db-4222-9ba0-a32e5185adfb",
    "Static":        "fa3ddb7c-53ee-4383-aa17-97ae65f180e5",
    "Push To Glass": "30a02896-cdda-469d-9ed9-52cbba1c04a8",
    "Crane Up":      "68af9add-43ea-4261-a706-16b640fdcff9",
    "Zoom In":       "fbcbec5b-30f8-4b17-ba6e-8e8d5b265562",
    "360 Orbit":     "ea035f68-b350-40f1-b7f4-7dff999fdd67",
}
HERO_MOTION = "Dolly In"      # dramatic move for a hero shot
FILLER_MOTION = "Static"     # gentle/neutral for filler

# ---- brand LOCKS (v1 hard-coded; mx-engine owns brands/<slug>.json `cinema` block in v2) --
# Fail-closed: an unknown brand raises — never let the LLM/router invent brand color (the LOCK
# is the cross-shot consistency mechanism). myndaix defaults per the design §4 worked example.
BRAND_DEFAULTS = {
    "myndaix": {
        "hexes": ["#0A0A0A", "#1A1D22", "#5AE0A0"],
        "style": "cinematic, premium, minimal, high-contrast dark tech aesthetic",
        "lighting": "low-key dramatic lighting, soft volumetric haze, subtle rim light",
        "camera_lens": "Arri Alexa Mini LF, 35mm anamorphic",
        "film_stock": "clean digital capture, fine grain, shallow depth of field",
        "banned_tropes": ["no AI-hype clichés", "no glowing-brain imagery",
                          "no generic stock-tech blue"],
    },
}
# standing NEGATIVE block — the supplier renders motion+background ONLY; WE render brand text.
STANDING_NEGATIVES = ["no on-screen text", "no watermark", "no logo", "no warped faces",
                      "no extra fingers", "no text artifacts", "no people"]

# rough display-only cost estimate (credits) — UNVERIFIED; measure live before any cost LOGIC.
COST_EST = {"hero": 40, "filler": 6}

POLL_INTERVAL_S = 3.0
NOMINAL_DURATION_S = 3.0     # DoP/lite nominal; not measured in v1


# ============================ stage 1: prompt-director (in-process) =========================
def build_prompt(brief: str, brand: str) -> dict:
    """Expand the brief into the labeled-block prompt. The ONLY free-text slot is Caption (the
    brief); every brand slot is filled VERBATIM from the brand LOCKS. Fail-closed on an unknown
    brand or a missing LOCK slot (design §4)."""
    brief = _sanitize_brief(brief)
    if not brief:
        raise ValueError("empty brief")
    b = BRAND_DEFAULTS.get(brand)
    if b is None:
        raise ValueError(f"unknown brand {brand!r} (no LOCKS) — add a brand block; fail-closed")
    required = ("hexes", "style", "lighting", "camera_lens", "film_stock", "banned_tropes")
    missing = [k for k in required if not b.get(k)]
    if missing:
        raise ValueError(f"brand {brand!r} missing LOCK slots {missing} — fail-closed")
    negatives = STANDING_NEGATIVES + list(b["banned_tropes"])
    hexes = list(b["hexes"])
    prompt = "\n".join([
        f"Caption: {brief}",
        f"STYLE: {b['style']}",
        "COMPOSITION: cinematic hero composition, rule-of-thirds, deliberate negative space",
        f"SCENE: {brief}",
        f"CINEMATOGRAPHY_AND_LIGHTING: {b['lighting']}",
        f"CAMERA_AND_LENS: {b['camera_lens']}",
        f"PHYSICAL_ATTRIBUTES: {b['film_stock']}",
        f"HEX_VALUES: {', '.join(hexes)}",
        f"NEGATIVES: {', '.join(negatives)}",
    ])
    return {
        "prompt": prompt,
        "caption": brief,
        "locks": {"hexes": hexes, "style": b["style"], "camera_lens": b["camera_lens"]},
        "negatives": negatives,
        "total_shots": 1,
    }


# ============================ stage 2: model-router (in-process) ============================
def route(shot: dict, *, role: str = "hero", motion: Optional[str] = None,
          motion_strength: Optional[float] = None) -> dict:
    """Trivial v1 router: one shot -> the higgsfield (dop/lite image->video) supplier; pick a
    named motion from the pinned catalog (-> its UUID); attach a display-only cost estimate."""
    if role not in ("hero", "filler"):
        raise ValueError(f"bad role {role!r}")
    name = motion or (HERO_MOTION if role == "hero" else FILLER_MOTION)
    if name not in MOTION_CATALOG:
        raise ValueError(f"unknown motion {name!r}; choices: {sorted(MOTION_CATALOG)}")
    ms = 0.5 if motion_strength is None else float(motion_strength)
    return {
        "to_agent": "higgsfield",
        "role": role,
        "motion_name": name,
        "motion_id": MOTION_CATALOG[name],
        "motion_strength": ms,
        "cost_est": COST_EST.get(role, COST_EST["filler"]),
        "prompt": shot["prompt"],
        "locks": shot["locks"],
    }


def estimate_cost(routed: dict) -> float:
    return float(routed["cost_est"])


# ================================ the driver ===============================================
SupplierFn = Callable[[dict, float], Awaitable[dict]]      # (context, deadline) -> {artifact_ref, cost}
FrameGrabber = Callable[[str], Awaitable[tuple]]           # (plate_ref) -> (rgb_bytes, w, h)
ApproveFn = Callable[[dict], bool]                         # (plan) -> approved?


class OrchestratorDriver:
    """Runs stages 1 -> 2 -> [human cost gate] -> 3 (ledger) -> 4 (critic), threading each
    stage's output into the next. Ledger is connected LAZILY (only the real supplier path needs
    it), so the pure loop is testable with an injected supplier + frame grabber and no DB."""

    def __init__(self, dsn: Optional[str] = None):
        self.dsn = dsn
        self._led = None

    async def _ledger(self):
        if self._led is None:
            from runtime.ledger.postgres_store import PostgresLedger
            dsn = self.dsn or "postgresql://localhost/runtime"
            self._led = await PostgresLedger.connect(dsn)
        return self._led

    async def close(self):
        if self._led is not None:
            try:
                await self._led.close()
            finally:
                self._led = None

    # ---- stage 3 (the only ledger job): submit to higgsfield, poll >=600s, read artifact ----
    async def _supplier_ledger(self, context: dict, deadline_s: float) -> dict:
        led = await self._ledger()
        jid = await led.submit_job(to_agent="higgsfield", prompt=context["prompt"],
                                   context=context, created_by="orchestrator",
                                   repo_id=context.get("repo_id"))
        loop = asyncio.get_event_loop()
        # Two windows (cross-family MAJOR): the supplier's render timeout starts when the worker
        # LEASES the job, not at submit. So allow QUEUE_GRACE while QUEUED, then a FULL deadline_s
        # render window from the first leased/running observation — queue delay can't false-time-out
        # a still-charging render. A timeout surfaces the job id (it may have charged); never abandon.
        queue_cap = loop.time() + QUEUE_GRACE_S
        render_cap = None
        st = {}
        while True:
            now = loop.time()
            st = await led.get_status(jid)
            stt = st.get("status") if st else None
            if stt in ("done", "failed", "dead"):
                break
            if stt in ("leased", "running") and render_cap is None:
                render_cap = now + deadline_s
            cap = render_cap if render_cap is not None else queue_cap
            if now >= cap:
                where = "rendering" if render_cap is not None else "queued"
                raise TimeoutError(f"stage-3 job {jid} stuck {where} past its deadline — "
                                   f"recover via `mxr get {jid}` (it may have charged)")
            await asyncio.sleep(POLL_INTERVAL_S)
        if st.get("status") != "done" or not st.get("artifact_ref"):
            err = next((a.get("text") for a in (st.get("attempts") or [])
                        if a.get("status") == "failed" and a.get("text")), st.get("status"))
            raise RuntimeError(f"stage-3 supplier {st.get('status')}: {err}")
        # get_status does NOT expose Result.cost -> v1 manifest uses the gate estimate (Open Q5).
        return {"artifact_ref": st["artifact_ref"], "cost": float(context.get("cost_est", 0.0)),
                "job_id": str(jid)}

    async def _grab_frame(self, plate_ref: str, w: int = 64, h: int = 36) -> tuple:
        """Extract ONE downscaled rgb24 frame from the plate via ffmpeg, for the critic.
        HTTPS-only + SSRF-guarded (a poisoned artifact_ref must not make ffmpeg fetch a
        loopback/file target — the runner does NOT scheme-check the result url) + wall-clock
        bounded (a dead-but-open URL must not hang the driver AFTER a paid render). v1 plates are
        public Higgsfield/Cloudinary https urls; tests inject their own grabber."""
        if not plate_ref.startswith("https://"):
            raise ValueError(f"refusing to grab a non-https plate_ref: {plate_ref!r}")
        from runtime.runner import _reject_unsafe_url
        reason = await _reject_unsafe_url(plate_ref)
        if reason:
            raise ValueError(f"plate_ref rejected (SSRF): {reason}")
        cmd = ["ffmpeg", "-v", "error", "-rw_timeout", "20000000", "-i", plate_ref,
               "-frames:v", "1", "-vf", f"scale={w}:{h}", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=FRAME_GRAB_TIMEOUT_S)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except ProcessLookupError:
                pass
            raise RuntimeError(f"ffmpeg frame-grab timed out after {FRAME_GRAB_TIMEOUT_S:.0f}s")
        if proc.returncode != 0 or len(out) < w * h * 3:
            raise RuntimeError(f"ffmpeg frame-grab failed ({proc.returncode}): {err.decode()[:200]}")
        return out[:w * h * 3], w, h

    async def run(self, brief: str, *, brand: str = "myndaix", image_url: str,
                  approve: ApproveFn, shot_id: str = "shot-01", role: str = "hero",
                  motion: Optional[str] = None, motion_strength: Optional[float] = None,
                  end_image_url: Optional[str] = None, repo_id: Optional[str] = None,
                  max_retries: int = 2,
                  supplier: Optional[SupplierFn] = None,
                  frame_grabber: Optional[FrameGrabber] = None) -> dict:
        supplier = supplier or self._supplier_ledger
        frame_grabber = frame_grabber or self._grab_frame
        hexes = BRAND_DEFAULTS.get(brand, {}).get("hexes", [])
        max_retries = max(0, min(MAX_RETRIES_CAP, int(max_retries)))   # HARD cap on charged retries

        # stage 1 + 2 (free, in-process)
        shot = build_prompt(brief, brand)
        routed = route(shot, role=role, motion=motion, motion_strength=motion_strength)
        cost_est = estimate_cost(routed)

        # ===== HUMAN COST GATE (before ANY spend) =====
        # disclose the WORST-CASE spend: the loop may run 1 + max_retries charged attempts, so the
        # gate must approve the max, not a single attempt (cross-family CRITICAL).
        max_attempts = max_retries + 1
        plan = {"brief": brief, "brand": brand, "shot_id": shot_id, "image_url": image_url,
                "motion": routed["motion_name"], "motion_id": routed["motion_id"],
                "motion_strength": routed["motion_strength"], "estimated_cost": cost_est,
                "max_attempts": max_attempts,
                "max_estimated_cost": round(cost_est * max_attempts, 4)}
        if not approve(plan):
            return {"status": "aborted", "reason": "cost gate not approved", "plan": plan}

        # ===== stage 3 (supplier, spends) + stage 4 (critic), bounded one-variable retry =====
        spec = get_spec("higgsfield")
        prof_to = getattr(getattr(spec, "profile", None), "timeout_s", 600) if spec else 600
        deadline = max(600.0, float(prof_to or 600))
        ms = routed["motion_strength"]
        attempts = []
        result = None
        for attempt in range(max_attempts):
            ctx = {"image_url": image_url, "prompt": routed["prompt"],
                   "motion_id": routed["motion_id"], "motion_strength": ms,
                   "cost_est": cost_est, "repo_id": repo_id}
            if end_image_url:
                ctx["end_image_url"] = end_image_url
            try:
                sup = await supplier(ctx, deadline)
                rgb, w, h = await frame_grabber(sup["artifact_ref"])
            except Exception as e:                       # noqa: BLE001
                # a supplier/frame-grab failure is NOT a critic FAIL: do NOT re-run stage 3 (a
                # re-submit of a non-idempotent paid supplier could double-charge). Record + surface
                # to a human with the context — never crash the driver (cross-family + workflow MAJOR).
                attempts.append({"motion_strength": ms, "cost": 0.0, "critic": "error",
                                 "reasons": [f"{type(e).__name__}: {e}"]})
                return {"status": "needs_human", "reason": f"stage-3/critic error: {e}",
                        "attempts": attempts, "plan": plan}
            verdict = critic.critic_generic(rgb, w, h, hexes=hexes)
            attempts.append({"motion_strength": ms, "cost": sup.get("cost", 0.0),
                             "critic": verdict["status"], "reasons": verdict["reasons"]})
            if verdict["status"] != "fail":
                result = (sup, verdict)
                break
            hint = verdict.get("retry_hint") or {}
            ms = round(max(0.0, ms + hint.get("motion_strength_delta", -0.15)), 3)   # ONE variable
        if result is None:
            return {"status": "needs_human", "reason": "critic FAIL after retries",
                     "attempts": attempts, "plan": plan}

        sup, verdict = result
        total_cost = round(sum(a.get("cost", 0.0) for a in attempts), 4)
        return {
            "status": "ok",
            "plate_url": sup["artifact_ref"],
            "shot_id": shot_id,
            "duration": NOMINAL_DURATION_S,
            "render_type": "generic",
            "applied_locks": {"hexes": hexes, "camera_preset": routed["motion_name"],
                              "motion_id": routed["motion_id"],
                              "motion_strength": ms, "brand": brand},
            "seed_still": image_url,
            "cost": total_cost,
            "critic": {"status": verdict["status"], "metric": verdict["metric"],
                       "reasons": verdict["reasons"]},
            "prompt": routed["prompt"],
            "retries": len(attempts) - 1,
        }


# ================================ CLI entrypoint ===========================================
def _interactive_approve(plan: dict) -> bool:
    print("\n=== MX QUALITY ORCHESTRATOR — COST GATE (no spend until you approve) ===",
          file=sys.stderr)
    for k in ("brief", "brand", "shot_id", "motion", "motion_strength", "estimated_cost", "image_url"):
        print(f"  {k:16}: {plan.get(k)}", file=sys.stderr)
    try:
        ans = input("approve spend? [y/N] ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


async def _amain(args) -> int:
    drv = OrchestratorDriver(dsn=args.dsn)
    approve = (lambda _p: True) if args.yes else _interactive_approve
    try:
        manifest = await drv.run(
            args.brief, brand=args.brand, image_url=args.image_url, approve=approve,
            shot_id=args.shot_id, role=args.role, motion=args.motion,
            motion_strength=args.motion_strength, end_image_url=args.end_image_url,
            repo_id=args.repo)
    finally:
        await drv.close()
    print(json.dumps(manifest, indent=2))
    return 0 if manifest.get("status") == "ok" else 1


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m runtime.orchestrator",
                                description="MX Quality Orchestrator v1 — brief -> motion plate")
    p.add_argument("brief", help="one-line brand brief")
    p.add_argument("--brand", default="myndaix")
    p.add_argument("--image-url", dest="image_url", required=True,
                   help="public seed still URL (rendered+uploaded by mx-engine; image->video)")
    p.add_argument("--shot-id", dest="shot_id", default="shot-01")
    p.add_argument("--role", default="hero", choices=["hero", "filler"])
    p.add_argument("--motion", default=None, help=f"motion name (default by role); one of {sorted(MOTION_CATALOG)}")
    p.add_argument("--motion-strength", dest="motion_strength", type=float, default=None)
    p.add_argument("--end-image-url", dest="end_image_url", default=None)
    p.add_argument("--repo", default=None, help="repo_id scope for the stage-3 job")
    p.add_argument("--dsn", default=None, help="MYNDAIX_DSN override")
    p.add_argument("--yes", action="store_true", help="auto-approve the cost gate (non-interactive)")
    args = p.parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
