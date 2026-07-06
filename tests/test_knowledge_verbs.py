"""knowledge_doc ledger verbs against a real Postgres: sync (insert/skip/tombstone), the
restore-after-archive-of-identical-content case (WHY there is no unique index), the recall
ladder rungs against the ACTIVE view, and the rebuild sweep. Proves 0009 is idempotent against
the schema.sql mirror (main() runs init_schema THEN migrate, like test_outcomes_verbs).

Run:  LEDGER_TEST_DSN=postgresql://localhost/runtime_test PYTHONPATH=src python3 tests/test_knowledge_verbs.py
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


async def _truncate(led):
    async with led._pool.acquire() as con:
        await con.execute("TRUNCATE knowledge_doc RESTART IDENTITY")


def _doc(path, body, **kw):
    d = {"path": path, "title": kw.get("title", path), "tags": kw.get("tags", ""),
         "doc_date": kw.get("doc_date"), "body": body,
         "content_sha": hashlib.sha256(body.encode()).hexdigest(),
         "lossy": kw.get("lossy", False)}
    return d


async def _current(led, scope, path):
    return await led._pool.fetchrow(
        "SELECT status, content_sha, lossy FROM knowledge_doc_current WHERE scope=$1 AND path=$2",
        scope, path)


# ---- sync -------------------------------------------------------------------------------------
async def test_sync_insert_skip_change(led):
    await _truncate(led)
    d = _doc("a.md", "# A\nhiggsfield pricing brief", doc_date="2026-06-08")
    r1 = await led.knowledge_sync("research", [d])
    ok(r1 == {"inserted": 1, "tombstoned": 0, "unchanged": 0}, "first sync inserts")
    r2 = await led.knowledge_sync("research", [d])
    ok(r2 == {"inserted": 0, "tombstoned": 0, "unchanged": 1}, "re-sync is a no-op (idempotent)")
    d2 = _doc("a.md", "# A\nedited body")
    r3 = await led.knowledge_sync("research", [d2])
    ok(r3["inserted"] == 1, "changed sha appends a new event")
    row = await _current(led, "research", "a.md")
    ok(row["content_sha"] == d2["content_sha"], "current view shows the latest event")
    n = await led._pool.fetchval("SELECT count(*) FROM knowledge_doc")
    ok(n == 2, "append-only: both events retained")


async def test_sync_tombstone_and_restore_identical(led):
    await _truncate(led)
    d = _doc("b.md", "# B\nsame content forever")
    await led.knowledge_sync("research", [d])
    r = await led.knowledge_sync("research", [])           # file gone from disk
    ok(r["tombstoned"] == 1, "missing file tombstoned")
    row = await _current(led, "research", "b.md")
    ok(row["status"] == "archived", "tombstone is current")
    active = await led._pool.fetchval(
        "SELECT count(*) FROM knowledge_doc_active WHERE scope='research'")
    ok(active == 0, "active view hides tombstones")
    # THE case the missing unique index protects: the file comes back with IDENTICAL content.
    r2 = await led.knowledge_sync("research", [d])
    ok(r2["inserted"] == 1, "restore-after-archive of identical content INSERTS (no ghost)")
    row2 = await _current(led, "research", "b.md")
    ok(row2["status"] == "active" and row2["content_sha"] == d["content_sha"],
       "restored doc is current + active again")


async def test_sync_scope_isolation_and_bad_date(led):
    await _truncate(led)
    await led.knowledge_sync("research", [_doc("s.md", "# scoped")])
    other = await led._pool.fetchval(
        "SELECT count(*) FROM knowledge_doc_active WHERE scope='other'")
    ok(other == 0, "scope isolation: other scope sees nothing")
    d = _doc("bad-date.md", "# x", doc_date="2026-13-99")   # regex-passing, not a real date
    await led.knowledge_sync("research", [d])
    row = await led._pool.fetchrow(
        "SELECT doc_date FROM knowledge_doc_current WHERE scope='research' AND path='bad-date.md'")
    ok(row["doc_date"] is None, "invalid ISO date stored as NULL, not a crash")


# ---- recall ladder ----------------------------------------------------------------------------
async def test_recall_fts_rung(led):
    await _truncate(led)
    await led.knowledge_sync("research", [
        _doc("2026-06-22-higgsfield-api.md", "# Higgsfield API automation\npricing and endpoints",
             doc_date="2026-06-22", tags="higgsfield api"),
        _doc("2026-06-20-gemini.md", "# Gemini Live\nrealtime voice for fieldvision",
             doc_date="2026-06-20"),
        _doc("lossy-doc.md", "# Corrupt\nhiggsfield mentioned here too", lossy=True),
    ])
    hits = await led.knowledge_recall_fts("research", "higgsfield pricing", 8)
    ok(hits and hits[0]["path"] == "2026-06-22-higgsfield-api.md",
       "fts rung ranks the both-terms doc first")
    ok(any(h["lossy"] for h in hits if h["path"] == "lossy-doc.md") or
       all(h["path"] != "lossy-doc.md" for h in hits), "lossy flag surfaced when hit")
    ok(all("headline" in h for h in hits), "headline present (top-k only)")
    none = await led.knowledge_recall_fts("research", "zzqx notaword", 8)
    ok(none == [], "no-match fts returns empty (ladder falls through)")


async def test_recall_prefix_and_ilike_rungs(led):
    await _truncate(led)
    await led.knowledge_sync("research", [
        _doc("tool.md", "# Tooling\nthe play-review worker and mxr dispatch live here"),
    ])
    hits = await led.knowledge_recall_prefix("research", ["dispat"], 8)
    ok(hits and hits[0]["path"] == "tool.md", "prefix rung matches dispat:* -> dispatch")
    ok(await led.knowledge_recall_prefix("research", [], 8) == [],
       "empty token list -> empty (rung skipped upstream)")
    hits2 = await led.knowledge_recall_ilike("research", "%play-review%", 8)
    ok(hits2 and hits2[0]["path"] == "tool.md", "ilike rung catches the hyphenated code token")
    ok(await led.knowledge_recall_ilike("research", "%nOtThErE%", 8) == [], "ilike miss is empty")


async def test_recall_active_only(led):
    await _truncate(led)
    d = _doc("gone.md", "# Gone\nunique zebra content")
    await led.knowledge_sync("research", [d])
    await led.knowledge_sync("research", [])               # tombstone it
    ok(await led.knowledge_recall_fts("research", "zebra", 8) == [],
       "recall never cites a deleted (tombstoned) doc")


# ---- rebuild ----------------------------------------------------------------------------------
async def test_rebuild_single_lock(led):
    await _truncate(led)
    await led.knowledge_sync("research", [_doc("r1.md", "# r1 old"), _doc("r2.md", "# r2")])
    # rebuild with a CHANGED r1 + dropped r2 + new r3, all under one lock (no empty-index window)
    res = await led.knowledge_rebuild("research", [_doc("r1.md", "# r1 new"), _doc("r3.md", "# r3")])
    ok(res["tombstoned"] == 2 and res["inserted"] == 2, "rebuild tombstones all active then reingests")
    active = {r["path"] for r in await led._pool.fetch(
        "SELECT path FROM knowledge_doc_active WHERE scope='research'")}
    ok(active == {"r1.md", "r3.md"}, "active set = the freshly walked docs (r2 gone, r3 in)")
    row = await _current(led, "research", "r1.md")
    ok(row["content_sha"] == _doc("r1.md", "# r1 new")["content_sha"], "r1 rebuilt to new content")
    total = await led._pool.fetchval("SELECT count(*) FROM knowledge_doc")
    ok(total == 2 + 2 + 2, "all appends — full history retained, never TRUNCATE")


async def main():
    led = await PostgresLedger.connect(DSN)
    async with led._pool.acquire() as con:
        await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await led.init_schema()
    await led.migrate()   # prove 0009 is idempotent against the schema.sql mirror
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
