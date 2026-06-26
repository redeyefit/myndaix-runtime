"""controller.py — the controller-loop ("the brain"), north-star rung 3.

A bounded, non-Claude (launchd-triggered) controller that turns push-triggered
review into BRAIN-decided review. It is a level-triggered reconciler (decide from
observed state, not from an event): each hourly tick, for every trusted repo, it
fetches the watched ref, compares HEAD to a durable per-(repo,ref) cursor
(review_cursor, DESIGN v0.2 §2), and — if HEAD advanced past the last DELIVERED
review and none is in flight — triggers the EXISTING play-review.sh pipeline for
the delta. Then it exits. NOT a daemon: one bounded job per launchd tick.

What it deliberately does NOT do (later north-star rungs): no LLM in the decision
path, no auto-fix (it never sets PLAY_AUTOFIX, so play-review's autofix bridge can
NEVER fire from a brain review even if armed), no auto-merge, no learning.

Trigger model (DESIGN, locked): SYNTHETIC-STDIN, zero-touch — the brain pipes a
constructed git pre-push line "<ref> <head> <ref> <reviewed_sha>" plus argv
`origin <url>` into the unmodified play-review.sh, reproducing a push of
reviewed_sha..head. The fetch (B1) guarantees the objects are local; the cursor
bootstrap (B2) guarantees reviewed_sha is never the zero/empty-tree sha.

Run one tick:
    MYNDAIX_DSN=postgresql://localhost/runtime PYTHONPATH=src python3 -m runtime.controller tick

Safe first run (decide + log, write nothing, dispatch nothing):
    MYNDAIX_CONTROLLER_DRY_RUN=1 ... python3 -m runtime.controller tick
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import re
import shutil
import socket
import stat as _stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from runtime.ledger.postgres_store import PostgresLedger

# -- config (all overridable by env; defaults match play-review.sh paths) ------
DSN = os.environ.get("MYNDAIX_DSN", "postgresql://localhost/runtime")
HOME = Path(os.environ.get("HOME", str(Path.home())))
ORCH = Path(os.environ.get("MYNDAIX_ORCH", str(HOME / ".myndaix" / "orchestrator")))
STATE = ORCH / "state"                                   # play-review writes done-<sha> here
REPOS_JSON = Path(os.environ.get("MYNDAIX_REPOS_JSON", str(ORCH / "repos.json")))
PLAY_REVIEW = Path(os.environ.get("PLAY_SELF", str(ORCH / "play-review.sh")))
LOCK = ORCH / "controller.lock"

DEFAULT_WATCH_REF = "refs/heads/main"
MAX_DISPATCH_PER_TICK = int(os.environ.get("MYNDAIX_CONTROLLER_MAX_DISPATCH", "3"))
MAX_DISPATCH_PER_DAY = int(os.environ.get("MYNDAIX_CONTROLLER_MAX_DAY", "20"))
MAX_ATTEMPTS = int(os.environ.get("MYNDAIX_CONTROLLER_MAX_ATTEMPTS", "3"))
LOCK_TTL = int(os.environ.get("MYNDAIX_CONTROLLER_LOCK_TTL", "900"))          # 15 min
PENDING_STALE = int(os.environ.get("MYNDAIX_CONTROLLER_PENDING_STALE", "3600"))  # 1 h
FETCH_TIMEOUT = int(os.environ.get("MYNDAIX_CONTROLLER_FETCH_TIMEOUT", "60"))
REVIEW_TIMEOUT = int(os.environ.get("MYNDAIX_CONTROLLER_REVIEW_TIMEOUT", "60"))

DRY_RUN = os.environ.get("MYNDAIX_CONTROLLER_DRY_RUN") == "1"
TEST_MODE = os.environ.get("MYNDAIX_CONTROLLER_TEST_MODE") == "1"
DISPATCH_OVERRIDE = os.environ.get("MYNDAIX_CONTROLLER_DISPATCH_OVERRIDE", "")

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REF_RE = re.compile(r"^refs/heads/[A-Za-z0-9._][A-Za-z0-9._/-]*$")
# remote URL transports we allow play-review's ls-remote to use. The git "ext::"/
# "fd::" transports are RCE vectors (run arbitrary commands) -> rejected, as is
# anything containing "::" (transport-helper exec form).
_URL_OK = ("https://", "http://", "ssh://", "git://", "file://", "git@")


def log(msg: str) -> None:
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [controller] {msg}", flush=True)


# -- minimal, allowlisted subprocess env (build up, never inherit blindly) ------
def _git_env() -> dict:
    env = {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(HOME),
        "GIT_TERMINAL_PROMPT": "0",                      # never block on a credential prompt
    }
    for k in ("SSH_AUTH_SOCK", "TMPDIR", "LANG"):        # needed for ssh remotes / tmp
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def _review_env() -> dict:
    # play-review.sh resets its own PATH and derives ORCH from HOME; it needs HOME +
    # MYNDAIX_DSN (so its `mxr` calls reach the ledger) + ssh auth for confirm_pushed.
    # Crucially it must NOT receive PLAY_AUTOFIX (B3) — the brain never auto-fixes.
    env = _git_env()
    env["MYNDAIX_DSN"] = DSN
    if os.environ.get("MYNDAIX_ORCH"):
        env["MYNDAIX_ORCH"] = os.environ["MYNDAIX_ORCH"]
    return env


def _git(repo: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env=_git_env(), timeout=timeout, check=False,
    )


# -- config --------------------------------------------------------------------
class Repo:
    __slots__ = ("repo_id", "path", "watch_ref")

    def __init__(self, repo_id: str, path: Path, watch_ref: str):
        self.repo_id, self.path, self.watch_ref = repo_id, path, watch_ref


def load_config() -> list[Repo]:
    """Trusted repo list from $ORCH/repos.json (chmod-600, outside any repo, so a
    commit can't redefine it). repo_id = basename(path) to MATCH play-review.sh:76;
    duplicate basenames are rejected (they'd collide on the cursor key). A missing or
    malformed file logs and yields [] (the tick no-ops; never crash-loops launchd)."""
    try:
        raw = json.loads(REPOS_JSON.read_text())
    except FileNotFoundError:
        log(f"no repos.json at {REPOS_JSON} — nothing to watch"); return []
    except (json.JSONDecodeError, OSError) as e:
        log(f"repos.json unreadable ({e}) — skipping tick"); return []

    repos: list[Repo] = []
    seen: set[str] = set()
    for key, entry in raw.items():
        if key.startswith("_") or not isinstance(entry, dict):
            continue                                     # _comment / _verify_note
        p = entry.get("path")
        if not p:
            log(f"config '{key}': no path — skipped"); continue
        path = Path(p).expanduser().resolve()
        if not (path.is_dir() and (path / ".git").exists()):
            log(f"config '{key}': {path} is not a git repo — skipped"); continue
        watch_ref = entry.get("watch_ref", DEFAULT_WATCH_REF)
        if not _REF_RE.match(watch_ref):
            log(f"config '{key}': bad watch_ref {watch_ref!r} — skipped"); continue
        repo_id = path.name                              # basename == play-review's repo_id
        if repo_id in seen:
            log(f"config '{key}': duplicate basename {repo_id!r} — rejected (cursor key clash)")
            continue
        seen.add(repo_id)
        repos.append(Repo(repo_id, path, watch_ref))
    return repos


# -- single-instance lock (atomic mkdir + mtime stale-reap, mirrors play-review) -
def acquire_lock() -> bool:
    ORCH.mkdir(parents=True, exist_ok=True)
    try:
        LOCK.mkdir()
    except FileExistsError:
        age = time.time() - LOCK.stat().st_mtime
        if age <= LOCK_TTL:
            log(f"another tick holds the lock ({int(age)}s old) — exiting"); return False
        log(f"reaping stale lock ({int(age)}s > {LOCK_TTL}s)")
        shutil.rmtree(LOCK, ignore_errors=True)
        try:
            LOCK.mkdir()
        except FileExistsError:
            log("lost the reap race — exiting"); return False
    try:
        (LOCK / "meta").write_text(f"{os.getpid()} {socket.gethostname()} {int(time.time())}\n")
    except OSError:
        pass
    return True


def release_lock() -> None:
    shutil.rmtree(LOCK, ignore_errors=True)


# -- the trusted play-review.sh gate + the review trigger ----------------------
def _trusted_script(p: Path) -> bool:
    """Only ever exec the FIXED installed play-review.sh, never a worktree copy: a
    regular file (not a symlink), owned by us, not group/world-writable, executable."""
    try:
        st = p.lstat()
    except OSError:
        log(f"play-review.sh missing at {p}"); return False
    if _stat.S_ISLNK(st.st_mode):
        log(f"play-review.sh is a symlink ({p}) — refusing"); return False
    if not _stat.S_ISREG(st.st_mode):
        log(f"play-review.sh not a regular file ({p}) — refusing"); return False
    if st.st_uid != os.getuid():
        log(f"play-review.sh not owned by us ({p}) — refusing"); return False
    if st.st_mode & (_stat.S_IWGRP | _stat.S_IWOTH):
        log(f"play-review.sh is group/world-writable ({p}) — refusing"); return False
    if not os.access(p, os.X_OK):
        log(f"play-review.sh not executable ({p}) — refusing"); return False
    return True


def _remote_url(repo: Repo) -> Optional[str]:
    r = _git(repo.path, "config", "--get", "remote.origin.url")
    url = r.stdout.strip()
    if r.returncode != 0 or not url:
        log(f"{repo.repo_id}: no origin url — skip"); return None
    if "::" in url or not url.startswith(_URL_OK):
        log(f"{repo.repo_id}: disallowed remote url scheme — skip"); return None
    return url


def trigger_review(repo: Repo, head: str, base: str) -> bool:
    """Fire the review via synthetic-stdin into the trusted play-review.sh. Returns
    True iff the trigger was issued (or recorded under the test seam)."""
    line = f"{repo.watch_ref} {head} {repo.watch_ref} {base}\n"

    if TEST_MODE and DISPATCH_OVERRIDE:                  # unit-test seam: record, don't run
        rec = {"repo_id": repo.repo_id, "ref": repo.watch_ref, "head": head,
               "base": base, "cwd": str(repo.path)}
        with open(DISPATCH_OVERRIDE, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        log(f"{repo.repo_id}: TEST SEAM recorded dispatch {base[:8]}..{head[:8]}")
        return True
    if DISPATCH_OVERRIDE and not TEST_MODE:
        log("DISPATCH_OVERRIDE set without TEST_MODE=1 — refusing (not a production path)")
        return False

    if not _trusted_script(PLAY_REVIEW):
        return False
    url = _remote_url(repo)
    if url is None:
        return False
    try:
        subprocess.run(
            [str(PLAY_REVIEW), "origin", url],
            input=line.encode(), cwd=str(repo.path), env=_review_env(),
            timeout=REVIEW_TIMEOUT, check=False,
        )                                                # FRONT detaches the worker + exits 0 fast
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"{repo.repo_id}: play-review trigger failed ({e})")
        return False
    log(f"{repo.repo_id}: review triggered {base[:8]}..{head[:8]}")
    return True


# -- per-repo decision (level-triggered reconcile) -----------------------------
def _done(sha: str) -> bool:
    return (STATE / f"done-{sha}").exists()


async def process_repo(led: PostgresLedger, repo: Repo, budget: list[int]) -> None:
    rid, ref = repo.repo_id, repo.watch_ref
    cur = await led.get_cursor(rid, ref)

    # advance pass: a prior dispatch whose review DELIVERED (done-<sha>) moves the cursor.
    if cur and cur["pending_sha"] and _done(cur["pending_sha"]):
        if await led.advance_cursor(rid, ref, cur["pending_sha"]):
            log(f"{rid}: cursor advanced to {cur['pending_sha'][:8]} (review delivered)")
        cur = await led.get_cursor(rid, ref)

    # observe (fetch makes the remote head's objects local — B1)
    fr = _git(repo.path, "fetch", "--no-tags", "--quiet", "origin", ref, timeout=FETCH_TIMEOUT)
    if fr.returncode != 0:
        log(f"{rid}: fetch failed ({fr.stderr.strip()[:120]}) — skip this tick"); return
    hr = _git(repo.path, "rev-parse", "FETCH_HEAD")
    head = hr.stdout.strip()
    if hr.returncode != 0 or not _SHA_RE.match(head):
        log(f"{rid}: could not resolve a valid HEAD sha — skip"); return

    # decide
    if cur is None:                                      # first sight (B2): seed, do NOT review
        if DRY_RUN:
            log(f"{rid}: DRY-RUN would seed baseline {head[:8]}"); return
        if await led.upsert_baseline(rid, ref, head):
            log(f"{rid}: seeded baseline {head[:8]} (not reviewed)")
        return
    if head == cur["reviewed_sha"]:
        return                                           # up to date

    # ceiling: stop chasing a head that has failed MAX_ATTEMPTS dispatches
    if cur["pending_sha"] == head and cur["attempts"] >= MAX_ATTEMPTS and cur["state"] != "blocked":
        await led.mark_blocked(rid, ref)
        log(f"{rid}: BLOCKED {head[:8]} after {cur['attempts']} attempts — surfaced, backing off")
        return

    if budget[0] >= MAX_DISPATCH_PER_TICK:
        log(f"{rid}: per-tick dispatch budget reached — deferring {head[:8]} to next tick"); return

    base = cur["reviewed_sha"]                           # review reviewed_sha..head (never empty-tree)
    # the objects MUST be local for play-review's diff/cat-file to work
    if _git(repo.path, "cat-file", "-e", f"{head}^{{commit}}").returncode != 0 or \
       _git(repo.path, "cat-file", "-e", f"{base}^{{commit}}").returncode != 0:
        log(f"{rid}: head/base objects not present locally — skip"); return

    if DRY_RUN:
        log(f"{rid}: DRY-RUN would dispatch review {base[:8]}..{head[:8]}"); return

    stale_before = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=PENDING_STALE)
    if not await led.claim_dispatch(rid, ref, head, stale_before):
        log(f"{rid}: dispatch not claimed (in flight or blocked) — skip"); return
    if trigger_review(repo, head, base):
        budget[0] += 1
        _charge_day()


# -- daily dispatch budget (file counter, mirrors play-review's DAILY_CAP) ------
def _day_file() -> Path:
    return STATE / f"controller-day-{_dt.datetime.now().strftime('%Y%m%d')}"


def _day_count() -> int:
    try:
        return int(_day_file().read_text().strip() or "0")
    except (OSError, ValueError):
        return 0


def _charge_day() -> None:
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        _day_file().write_text(str(_day_count() + 1))
    except OSError:
        pass


async def tick() -> int:
    repos = load_config()
    if not repos:
        return 0
    if (ORCH / "AUTOFIX_ENABLED").exists():
        log("note: AUTOFIX_ENABLED is armed — brain reviews still NEVER auto-fix (PLAY_AUTOFIX unset)")
    if not acquire_lock():
        return 0
    try:
        if not DRY_RUN and _day_count() >= MAX_DISPATCH_PER_DAY:
            log(f"daily dispatch budget ({MAX_DISPATCH_PER_DAY}) reached — observing only");
        led = await PostgresLedger.connect(DSN)
        budget = [0]
        try:
            for repo in repos:
                if not DRY_RUN and _day_count() >= MAX_DISPATCH_PER_DAY:
                    break
                try:
                    await process_repo(led, repo, budget)
                except Exception as e:                   # one bad repo never sinks the tick
                    log(f"{repo.repo_id}: tick error {e!r}")
        finally:
            await led.close()
        log(f"tick complete — {budget[0]} review(s) dispatched across {len(repos)} repo(s)")
        return 0
    finally:
        release_lock()


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] != "tick":
        print("usage: python -m runtime.controller tick", file=sys.stderr)
        return 2
    return asyncio.run(tick())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
