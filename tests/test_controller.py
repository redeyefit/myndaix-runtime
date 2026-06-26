"""controller.py proofs — the brain's tick over REAL temp git repos + a real ledger.

Each test builds a bare "remote" + a working clone, drives the level-triggered
reconcile, and asserts the decision (seed / dispatch / dedup / advance / block /
skip) via the cursor table + the TEST-MODE dispatch seam (records the would-be
play-review invocation instead of running it). No live pool / no play-review needed.

Setup (once):  createdb runtime_test
Run:
    LEDGER_TEST_DSN=postgresql://localhost/runtime_test \\
        PYTHONPATH=src python3 tests/test_controller.py
"""
import asyncio
import datetime as _dt
import inspect
import json
import os
import subprocess
import tempfile
from pathlib import Path

import asyncpg

from runtime.ledger.postgres_store import PostgresLedger
import runtime.controller as C

DSN = os.environ.get("LEDGER_TEST_DSN", "postgresql://localhost/runtime_test")
C.DSN = DSN

_TMP = Path(tempfile.mkdtemp(prefix="ctrl-test-"))
_N = [0]


def g(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r


def make_repo(name: str) -> C.Repo:
    bare = _TMP / f"{name}.git"
    work = _TMP / name
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    g(work, "config", "user.email", "t@t"); g(work, "config", "user.name", "t")
    (work / "f.txt").write_text("0\n")
    g(work, "add", "-A"); g(work, "commit", "-q", "-m", "c0")
    g(work, "remote", "add", "origin", f"file://{bare}")  # file:// = an allowed transport
    g(work, "push", "-q", "-u", "origin", "main")
    return C.Repo(name, work, "refs/heads/main")


def advance(repo: C.Repo, content: str) -> str:
    (repo.path / "f.txt").write_text(content + "\n")
    g(repo.path, "add", "-A"); g(repo.path, "commit", "-q", "-m", content)
    g(repo.path, "push", "-q", "origin", "main")
    return g(repo.path, "rev-parse", "HEAD").stdout.strip()


def head_of(repo: C.Repo) -> str:
    return g(repo.path, "rev-parse", "HEAD").stdout.strip()


def fresh_seam(name: str) -> Path:
    """Point STATE + the dispatch seam at a clean per-test dir; enable the test seam."""
    base = _TMP / f"seam-{name}-{_N[0]}"; _N[0] += 1
    state = base / "state"; state.mkdir(parents=True)
    C.STATE = state
    C.TEST_MODE = True
    C.DISPATCH_OVERRIDE = str(base / "dispatched.jsonl")
    C.DRY_RUN = False
    C.MAX_DISPATCH_PER_TICK = 3
    return Path(C.DISPATCH_OVERRIDE)


def records(seam: Path) -> list[dict]:
    if not seam.exists():
        return []
    return [json.loads(l) for l in seam.read_text().splitlines() if l.strip()]


async def _truncate(led: PostgresLedger) -> None:
    async with led._pool.acquire() as con:
        await con.execute("TRUNCATE review_cursor RESTART IDENTITY CASCADE")


# -- first sight seeds a baseline and reviews NOTHING (B2) ----------------------
async def test_bootstrap_seeds_no_dispatch(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("boot"); repo = make_repo("boot")
    await C.process_repo(led, repo, [0])
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur is not None and cur["state"] == "baseline"
    assert cur["reviewed_sha"] == head_of(repo) and cur["pending_sha"] is None
    assert records(seam) == [], "the brain must NOT review on first sight"


# -- HEAD advances -> one dispatch of exactly reviewed..head -------------------
async def test_advance_dispatches_correct_range(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("adv"); repo = make_repo("adv")
    base = head_of(repo)
    await C.process_repo(led, repo, [0])                 # seed baseline at `base`
    head2 = advance(repo, "c1")
    await C.process_repo(led, repo, [0])                 # HEAD moved -> dispatch
    recs = records(seam)
    assert len(recs) == 1, f"expected one dispatch, got {len(recs)}"
    assert recs[0]["base"] == base and recs[0]["head"] == head2
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["pending_sha"] == head2 and cur["state"] == "dispatching"


# -- a fresh in-flight head is NOT re-dispatched (dedup) -----------------------
async def test_inflight_is_not_redispatched(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("dedup"); repo = make_repo("dedup")
    await C.process_repo(led, repo, [0])
    advance(repo, "c1")
    await C.process_repo(led, repo, [0])                 # dispatch #1
    await C.process_repo(led, repo, [0])                 # same head, in flight -> no-op
    assert len(records(seam)) == 1, "an in-flight head must not re-dispatch"


# -- a delivered review (done-<sha> marker) advances the cursor ---------------
async def test_done_marker_advances_cursor(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("done"); repo = make_repo("done")
    await C.process_repo(led, repo, [0])
    head2 = advance(repo, "c1")
    await C.process_repo(led, repo, [0])                 # pending = head2
    (C.STATE / f"done-{head2}").write_text("")           # play-review "delivered" (marker)
    await C.process_repo(led, repo, [0])                 # advance pass picks it up
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["reviewed_sha"] == head2 and cur["pending_sha"] is None
    assert cur["state"] == "delivered"
    assert len(records(seam)) == 1, "no extra dispatch once delivered + up to date"


# -- a synchronous trigger failure releases the claim (no 1h stall) ------------
async def test_trigger_failure_releases(led: PostgresLedger) -> None:
    await _truncate(led)
    fresh_seam("trigfail"); repo = make_repo("trigfail")
    await C.process_repo(led, repo, [0])                 # baseline
    head2 = advance(repo, "c1")
    C.TEST_MODE = False; C.DISPATCH_OVERRIDE = ""        # force the REAL trigger path
    C.PLAY_REVIEW = repo.path / "no-such-play-review.sh" # untrusted/missing -> trigger fails
    try:
        await C.process_repo(led, repo, [0])             # claims, trigger fails, releases
    finally:
        C.TEST_MODE = True
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["pending_sha"] == head2 and cur["state"] == "dispatching"
    # released = updated_at forced stale -> a stale-cutoff re-claim fires immediately (no PENDING_STALE wait)
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=1)
    assert await led.claim_dispatch(repo.repo_id, repo.watch_ref, head2, cutoff) is True


# -- the flock is exclusive: a second holder cannot acquire it -----------------
async def test_lock_is_exclusive(led: PostgresLedger) -> None:
    import fcntl
    C.LOCK = _TMP / "controller.lock"                    # don't touch the live orchestrator dir
    assert C.acquire_lock() is True
    fd2 = os.open(str(C.LOCK), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        raised = False
        try:
            fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raised = True
        assert raised, "a second flock must NOT acquire the held lock"
    finally:
        os.close(fd2)
    C.release_lock()
    assert C.acquire_lock() is True, "lock is re-acquirable after release"
    C.release_lock()


# -- a legacy mkdir-style lock DIRECTORY is reaped, not a permanent crash ------
async def test_legacy_lock_dir_is_reaped(led: PostgresLedger) -> None:
    import fcntl
    C.LOCK = _TMP / "controller.lock.legacy"
    C.LOCK.mkdir(parents=True, exist_ok=True)            # simulate the pre-v0.4 dir lock
    (C.LOCK / "pid").write_text("123")                   # non-empty -> needs rmtree, not rmdir
    assert C.acquire_lock() is True, "must reap a legacy dir lock and acquire"
    assert not C.LOCK.is_dir(), "legacy dir replaced by the flock file"
    C.release_lock()


# -- a new head while one is in flight WAITS (no supersede, Oracle B2) ---------
async def test_new_head_waits_for_inflight(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("wait"); repo = make_repo("wait")
    await C.process_repo(led, repo, [0])
    advance(repo, "c1")
    await C.process_repo(led, repo, [0])                 # dispatch head2 (in flight)
    advance(repo, "c2")                                  # head3 arrives before head2 delivered
    await C.process_repo(led, repo, [0])                 # must NOT dispatch head3
    assert len(records(seam)) == 1, "an in-flight head must not be superseded by a new head"


# -- a fetch failure skips the repo, never crashes, never seeds ----------------
async def test_fetch_failure_skips(led: PostgresLedger) -> None:
    await _truncate(led)
    fresh_seam("fetchfail"); repo = make_repo("fetchfail")
    g(repo.path, "remote", "set-url", "origin", str(_TMP / "does-not-exist.git"))
    await C.process_repo(led, repo, [0])                 # must not raise
    assert await led.get_cursor(repo.repo_id, repo.watch_ref) is None, "no cursor on a failed observe"


# -- the per-tick dispatch budget caps work across repos -----------------------
async def test_per_tick_budget(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("budget"); C.MAX_DISPATCH_PER_TICK = 1
    r1, r2 = make_repo("budgetA"), make_repo("budgetB")
    for r in (r1, r2):
        await C.process_repo(led, r, [0])               # seed both
        advance(r, "c1")
    budget = [0]
    await C.process_repo(led, r1, budget)               # spends the only slot
    await C.process_repo(led, r2, budget)               # deferred
    assert len(records(seam)) == 1, "per-tick budget must cap dispatches"


# -- a head that failed MAX_ATTEMPTS gets blocked, not re-dispatched ------------
async def test_blocked_after_max_attempts(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("block"); repo = make_repo("block")
    await C.process_repo(led, repo, [0])
    head2 = advance(repo, "c1")
    await C.process_repo(led, repo, [0])                 # pending=head2, attempts=1
    async with led._pool.acquire() as con:              # simulate the ceiling reached
        await con.execute(
            "UPDATE review_cursor SET attempts=$1 WHERE repo_id=$2",
            C.MAX_ATTEMPTS, repo.repo_id)
    before = len(records(seam))
    await C.process_repo(led, repo, [0])                 # must block, not dispatch
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["state"] == "blocked"
    assert len(records(seam)) == before, "a blocked head must not re-dispatch"


# -- config: duplicate basenames rejected; junk entries skipped ----------------
async def test_load_config_dedup_and_validation(led: PostgresLedger) -> None:
    a = make_repo("dup")                                  # basename 'dup'
    nested = _TMP / "other" / "dup"; nested.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(nested)], check=True)  # same basename, diff path
    notgit = _TMP / "plain"; notgit.mkdir(exist_ok=True)
    cfg = {
        "_comment": "ignored",
        "dup": {"path": str(a.path)},
        "dup2": {"path": str(nested)},                   # duplicate basename -> rejected
        "plain": {"path": str(notgit)},                  # not a git repo -> skipped
        "badref": {"path": str(a.path), "watch_ref": "refs/heads/m;rm -rf"},  # bad ref -> skipped
    }
    cfgfile = _TMP / "repos.json"; cfgfile.write_text(json.dumps(cfg))
    C.REPOS_JSON = cfgfile
    repos = C.load_config()
    assert len(repos) == 1 and repos[0].repo_id == "dup", f"got {[r.repo_id for r in repos]}"


# -- security: ext:: (and other exec transports) remote urls are refused -------
async def test_remote_url_rejects_exec_transport(led: PostgresLedger) -> None:
    repo = make_repo("url")
    g(repo.path, "remote", "set-url", "origin", "ext::sh -c whoami")
    assert C.remote_url(repo) is None, "ext:: transport must be rejected"
    g(repo.path, "remote", "set-url", "origin", "git://anon/x.git")
    assert C.remote_url(repo) is None, "plain git:// transport must be rejected"
    g(repo.path, "remote", "set-url", "origin", "https://github.com/x/y.git")
    assert C.remote_url(repo) == "https://github.com/x/y.git"


# -- the test seam fails closed if armed without TEST_MODE ---------------------
async def test_dispatch_override_requires_test_mode(led: PostgresLedger) -> None:
    seam = fresh_seam("guard"); repo = make_repo("guard")
    C.TEST_MODE = False                                  # override set, test-mode OFF
    ok = C.trigger_review(repo, "a" * 40, "b" * 40)
    assert ok is False and records(seam) == [], "must refuse the override outside test mode"
    C.TEST_MODE = True


async def main() -> None:
    led = await PostgresLedger.connect(DSN)
    async with led._pool.acquire() as con:
        await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await led.init_schema()
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and inspect.iscoroutinefunction(v)]
    passed = 0
    try:
        for t in tests:
            await t(led)
            print("PASS", t.__name__)
            passed += 1
    finally:
        await led.close()
    print(f"ALL PASS ({passed})")


if __name__ == "__main__":
    asyncio.run(main())
