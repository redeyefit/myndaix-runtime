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
import secrets
import shutil
import subprocess
import sys
import tempfile
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
    """STRICT parse of `git diff --raw -z -M base..H`. RAISES ValueError on ANY anomaly —
    a security boundary must REJECT malformed input, never silently skip an entry (codex
    BLOCKER). Each entry: {omode, nmode, status, paths:[...]} (paths=2 for rename/copy)."""
    if out == b"":
        return []
    toks = out.split(b"\x00")
    if toks and toks[-1] == b"":
        toks = toks[:-1]                              # drop the trailing empty from the final NUL
    entries: list[dict] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if not t.startswith(b":"):
            raise ValueError(f"expected an info line, got {t!r}")
        fields = t[1:].split(b" ")
        if len(fields) != 5:
            raise ValueError(f"info line has {len(fields)} fields, expected 5: {t!r}")
        omode, nmode, _osha, _nsha, status = fields
        try:
            st = status.decode("ascii")
            omode_s, nmode_s = omode.decode("ascii"), nmode.decode("ascii")
        except UnicodeDecodeError as e:
            raise ValueError(f"non-ascii info field: {e}")
        n = 2 if st[:1] in ("R", "C") else 1
        raw_paths = toks[i + 1:i + 1 + n]
        if len(raw_paths) != n:
            raise ValueError(f"truncated path list for status {st!r}")
        paths = []
        for p in raw_paths:
            try:
                paths.append(p.decode("utf-8"))
            except UnicodeDecodeError as e:
                raise ValueError(f"non-utf8 path: {e}")
        entries.append({"omode": omode_s, "nmode": nmode_s, "status": st, "paths": paths})
        i += 1 + n
    return entries


def classify_diff(entries: list[dict]) -> tuple[bool, str]:
    """THE security gate. True iff EVERY entry is an Add/Modify/Delete/Rename/Copy of an
    inert, non-denylisted `.md` regular file — with BOTH the old and new modes validated
    per status (codex BLOCKER: validating only the new mode let a typechange from a symlink
    `T 120000->100644 x.md`, or an unknown status `X`, slip through). Any other status or
    mode → reject."""
    if not entries:
        return False, "empty changeset"
    for e in entries:
        st, omode, nmode, paths = e["status"], e["omode"], e["nmode"], e["paths"]
        code = st[:1]
        if code not in ("A", "M", "D", "R", "C"):    # whitelist; reject T/U/X/B/anything else
            return False, f"unsupported status {st!r}"
        for p in paths:                              # every path on every side must be an inert .md
            if not _doc_path(p):
                return False, f"non-doc or denylisted path: {p!r}"
        if code == "A":
            if (omode, nmode) != ("000000", "100644"):
                return False, f"unsafe add modes {omode}->{nmode} for {paths}"
        elif code == "D":
            if (omode, nmode) != ("100644", "000000"):
                return False, f"unsafe delete modes {omode}->{nmode} for {paths}"
        else:                                        # M / R / C: BOTH sides must be regular non-exec blobs
            if omode != "100644" or nmode != "100644":
                return False, (f"unsafe {code} modes {omode}->{nmode} for {paths} "
                               f"(reject symlink/gitlink/exec/typechange)")
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
        info = _gh_json(path, "repo", "view", "--json", "nameWithOwner")  # JSON object, not a -q raw string
        nwo = info.get("nameWithOwner") if isinstance(info, dict) else None
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
    if not p.exists():
        return 0                                     # legitimately the first of the day
    try:
        return int(p.read_text().strip() or "0")
    except (OSError, ValueError):
        return 1 << 30                               # corrupt/unreadable counter -> treat as OVER cap (fail-closed)


def _charge(author: str) -> None:
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        _day().write_text(str(_count(_day()) + 1))
        ap = _day(f"-author-{author}")
        ap.write_text(str(_count(ap) + 1))
    except OSError:
        pass


# -- the per-PR gate -----------------------------------------------------------
def _ci_green(repo: dict, head: str) -> Optional[bool]:
    """The required `test` check(s) for THIS commit. Returns True (all COMPLETED+SUCCESS),
    False (a `test` run FAILED — terminal for this head), or None (no `test` run yet, still
    RUNNING, or an unparseable/error result — TRANSIENT, retry next tick). `--paginate` with
    a per-line jq filter emits JSONL (one object per match across pages), parsed line-by-line
    (a `-q '[...]'` array wrap would emit one array PER PAGE and break json.loads — codex/Oracle)."""
    r = subprocess.run(
        ["gh", "api", "--paginate", f"repos/{repo['nwo']}/commits/{head}/check-runs",
         "-q", '.check_runs[] | select(.name=="test") | "\\(.status) \\(.conclusion)"'],
        cwd=str(repo["path"]), capture_output=True, text=True, env=_git_env(),
        timeout=GH_TIMEOUT, check=False)
    if r.returncode != 0:
        log("  CI: check-runs query failed — transient (retry)"); return None
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    if not lines:
        log("  CI: no `test` check-run yet — transient (retry)"); return None
    for ln in lines:
        status, _, conclusion = ln.partition(" ")
        if status != "completed":
            log(f"  CI: a `test` run is {status!r} — still running (retry)"); return None
        if conclusion != "success":
            log(f"  CI: a `test` run concluded {conclusion!r} — FAILED for this head"); return False
    return True


def _review_pass(repo: dict, B: str, H: str) -> str:
    """Synchronous play-review --gate. Returns 'pass' | 'needs_fix' (terminal model rejection)
    | 'transient' (abort/contention/timeout/oracle-down — retry next tick, NOT a permanent
    record). A random per-attempt run_id + a fresh 0700 dir prevent stale/replay; play-review
    exits 0=PASS, 1=NEEDS-FIX, 2=ABORT, and the verdict file is validated against {run_id,B,H}.
    Security note: the §3 diff-class gate already bounds blast radius, so this is a QUALITY gate."""
    if TEST_MODE:
        return os.environ.get("MYNDAIX_AUTOMERGE_FAKE_VERDICT", "transient")
    run_id = "am-" + secrets.token_hex(8)            # unpredictable, per-attempt
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        gate_dir = Path(tempfile.mkdtemp(prefix="automerge-gate.", dir=str(STATE)))
        os.chmod(gate_dir, 0o700)
    except OSError as e:
        log(f"  review: cannot create a fresh gate dir ({e}) — transient"); return "transient"
    vpath = gate_dir / "verdict.json"
    env = dict(_git_env())
    env.update({"PLAY_GATE": "1", "PLAY_GATE_VERDICT": str(vpath), "PLAY_GATE_RUN_ID": run_id,
                "PLAY_DISABLE_AUTOFIX": "1", "MYNDAIX_DSN": DSN, "PLAY_SELF": str(PLAY_REVIEW)})
    try:
        rc = subprocess.run([str(PLAY_REVIEW), "--worker", str(repo["path"]), B, H, BASE_REF, ""],
                            cwd=str(repo["path"]), env=env, timeout=REVIEW_TIMEOUT, check=False).returncode
    except (subprocess.TimeoutExpired, OSError) as e:
        shutil.rmtree(gate_dir, ignore_errors=True)
        log(f"  review: play-review --gate failed ({e}) — transient"); return "transient"
    try:
        v = json.loads(vpath.read_text())
    except (OSError, json.JSONDecodeError):
        shutil.rmtree(gate_dir, ignore_errors=True)
        log("  review: no/invalid verdict file — transient"); return "transient"
    shutil.rmtree(gate_dir, ignore_errors=True)
    fresh = (v.get("run_id") == run_id and v.get("base") == B and v.get("head") == H)
    if rc == 0 and fresh and v.get("verdict") == "PASS":
        return "pass"
    if rc == 1 and fresh and v.get("verdict") == "NEEDS-FIX":
        return "needs_fix"
    log(f"  review: rc={rc} verdict={v} fresh={fresh} — transient (retry)")
    return "transient"


def _merge_queue(repo: dict) -> Optional[bool]:
    """True if main is behind a merge queue (we must refuse — a queued merge escapes the gate),
    False if not, None if undetermined (query failed → caller fail-closes). Parses the rules
    array in Python (no fragile -q)."""
    rules = _gh_json(repo["path"], "api", f"repos/{repo['nwo']}/rules/branches/main")
    if not isinstance(rules, list):
        return None
    return any(isinstance(r, dict) and r.get("type") == "merge_queue" for r in rules)


def _recheck(repo: dict, n: int, H: str, M: str) -> bool:
    """v0.3 just-before-merge recheck: re-read the PR + rules + CI and require the head is
    still exactly H, the base still M, state OPEN+CLEAN/BEHIND, no merge queue, CI still
    green. Anything else → False (defer). `sha=H` in the PUT also closes the head move at the
    server, but this catches a base/CI/queue change in the long review window."""
    pr = _gh_json(repo["path"], "pr", "view", str(n), "--json",
                  "headRefOid,baseRefOid,mergeStateStatus,state")
    if not isinstance(pr, dict):
        return False
    if pr.get("state") != "OPEN" or pr.get("headRefOid") != H or pr.get("baseRefOid") != M:
        return False
    if pr.get("mergeStateStatus") not in ("CLEAN", "BEHIND"):
        return False
    if _merge_queue(repo) is not False:              # None (undetermined) or True both fail
        return False
    return _ci_green(repo, H) is True


# decisions: a tuple is RECORDED (terminal for this head); None DEFERS (retry next tick).
# Only head-determined outcomes are recorded; anything that can change without a new push
# (draft/behind/queue/CI-pending/contention/caps) defers, so the gate never wedges a PR.
def evaluate_pr(repo: dict, pr: dict, budget: list) -> Optional[tuple]:
    """Run the gate on one PR; merge if all pass. Pure-sync (git/gh subprocess)."""
    n = pr.get("number")
    H = pr.get("headRefOid") or ""
    M = pr.get("baseRefOid") or ""
    author = (pr.get("author") or {}).get("login", "")
    rid = repo["nwo"]
    if not (_SHA_RE.match(H) and _SHA_RE.match(M)):
        log(f"PR#{n}: invalid head/base sha — skip"); return None

    # scope: terminal skips (won't change without a new head) are recorded; draft can change → defer
    if pr.get("isDraft"):
        return None
    if pr.get("isCrossRepository"):
        return ("skipped", "fork PR (cross-repo) — human")
    if author not in AUTHOR_ALLOWLIST:
        return ("skipped", f"author {author!r} not in allowlist — human")
    if pr.get("mergeStateStatus") not in ("CLEAN", "BEHIND"):
        log(f"PR#{n}: mergeStateStatus={pr.get('mergeStateStatus')} — defer"); return None
    if _merge_queue(repo) is not False:
        log(f"PR#{n}: merge queue present/undetermined — fail-closed defer"); return None

    # fetch the PR head + main into automerge-OWNED refs; pin H (assert it didn't move) and
    # snapshot main, so the whole evaluation judges ONE immutable range B..H (codex MAJOR).
    if _git(repo["path"], "fetch", "--no-tags", "origin",
            f"+refs/pull/{n}/head:refs/automerge/pr/{n}",
            f"+{BASE_REF}:refs/automerge/main", timeout=GH_TIMEOUT).returncode != 0:
        log(f"PR#{n}: fetch failed — defer"); return None
    Hpin = _git(repo["path"], "rev-parse", f"refs/automerge/pr/{n}").stdout.decode().strip()
    Mpin = _git(repo["path"], "rev-parse", "refs/automerge/main").stdout.decode().strip()
    if Hpin != H:
        log(f"PR#{n}: head moved {H[:8]}->{Hpin[:8]} during fetch — defer"); return None
    if not _SHA_RE.match(Mpin):
        return None
    mb = _git(repo["path"], "merge-base", Mpin, H)
    B = mb.stdout.decode().strip()
    if mb.returncode != 0 or not _SHA_RE.match(B):
        log(f"PR#{n}: merge-base failed — defer"); return None

    # gate 2: docs-only diff-class over B..H (THE security boundary). A parse error on a real
    # diff is suspicious → route to human (record), never silently pass.
    dr = _git(repo["path"], "diff", "--raw", "-z", "-M", "--no-ext-diff", f"{B}..{H}")
    if dr.returncode != 0:
        log(f"PR#{n}: git diff failed — defer"); return None
    try:
        entries = parse_raw_z(dr.stdout)
    except ValueError as ex:
        return ("skipped", f"unparseable diff ({ex}) — human")
    ok, why = classify_diff(entries)
    if not ok:
        return ("skipped", f"not docs-only: {why} — human")

    # gate 3: CI — True=green / False=failed(terminal) / None=pending(defer)
    ci = _ci_green(repo, H)
    if ci is None:
        return None
    if ci is False:
        return ("skipped", "CI failed for this head — human")

    # gate 4: synchronous review — pass / needs_fix(terminal) / transient(defer)
    rev = _review_pass(repo, B, H)
    if rev == "transient":
        return None
    if rev == "needs_fix":
        return ("needs_fix", "review did not PASS — human")

    # gate 5: bounds — all DEFER (None), never recorded as terminal
    if budget[0] >= MAX_PER_TICK:
        return None
    if not DRY_RUN and _count(_day()) >= MAX_PER_DAY:
        log(f"PR#{n}: daily merge cap — defer"); return None
    if not DRY_RUN and _count(_day(f"-author-{author}")) >= MAX_PER_AUTHOR_DAY:
        log(f"PR#{n}: author {author} daily cap — defer"); return None

    if DRY_RUN:
        log(f"PR#{n} @ {H[:8]}: DRY-RUN would MERGE (docs-only + CI green + review PASS)")
        return None
    if TEST_MODE and MERGE_OVERRIDE:
        with open(MERGE_OVERRIDE, "a") as fh:
            fh.write(json.dumps({"pr": n, "head": H, "base": B}) + "\n")
        budget[0] += 1; _charge(author)
        return ("merged", "TEST SEAM recorded merge")

    # gate 6: just-before-merge recheck (head/base/state/queue/CI unchanged), then atomic merge at H
    if not _recheck(repo, n, H, M):
        log(f"PR#{n}: pre-merge recheck failed — defer"); return None
    res = _gh_json(repo["path"], "api", "-X", "PUT", f"repos/{rid}/pulls/{n}/merge",
                   "-f", f"sha={H}", "-f", "merge_method=merge")
    if isinstance(res, dict) and res.get("merged") is True:
        budget[0] += 1; _charge(author)
        return ("merged", f"merge sha {str(res.get('sha'))[:8]}")
    log(f"PR#{n}: merge not confirmed ({res}) — defer"); return None


async def tick() -> int:
    repo = load_repo()
    if repo is None:
        return 0
    if not ENABLED_FLAG.exists():
        log("AUTOMERGE_ENABLED absent — gate is OFF; exiting"); return 0
    rl = _gh_json(repo["path"], "api", "rate_limit")
    remaining = (rl or {}).get("resources", {}).get("core", {}).get("remaining") if isinstance(rl, dict) else None
    if not isinstance(remaining, int) or remaining < RATE_FLOOR:
        log(f"gh rate remaining={remaining} (< {RATE_FLOOR} or unknown) — fail-closed this tick"); return 0
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
