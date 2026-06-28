"""Pure, DB-free logic for the +learning rung's review-skill selection + safety scan.

Kept separate from postgres_store / skillselect / controller so the matching + injection
logic is unit-testable WITHOUT a database (mirrors automerge.classify_diff being a pure,
adversarially-tested core). Imported by:
  - runtime.ledger.postgres_store.select_skills  — path-segment match + specificity ordering
  - runtime.skillselect                          — inject-time injection tripwire
  - runtime.controller (indexer lint)            — banned-trigger + injection scan at promotion

DESIGN: docs/learning-rung-plan.md Step 2/3/5 + design v0.3 #6 (path-segment matching) and
v0.3.2 (the injection tripwire, borrowed from openclaw and NARROWED for our context).
"""
from __future__ import annotations

import re
from fnmatch import fnmatchcase

__all__ = ["is_banned_trigger", "seg_match", "specificity", "scan_injection", "INJECTION_PATTERNS"]


# ---- path-segment trigger matching (design v0.3 #6) ------------------------------------
# A path_trigger matches a changed path by SEGMENT, never fnmatch-across-"/": split both on
# "/", require EQUAL segment count, fnmatch each segment. So `src/*.py` matches `src/a.py`
# but NOT `src/sub/a.py` (plain fnmatch's "*" would otherwise cross "/" and over-match).

def is_banned_trigger(trigger: str) -> bool:
    """A trigger too broad to allow — it would attach to ~every review and starve specific
    skills under the LIMIT 2 selection (Oracle/codex). Banned: empty; any `**` segment
    (cross-segment wildcard has no place in segment matching); and any BARE `*` segment, so
    `*`, `*/*`, `dir/*`, `src/*` are all rejected — every segment must carry a literal. A
    pattern like `src/*.py` (segment `*.py`) is allowed."""
    t = trigger.strip()
    if not t:
        return True
    segs = t.split("/")
    return any(s == "*" or s == "**" for s in segs)


def seg_match(trigger: str, path: str) -> bool:
    """True iff `trigger` matches `path` by path-SEGMENT (equal depth, per-segment
    case-sensitive fnmatch, `*` never crossing "/"). Caller should have rejected the
    trigger via is_banned_trigger() first."""
    tsegs = trigger.strip().split("/")
    psegs = path.strip().split("/")
    if len(tsegs) != len(psegs):
        return False
    return all(fnmatchcase(p, t) for t, p in zip(tsegs, psegs))


def specificity(trigger: str) -> int:
    """A trigger's specificity = count of segments with NO wildcard char (more literal
    segments = more specific). The middle ORDER BY key (after new-first, before recency)
    so specific triggers beat broad ones at LIMIT 2 (Oracle fairness fold)."""
    return sum(0 if any(c in s for c in "*?[") else 1 for s in trigger.strip().split("/"))


# ---- injection tripwire (design v0.3.2, from openclaw — NARROWED for our context) -------
# Deterministic, fail-closed scan for prompt-injection FRAMING in a skill body. Defense in
# depth ON TOP of the nonce-fence: the fence makes the body DATA; this drops an obviously
# adversarial body before it reaches a reviewer (skillselect) or before it is ever promoted
# to `active` (controller index lint). Mirrors openclaw's hasReviewerDirective tripwire.
#
# DELIBERATELY NARROW: a legitimate REVIEW skill is DESCRIPTIVE about code review — it will
# naturally say "flag any `curl ... | sh`", "reject if it auto-approves", "check env vars".
# So we do NOT scan for those content words (openclaw's SKILL_CONTENT_RULES do, because their
# learned skills describe TASKS, not reviews) — they would false-positive the very skills we
# want and make the rung unusable. We catch ONLY directives that try to RE-FRAME the reviewer
# (role-override / "ignore instructions" / system-prompt spoof / fence break), which a genuine
# descriptive review skill has no reason to contain. [Patterns are a security judgment call —
# scrutinize in review; tune toward fewer false-negatives only with evidence.]
INJECTION_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    # Tight on purpose — only the unambiguous injection targets (instruction/prompt/system
    # message), NOT descriptive words a review skill legitimately uses ("ignore the lint
    # rule", "check env vars", "reject if it auto-approves" must all stay CLEAN).
    ("ignore-instructions", re.compile(
        r"\b(ignore|disregard|forget|override)\b[^.\n]{0,20}\b(instruction|prompt|system\s+message)s?\b", re.I)),
    ("role-override", re.compile(
        r"\byou\s+are\s+(now|actually|really)\b|\bact\s+as\b(?![^.\n]{0,20}\bnormal)|\bnew\s+instructions?\s*:|\bfrom\s+now\s+on\b", re.I)),
    ("system-prompt-spoof", re.compile(
        r"\bsystem\s+prompt\b|</?(system|instructions?)>|^\s*(system|assistant)\s*:", re.I | re.M)),
    ("fence-break", re.compile(r"===\s*(BEGIN|END)\s+UNTRUSTED", re.I)),
)


def scan_injection(body: str) -> str | None:
    """Return the NAME of the first injection-framing pattern matching `body`, else None.
    Fail-closed callers DROP (skillselect) / QUARANTINE + alert (controller) on a non-None
    result — a skill body should never try to re-frame the reviewer."""
    for name, pat in INJECTION_PATTERNS:
        if pat.search(body):
            return name
    return None
