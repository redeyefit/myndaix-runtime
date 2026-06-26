"""controller.py — the controller-loop ("the brain"), north-star rung 3.

A bounded, non-Claude (launchd-triggered) controller that turns push-triggered
review into BRAIN-decided review. It is a level-triggered reconciler (decide from
observed state, not from an event): each hourly tick, for every trusted repo, it
fetches the watched ref into a controller-OWNED ref, compares HEAD to a durable
per-(repo,ref) cursor (review_cursor, DESIGN v0.2 §2), and — if HEAD advanced past
the last DELIVERED review and none is in flight — triggers the EXISTING
play-review.sh pipeline for the delta. Then it exits. NOT a daemon: one bounded
job per launchd tick.

What it deliberately does NOT do (later north-star rungs): no LLM in the decision
path, no auto-fix (it sets PLAY_DISABLE_AUTOFIX=1, a HARD override, so play-review's
autofix bridge can NEVER fire from a brain review even where autofix is armed), no
auto-merge, no learning.

Trigger model (DESIGN, locked): SYNTHETIC-STDIN, near-zero-touch — the brain pipes a
constructed git pre-push line "<ref> <head> <ref> <reviewed_sha>" plus argv
`origin <url>` into play-review.sh (the ONLY edit there is a fail-closed autofix
disable), reproducing a push of reviewed_sha..head. The fetch (B1) + controller-owned
refs guarantee both objects are local and gc-safe; the cursor bootstrap (B2)
guarantees reviewed_sha is never the zero/empty-tree sha. The cursor advances from a
LEDGER signal (a delivered review job), NOT play-review's done-marker, which is
suppressed when the branch moves mid-review.

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

# -- config --------------------------------------------------------------------
DSN = os.environ.get("MYNDAIX_DSN", "postgresql://localhost/runtime")
HOME = Path(os.environ.get("HOME", str(Path.home())))
# ORCH is NOT env-overridable: play-review.sh hardcodes $HOME/.myndaix/orchestrator,
# and the controller must read/write the SAME state it does (codex M6).
ORCH = HOME / ".myndaix" / "orchestrator"
STATE = ORCH / "state"
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
# remote transports play-review's ls-remote may use. plain/unauth http:// and git://
# are rejected (codex MINOR); "ext::"/"fd::" (and any "::") are arbitrary-exec RCE.
_URL_OK = ("https://", "ssh://", "file://", "git@")
# command-line config that OVERRIDES repo/global config (a -c flag wins), so a poisoned
# .git/config can't re-enable an exec transport, a credential helper, or submodule recursion.
_GIT_HARDEN = [
    "-c", "protocol.ext.allow=never", "-c", "protocol.fd.allow=never",
    "-c", "credential.helper=", "-c", "fetch.recurseSubmodules=false",
]


def log(msg: str) -> None:
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [controller] {msg}", flush=True)


def _utcday() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d")


def _slug(ref: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", ref)


def _ctl_head_ref(ref: str) -> str:
    return f"refs/myndaix/controller/{_slug(ref)}"        # anchors the fetched head (no FETCH_HEAD race, gc-safe)


def _ctl_reviewed_ref(ref: str) -> str:
    return f"refs/myndaix/reviewed/{_slug(ref)}"          # anchors the reviewed base against git gc


# -- minimal, allowlisted subprocess envs (build up, never inherit blindly) -----
def _git_env() -> dict:
    env = {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(HOME),
        "GIT_TERMINAL_PROMPT": "0",                      # never block on a credential prompt
        "GIT_CONFIG_NOSYSTEM": "1",                      # ignore /etc git config (url rewrites, helpers)
        "GIT_CONFIG_GLOBAL": "/dev/null",                # ignore ~/.gitconfig too
    }
    for k in ("SSH_AUTH_SOCK", "TMPDIR", "LANG"):        # ssh remotes / tmp
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def _review_env() -> dict:
    # play-review.sh resets its own PATH and derives ORCH from HOME. It needs HOME +
    # MYNDAIX_DSN (so its `mxr` calls reach the ledger) + ssh auth for confirm_pushed.
    # PLAY_SELF pins its worker to the validated trusted path (codex M7, no worktree
    # fallback). PLAY_DISABLE_AUTOFIX hard-disables autofix (B1). PLAY_AUTOFIX is never set.
    env = {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(HOME),
        "GIT_TERMINAL_PROMPT": "0",
        "MYNDAIX_DSN": DSN,
        "PLAY_SELF": str(PLAY_REVIEW),
        "PLAY_DISABLE_AUTOFIX": "1",
    }
    for k in ("SSH_AUTH_SOCK", "TMPDIR", "LANG"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def _git(repo: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *_GIT_HARDEN, *args],
        capture_output=True, text=True, env=_git_env(), timeout=timeout, check=False,
    )


# -- config --------------------------------------------------------------------
class Repo:
    __slots__ = ("repo_id", "path", "watch_ref")

    def __init__(self, repo_id: str, path: Path, watch_ref: str):
        self.repo_id, self.path, self.watch_ref = repo_id, path, watch_ref


def load_config() -> list[Repo]:
    """Trusted repo list from $ORCH/repos.json (chmod-600, outside any repo, so a commit
    can't redefine it). repo_id = basename(path) to MATCH play-review.sh:76; duplicate
    basenames are rejected (they'd collide on the cursor key). A missing or malformed file
    logs and yields [] (the tick no-ops; never crash-loops launchd)."""
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
        repo_id = path.name
        if repo_id in seen:
            log(f"config '{key}': duplicate basename {repo_id!r} — rejected (cursor key clash)")
            continue
        seen.add(repo_id)
        repos.append(Repo(repo_id, path, watch_ref))
    return repos


# -- single-instance lock (atomic; mtime stale-reap via rename; heartbeat) -------
def _stamp_lock() -> None:
    try:
        (LOCK / "meta").write_text(f"{os.getpid()} {socket.gethostname()} {int(time.time())}\n")
    except OSError:
        pass


def acquire_lock() -> bool:
    ORCH.mkdir(parents=True, exist_ok=True)
    try:
        LOCK.mkdir()
    except FileExistsError:
        age = time.time() - LOCK.stat().st_mtime
        if age <= LOCK_TTL:
            log(f"another tick holds the lock ({int(age)}s old) — exiting"); return False
        # STEAL the stale lock atomically: rename (one winner) then reap. Two racing ticks
        # can't both succeed — rename of an already-moved dir fails (Oracle BLOCKER: a
        # plain rmtree->mkdir lets B delete A's fresh lock and both run).
        log(f"reaping stale lock ({int(age)}s > {LOCK_TTL}s)")
        stale = LOCK.with_name(f"{LOCK.name}.stale.{os.getpid()}")
        try:
            LOCK.rename(stale)
        except OSError:
            log("lost the reap race — exiting"); return False
        shutil.rmtree(stale, ignore_errors=True)
        try:
            LOCK.mkdir()
        except FileExistsError:
            log("lost the reap race — exiting"); return False
    _stamp_lock()
    return True


def heartbeat_lock() -> None:
    """Refresh the lock mtime so a long tick (many repos / slow DB) is never reaped as
    stale by a later tick (codex M8). Cheap; called once per repo."""
    _stamp_lock()


def release_lock() -> None:
    shutil.rmtree(LOCK, ignore_errors=True)


# -- the trusted play-review.sh gate + the review trigger ----------------------
def _trusted_script(p: Path) -> bool:
    """Only ever exec the FIXED installed play-review.sh, never a worktree copy: a regular
    file (not a symlink), owned by us, not group/world-writable, executable."""
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


def remote_url(repo: Repo) -> Optional[str]:
    """The repo's origin URL, restricted to authenticated/local transports (no exec
    transports). Validated BEFORE any fetch (codex M1) so a poisoned remote.origin.url
    never reaches `git fetch`."""
    r = _git(repo.path, "config", "--get", "remote.origin.url")
    url = r.stdout.strip()
    if r.returncode != 0 or not url:
        log(f"{repo.repo_id}: no origin url — skip"); return None
    if "::" in url or not url.startswith(_URL_OK):
        log(f"{repo.repo_id}: disallowed remote url transport — skip"); return None
    return url


def trigger_review(repo: Repo, head: str, base: str, url: str) -> bool:
    """Fire the review via synthetic-stdin into the trusted play-review.sh. Returns True
    iff the trigger ran cleanly (or was recorded under the test seam)."""
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
    try:
        proc = subprocess.run(
            [str(PLAY_REVIEW), "origin", url],
            input=line.encode(), cwd=str(repo.path), env=_review_env(),
            timeout=REVIEW_TIMEOUT, check=False,
        )                                                # FRONT detaches the worker + exits 0 fast
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"{repo.repo_id}: play-review trigger failed ({e})")
        return False
    if proc.returncode != 0:                             # FRONT itself failed -> don't charge budget
        log(f"{repo.repo_id}: play-review FRONT exited {proc.returncode} — not charging budget")
        return False
    log(f"{repo.repo_id}: review triggered {base[:8]}..{head[:8]}")
    return True


# -- per-repo decision (level-triggered reconcile) -----------------------------
def _done(sha: str) -> bool:
    return (STATE / f"done-{sha}").exists()


def _pin(repo: Repo, sha: str) -> None:
    """Anchor a sha behind a controller-owned ref so git gc can't prune it (codex M4 /
    Oracle MAJOR: a force-pushed-away reviewed_sha would else fail cat-file forever)."""
    _git(repo.path, "update-ref", _ctl_reviewed_ref(repo.watch_ref), sha)


async def process_repo(led: PostgresLedger, repo: Repo, budget: list[int]) -> None:
    rid, ref = repo.repo_id, repo.watch_ref
    cur = await led.get_cursor(rid, ref)

    # advance pass: a prior dispatch whose review reached the ledger (or wrote done-<sha>)
    # moves the cursor. The ledger signal is branch-move-proof (codex B2).
    if cur and cur["pending_sha"]:
        ps = cur["pending_sha"]
        if await led.review_delivered(rid, ps) or _done(ps):
            if await led.advance_cursor(rid, ref, ps):
                _pin(repo, ps)
                log(f"{rid}: cursor advanced to {ps[:8]} (review delivered)")
            cur = await led.get_cursor(rid, ref)

    url = remote_url(repo)                                # validate transport BEFORE fetch (M1)
    if url is None:
        return

    # observe: fetch into a controller-OWNED ref (no FETCH_HEAD race, gc-safe — codex M3/M4)
    fr = _git(repo.path, "fetch", "--no-tags", "--quiet", "origin",
              f"+{ref}:{_ctl_head_ref(ref)}", timeout=FETCH_TIMEOUT)
    if fr.returncode != 0:
        log(f"{rid}: fetch failed ({fr.stderr.strip()[:120]}) — skip this tick"); return
    hr = _git(repo.path, "rev-parse", _ctl_head_ref(ref))
    head = hr.stdout.strip()
    if hr.returncode != 0 or not _SHA_RE.match(head):
        log(f"{rid}: could not resolve a valid HEAD sha — skip"); return

    # decide
    if cur is None:                                      # first sight (B2): seed, do NOT review
        if DRY_RUN:
            log(f"{rid}: DRY-RUN would seed baseline {head[:8]}"); return
        if await led.upsert_baseline(rid, ref, head):
            _pin(repo, head)
            log(f"{rid}: seeded baseline {head[:8]} (not reviewed)")
        return
    if head == cur["reviewed_sha"]:
        return                                           # up to date

    # ceiling: stop chasing a head that has failed MAX_ATTEMPTS dispatches
    if cur["pending_sha"] == head and cur["attempts"] >= MAX_ATTEMPTS and cur["state"] != "blocked":
        if await led.mark_blocked(rid, ref, head, MAX_ATTEMPTS):
            log(f"{rid}: BLOCKED {head[:8]} after {cur['attempts']} attempts — surfaced, backing off")
        return

    if budget[0] >= MAX_DISPATCH_PER_TICK:
        log(f"{rid}: per-tick dispatch budget reached — deferring {head[:8]} to next tick"); return

    base = cur["reviewed_sha"]                            # review reviewed_sha..head (never empty-tree)
    if _git(repo.path, "cat-file", "-e", f"{head}^{{commit}}").returncode != 0 or \
       _git(repo.path, "cat-file", "-e", f"{base}^{{commit}}").returncode != 0:
        log(f"{rid}: head/base objects not present locally — skip"); return

    if DRY_RUN:
        log(f"{rid}: DRY-RUN would dispatch review {base[:8]}..{head[:8]}"); return

    stale_before = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=PENDING_STALE)
    if not await led.claim_dispatch(rid, ref, head, stale_before):
        log(f"{rid}: dispatch not claimed (in flight or blocked) — skip"); return
    if trigger_review(repo, head, base, url):
        budget[0] += 1
        _charge_day()


# -- daily dispatch budget (UTC-keyed file counter, mirrors play-review's cap) ---
def _day_file() -> Path:
    return STATE / f"controller-day-{_utcday()}"


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
        log("note: AUTOFIX_ENABLED is armed — brain reviews still NEVER auto-fix (PLAY_DISABLE_AUTOFIX=1)")
    if not acquire_lock():
        return 0
    try:
        if not DRY_RUN and _day_count() >= MAX_DISPATCH_PER_DAY:
            log(f"daily dispatch budget ({MAX_DISPATCH_PER_DAY}) reached — observing only")
        led = await PostgresLedger.connect(DSN)
        budget = [0]
        try:
            for repo in repos:
                heartbeat_lock()                         # keep the lock fresh across a long tick (M8)
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
