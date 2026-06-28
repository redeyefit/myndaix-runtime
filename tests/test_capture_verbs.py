"""auto-capture ledger verbs (v0.4) against a real Postgres — multi-signal recurrence (S3) + the
S6 state machine (record -> ready -> proposing -> proposed -> promoted|declined|stale). The pure
recurrence core is in test_capture.py.

Run:  LEDGER_TEST_DSN=postgresql://localhost/runtime_test PYTHONPATH=src python3 tests/test_capture_verbs.py
"""
import asyncio
import inspect
import os

from runtime import capture
from runtime.ledger.postgres_store import PostgresLedger

DSN = os.environ.get("LEDGER_TEST_DSN", "postgresql://localhost/runtime_test")
PASS = [0]
FAIL = [0]
TH = dict(min_recur=3, min_events=2, min_authors=1, repropose_mult=2)


def ok(cond, label):
    if cond:
        PASS[0] += 1
    else:
        FAIL[0] += 1
        print("  FAIL:", label)


async def _truncate(led):
    async with led._pool.acquire() as con:
        await con.execute("TRUNCATE capture_candidate, capture_occurrence")


async def sight(led, repo, tag, glob, commit, event, author, cross=True, **over):
    th = {**TH, **over}
    return await led.record_capture(repo, tag, glob, commit, event, author, cross, **th)


async def _drive_to_ready(led, repo="repoA", tag="fail-open", glob="src/*.py"):
    """3 distinct commits across 2 events, 1 author -> ready; returns the ready dict from the 3rd."""
    await sight(led, repo, tag, glob, "c1", "e1", "a1")
    await sight(led, repo, tag, glob, "c2", "e1", "a1")
    return await sight(led, repo, tag, glob, "c3", "e2", "a1")


# ---- S3: multi-signal recurrence --------------------------------------------------------
async def test_fires_only_when_all_signals_met(led):
    await _truncate(led)
    ok(await sight(led, "repoA", "fail-open", "src/*.py", "c1", "e1", "a1") is None, "1 commit -> not ready")
    ok(await sight(led, "repoA", "fail-open", "src/*.py", "c2", "e1", "a1") is None,
       "2 commits but ONE event -> not ready (anti-single-push)")
    r = await sight(led, "repoA", "fail-open", "src/*.py", "c3", "e2", "a1")
    ok(r is not None and r["rule_tag"] == "fail-open" and r["commits"] == 3,
       "3rd commit in a 2nd event -> READY")
    ok(r["events"] == 2 and r["authors"] == 1, "ready dict carries the distinct counts")


async def test_occurrence_deduped_per_commit(led):
    await _truncate(led)
    await sight(led, "repoA", "toctou-race", "src/*.py", "c1", "e1", "a1")
    await sight(led, "repoA", "toctou-race", "src/*.py", "c1", "e9", "a9")  # same commit, replayed
    fp = capture.fingerprint("repoA", "toctou-race")
    n = await led._pool.fetchval("SELECT count(*) FROM capture_occurrence WHERE fingerprint=$1", fp)
    ok(n == 1, "a replayed commit does not double-count (PK dedup)")


# ---- fail-closed gates ------------------------------------------------------------------
async def test_non_cross_family_records_nothing(led):
    await _truncate(led)
    ok(await sight(led, "repoA", "fail-open", "src/*.py", "c1", "e1", "a1", cross=False) is None,
       "single-family signal returns None")
    fp = capture.fingerprint("repoA", "fail-open")
    n = await led._pool.fetchval("SELECT count(*) FROM capture_candidate WHERE fingerprint=$1", fp)
    ok(n == 0, "single-family signal records NO candidate row at all")


async def test_offlist_tag_and_skills_path_record_nothing(led):
    await _truncate(led)
    ok(await sight(led, "repoA", "made-up-tag", "src/*.py", "c1", "e1", "a1") is None, "off-list tag ignored")
    ok(await sight(led, "repoA", "fail-open", "skills/x/SKILL.md", "c1", "e1", "a1") is None,
       "a signal from skills/** is ignored (no self-capture loop)")
    n = await led._pool.fetchval("SELECT count(*) FROM capture_candidate")
    ok(n == 0, "neither formed a candidate")


# ---- S6 state machine -------------------------------------------------------------------
async def test_claim_propose_resolve_happy_path(led):
    await _truncate(led)
    r = await _drive_to_ready(led)
    fp = r["fingerprint"]
    ok(await led.claim_for_proposing(fp, "skill/auto/fail-open", "deadbeef") is True, "ready -> proposing (CAS)")
    ok(await led.claim_for_proposing(fp, "skill/auto/fail-open", "deadbeef") is False, "double-claim blocked")
    ok(await led.count_open_proposals() == 1, "a proposing class counts as open")
    ok(await led.mark_capture_proposed(fp, 42) is True, "proposing -> proposed")
    ok(await led.mark_capture_proposed(fp, 43) is False, "double-propose blocked by CAS")
    ok(await led.resolve_capture(fp, "promoted") is True, "proposed -> promoted")
    ok(await led.count_open_proposals() == 0, "a promoted class is no longer open")
    ok(await led.resolve_capture(fp, "declined") is False, "resolve is a CAS from 'proposed' only")


async def test_already_ready_not_re_returned(led):
    await _truncate(led)
    await _drive_to_ready(led)
    ok(await sight(led, "repoA", "fail-open", "src/*.py", "c4", "e3", "a1") is None,
       "a class already 'ready' keeps counting but is not re-returned (no double-claim)")


async def test_release_proposing_recovers(led):
    await _truncate(led)
    r = await _drive_to_ready(led)
    fp = r["fingerprint"]
    await led.claim_for_proposing(fp, "skill/auto/fail-open", "sha")
    ok(await led.release_proposing(fp) is True, "proposing -> ready (recovery after a pre-PR failure)")
    ok(await led.claim_for_proposing(fp, "skill/auto/fail-open", "sha2") is True, "re-claimable after release")


async def test_declined_reproposes_only_past_higher_floor(led):
    await _truncate(led)
    r = await _drive_to_ready(led)
    fp = r["fingerprint"]
    await led.claim_for_proposing(fp, "b", "s")
    await led.mark_capture_proposed(fp, 7)
    ok(await led.resolve_capture(fp, "declined") is True, "proposed -> declined")
    dc = await led._pool.fetchval("SELECT decline_count FROM capture_candidate WHERE fingerprint=$1", fp)
    ok(dc == 1, "decline_count incremented")
    # base floor is 3; declined-once floor is 3*2=6. commits 4 and 5 do NOT re-fire.
    ok(await sight(led, "repoA", "fail-open", "src/*.py", "c4", "e3", "a1") is None, "4 commits < 6 floor")
    ok(await sight(led, "repoA", "fail-open", "src/*.py", "c5", "e4", "a1") is None, "5 commits < 6 floor")
    r6 = await sight(led, "repoA", "fail-open", "src/*.py", "c6", "e5", "a1")
    ok(r6 is not None and r6["decline_count"] == 1, "6th commit crosses the doubled floor -> re-ready")


async def test_resolve_bad_outcome_raises(led):
    await _truncate(led)
    r = await _drive_to_ready(led)
    await led.claim_for_proposing(r["fingerprint"], "b", "s")
    await led.mark_capture_proposed(r["fingerprint"], 9)
    raised = False
    try:
        await led.resolve_capture(r["fingerprint"], "garbage")
    except ValueError:
        raised = True
    ok(raised, "resolve_capture rejects a bad outcome (no silent bad state)")


async def test_expire_stale_closes_abandoned_pr(led):
    await _truncate(led)
    r = await _drive_to_ready(led)
    fp = r["fingerprint"]
    await led.claim_for_proposing(fp, "b", "s")
    await led.mark_capture_proposed(fp, 99)
    # backdate the proposal past the TTL
    await led._pool.execute(
        "UPDATE capture_candidate SET proposed_at = now() - interval '30 days' WHERE fingerprint=$1", fp)
    stale = await led.expire_stale_captures(14)
    ok(len(stale) == 1 and stale[0]["pr_number"] == 99, "an over-TTL proposal is returned for PR-close")
    ok(await led.count_open_proposals() == 0, "a stale class frees its MAX_OPEN slot (anti-wedge)")
    # a fresh proposal is NOT expired
    await _truncate(led)
    r2 = await _drive_to_ready(led)
    await led.claim_for_proposing(r2["fingerprint"], "b", "s")
    await led.mark_capture_proposed(r2["fingerprint"], 100)
    ok(await led.expire_stale_captures(14) == [], "a fresh proposal is not expired")


async def main():
    led = await PostgresLedger.connect(DSN)
    async with led._pool.acquire() as con:
        await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await led.init_schema()
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and inspect.iscoroutinefunction(v)]
    try:
        for t in tests:
            await t(led)
            print("PASS", t.__name__)
    finally:
        await led.close()
    print(f"ALL PASS ({PASS[0]} checks)" if FAIL[0] == 0 else f"FAILED ({FAIL[0]})")
    raise SystemExit(1 if FAIL[0] else 0)


if __name__ == "__main__":
    asyncio.run(main())
