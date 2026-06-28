"""auto-capture ledger verbs (record_capture / mark_capture_proposed / resolve_capture) against a
real Postgres. The pure recurrence core is in test_capture.py.

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


def ok(cond, label):
    if cond:
        PASS[0] += 1
    else:
        FAIL[0] += 1
        print("  FAIL:", label)


async def _truncate(led):
    async with led._pool.acquire() as con:
        await con.execute("TRUNCATE capture_candidate")


async def test_record_increments_and_fires_at_threshold(led):
    await _truncate(led)
    ok(await led.record_capture("repoA", ["src/*.py"], 3) == [], "1st sighting -> below threshold, not ready")
    ok(await led.record_capture("repoA", ["src/*.py"], 3) == [], "2nd sighting -> still below")
    r = await led.record_capture("repoA", ["src/*.py"], 3)
    ok(len(r) == 1 and r[0]["path_glob"] == "src/*.py" and r[0]["seen_count"] == 3, "3rd sighting -> READY to propose")


async def test_multiple_globs_in_one_review(led):
    await _truncate(led)
    for _ in range(3):
        ready = await led.record_capture("repoA", ["a/*.sql", "b/*.swift"], 3)
    names = sorted(x["path_glob"] for x in ready)
    ok(names == ["a/*.sql", "b/*.swift"], "each changed glob accrues its own recurrence")


async def test_proposed_is_not_reproposed(led):
    await _truncate(led)
    for _ in range(3):
        await led.record_capture("repoA", ["a/*.sql"], 3)
    fp = capture.fingerprint("repoA", "a/*.sql")
    ok(await led.mark_capture_proposed(fp, 42) is True, "candidate -> proposed (CAS)")
    ok(await led.mark_capture_proposed(fp, 43) is False, "double-propose blocked by the state CAS")
    ok(await led.record_capture("repoA", ["a/*.sql"], 3) == [], "a proposed class keeps counting but is NOT re-returned")


async def test_declined_class_never_reproposed(led):
    await _truncate(led)
    for _ in range(3):
        await led.record_capture("repoA", ["x/*.ts"], 3)
    fp = capture.fingerprint("repoA", "x/*.ts")
    await led.mark_capture_proposed(fp, 7)
    ok(await led.resolve_capture(fp, "declined") is True, "proposed -> declined")
    ok(await led.record_capture("repoA", ["x/*.ts"], 3) == [], "a declined class is remembered, never re-proposed")
    ok(await led.resolve_capture(fp, "promoted") is False, "resolve is a CAS from 'proposed' only")


async def test_resolve_promoted_and_bad_outcome(led):
    await _truncate(led)
    for _ in range(3):
        await led.record_capture("repoB", ["m/*.go"], 3)
    fp = capture.fingerprint("repoB", "m/*.go")
    await led.mark_capture_proposed(fp, 9)
    ok(await led.resolve_capture(fp, "promoted") is True, "proposed -> promoted")
    raised = False
    try:
        await led.resolve_capture(fp, "garbage")
    except ValueError:
        raised = True
    ok(raised, "resolve_capture rejects a bad outcome (no silent bad state)")


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
