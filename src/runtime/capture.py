"""Pure, DB-free core for the auto-capture rung (the proposer).

Kept separate from the ledger/controller so the recurrence keying is unit-testable WITHOUT a DB
(mirrors runtime.skillmatch). The trigger is DETERMINISTIC — no LLM decides recurrence: a changed
file path normalizes to a stable path-GLOB, and (repo, glob) hashes to a recurrence FINGERPRINT.
Same lesson-class -> same fingerprint -> the seen_count accumulates -> propose once at threshold.

DESIGN: docs/auto-capture-design.md. The glob this produces is also the `path_trigger` a proposed
skill would carry, so it MUST be a valid, non-banned segment trigger (runtime.skillmatch).
"""
from __future__ import annotations

import hashlib

from runtime import skillmatch

__all__ = ["path_to_glob", "fingerprint", "candidate_glob"]


def path_to_glob(path: str) -> str:
    """Normalize a changed file path to the path-glob a proposed skill would trigger on: keep the
    directory, generalize the basename to `*.<ext>` (so every file of one kind in a dir shares a
    glob). An extensionless or dotfile basename keeps its literal name (nothing to generalize).
    Pure + deterministic. Returns "" for an empty/degenerate path."""
    p = path.strip().strip("/")
    if not p:
        return ""
    parts = p.split("/")
    base = parts[-1]
    dot = base.rfind(".")
    if dot > 0:                                  # a real extension (not a leading-dot dotfile)
        parts[-1] = "*" + base[dot:]            # foo/0099_x.sql -> foo/*.sql
    return "/".join(parts)


def candidate_glob(path: str) -> str | None:
    """The path-glob for `path` IF it is usable as a skill trigger, else None. Drops a glob that
    skillmatch would BAN (too broad, e.g. a bare `*` segment) so auto-capture never proposes a
    skill that the controller's own promotion lint would reject. Fail-closed: degenerate -> None."""
    g = path_to_glob(path)
    if not g or skillmatch.is_banned_trigger(g):
        return None
    return g


def fingerprint(repo_scope: str, path_glob: str) -> str:
    """Deterministic recurrence key for a (repo, glob) class. NUL-separated so
    ('a','b/c') and ('a/b','c') can't collide. Same class -> same key -> seen_count accumulates."""
    return hashlib.sha256(f"{repo_scope}\x00{path_glob}".encode()).hexdigest()
