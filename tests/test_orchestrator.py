"""MX Quality Orchestrator v1 — pure stages (prompt-director / router) + the full
seed->motion->critic loop with an INJECTED supplier + frame grabber (no ledger, no spend).
Proves: the loop produces a manifest, the cost gate blocks spend, the one-variable bounded
retry fires on a critic FAIL. Run: PYTHONPATH=src python3 tests/test_orchestrator.py
"""
import asyncio

import runtime.orchestrator as O

PASS = [0]
FAIL = [0]


def ok(cond, label):
    if cond:
        PASS[0] += 1
    else:
        FAIL[0] += 1
        print("  FAIL:", label)


W, H = 64, 36
_PAL = [(10, 10, 10), (26, 29, 34), (90, 224, 160)]


def _good_frame():
    """A brand-hex-blend frame the critic PASSes (same construction as test_critic)."""
    pairs = [(_PAL[0], _PAL[1]), (_PAL[1], _PAL[2]), (_PAL[2], _PAL[0])]
    out = bytearray()
    for y in range(H):
        a, b = pairs[y % 3]
        for x in range(W):
            t = x / (W - 1)
            out += bytes(int(a[i] + (b[i] - a[i]) * t) for i in range(3))
    return bytes(out), W, H


def _flat_frame():
    return bytes((10, 10, 10)) * (W * H), W, H


def _supplier(record, *, cost=0.4, url="https://res.cloudinary.com/x/plate.mp4"):
    async def fn(context, deadline):
        record.append(dict(context))
        return {"artifact_ref": url, "cost": cost}
    return fn


def _grab(frame):
    async def fn(plate_ref):
        return frame
    return fn


# ---- stage 1: prompt-director ----
def test_build_prompt():
    s = O.build_prompt("a neon city at dusk", "myndaix")
    ok("a neon city at dusk" == s["caption"], "caption = brief verbatim")
    ok("#5AE0A0" in s["prompt"] and "#0A0A0A" in s["prompt"], "brand HEX LOCKS in prompt")
    ok("no on-screen text" in s["prompt"] and "no logo" in s["prompt"], "standing NEGATIVES present")
    ok("Arri Alexa Mini LF" in s["prompt"], "camera LOCK present")
    for brief, brand, why in [("", "myndaix", "empty brief"), ("x", "nope", "unknown brand")]:
        raised = False
        try:
            O.build_prompt(brief, brand)
        except ValueError:
            raised = True
        ok(raised, f"fail-closed on {why}")


# ---- stage 2: model-router ----
def test_route():
    s = O.build_prompt("brief", "myndaix")
    r = O.route(s, role="hero")
    ok(r["motion_name"] == "Dolly In", "hero default motion")
    ok(r["motion_id"] == O.MOTION_CATALOG["Dolly In"], "router forwards the UUID, not the name")
    ok(len(r["motion_id"]) == 36 and "-" in r["motion_id"], "motion_id is a UUID")
    ok(O.route(s, role="filler")["motion_name"] == "Static", "filler default motion")
    ok(O.route(s, role="hero", motion="Zoom In")["motion_id"] == O.MOTION_CATALOG["Zoom In"], "explicit motion")
    raised = False
    try:
        O.route(s, motion="Nonexistent Move")
    except ValueError:
        raised = True
    ok(raised, "unknown motion -> fail-closed")
    ok(O.estimate_cost(O.route(s, role="hero")) > O.estimate_cost(O.route(s, role="filler")),
       "hero costs more than filler (estimate)")


# ---- the loop ----
def test_loop_happy_path():
    calls = []
    m = asyncio.run(O.OrchestratorDriver().run(
        "neon city", brand="myndaix", image_url="https://res.cloudinary.com/x/seed.png",
        approve=lambda p: True, supplier=_supplier(calls), frame_grabber=_grab(_good_frame()),
        shot_id="city-01"))
    ok(m["status"] == "ok", f"loop completes ok (got {m.get('status')}: {m.get('reason')})")
    ok(m["plate_url"] == "https://res.cloudinary.com/x/plate.mp4", "manifest plate_url = supplier artifact")
    ok(m["seed_still"] == "https://res.cloudinary.com/x/seed.png", "manifest seed_still = input image")
    ok(m["applied_locks"]["motion_id"] == O.MOTION_CATALOG["Dolly In"], "applied_locks carries the UUID")
    ok(m["applied_locks"]["hexes"] == ["#0A0A0A", "#1A1D22", "#5AE0A0"], "applied_locks carries brand hexes")
    ok(m["critic"]["status"] == "pass", f"critic passed on a good plate ({m['critic']['status']})")
    ok(m["cost"] == 0.4 and m["retries"] == 0, "cost accumulated, no retries on a clean pass")
    ok(len(calls) == 1, "supplier called exactly once")
    ok(calls[0]["image_url"] == "https://res.cloudinary.com/x/seed.png", "supplier got the seed image_url")
    ok("no on-screen text" in calls[0]["prompt"], "supplier prompt forbids text")


def test_cost_gate_blocks_spend():
    calls = []
    m = asyncio.run(O.OrchestratorDriver().run(
        "neon city", image_url="https://res.cloudinary.com/x/seed.png",
        approve=lambda p: False, supplier=_supplier(calls), frame_grabber=_grab(_good_frame())))
    ok(m["status"] == "aborted", "unapproved gate -> aborted")
    ok(len(calls) == 0, "supplier NEVER called when the cost gate is not approved (no spend)")


def test_critic_fail_retries_then_needs_human():
    calls = []
    m = asyncio.run(O.OrchestratorDriver().run(
        "neon city", image_url="https://res.cloudinary.com/x/seed.png", approve=lambda p: True,
        supplier=_supplier(calls), frame_grabber=_grab(_flat_frame()),
        motion_strength=0.6, max_retries=2))
    ok(m["status"] == "needs_human", "persistent critic FAIL -> needs_human")
    ok(len(calls) == 3, "stage 3 ran 1 + 2 retries = 3 charged attempts")
    strengths = [c["motion_strength"] for c in calls]
    ok(strengths == sorted(strengths, reverse=True) and strengths[0] > strengths[-1],
       f"retry changes exactly ONE variable (motion_strength), descending: {strengths}")
    ok(len(m["attempts"]) == 3, "needs_human manifest lists every attempt")


def test_warn_is_not_fail():
    # an off-palette plate WARNs (drift) but still completes ok (warn != fail), no retry
    def _offpal():
        out = bytearray()
        for y in range(H):
            for x in range(W):
                t = x / (W - 1)
                out += bytes((int(180 + 70 * t), int(60 + 120 * (y / (H - 1))), int(30 * (1 - t))))
        return bytes(out), W, H
    calls = []
    m = asyncio.run(O.OrchestratorDriver().run(
        "neon city", image_url="https://res.cloudinary.com/x/seed.png", approve=lambda p: True,
        supplier=_supplier(calls), frame_grabber=_grab(_offpal())))
    ok(m["status"] == "ok", "a WARN plate still completes ok")
    ok(m["critic"]["status"] == "warn" and m["retries"] == 0, "warn surfaced, no retry burned")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print(f"ALL PASS ({PASS[0]} checks)" if FAIL[0] == 0 else f"FAILED ({FAIL[0]})")
    raise SystemExit(1 if FAIL[0] else 0)


if __name__ == "__main__":
    main()
