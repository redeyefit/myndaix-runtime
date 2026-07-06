# Curator v1 — DESIGN (research/ → the first folder-agent)

_Status: v0.1 draft — for cross-family design review (kilabz + oracle), then Jefe plan approval.
The confirmed next rung (2026-07-04): promote `~/research` from a passive folder to a curated
corpus with an on-demand librarian agent in the runtime roster. Zero teeth in v1. Prior-art +
in-repo recon: 6-reader workflow `wf_357e9782` (2026-07-05)._

## What

A **curator** agent — the librarian of the `~/research` corpus, NOT the field researcher. Invoked
on demand (`mxr curator "…"`), it answers "do we already know X?" with citations before anything
gets re-researched, files new reports per the corpus's own conventions, maintains a single
`index.md` (the map of the corpus), cross-links related briefs, and reports duplicates, dead
directions, and gaps. Four pieces: (1) a `knowledge_doc` tsvector table + `mxr knowledge-ingest` /
`mxr recall` verbs (deterministic, no LLM), (2) one AgentSpec row in the registry, (3) a small
`workdir` adapter key in the runner, (4) a **curator constitution** written into the corpus's own
`CLAUDE.md` — the reusable piece you drop into any future corpus folder.

## Why

The named pain (Jefe, 2026-07-04): *"I gather reports and don't reopen them."* Without a librarian
the corpus rots — re-research, orphan reports, grows-but-never-compounds. The survey confirms it:
15 real documents across FOUR scattered locations, no index, dead directions (the killed marketing
thread) distinguishable only by reading timestamps, 5 orphan strays. The curator's value in v1 is
**retrieval + compounding, which needs zero authority** — and it proves the folder→agent shape for
every later folder (`ask/` → dispatcher, `runtime-engine/` → producer).

## The folder→agent shape (the load-bearing idea)

An agent = a registry row; its **behavior lives in the folder itself**. `claude -p` loads
`CLAUDE.md` from its cwd — so `adapter.workdir = ~/research` makes the folder's own constitution
the agent's operating rules. Nothing agent-specific is hardcoded in the runtime: the reusable
"curator skill" is the constitution template, and pointing it at another corpus later = another
registry row + a `CLAUDE.md`. (Per registry.py:1-7: roster is data, never code.)

- **Registry row:** `curator` — reach=CLI, authority=WORKSPACE_ACTOR (it writes files),
  model pinned (see open call #4), adapter `{kind: cli, argv: [claude, -p, --model, …,
  --output-format, text, --permission-mode dontAsk, --allowedTools "Read Glob Grep Write Edit"],
  prompt_channel: stdin, env_passthrough: [CLAUDE_CODE_OAUTH_TOKEN], workdir: ~/research}`.
  No Bash, no WebSearch, no mxr — enforced at the tool-allowlist layer, not by prompt alone.
- **Runner change (~5 lines):** `invoke_cli` honors `adapter.workdir` when declared and the dir
  exists; otherwise the PR #39 fresh-scratch-cwd behavior is unchanged. Declaring workdir is a
  deliberate, narrow, per-agent reopening of cwd — pinned by tests so the gate-cwd bug class
  can't silently return.

## Data flow

**1. Ingest (deterministic Python — no LLM):** `mxr knowledge-ingest --scope research`
walks the corpus root for `*.md`, excluding noise globs (`.venv*`, `__pycache__`,
`.playwright-mcp`, `.claude`, dotfiles). Per file: title = first `#` heading (fallback filename),
tags = frontmatter if parseable else empty, body capped at 900KB (tsvector 1MB limit) with a
truncation marker, `content_sha` over raw bytes. Append-only writes under a per-scope advisory
lock: if the current row for (scope, path) has the same sha and status → skip; else INSERT a new
row. Files present in the table but gone from disk → INSERT a tombstone row (status=archived).
Idempotent, concurrent-safe, rebuildable from disk at any time — **files are the source of truth,
Postgres is only the derived search index.**

**2. Recall (deterministic read verb):** `mxr recall --scope research "query"` — scope REQUIRED
(fail-closed; no scope = error, unknown scope = empty). Query ladder per FTS recon:
`websearch_to_tsquery` (never errors on LLM-issued text) → on zero hits, sanitized
`to_tsquery('tok:* & …')` prefix form → on zero hits, `ILIKE` substring (catches `play-review`,
`mxr`, code tokens FTS can't). Rank `ts_rank_cd(tsv, q, 1)` over
`setweight(title 'A' || tags 'B' || body 'D')`; `ts_headline` snippets computed on the top-k only
(post-LIMIT). `--fenced` emits nonce-fenced `===BEGIN UNTRUSTED…===` regions (skillselect's
`_fence` discipline) — REQUIRED whenever recall output is injected into any prompt.
Freshness: recall runs a bounded sha-compare pass over the root first (~ms at this scale), so the
index can never be stale — no tick needed (open call #3).

**3. Curator operations (LLM, on-demand only):** the constitution defines three named ops —
- **QUERY** — recall-first, then read the top hits and answer with `path:date` citations; state
  "known / partially known / unknown". Propose (not perform) filing the answer back if it filled
  a real gap (the compounding loop).
- **FILE** — given a new report: recall FIRST for an existing page on the topic and **merge into
  it rather than fragment** (dedupe-at-ingest); name per `YYYY-MM-DD-<slug>.md`; add
  `[[wikilinks]]` to related briefs; update `index.md` in the same operation (mandatory step, not
  a separate job).
- **LINT** — report: index↔file-listing mismatches, ghost `[[links]]`, orphans, dead-direction
  candidates, near-duplicate pairs, and gaps (concept + evidence + suggested title), triaged
  P1/P2/P3 with P3 orphans explicitly ignored. Contradictions between briefs are FLAGGED, never
  auto-resolved. Output = a report; gaps never auto-create pages.

## Authority (v1 = the shadow rung, zero teeth)

| Class | v1 | Enforcement |
|---|---|---|
| Additive writes: create NEW file, update `index.md`, append link sections | ALLOWED | allowedTools has Write/Edit but no Bash — cannot `mv`/`rm` |
| Mutative: move / rename / archive / delete existing files | PROPOSE-ONLY — prints exact `mv` lines, human runs them | no Bash tool; constitution rule |
| Auto-dispatch research on gaps; scheduled ticks; network | FORBIDDEN | not in allowedTools; no trigger exists |

Every curator reply ends with an `OPERATIONS:` footer listing every file written (empty if none) —
the audit line a human scans. Plus git history if #2 is accepted.

## Schema (`0009_knowledge.sql` + `schema.sql` mirror, in lockstep)

```sql
CREATE TABLE IF NOT EXISTS knowledge_doc (
    id          uuid PRIMARY KEY,
    seq         bigserial,
    scope       text NOT NULL,           -- corpus id ('research'); recall fail-closed on it
    path        text NOT NULL,           -- relative to corpus root, traversal-checked at ingest
    title       text NOT NULL DEFAULT '',
    tags        text NOT NULL DEFAULT '',
    body        text NOT NULL,
    content_sha text NOT NULL,           -- sha256 of raw file bytes; 'absent' for tombstones
    status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
    tsv tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title,'')), 'A') ||
        setweight(to_tsvector('english', coalesce(tags,'')),  'B') ||
        setweight(to_tsvector('english', coalesce(body,'')),  'D')) STORED,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS knowledge_doc_tsv ON knowledge_doc USING GIN (tsv);
CREATE OR REPLACE VIEW knowledge_doc_current AS
    SELECT DISTINCT ON (scope, path) * FROM knowledge_doc ORDER BY scope, path, seq DESC;
```

Append-only discipline mirrors `finding_outcome`: INSERT-only, current state = computed view,
latest-by-seq. Idempotency is compare-before-insert under the advisory lock (NOT a unique index —
a (scope,path,sha) unique index would silently swallow a restore-after-archive of identical
content and leave a ghost tombstone as current). 2-arg `to_tsvector('english', …)` is required —
the 1-arg form is not IMMUTABLE and a GENERATED column rejects it.

## Edge cases

- Empty corpus → ingest records 0, recall exits 0 with "no hits".
- File >1MB → body truncated at 900KB with marker; file on disk untouched.
- Non-md artifacts (json/png/py) → not FTS-indexed in v1; curator lists them in `index.md`.
- Malformed/missing frontmatter → soft-parse (heading/filename fallback), never a crash.
- Over-specified LLM query (6-term AND → 0 rows) → the ladder + constitution guidance ("2–3 terms,
  expand synonyms with OR at query time" — replaces Postgres synonym-file machinery entirely).
- Concurrent ingest / recall-freshness races → per-scope advisory lock; append-only makes any
  interleaving safe.
- Corpus root missing or not a directory → verbs error out fail-closed, zero partial writes.
- Duplicate topic arriving → curator's FILE op is recall-first and merges (a corpus that
  converges, not fragments).

## Security surface

- **Untrusted text in the corpus**: briefs quote web content — treat every recall hit entering a
  prompt as UNTRUSTED: `--fenced` nonce regions, objective above the fence (skillselect's exact
  model). The curator reading its own corpus as context is an accepted v1 risk, bounded by the
  zero-teeth authority: no Bash, no network, no dispatch — worst case is a poisoned additive
  file edit, visible in the OPERATIONS footer and recoverable (git, if #2 accepted).
- **Path traversal**: ingest resolves realpath per entry and rejects anything escaping the root;
  stored paths are relative; recall touches only the DB plus the bounded freshness pass.
- **Scope fail-closed**: recall without `--scope` = error; unknown scope = empty result.
- **Secrets**: noise globs exclude `.venv*`/`.playwright-mcp` (session logs can carry tokens) —
  they never reach the index.
- **Shell safety**: verbs take input via argv (`sys.argv`), no interpolation anywhere.

## Files

- **Create:** `src/runtime/ledger/migrations/0009_knowledge.sql`, `src/runtime/knowledge.py`
  (pure: walk/parse/sha/noise-globs/traversal checks), `src/runtime/knowledgerecord.py` (verb
  entrypoints), tests (pure + DB + verb), `~/research/CLAUDE.md` v2 (constitution — supersedes
  the current usage note), `~/research/index.md` (seeded by the curator's first FILE/LINT run).
- **Modify:** `src/runtime/ledger/schema.sql`, `src/runtime/cli.py` (verb wiring),
  `src/runtime/registry.py` (one AgentSpec row), `src/runtime/runner.py` (workdir, ~5 lines).

## Deliberately NOT built (each gated, per [[acting-rungs-gate-on-data]])

- Scheduled tick / file-watcher → gate: on-demand use proves there's something worth waking for.
- Auto-dispatching field research on gaps → gate: the authority ladder; v1 flags only.
- Agent-performed moves/renames/deletes → gate: a track record of clean additive operations.
- pgvector / embeddings / chunking / synonym files / typed link ontologies → gate: a FELT
  "phrased-it-differently" recall miss (memory-second-brain-design §5); rejected 3× on record.
- Khoj / Onyx / LlamaIndex / Dataview-style adopted infra → rejected as wrong-altitude (recon).
- Mini-side curator, phone recall transport, additional corpora (`ask/`, mx-engine) → after
  `research/` proves the shape; the constitution is the reusable piece.

## Open calls (for reviewers + Jefe)

1. **Additive-Write enabled (lean) vs read-only responder v1** — without Write the index never
   gets maintained and compounding never starts; with it, the blast radius is bounded above.
2. **`git init ~/research` (local-only, no remote/branches) (lean yes)** — free audit + rollback
   for every curator write; the folder's "not a git repo" note meant no PR/worktree flow, which
   stays true.
3. **Recall-time freshness pass (lean yes)** — kills the stale-index class without a tick;
   bounded to a ~15-file sha compare today.
4. **Model pin: sonnet (lean)** — librarian judgment is mid-tier; flat-rate; opus if merge-draft
   quality disappoints.
5. **Standalone `knowledge_doc` (lean)** vs unifying now with the §6 facts-table — facts stay
   behind their own trigger; don't merge speculatively.
