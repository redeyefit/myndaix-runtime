"""outcomes-ledger ledger verbs (v0.3) against a real Postgres — the append-only state machine:
record_findings (CLOSE applied_fixed + OPEN with sticky dismissals), human_dismiss (fail-closed
prefix, human-terminal precedence), expire_open, outcome_stats. The pure identity/parser core is in
test_outcomes.py.

Run:  LEDGER_TEST_DSN=postgresql://localhost/runtime_test PYTHONPATH=src python3 tests/test_outcomes_verbs.py
"""
import asyncio
import inspect
import os

from runtime import outcomes
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
        await con.execute("TRUNCATE finding_outcome RESTART IDENTITY")


def _finding(tag, path, content, family):
    """A resolved open-finding dict as the wiring (PR-B) would hand to record_findings."""
    return {"tag": tag, "path": path, "line_hash": outcomes.line_hash(content),
            "reviewer_family": family}


async def _current(led, fk, fam):
    return await led._pool.fetchrow(
        "SELECT outcome, outcome_source FROM finding_current WHERE finding_key=$1 AND reviewer_family=$2",
        fk, fam)


# ---- OPEN phase -------------------------------------------------------------------------
async def test_open_phase_inserts_open_rows(led):
    await _truncate(led)
    f = _finding("fail-open", "src/a.py", "return None", "kilabz")
    res = await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py"], [f])
    ok(res["opened"] == 1 and res["closed"] == 0, "one open row inserted, nothing closed")
    fk = outcomes.finding_key("repoA", "fail-open", "src/a.py", f["line_hash"])
    row = await _current(led, fk, "kilabz")
    ok(row is not None and row["outcome"] == "open", "the finding is CURRENTLY open")


async def test_idempotent_re_record_is_noop(led):
    await _truncate(led)
    f = _finding("fail-open", "src/a.py", "return None", "kilabz")
    await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py"], [f])
    res2 = await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py"], [f])
    ok(res2["opened"] == 0, "re-recording the SAME review (same source_event) opens nothing (idempotent)")
    n = await led._pool.fetchval("SELECT count(*) FROM finding_outcome WHERE outcome='open'")
    ok(n == 1, "still exactly one open event (unique index dedup)")


# ---- CLOSE phase ------------------------------------------------------------------------
async def test_close_phase_inserts_applied_fixed_when_line_gone(led):
    await _truncate(led)
    f = _finding("fail-open", "src/a.py", "return None", "kilabz")
    await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py"], [f])
    fk = outcomes.finding_key("repoA", "fail-open", "src/a.py", f["line_hash"])
    # a LATER review of the same repo/ref whose diff touched src/a.py but no longer flags that line
    # (open_findings for src/a.py is empty) -> the stored hash is gone -> applied_fixed.
    res = await led.record_findings("repoA", "main", "tip2", "play2", ["src/a.py"], [])
    ok(res["closed"] == 1, "the vanished line is closed as applied_fixed")
    row = await _current(led, fk, "kilabz")
    ok(row["outcome"] == "applied_fixed" and row["outcome_source"] == "auto_fix_landed",
       "current state is applied_fixed via auto_fix_landed")


async def test_close_skips_still_flagged_line(led):
    await _truncate(led)
    f = _finding("fail-open", "src/a.py", "return None", "kilabz")
    await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py"], [f])
    # the SAME line is still flagged this review -> present -> NOT closed (still open).
    res = await led.record_findings("repoA", "main", "tip2", "play2", ["src/a.py"], [f])
    ok(res["closed"] == 0, "a line still flagged is NOT closed")


async def test_close_requires_exact_ref_and_changed_path(led):
    await _truncate(led)
    f = _finding("fail-open", "src/a.py", "return None", "kilabz")
    await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py"], [f])
    # a review on a DIFFERENT ref must not close a main finding
    r_ref = await led.record_findings("repoA", "feature/x", "tip2", "playX", ["src/a.py"], [])
    ok(r_ref["closed"] == 0, "a different-ref review does NOT close (exact ref scoping)")
    # a review that did NOT touch the finding's path must not close it
    r_path = await led.record_findings("repoA", "main", "tip3", "playY", ["src/other.py"], [])
    ok(r_path["closed"] == 0, "a review that didn't change the path does NOT close it")


# ---- sticky dismissal + human-terminal precedence ---------------------------------------
async def test_human_dismiss_and_sticky_reopen(led):
    await _truncate(led)
    f = _finding("fail-open", "src/a.py", "return None", "kilabz")
    await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py"], [f])
    fk = outcomes.finding_key("repoA", "fail-open", "src/a.py", f["line_hash"])
    res = await led.human_dismiss(fk[:12], "kilabz", "fp")
    ok(res.get("dismissed") == 1 and res["finding_key"] == fk, "human_dismiss wrote one fp row")
    row = await _current(led, fk, "kilabz")
    ok(row["outcome"] == "dismissed_false_positive", "current state is the human dismissal")
    # a LATER review re-flagging the SAME line must NOT re-open (sticky dismissal).
    res2 = await led.record_findings("repoA", "main", "tip2", "play2", ["src/a.py"], [f])
    ok(res2["opened"] == 0 and res2["skipped_dismissed"] == 1, "a dismissed key does NOT re-open (sticky)")
    row2 = await _current(led, fk, "kilabz")
    ok(row2["outcome"] == "dismissed_false_positive", "still dismissed after the re-record")


async def test_human_terminal_precedence_over_later_machine_row(led):
    await _truncate(led)
    f = _finding("fail-open", "src/a.py", "return None", "kilabz")
    await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py"], [f])
    fk = outcomes.finding_key("repoA", "fail-open", "src/a.py", f["line_hash"])
    await led.human_dismiss(fk[:12], "kilabz", "wontfix")
    # force a LATER machine row (higher seq) for the same key directly — a close event that races in.
    await led._pool.execute(
        """INSERT INTO finding_outcome (id, finding_key, repo_id, ref, rule_tag, reviewer_family,
               path, line_hash, source_event, tip_sha, outcome, outcome_source)
           VALUES (gen_random_uuid(), $1, 'repoA', 'main', 'fail-open', 'kilabz', 'src/a.py', $2,
                   'review:late', 'tipLate', 'applied_fixed', 'auto_fix_landed')""",
        fk, f["line_hash"])
    row = await _current(led, fk, "kilabz")
    ok(row["outcome"] == "dismissed_wontfix",
       "the human dismissed_* row WINS over a later (higher-seq) machine applied_fixed (precedence)")


async def test_human_dismiss_fail_closed_on_short_prefix(led):
    await _truncate(led)
    f = _finding("fail-open", "src/a.py", "return None", "kilabz")
    await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py"], [f])
    fk = outcomes.finding_key("repoA", "fail-open", "src/a.py", f["line_hash"])
    res = await led.human_dismiss(fk[:8], "kilabz", "fp")   # < 12 hex chars
    ok("error" in res, "a <12-hex prefix is refused (fail-closed)")
    row = await _current(led, fk, "kilabz")
    ok(row["outcome"] == "open", "nothing was dismissed on the short-prefix refusal")


async def test_human_dismiss_fail_closed_on_ambiguous_prefix(led):
    await _truncate(led)
    # two DISTINCT keys that share a >=12-hex prefix don't occur naturally (sha256), so seed two rows
    # whose finding_key we control to share a prefix, and assert the verb refuses + lists both.
    shared = "abcdef012345"
    for suffix in ("aaaa", "bbbb"):
        fk = shared + suffix + "0" * (64 - len(shared) - 4)
        await led._pool.execute(
            """INSERT INTO finding_outcome (id, finding_key, repo_id, ref, rule_tag, reviewer_family,
                   path, line_hash, source_event, tip_sha, outcome, outcome_source)
               VALUES (gen_random_uuid(), $1, 'repoA', 'main', 'fail-open', 'kilabz', 'src/a.py',
                       'h', 'review:x', 'tip', 'open', 'review_raised')""", fk)
    res = await led.human_dismiss(shared, "kilabz", "fp")
    ok("error" in res and len(res.get("candidates", [])) == 2,
       "an ambiguous prefix is refused and BOTH colliding full keys are returned (fail-closed)")


# ---- cross-file collision does NOT merge histories --------------------------------------
async def test_cross_file_collision_does_not_merge(led):
    await _truncate(led)
    fa = _finding("fail-open", "src/a.py", "return None", "kilabz")
    fb = _finding("fail-open", "src/b.py", "return None", "kilabz")   # SAME line, different file
    ok(fa["line_hash"] == fb["line_hash"], "the two files share a line_hash (same content)")
    await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py", "src/b.py"], [fa, fb])
    ka = outcomes.finding_key("repoA", "fail-open", "src/a.py", fa["line_hash"])
    kb = outcomes.finding_key("repoA", "fail-open", "src/b.py", fb["line_hash"])
    ok(ka != kb, "path-in-key gives them DISTINCT finding_keys")
    # dismiss a.py's finding; b.py's must stay open (histories not merged).
    await led.human_dismiss(ka[:12], "kilabz", "fp")
    ok((await _current(led, ka, "kilabz"))["outcome"] == "dismissed_false_positive", "a.py finding dismissed")
    ok((await _current(led, kb, "kilabz"))["outcome"] == "open", "b.py finding UNAFFECTED (separate history)")


# ---- expire_open ------------------------------------------------------------------------
async def test_expire_open(led):
    await _truncate(led)
    f = _finding("fail-open", "src/a.py", "return None", "kilabz")
    await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py"], [f])
    fk = outcomes.finding_key("repoA", "fail-open", "src/a.py", f["line_hash"])
    ok(await led.expire_open(30) == 0, "a fresh open finding is NOT expired")
    # backdate the open row past the TTL
    await led._pool.execute(
        "UPDATE finding_outcome SET created_at = now() - interval '40 days' WHERE finding_key=$1", fk)
    ok(await led.expire_open(30) == 1, "an over-TTL open finding is expired")
    ok((await _current(led, fk, "kilabz"))["outcome"] == "expired", "current state is expired")
    ok(await led.expire_open(30) == 0, "re-running the sweep the same UTC day is a no-op (deterministic source_event)")


# ---- outcome_stats ----------------------------------------------------------------------
async def test_outcome_stats(led):
    await _truncate(led)
    # one applied_fixed + one dismissed_false_positive for the SAME class -> precision 0.5.
    f1 = _finding("fail-open", "src/a.py", "line one", "kilabz")
    f2 = _finding("fail-open", "src/b.py", "line two", "kilabz")
    await led.record_findings("repoA", "main", "t1", "p1", ["src/a.py", "src/b.py"], [f1, f2])
    k2 = outcomes.finding_key("repoA", "fail-open", "src/b.py", f2["line_hash"])
    await led.human_dismiss(k2[:12], "kilabz", "fp")                 # f2 -> fp
    await led.record_findings("repoA", "main", "t2", "p2", ["src/a.py"], [])   # f1 -> applied_fixed
    stats = await led.outcome_stats()
    ok(stats["open_count"] == 0, "no findings left open")
    row = next((r for r in stats["precision"] if r["rule_tag"] == "fail-open"
                and r["reviewer_family"] == "kilabz"), None)
    ok(row is not None and row["applied_fixed"] == 1 and row["dismissed_false_positive"] == 1,
       "the class shows 1 applied_fixed + 1 fp")
    ok(abs(float(row["precision"]) - 0.5) < 1e-9, "precision = 1/(1+1) = 0.5")


async def main():
    led = await PostgresLedger.connect(DSN)
    async with led._pool.acquire() as con:
        await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await led.init_schema()
    await led.migrate()   # prove 0008 is idempotent against the schema.sql mirror (IF NOT EXISTS)
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
