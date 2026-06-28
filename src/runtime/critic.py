"""Pure, dependency-free measured critic for the MX Quality Orchestrator (stage 4).

"Measure, don't eyeball" (mirrors mx-engine verify_render.check_no_clip's (ok, message)
discipline). Operates on a RAW RGB frame (bytes, w, h) — the orchestrator extracts one
downscaled frame from the supplier plate via ffmpeg (`-pix_fmt rgb24 -f rawvideo`), so this
core needs NO numpy/Pillow/ffmpeg and is unit-testable on synthetic byte frames.

v1 gate (background-plate-only): HARD-FAIL only on a missing / trivial (flat, near-empty)
plate — the thing that means generation actually failed. Palette-drift and text-presence are
ADVISORY WARNs, not fails: per the build corollary, v1 prompts the supplier to emit no text and
generates background only, so a cheap text-region FLAG (not a hard gate) is the right scope; a
calibrated hard text/face gate is v2. NO LLM in the decision.

DESIGN: docs/MX_QUALITY_ORCHESTRATOR.md §6 + corollary 8.
"""
from __future__ import annotations

__all__ = ["parse_hex", "frame_stats", "critic_generic", "DEFAULTS"]

DEFAULTS = {
    "MIN_DISTINCT": 12,      # quantized (4-bit/chan) distinct colors below this = trivial/flat plate
    "MIN_LUMA_VAR": 80.0,    # luma variance below this = a near-flat frame (failed render)
    "PALETTE_TOL": 70,       # RGB euclidean distance counting a pixel as "on brand palette"
    "MIN_PALETTE_FRAC": 0.10,    # below this fraction on-palette -> WARN palette drift (advisory)
    "TEXT_EDGE_THRESH": 48,  # per-pixel horizontal luma delta counting as a hard edge
    "MAX_TEXT_FRAC": 0.14,   # high-edge fraction above this -> WARN possible text/UI (advisory)
}


def parse_hex(h: str) -> tuple[int, int, int]:
    """'#5AE0A0' or '5ae0a0' -> (r,g,b). Raises ValueError on a malformed hex."""
    s = h.strip().lstrip("#")
    if len(s) != 6:
        raise ValueError(f"bad hex {h!r}")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def _luma(r: int, g: int, b: int) -> int:
    return (r * 299 + g * 587 + b * 114) // 1000


def frame_stats(rgb: bytes, w: int, h: int, hexes: list[str]) -> dict:
    """Measure ONE raw rgb24 frame. Pure arithmetic over the byte array (no deps).
    Returns distinct_colors, luma_var, palette_frac (frac of px near a brand hex), text_frac
    (frac of horizontally high-contrast px ≈ text/UI heuristic), and pixel count."""
    n = w * h
    if n <= 0 or len(rgb) < n * 3:
        return {"pixels": 0, "distinct_colors": 0, "luma_var": 0.0,
                "palette_frac": 0.0, "text_frac": 0.0}
    pal = [parse_hex(x) for x in (hexes or [])]
    tol2 = DEFAULTS["PALETTE_TOL"] ** 2
    edge_thr = DEFAULTS["TEXT_EDGE_THRESH"]

    seen: set[int] = set()
    luma_sum = 0
    luma_sq = 0
    on_pal = 0
    high_edge = 0
    lumas = [0] * n
    for i in range(n):
        o = i * 3
        r, g, b = rgb[o], rgb[o + 1], rgb[o + 2]
        seen.add((r >> 4 << 8) | (g >> 4 << 4) | (b >> 4))   # 4-bit/chan quantize
        lu = _luma(r, g, b)
        lumas[i] = lu
        luma_sum += lu
        luma_sq += lu * lu
        for (pr, pg, pb) in pal:
            if (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2 <= tol2:
                on_pal += 1
                break
    # horizontal high-contrast edges (row-wise), a cheap text/UI proxy
    edges = 0
    for y in range(h):
        base = y * w
        prev = lumas[base]
        for x in range(1, w):
            cur = lumas[base + x]
            if abs(cur - prev) > edge_thr:
                edges += 1
            prev = cur
    high_edge = edges
    mean = luma_sum / n
    var = max(0.0, luma_sq / n - mean * mean)
    edge_denom = max(1, h * (w - 1))
    return {
        "pixels": n,
        "distinct_colors": len(seen),
        "luma_var": round(var, 2),
        "palette_frac": round(on_pal / n, 4),
        "text_frac": round(high_edge / edge_denom, 4),
    }


def critic_generic(rgb: bytes, w: int, h: int, *, hexes: list[str],
                   cfg: dict | None = None) -> dict:
    """Gate a generic background plate frame. HARD-FAIL only on missing/trivial (a failed
    render); palette-drift + text-presence are advisory WARNs. Returns:
      {status: 'pass'|'warn'|'fail', metric: {...}, reasons: [...], retry_hint: {...}|None}
    retry_hint (on FAIL) adjusts exactly ONE variable (motion_strength) per the design's
    bounded-one-variable retry rule."""
    c = {**DEFAULTS, **(cfg or {})}
    st = frame_stats(rgb, w, h, hexes)
    reasons: list[str] = []

    if st["pixels"] == 0:
        return {"status": "fail", "metric": st, "reasons": ["empty/unreadable frame"],
                "retry_hint": {"motion_strength_delta": -0.15}}
    trivial = (st["distinct_colors"] < c["MIN_DISTINCT"] or st["luma_var"] < c["MIN_LUMA_VAR"])
    if trivial:
        reasons.append(f"trivial plate (distinct={st['distinct_colors']}<{c['MIN_DISTINCT']} "
                       f"or luma_var={st['luma_var']}<{c['MIN_LUMA_VAR']}) — likely failed render")
        return {"status": "fail", "metric": st, "reasons": reasons,
                "retry_hint": {"motion_strength_delta": -0.15}}

    if st["palette_frac"] < c["MIN_PALETTE_FRAC"]:
        reasons.append(f"palette drift (on-brand frac {st['palette_frac']} < {c['MIN_PALETTE_FRAC']})")
    if st["text_frac"] > c["MAX_TEXT_FRAC"]:
        reasons.append(f"possible text/UI on plate (edge frac {st['text_frac']} > {c['MAX_TEXT_FRAC']}; "
                       "advisory heuristic — supplier must render NO text)")
    status = "warn" if reasons else "pass"
    return {"status": status, "metric": st, "reasons": reasons, "retry_hint": None}
