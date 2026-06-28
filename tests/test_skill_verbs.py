"""+learning rung — DB-backed tests for the skill ledger verbs (index/select/record/prune)
against a real Postgres. The pure matching/injection core is in test_skillselect.py.

Run:  LEDGER_TEST_DSN=postgresql://localhost/runtime_test \
      PYTHONPATH=src python3 tests/test_skill_verbs.py
"""
import asyncio
import hashlib
import inspect
import os

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


def _sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def skill(name, trigger="src/*.py", desc="d", body="check the flock"):
    return {"name": name, "description": desc, "body": body,
            "body_sha": _sha(body), "content_sha": _sha(name + trigger + body),
            "path_trigger": trigger}


async def _truncate(led):
    async with led._pool.acquire() as con:
        await con.execute("TRUNCATE skill, skill_use")


async def test_index_upsert_idempotent_and_drift(led):
    await _truncate(led)
    r = await led.index_skills("repoA", [skill("a"), skill("b")])
    ok(r["upserted"] == 2, "two new skills upserted")
    r = await led.index_skills("repoA", [skill("a"), skill("b")])
    ok(r["upserted"] == 0, "re-index same content is a no-op (idempotent)")
    r = await led.index_skills("repoA", [skill("a", body="check the flock NOW"), skill("b")])
    ok(r["upserted"] == 1, "changed content re-upserts only the drifted skill")


async def test_index_archives_removed(led):
    await _truncate(led)
    await led.index_skills("repoA", [skill("a"), skill("b")])
    r = await led.index_skills("repoA", [skill("a")])          # b removed from the ref
    ok(r["archived_removed"] == 1, "a skill removed from the ref is archived")
    sel = await led.select_skills("repoA", ["src/x.py"])
    ok(all(s["name"] != "b" for s in sel["skills"]), "archived skill is not selected")


async def test_select_matches_caps_and_orders(led):
    await _truncate(led)
    await led.index_skills("repoA", [
        skill("broad", trigger="src/*.py"),            # specificity 1
        skill("specific", trigger="src/automerge.py"),  # specificity 2 (all literal)
        skill("third", trigger="src/*.py"),            # also matches -> forces the cap
        skill("nomatch", trigger="docs/*.md"),
    ])
    sel = await led.select_skills("repoA", ["src/automerge.py"])
    names = [s["name"] for s in sel["skills"]]
    ok("nomatch" not in names, "non-matching trigger excluded")
    ok(len(names) == 2, "capped at 2 even with 3 matching")
    ok(names[0] == "specific", "more specific trigger ranks first")


async def test_select_drops_sha_drift(led):
    await _truncate(led)
    await led.index_skills("repoA", [skill("a")])
    async with led._pool.acquire() as con:   # tamper the stored body, leave body_sha stale
        await con.execute("UPDATE skill SET body = 'TAMPERED' WHERE name = 'a'")
    sel = await led.select_skills("repoA", ["src/x.py"])
    ok(sel["skills"] == [], "sha-drifted skill is dropped from selection")
    ok("a" in sel["drift"], "drift surfaced for a jefe alert")


async def test_record_use_and_audit(led):
    await _truncate(led)
    await led.index_skills("repoA", [skill("a")])
    await led.record_skill_use("repoA", "play-1", [{"name": "a", "body_sha": _sha("check the flock")}])
    async with led._pool.acquire() as con:
        lu = await con.fetchval("SELECT last_used_at FROM skill WHERE name='a'")
        n = await con.fetchval("SELECT count(*) FROM skill_use WHERE skill_name='a'")
    ok(lu is not None, "record_skill_use bumps last_used_at")
    ok(n == 1, "an audit row is appended")


async def test_prune_transitions_no_reactivate(led):
    await _truncate(led)
    await led.index_skills("repoA", [skill("midaged"), skill("ancient")])
    async with led._pool.acquire() as con:
        # midaged: inactive 45d -> stale only (between STALE=30 and ARCHIVE=90 windows)
        await con.execute("UPDATE skill SET created_at = now() - interval '45 days' WHERE name='midaged'")
        # ancient: inactive 200d -> staled AND archived in the SAME prune (skips no window)
        await con.execute("UPDATE skill SET created_at = now() - interval '200 days' WHERE name='ancient'")
    r = await led.prune_skills()
    ok(r["staled"] == 2, "both inactive-active skills go stale")
    ok(r["archived"] == 1, "the long-inactive one is also archived in the same tick")
    async with led._pool.acquire() as con:
        s_mid = await con.fetchval("SELECT state FROM skill WHERE name='midaged'")
        s_anc = await con.fetchval("SELECT state FROM skill WHERE name='ancient'")
    ok(s_mid == "stale", "mid-aged skill stays stale (45d < 90d archive window)")
    ok(s_anc == "archived", "ancient skill archived")
    # no reactivate-on-reuse: a use on the archived skill does NOT revive it
    await led.record_skill_use("repoA", "p", [{"name": "ancient", "body_sha": _sha("x")}])
    async with led._pool.acquire() as con:
        st = await con.fetchval("SELECT state FROM skill WHERE name='ancient'")
    ok(st == "archived", "no reactivate-on-reuse (archived stays archived)")


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
