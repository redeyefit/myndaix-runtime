"""MX critic pure core — frame_stats + critic_generic over synthetic raw rgb24 frames.
Dependency-free (no numpy/Pillow/ffmpeg). Run: PYTHONPATH=src python3 tests/test_critic.py
"""
import runtime.critic as C

PASS = [0]
FAIL = [0]


def ok(cond, label):
    if cond:
        PASS[0] += 1
    else:
        FAIL[0] += 1
        print("  FAIL:", label)


W, H = 64, 36                                    # matches the orchestrator's ffmpeg grab size
HEXES = ["#0A0A0A", "#1A1D22", "#5AE0A0"]
_PAL = [C.parse_hex(h) for h in HEXES]


def _flat(rgb):
    return bytes(list(rgb)) * (W * H)


def _from_fn(fn):
    out = bytearray()
    for y in range(H):
        for x in range(W):
            r, g, b = fn(x, y)
            out += bytes((max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b))))
    return bytes(out)


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _onpalette_varied(x, y):
    # smooth horizontal blend between brand-hex PAIRS (per row) -> many distinct colors, low
    # horizontal-edge (text_frac), endpoints/most pixels on-palette -> PASS
    pairs = [(_PAL[0], _PAL[1]), (_PAL[1], _PAL[2]), (_PAL[2], _PAL[0])]
    a, b = pairs[y % len(pairs)]
    return _lerp(a, b, x / (W - 1))


def _offpalette_gradient(x, y):
    # bright orange/red/yellow, varies BOTH axes (distinct + variance), smooth, FAR from brand -> warn drift
    t = x / (W - 1)
    return (int(180 + 70 * t), int(60 + 120 * (y / (H - 1))), int(30 * (1 - t)))


def _texty(x, y):
    # per-row varied base (distinct) + sharp per-COLUMN brightness flip (high text_frac) -> warn text
    base = ((y * 9) % 220, (y * 5) % 180, (y * 3 + 40) % 200)
    bright = 180 if (x % 2 == 0) else 0
    return (min(255, base[0] + bright), base[1], base[2])


def test_parse_hex():
    ok(C.parse_hex("#5AE0A0") == (90, 224, 160), "hex parse")
    ok(C.parse_hex("0a0a0a") == (10, 10, 10), "hex parse no-hash lowercase")
    raised = False
    try:
        C.parse_hex("xyz")
    except ValueError:
        raised = True
    ok(raised, "bad hex raises")


def test_flat_is_trivial_fail():
    v = C.critic_generic(_flat((10, 10, 10)), W, H, hexes=HEXES)
    ok(v["status"] == "fail", "flat near-black frame -> FAIL (trivial/failed render)")
    ok(v["retry_hint"] and v["retry_hint"]["motion_strength_delta"] < 0,
       "fail carries a one-variable (motion_strength) retry hint")
    ok(C.critic_generic(b"", 0, 0, hexes=HEXES)["status"] == "fail", "empty frame -> FAIL")
    ok(C.critic_generic(_flat((128, 200, 90)), W, H, hexes=HEXES)["status"] == "fail",
       "flat mid-color (zero variance) -> FAIL even if bright")


def test_onpalette_varied_passes():
    st = C.frame_stats(_from_fn(_onpalette_varied), W, H, HEXES)
    ok(st["distinct_colors"] >= C.DEFAULTS["MIN_DISTINCT"], f"varied -> distinct ok ({st['distinct_colors']})")
    ok(st["luma_var"] >= C.DEFAULTS["MIN_LUMA_VAR"], f"varied -> non-trivial variance ({st['luma_var']})")
    v = C.critic_generic(_from_fn(_onpalette_varied), W, H, hexes=HEXES)
    ok(v["status"] == "pass", f"on-palette varied smooth frame -> PASS (got {v['status']}: {v['reasons']})")


def test_offpalette_gradient_warns_drift():
    v = C.critic_generic(_from_fn(_offpalette_gradient), W, H, hexes=HEXES)
    ok(v["status"] != "fail", "smooth off-palette gradient is non-trivial (not a fail)")
    ok(v["status"] == "warn" and any("palette drift" in r for r in v["reasons"]),
       f"off-brand colors -> WARN palette drift (got {v['status']}: {v['reasons']})")


def test_texty_warns_text():
    st = C.frame_stats(_from_fn(_texty), W, H, HEXES)
    v = C.critic_generic(_from_fn(_texty), W, H, hexes=HEXES)
    ok(st["text_frac"] > C.DEFAULTS["MAX_TEXT_FRAC"], f"sharp columns -> high text_frac ({st['text_frac']})")
    ok(v["status"] == "warn" and any("text" in r for r in v["reasons"]),
       f"text-like frame -> WARN possible text (got {v['status']}: {v['reasons']})")


def test_short_buffer_is_safe():
    # a truncated buffer (fewer bytes than w*h*3) must not crash -> treated as empty/fail
    ok(C.critic_generic(b"\x10\x20", W, H, hexes=HEXES)["status"] == "fail", "short buffer -> FAIL, no crash")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print(f"ALL PASS ({PASS[0]} checks)" if FAIL[0] == 0 else f"FAILED ({FAIL[0]})")
    raise SystemExit(1 if FAIL[0] else 0)


if __name__ == "__main__":
    main()
