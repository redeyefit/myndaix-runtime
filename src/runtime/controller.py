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
import fcntl
import json
import os
import re
import shutil
import stat as _stat
import subprocess
import sys
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
LOCK = ORCH / "controller.lock"                          # an flock'd FILE, not a dir

DEFAULT_WATCH_REF = "refs/heads/main"
MAX_DISPATCH_PER_TICK = int(os.environ.get("MYNDAIX_CONTROLLER_MAX_DISPATCH", "3"))
MAX_DISPATCH_PER_DAY = int(os.environ.get("MYNDAIX_CONTROLLER_MAX_DAY", "20"))
MAX_ATTEMPTS = int(os.environ.get("MYNDAIX_CONTROLLER_MAX_ATTEMPTS", "3"))
PENDING_STALE = int(os.environ.get("MYNDAIX_CONTROLLER_PENDING_STALE", "3600"))  # 1 h
FETCH_TIMEOUT = int(os.environ.get("MYNDAIX_CONTROLLER_FETCH_TIMEOUT", "60"))
REVIEW_TIMEOUT = int(os.environ.get("MYNDAIX_CONTROLLER_REVIEW_TIMEOUT", "60"))

DRY_RUN = os.environ.get("MYNDAIX_CONTROLLER_DRY_RUN") == "1"
TEST_MODE = os.environ.get("MYNDAIX_CONTROLLER_TEST_MODE") == "1"
DISPATCH_OVERRIDE = os.environ.get("MYNDAIX_CONTROLLER_DISPATCH_OVERRIDE", "")

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REF_RE = re.compile(r"^refs/heads/[A-Za-z0-9._][A-Za-z0-9._/-]*$")
# remote transports we accept. plain/unauth http:// and git:// are rejected; "ext::"/"fd::"
# (and any "::") are arbitrary-exec RCE. The SAME set is enforced at the git layer via
# GIT_ALLOW_PROTOCOL below, so an `insteadOf`/`protocol.ext.allow=always` config trick can't
# rewrite a validated URL into ext:: — including inside play-review's own ls-remote.
_URL_OK = ("https://", "ssh://", "file://", "git@")
# env-level protocol allowlist (overrides config, inherited by play-review). Authenticated/local
# only — NOT ext/fd (RCE) or git:// (plaintext). Preserves global insteadOf/credential helpers for
# the ALLOWED transports (so private-repo auth keeps working — Oracle/codex: do NOT nuke git config).
_GIT_ALLOW_PROTOCOL = "https:ssh:file"


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


def _ctl_pending_ref(ref: str) -> str:
    return f"refs/myndaix/pending/{_slug(ref)}"           # anchors the in-flight head against gc (codex MAJOR)


# -- minimal, allowlisted subprocess envs (build up, never inherit blindly) -----
def _git_env() -> dict:
    env = {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(HOME),
        "GIT_TERMINAL_PROMPT": "0",                      # never block on a credential prompt
        "GIT_ALLOW_PROTOCOL": _GIT_ALLOW_PROTOCOL,       # block ext::/fd:: RCE (keeps auth config working)
    }
    for k in ("SSH_AUTH_SOCK", "TMPDIR", "LANG"):        # ssh remotes / tmp
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def _review_env() -> dict:
    # play-review.sh resets its own PATH and derives ORCH from HOME. It needs HOME +
    # MYNDAIX_DSN (so its `mxr` calls reach the ledger) + ssh auth for confirm_pushed.
    # PLAY_SELF pins its worker to the validated trusted path (codex M7, no worktree fallback).
    # PLAY_DISABLE_AUTOFIX hard-disables autofix (B1). The controller passes an EMPTY remote URL
    # to play-review (see trigger_review), so confirm_pushed treats the dispatch as pushed and
    # writes the post-delivery done-<sha> marker WITHOUT a public PLAY_FORCE_DONE bypass and
    # WITHOUT running ls-remote (codex MAJOR). GIT_ALLOW_PROTOCOL is belt-and-suspenders.
    env = {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(HOME),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": _GIT_ALLOW_PROTOCOL,
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
        ["git", "-C", str(repo), *args],
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
    if not isinstance(raw, dict):                        # valid JSON but not an object (e.g. a list)
        log(f"repos.json is a {type(raw).__name__}, expected an object — skipping tick"); return []

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


# -- single-instance lock (kernel flock: atomic, auto-released on crash) ---------
# flock replaces the old mkdir + mtime-reap + heartbeat machinery wholesale: the kernel
# grants the lock to exactly one open fd and releases it automatically when the holder
# exits or dies, so there is NO stale-reap race (Oracle BLOCKER) and NO need for a TTL or
# heartbeat (codex/Oracle: writing LOCK/meta never bumped the dir mtime being checked). The
# fd is held open in a module global for the tick's lifetime; release closes it.
_LOCK_FD: Optional[int] = None


def acquire_lock() -> bool:
    global _LOCK_FD
    ORCH.mkdir(parents=True, exist_ok=True)
    # a pre-v0.4 build used a mkdir-DIR lock at this path; os.open would raise IsADirectoryError
    # forever. The old scheme is gone, so any dir here is a stale artifact — reap it (codex MAJOR).
    if LOCK.is_dir():
        log("reaping a legacy directory-style lock"); shutil.rmtree(LOCK, ignore_errors=True)
    fd = os.open(str(LOCK), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        log("another tick holds the lock — exiting"); return False
    _LOCK_FD = fd
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()} {int(_dt.datetime.now().timestamp())}\n".encode())
    except OSError:
        pass
    return True


def release_lock() -> None:
    global _LOCK_FD
    if _LOCK_FD is not None:
        try:
            fcntl.flock(_LOCK_FD, fcntl.LOCK_UN)
            os.close(_LOCK_FD)
        except OSError:
            pass
        _LOCK_FD = None


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


def trigger_review(repo: Repo, head: str, base: str) -> bool:
    """Fire the review via synthetic-stdin into the trusted play-review.sh. Returns True
    iff the trigger ran cleanly (or was recorded under the test seam). We pass an EMPTY
    remote URL: the dispatched sha is already on the remote (we fetched it), so play-review's
    confirm_pushed treats it as pushed and writes its post-delivery done-marker — no public
    force-done bypass, and no ls-remote network call (codex MAJOR)."""
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
            [str(PLAY_REVIEW), "origin", ""],            # empty URL -> confirm_pushed treats as pushed, no ls-remote
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


def _pin(repo: Repo, refname: str, sha: str) -> bool:
    """Anchor a sha behind a controller-owned ref so git gc can't prune it (codex M4 /
    Oracle MAJOR: a force-pushed-away sha would else fail cat-file forever). Returns the
    update-ref success so callers can refuse to advance onto an unpinnable object."""
    return _git(repo.path, "update-ref", refname, sha).returncode == 0


async def process_repo(led: PostgresLedger, repo: Repo, budget: list[int]) -> None:
    rid, ref = repo.repo_id, repo.watch_ref
    cur = await led.get_cursor(rid, ref)

    # advance pass: a prior dispatch whose review DELIVERED moves the cursor. The signal is
    # play-review's post-delivery done-<sha> marker; the controller passes an empty remote URL
    # so confirm_pushed treats the dispatch as pushed and writes the marker unconditionally
    # (branch-move-proof) without ls-remote or a PLAY_FORCE_DONE bypass.
    if cur and cur["pending_sha"]:
        ps = cur["pending_sha"]
        if _done(ps):
            # anchor the new base against gc BEFORE advancing onto it; if the object is
            # somehow gone, don't advance onto an unpinnable sha (would wedge cat-file).
            if not _pin(repo, _ctl_reviewed_ref(ref), ps):
                log(f"{rid}: cannot pin reviewed {ps[:8]} — not advancing")
            elif await led.advance_cursor(rid, ref, ps):
                log(f"{rid}: cursor advanced to {ps[:8]} (review delivered)")
            cur = await led.get_cursor(rid, ref)

    url = remote_url(repo)                                # validate transport BEFORE fetch (M1)
    if url is None:
        return

    # observe: fetch into a controller-OWNED ref (no FETCH_HEAD race, gc-safe — codex M3/M4)
    fr = _git(repo.path, "fetch", "--no-tags", "--no-recurse-submodules", "--quiet",
              "origin", f"+{ref}:{_ctl_head_ref(ref)}", timeout=FETCH_TIMEOUT)
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
        # anchor the baseline base vs gc FIRST; only seed if the pin holds, else a later
        # cat-file on an unpinned base could wedge (workflow MAJOR — matches the advance path).
        if not _pin(repo, _ctl_reviewed_ref(ref), head):
            log(f"{rid}: could not pin baseline {head[:8]} — not seeding this tick"); return
        if await led.upsert_baseline(rid, ref, head):
            log(f"{rid}: seeded baseline {head[:8]} (not reviewed)")
        return
    if head == cur["reviewed_sha"]:
        return                                           # up to date

    base = cur["reviewed_sha"]                            # review reviewed_sha..head (never empty-tree)
    if _git(repo.path, "cat-file", "-e", f"{head}^{{commit}}").returncode != 0 or \
       _git(repo.path, "cat-file", "-e", f"{base}^{{commit}}").returncode != 0:
        log(f"{rid}: head/base objects not present locally — skip"); return

    # nothing-to-review short-circuit: base..head has no net diff (empty/revert-net-zero commit).
    # play-review aborts on an empty diff and never marks done, so dispatching would re-try to the
    # BLOCKED ceiling (workflow MAJOR). Advance straight past it instead.
    if _git(repo.path, "diff", "--quiet", base, head).returncode == 0:
        if DRY_RUN:
            log(f"{rid}: DRY-RUN would skip empty-diff {head[:8]} (advance, no review)"); return
        if _pin(repo, _ctl_reviewed_ref(ref), head) and await led.skip_to(rid, ref, head):
            log(f"{rid}: no net diff {base[:8]}..{head[:8]} — advanced without review")
        return

    # ceiling: stop chasing a head that has failed MAX_ATTEMPTS dispatches
    if cur["pending_sha"] == head and cur["attempts"] >= MAX_ATTEMPTS and cur["state"] != "blocked":
        if await led.mark_blocked(rid, ref, head, MAX_ATTEMPTS):
            log(f"{rid}: BLOCKED {head[:8]} after {cur['attempts']} attempts — surfaced, backing off")
        return

    if budget[0] >= MAX_DISPATCH_PER_TICK:
        log(f"{rid}: per-tick dispatch budget reached — deferring {head[:8]} to next tick"); return
    if not DRY_RUN and _day_count() >= MAX_DISPATCH_PER_DAY:  # daily gate wraps ONLY dispatch, so the
        log(f"{rid}: daily dispatch budget reached — observing only"); return  # advance pass above always runs

    if DRY_RUN:
        log(f"{rid}: DRY-RUN would dispatch review {base[:8]}..{head[:8]}"); return

    stale_before = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=PENDING_STALE)
    if not await led.claim_dispatch(rid, ref, head, stale_before):
        log(f"{rid}: dispatch not claimed (in flight or blocked) — skip"); return
    # anchor the in-flight head against gc BEFORE dispatch: a later force-push overwrites the head
    # ref while this sha is still pending, so without its own ref gc could prune it before advance
    # (codex MAJOR). If the anchor can't be written, do NOT dispatch unanchored — release + retry.
    if not _pin(repo, _ctl_pending_ref(ref), head):
        log(f"{rid}: could not pin pending {head[:8]} — releasing, will retry next tick")
        await led.release_dispatch(rid, ref, head); return
    if trigger_review(repo, head, base):
        budget[0] += 1
        _charge_day()
    else:                                                # FRONT failed -> un-stick now, don't wait out PENDING_STALE
        await led.release_dispatch(rid, ref, head)
        log(f"{rid}: released dispatch after trigger failure — will retry next tick")


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
            log(f"daily dispatch budget ({MAX_DISPATCH_PER_DAY}) reached — advancing only, no new dispatches")
        led = await PostgresLedger.connect(DSN)
        budget = [0]
        try:
            for repo in repos:                           # the daily gate lives INSIDE process_repo (wraps
                try:                                     # only dispatch), so the free advance pass always runs
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
