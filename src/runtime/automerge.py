"""automerge.py — docs-only PR auto-merge gate ("the brain" rung 4).

A bounded, non-Claude (launchd, hourly) sibling of the controller-loop. Each tick it
lists open same-repo PRs against main and AUTO-MERGES the ones that pass a hard, all-must-
pass gate — the FIRST removal of the human merge gate. v1 safe class = DOCS-ONLY.

THE security model (DESIGN v0.3): the mechanical diff-class gate (classify_diff) is the
SECURITY boundary — it proves every changed file is an inert, non-instruction `.md`. The
LLM review and CI are quality/defense layers. So even a prompt-injected "PASS" can only
merge a revertible docs sentence. Everything here is OFF by default, capped, revertible.

Run one tick:
    MYNDAIX_DSN=... GH_TOKEN=... PYTHONPATH=src python3 -m runtime.automerge tick
Dry-run (decide + log, merge nothing):
    MYNDAIX_AUTOMERGE_DRY_RUN=1 ... python3 -m runtime.automerge tick
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from runtime.ledger.postgres_store import PostgresLedger

# -- config --------------------------------------------------------------------
DSN = os.environ.get("MYNDAIX_DSN", "postgresql://localhost/runtime")
HOME = Path(os.environ.get("HOME", str(Path.home())))
ORCH = HOME / ".myndaix" / "orchestrator"
STATE = ORCH / "state"
REPOS_JSON = Path(os.environ.get("MYNDAIX_REPOS_JSON", str(ORCH / "repos.json")))
PLAY_REVIEW = Path(os.environ.get("PLAY_SELF", str(ORCH / "play-review.sh")))
LOCK = ORCH / "automerge.lock"
ENABLED_FLAG = ORCH / "AUTOMERGE_ENABLED"

BASE_REF = "refs/heads/main"
MAX_PER_TICK = int(os.environ.get("MYNDAIX_AUTOMERGE_MAX_TICK", "1"))
MAX_PER_DAY = int(os.environ.get("MYNDAIX_AUTOMERGE_MAX_DAY", "3"))
MAX_PER_AUTHOR_DAY = int(os.environ.get("MYNDAIX_AUTOMERGE_MAX_AUTHOR_DAY", "1"))
AUTHOR_ALLOWLIST = set(
    (os.environ.get("MYNDAIX_AUTOMERGE_AUTHORS", "redeyefit")).split(","))
GH_TIMEOUT = int(os.environ.get("MYNDAIX_AUTOMERGE_GH_TIMEOUT", "30"))
REVIEW_TIMEOUT = int(os.environ.get("MYNDAIX_AUTOMERGE_REVIEW_TIMEOUT", "600"))
RATE_FLOOR = int(os.environ.get("MYNDAIX_AUTOMERGE_RATE_FLOOR", "100"))

DRY_RUN = os.environ.get("MYNDAIX_AUTOMERGE_DRY_RUN") == "1"
TEST_MODE = os.environ.get("MYNDAIX_AUTOMERGE_TEST_MODE") == "1"
MERGE_OVERRIDE = os.environ.get("MYNDAIX_AUTOMERGE_MERGE_OVERRIDE", "")  # test seam: record, don't merge

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Files that are NOT inert docs even though they end in .md — they are read as live
# instructions/config by an agent/tool, or are operational ground truth (DESIGN v0.3 §3).
# A change to one of these is routed to a human even within the docs-only class.
_DENY_DIRS = {".github", ".claude", ".codex", ".cursor", ".agents", "rules", "skills", "prompts"}
_DENY_BASENAMES = {"CLAUDE.md", "AGENTS.md", "GEMINI.md", "CODEOWNERS", "DESIGN.md", ".cursorrules"}


def log(msg: str) -> None:
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [automerge] {msg}", flush=True)


# =====================================================================================
# PURE gate core — the SECURITY boundary. No I/O; adversarially unit-tested.
# =====================================================================================
def is_denylisted(path: str) -> bool:
    """A `.md` that is an instruction/config/ground-truth file → never auto-merge."""
    parts = path.split("/")
    base = parts[-1]
    dirs = parts[:-1]
    if any(d in _DENY_DIRS for d in dirs):
        return True
    if base in _DENY_BASENAMES:
        return True
    low = base.lower()
    if low.startswith(("copilot", "security")) and low.endswith(".md"):
        return True
    if low.endswith(("-design.md", "-spec.md")):     # operational ground-truth docs (v1-conservative)
        return True
    if path == "docs/OPERATING.md":
        return True
    return False


def _doc_path(path: str) -> bool:
    """Strictly an inert markdown file: the raw path ends in lowercase `.md` and is not
    denylisted. Case/homoglyph-strict on purpose (a `.MD`/`.md ` variant is rejected)."""
    return path.endswith(".md") and not is_denylisted(path)


def parse_raw_z(out: bytes) -> list[dict]:
    """Parse `git diff --raw -z -M base..H` into entries. Each entry:
    {omode, nmode, status, paths:[...]} — paths has 2 for rename/copy (old,new) else 1."""
    toks = out.split(b"\x00")
    entries: list[dict] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if not t.startswith(b":"):
            i += 1
            continue
        fields = t[1:].split(b" ")
        if len(fields) < 5:
            i += 1
            continue
        omode, nmode, _osha, _nsha, status = fields[0], fields[1], fields[2], fields[3], fields[4]
        st = status.decode("utf-8", "replace")
        n = 2 if st[:1] in ("R", "C") else 1
        raw_paths = toks[i + 1:i + 1 + n]
        if len(raw_paths) < n:
            break  # truncated/garbage → caller rejects (empty/short)
        entries.append({
            "omode": omode.decode(), "nmode": nmode.decode(), "status": st,
            "paths": [p.decode("utf-8", "replace") for p in raw_paths],
        })
        i += 1 + n
    return entries


def classify_diff(entries: list[dict]) -> tuple[bool, str]:
    """THE security gate. True iff every entry is an add/modify/delete of an inert,
    non-denylisted `.md` regular file. Rejects symlink(120000)/gitlink(160000)/executable
    (100755) modes, non-.md paths on EITHER rename side, denylisted paths, and empty diffs."""
    if not entries:
        return False, "empty changeset"
    for e in entries:
        st, omode, nmode, paths = e["status"], e["omode"], e["nmode"], e["paths"]
        # every path on every side must be an inert .md (denylist applies to both rename sides)
        for p in paths:
            if not _doc_path(p):
                return False, f"non-doc or denylisted path: {p!r}"
        if st.startswith("D"):                       # delete: dest mode 000000, src must have been a doc blob
            if nmode != "000000" or omode != "100644":
                return False, f"unsafe delete mode {omode}->{nmode} for {paths}"
        else:                                        # add/modify/rename/copy: dest must be a regular non-exec blob
            if nmode != "100644":
                return False, f"unsafe dest mode {nmode} for {paths} (reject exec/symlink/gitlink)"
    return True, "docs-only"


# =====================================================================================
# I/O: git + gh, all argv (never shell), output validated.
# =====================================================================================
def _git_env() -> dict:
    env = {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(HOME),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "https:ssh:file",
    }
    for k in ("SSH_AUTH_SOCK", "TMPDIR", "LANG", "GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def _git(repo: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), "--no-pager", *args],
                          capture_output=True, env=_git_env(), timeout=timeout, check=False)


def _gh_json(repo: Path, *args: str, timeout: Optional[int] = None):
    """Run a gh command that prints JSON; return the parsed value or None on failure."""
    r = subprocess.run(["gh", *args], cwd=str(repo), capture_output=True, text=True,
                       env=_git_env(), timeout=timeout or GH_TIMEOUT, check=False)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        return None


# -- config + lock (mirror controller patterns) --------------------------------
def load_repo() -> Optional[dict]:
    """The single watched repo from $ORCH/repos.json: {path, repo_id, nameWithOwner}."""
    try:
        raw = json.loads(REPOS_JSON.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        log(f"repos.json unreadable ({e}) — skip"); return None
    if not isinstance(raw, dict):
        log("repos.json is not an object — skip"); return None
    for key, entry in raw.items():
        if key.startswith("_") or not isinstance(entry, dict):
            continue
        p = entry.get("path")
        if not p:
            continue
        path = Path(p).expanduser().resolve()
        if not (path.is_dir() and (path / ".git").exists()):
            continue
        nwo = _gh_json(path, "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")
        if not isinstance(nwo, str) or "/" not in nwo:
            log(f"{path.name}: cannot resolve nameWithOwner via gh — skip"); continue
        return {"path": path, "repo_id": path.name, "nwo": nwo}
    return None


_LOCK_FD: Optional[int] = None


def acquire_lock() -> bool:
    global _LOCK_FD
    ORCH.mkdir(parents=True, exist_ok=True)
    if LOCK.is_dir():
        shutil.rmtree(LOCK, ignore_errors=True)
    fd = os.open(str(LOCK), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd); log("another tick holds the lock — exiting"); return False
    _LOCK_FD = fd
    return True


def release_lock() -> None:
    global _LOCK_FD
    if _LOCK_FD is not None:
        try:
            fcntl.flock(_LOCK_FD, fcntl.LOCK_UN); os.close(_LOCK_FD)
        except OSError:
            pass
        _LOCK_FD = None


# -- per-day caps (UTC file counters, mirror controller) -----------------------
def _day(suffix: str = "") -> Path:
    d = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d")
    return STATE / f"automerge-day-{d}{suffix}"


def _count(p: Path) -> int:
    try:
        return int(p.read_text().strip() or "0")
    except (OSError, ValueError):
        return 0


def _charge(author: str) -> None:
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        _day().write_text(str(_count(_day()) + 1))
        ap = _day(f"-author-{author}")
        ap.write_text(str(_count(ap) + 1))
    except OSError:
        pass


# -- the per-PR gate -----------------------------------------------------------
def _ci_green(repo: dict, head: str) -> bool:
    """The required `test` check is COMPLETED+SUCCESS for THIS commit. Paginated; defensive."""
    cr = _gh_json(repo["path"], "api", "--paginate",
                  f"repos/{repo['nwo']}/commits/{head}/check-runs", "-q",
                  '[.check_runs[] | select(.name=="test") | {s:.status,c:.conclusion}]')
    if not isinstance(cr, list) or not cr:
        log("  CI: no `test` check-run for this head — fail-closed"); return False
    for c in cr:
        if c.get("s") != "completed" or c.get("c") != "success":
            log(f"  CI: a `test` run is {c} — fail-closed"); return False
    return True


def _review_pass(repo: dict, B: str, H: str, run_id: str) -> bool:
    """Synchronous play-review --gate (PLAY_GATE): merge iff a fresh, structured verdict
    {run_id,B,H,PASS} comes back. Fail-closed on anything else (the §3 gate already bounded
    the blast radius, so this is a quality gate)."""
    vpath = STATE / f"automerge-verdict-{H}.json"
    try:
        vpath.unlink()
    except OSError:
        pass
    if TEST_MODE:                                    # unit-test seam handled by caller (no real review)
        return os.environ.get("MYNDAIX_AUTOMERGE_FAKE_VERDICT") == "PASS"
    env = dict(_git_env())
    env.update({"PLAY_GATE": "1", "PLAY_GATE_VERDICT": str(vpath), "PLAY_GATE_RUN_ID": run_id,
                "PLAY_DISABLE_AUTOFIX": "1", "MYNDAIX_DSN": DSN, "PLAY_SELF": str(PLAY_REVIEW)})
    try:
        subprocess.run([str(PLAY_REVIEW), "--worker", str(repo["path"]), B, H, BASE_REF, ""],
                       cwd=str(repo["path"]), env=env, timeout=REVIEW_TIMEOUT, check=False)
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"  review: play-review --gate failed ({e}) — fail-closed"); return False
    try:
        v = json.loads(vpath.read_text())
    except (OSError, json.JSONDecodeError):
        log("  review: no/invalid verdict file — fail-closed"); return False
    ok = (v.get("run_id") == run_id and v.get("base") == B and v.get("head") == H
          and v.get("verdict") == "PASS")
    if not ok:
        log(f"  review: verdict not a fresh PASS ({v}) — fail-closed")
    return ok


def _merge_queue(repo: dict) -> Optional[bool]:
    """True if main is behind a merge queue (we must refuse — a queued merge escapes the gate)."""
    r = _gh_json(repo["path"], "api", f"repos/{repo['nwo']}/rules/branches/main",
                 "-q", 'any(.[]; .type=="merge_queue")')
    return r if isinstance(r, bool) else None


def evaluate_pr(repo: dict, pr: dict, budget: list) -> Optional[tuple]:
    """Run the 7-gate on one PR; merge if all pass. Pure-sync (git/gh subprocess). Returns
    (decision, reason) for the tick loop to record, or None to defer without recording
    (budget exhausted / objects not yet local — retry next tick). The caller has already
    confirmed this (PR, head) is undecided."""
    n = pr.get("number")
    H = pr.get("headRefOid") or ""
    M = pr.get("baseRefOid") or ""
    author = (pr.get("author") or {}).get("login", "")
    rid = repo["nwo"]
    if not (_SHA_RE.match(H) and _SHA_RE.match(M)):
        log(f"PR#{n}: invalid head/base sha — skip"); return None

    # scope gates (cheap, no merge possible)
    if pr.get("isDraft"):
        return ("skipped", "draft")
    if pr.get("isCrossRepository"):
        return ("skipped", "fork PR (cross-repo) — human")
    if author not in AUTHOR_ALLOWLIST:
        return ("skipped", f"author {author!r} not in allowlist — human")
    if pr.get("mergeStateStatus") not in ("CLEAN", "BEHIND"):
        return ("skipped", f"mergeStateStatus={pr.get('mergeStateStatus')} — human")
    mq = _merge_queue(repo)
    if mq is None or mq:
        return ("skipped", "merge-queue present or undetermined — fail-closed")

    # fetch + pin both base (M) and head (H) locally, then the range
    if _git(repo["path"], "fetch", "--no-tags", "origin",
            f"{M}", f"refs/pull/{n}/head", timeout=GH_TIMEOUT).returncode != 0:
        # fall back to fetching the named refs (some gh/git versions)
        _git(repo["path"], "fetch", "--no-tags", "origin", BASE_REF, timeout=GH_TIMEOUT)
    if _git(repo["path"], "cat-file", "-e", f"{H}^{{commit}}").returncode != 0 or \
       _git(repo["path"], "cat-file", "-e", f"{M}^{{commit}}").returncode != 0:
        return ("skipped", "head/base objects not local after fetch — retry next tick")
    mb = _git(repo["path"], "merge-base", M, H)
    B = mb.stdout.decode().strip()
    if not _SHA_RE.match(B):
        return ("skipped", "could not compute merge-base — skip")

    # gate 2: docs-only diff-class (THE security boundary)
    dr = _git(repo["path"], "diff", "--raw", "-z", "-M", "--no-ext-diff", f"{B}..{H}")
    if dr.returncode != 0:
        return ("skipped", "git diff failed — skip")
    ok, why = classify_diff(parse_raw_z(dr.stdout))
    if not ok:
        return ("skipped", f"not docs-only: {why} — human")

    # gate 3: CI green for H
    if not _ci_green(repo, H):
        return ("skipped", "CI not green for this head")

    # gate 4: synchronous review PASS
    if not _review_pass(repo, B, H, f"am-{n}-{H[:12]}"):
        return ("needs_fix", "review did not PASS")

    # gate 5: bounds
    if budget[0] >= MAX_PER_TICK:
        return None  # defer (don't record — re-try next tick)
    if not DRY_RUN and _count(_day()) >= MAX_PER_DAY:
        return None
    if not DRY_RUN and _count(_day(f"-author-{author}")) >= MAX_PER_AUTHOR_DAY:
        return ("skipped", f"author {author} daily cap reached")

    # gate 6/7: atomic merge at H (server rejects on head mismatch)
    if DRY_RUN:
        log(f"PR#{n} @ {H[:8]}: DRY-RUN would MERGE (docs-only + CI green + review PASS)")
        return None
    if TEST_MODE and MERGE_OVERRIDE:
        with open(MERGE_OVERRIDE, "a") as fh:
            fh.write(json.dumps({"pr": n, "head": H, "base": B}) + "\n")
        budget[0] += 1; _charge(author)
        return ("merged", "TEST SEAM recorded merge")
    res = _gh_json(repo["path"], "api", "-X", "PUT", f"repos/{rid}/pulls/{n}/merge",
                   "-f", f"sha={H}", "-f", "merge_method=merge")
    if isinstance(res, dict) and res.get("merged") is True:
        budget[0] += 1; _charge(author)
        return ("merged", f"merge sha {str(res.get('sha'))[:8]}")
    return ("skipped", "merge call did not confirm merged (head moved / blocked) — human")


async def tick() -> int:
    repo = load_repo()
    if repo is None:
        return 0
    if not ENABLED_FLAG.exists():
        log("AUTOMERGE_ENABLED absent — gate is OFF; exiting"); return 0
    rl = _gh_json(repo["path"], "api", "rate_limit", "-q", ".resources.core.remaining")
    if isinstance(rl, int) and rl < RATE_FLOOR:
        log(f"gh rate remaining {rl} < {RATE_FLOOR} — fail-closed this tick"); return 0
    if not acquire_lock():
        return 0
    try:
        led = await PostgresLedger.connect(DSN)
        budget = [0]
        try:
            prs = _gh_json(repo["path"], "pr", "list", "--base", "main", "--state", "open",
                           "--json", "number,headRefOid,baseRefOid,author,isDraft,"
                           "isCrossRepository,mergeStateStatus")
            if not isinstance(prs, list):
                log("could not list PRs — skip"); return 0
            for pr in prs:
                H = pr.get("headRefOid") or ""
                n = pr.get("number")
                if not (_SHA_RE.match(H) and isinstance(n, int)):
                    continue
                prior = await led.automerge_decision(repo["repo_id"], n, H)
                if prior is not None:
                    continue  # this head already decided; a new push makes a new head
                decision = evaluate_pr(repo, pr, budget)
                if decision is not None:
                    await led.record_automerge(repo["repo_id"], n, H, decision[0], decision[1])
                    log(f"PR#{n} @ {H[:8]}: {decision[0]} — {decision[1]}")
                if budget[0] >= MAX_PER_TICK:
                    break
        finally:
            await led.close()
        log(f"tick complete — {budget[0]} merge(s)")
        return 0
    finally:
        release_lock()


def main(argv: list) -> int:
    if len(argv) < 2 or argv[1] != "tick":
        print("usage: python -m runtime.automerge tick", file=sys.stderr)
        return 2
    return asyncio.run(tick())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
