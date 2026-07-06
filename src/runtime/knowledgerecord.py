"""knowledgerecord.py — the curator rung's deterministic I/O verbs (no LLM anywhere):

  - `mxr knowledge-ingest --scope research`
        walk the scope's corpus root (knowledge.walk_corpus), sync the derived knowledge_doc
        index (insert changed/new, tombstone missing, skip unchanged). Idempotent; per-scope
        advisory lock in the ledger verb.
  - `mxr recall --scope research "query" [--fenced] [-k N]`
        the retrieval ladder: websearch_to_tsquery -> prefix to_tsquery -> ILIKE, against the
        ACTIVE view only. Refreshes the index first (bounded, same walk — kills the stale-index
        class without a tick). --fenced nonce-fences every hit for prompt injection paths.
  - `mxr knowledge-rebuild --scope research --yes`
        admin resync: archive-tombstone everything, then re-ingest from disk (all appends,
        never TRUNCATE). Explicitly separate from normal ingest (audit semantics).

CONTRACT (differs from the review-path verbs ON PURPOSE): these are OPERATOR verbs, so an
unknown scope / missing root is a HARD ERROR (exit 2) — misconfiguration must never read as
"no knowledge" (design v0.4, fail-closed everywhere). Diagnostics -> stderr; hits/summary ->
stdout. Naming mirrors outcomes.py/outcomerecord.py (pure core / verb wiring).
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as _dt
import os
import re
import sys
import uuid

from runtime import knowledge
from runtime.ledger.postgres_store import PostgresLedger

DSN = os.environ.get("MYNDAIX_DSN", "postgresql://localhost/runtime")
RECALL_DEFAULT_K = 8

# play-review.sh clean() reproduced (skillselect._C0_DEL): C0 minus \t\n\r, plus DEL.
_C0_DEL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def log(msg: str) -> None:
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [knowledgerecord] {msg}", file=sys.stderr, flush=True)


def _fence(label: str, body: str, nonce: str) -> str:
    """skillselect._fence byte-for-byte: the ONE fencing idiom every prompt-injection path uses."""
    return (f"===BEGIN UNTRUSTED {label} nonce={nonce}===\n"
            + _C0_DEL.sub("", body)
            + f"\n===END UNTRUSTED nonce={nonce}===\n")


async def _sync(led: PostgresLedger, scope: str) -> dict:
    """Walk + sync ONE scope. Raises on unknown scope / bad root (hard error, caller exits 2)."""
    root = knowledge.resolve_scope(scope)
    walk = knowledge.walk_corpus(root)
    for w in walk.warnings:
        log(f"{scope}: {w}")
    res = await led.knowledge_sync(scope, [dataclasses.asdict(d) for d in walk.docs])
    res["md_docs"] = len(walk.docs)
    res["artifacts"] = len(walk.artifacts)
    return res


async def ingest(scope: str) -> int:
    led = await PostgresLedger.connect(DSN)
    try:
        res = await _sync(led, scope)
    finally:
        await led.close()
    print(f"{scope}: {res['md_docs']} md docs on disk — "
          f"inserted {res['inserted']}, tombstoned {res['tombstoned']}, "
          f"unchanged {res['unchanged']}")
    return 0


async def rebuild(scope: str) -> int:
    led = await PostgresLedger.connect(DSN)
    try:
        root = knowledge.resolve_scope(scope)       # hard-error BEFORE tombstoning anything
        walk = knowledge.walk_corpus(root)          # walk FIRST, then one-lock tombstone+reingest
        for w in walk.warnings:
            log(f"{scope}: {w}")
        res = await led.knowledge_rebuild(
            scope, [dataclasses.asdict(d) for d in walk.docs])
    finally:
        await led.close()
    print(f"{scope}: rebuilt — tombstoned {res['tombstoned']} then re-ingested {res['inserted']} "
          f"({len(walk.docs)} md docs on disk)")
    return 0


async def recall_hits(led: PostgresLedger, scope: str, query: str, k: int,
                      *, refresh: bool = True) -> tuple[str, list[dict]]:
    """The ladder against the ACTIVE view. Returns (rung_name, hits). Refresh-first is the
    freshness pass (bounded: one walk + sha compare, ~ms at corpus scale) — a recall can never
    cite a deleted file or miss a new one. Refresh failures degrade to a stale-index WARN, the
    query itself still runs (read paths stay available while a walk hiccups)."""
    query = query[:knowledge.QUERY_CAP_CHARS]
    if refresh:
        try:
            await _sync(led, scope)
        except ValueError:
            raise                                    # unknown scope stays a hard error
        except Exception as e:
            log(f"{scope}: freshness refresh failed ({e}) — results may be stale")
    hits = await led.knowledge_recall_fts(scope, query, k)
    if hits:
        return "fts", hits
    toks = knowledge.prefix_tokens(query)
    if toks:
        hits = await led.knowledge_recall_prefix(scope, toks, k)
        if hits:
            return "prefix", hits
    hits = await led.knowledge_recall_ilike(scope, knowledge.ilike_pattern(query), k)
    return ("ilike", hits) if hits else ("none", [])


def format_hits(rung: str, hits: list[dict], *, fenced: bool, nonce: str) -> str:
    """stdout payload. Plain: aligned rows for a human. Fenced: one UNTRUSTED region per hit —
    REQUIRED whenever recall output lands in a prompt (curate.py always uses it)."""
    if not hits:
        return "no hits\n"
    out: list[str] = []
    for h in hits:
        date = h.get("doc_date") or "undated"
        lossy = " [lossy]" if h.get("lossy") else ""
        head = re.sub(r"\s+", " ", str(h.get("headline") or "")).strip()
        body = f"{h['path']} ({date}){lossy}\n  {h.get('title','')}\n  {head}"
        if fenced:
            out.append(_fence("recall-hit", body, nonce))
        else:
            out.append(body + "\n")
    if not fenced:
        out.insert(0, f"# rung={rung} hits={len(hits)}\n")
    return "".join(out)


async def recall(scope: str, query: str, k: int, fenced: bool) -> int:
    led = await PostgresLedger.connect(DSN)
    try:
        rung, hits = await recall_hits(led, scope, query, k)
    finally:
        await led.close()
    nonce = os.environ.get("PLAY_NONCE") or uuid.uuid4().hex
    sys.stdout.write(format_hits(rung, hits, fenced=fenced, nonce=nonce))
    return 0


def _scope_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--scope", required=True, help="corpus scope (static allowlist; e.g. research)")


def ingest_main(argv: list) -> int:
    p = argparse.ArgumentParser(prog="knowledge-ingest")
    _scope_arg(p)
    a = p.parse_args(argv[1:])
    try:
        return asyncio.run(ingest(a.scope))
    except ValueError as e:                          # unknown scope / bad root: HARD error
        log(str(e)); return 2


def recall_main(argv: list) -> int:
    p = argparse.ArgumentParser(prog="recall")
    _scope_arg(p)
    p.add_argument("query")
    p.add_argument("-k", type=int, default=RECALL_DEFAULT_K)
    p.add_argument("--fenced", action="store_true",
                   help="nonce-fence each hit (REQUIRED on any path into a prompt)")
    a = p.parse_args(argv[1:])
    if not a.query.strip():
        log("empty query"); return 2
    try:
        return asyncio.run(recall(a.scope, a.query, max(1, min(a.k, 50)), a.fenced))
    except ValueError as e:
        log(str(e)); return 2


def rebuild_main(argv: list) -> int:
    p = argparse.ArgumentParser(prog="knowledge-rebuild")
    _scope_arg(p)
    p.add_argument("--yes", action="store_true", help="required (admin operation)")
    a = p.parse_args(argv[1:])
    if not a.yes:
        log("knowledge-rebuild is an admin resync (tombstone everything + re-ingest); "
            "re-run with --yes"); return 2
    try:
        return asyncio.run(rebuild(a.scope))
    except ValueError as e:
        log(str(e)); return 2


if __name__ == "__main__":
    raise SystemExit(ingest_main(sys.argv))
