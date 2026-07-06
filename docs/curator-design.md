# Curator v1 — DESIGN (research/ → the first folder-agent)

_Status: v0.2 — cross-family round 1 folded (oracle: 2 BLOCKER + 2 MAJOR; kilabz: 4 BLOCKER +
9 MAJOR; both families converged on the recall paradox and the Write/Edit-authority illusion).
For round-2 review, then Jefe plan approval. Prior-art + in-repo recon: workflow `wf_357e9782`
(2026-07-05). Review history at the bottom._

## What

A **curator** agent — the librarian of the `~/research` corpus, NOT the field researcher. Invoked
on demand (`mxr curate "…"`), it answers "do we already know X?" with citations before anything
gets re-researched, files new reports per corpus conventions, maintains a single `index.md` (the
map of the corpus), cross-links related briefs, and reports duplicates, dead directions, and
gaps. Five pieces: (1) a `knowledge_doc` tsvector table + `mxr knowledge-ingest` / `mxr recall`
verbs (deterministic, no LLM), (2) **`mxr curate` — a deterministic guard verb** that runs recall
first, snapshots the corpus, dispatches the LLM, then diff-audits and auto-reverts noncompliant
writes, (3) one `curator` AgentSpec row in the registry, (4) a fail-closed `workdir` adapter key
in the runner, (5) a repo-tracked **curator constitution** injected at dispatch — the reusable
piece you point at any future corpus folder.

## Why

The named pain (Jefe, 2026-07-04): *"I gather reports and don't reopen them."* Without a
librarian the corpus rots — re-research, orphan reports, grows-but-never-compounds. The survey
confirms it: 15 real documents across FOUR scattered locations, no index, dead directions
distinguishable only by timestamps, 5 orphan strays. The curator's v1 value is **retrieval +
compounding, which needs zero standing authority** — and it proves the folder→agent shape for
every later folder (`ask/` → dispatcher, `runtime-engine/` → producer).

## Architecture: LLM judgment inside a deterministic cage

The safety model is NOT "prompt rules + tool flags" (round 1 killed that): tool allowlists are a
belt, and the **hard boundary is a deterministic pre/post guard around every run** — the same
idiom as the rest of the runtime (mechanical gates decide; the LLM is never trusted to self-report).

**`mxr curate "<task>"` (special verb, `src/runtime/curate.py`) does, in order:**
1. **Scope resolve (fail-closed):** scope→root comes from a STATIC allowlist
   (`{'research': ~/research}` in code/env); never derived from input. Unknown scope = hard error.
2. **Freshness refresh (bounded, best-effort):** the ingest primitive (walk + sha compare +
   append rows/tombstones) under a per-scope advisory **try-lock**; lock busy → skip and mark
   "index may be stale" in the output. Deterministic; ~ms at 15 files.
3. **Recall pre-injection:** run the recall ladder on the task text; top-k hits injected into the
   curator's prompt as a **nonce-fenced UNTRUSTED region** (skillselect's `_fence`). This
   resolves round-1's converged BLOCKER — the agent never needs to run `mxr` itself; for mid-task
   follow-ups it uses `Grep`/`Read` inside the folder, which at this scale IS local search
   (deliberate v1 choice, not degradation).
4. **Git baseline:** `~/research` is a local-only git repo (no remote, no branches — mandatory,
   round 1 unanimous). Commit any human drift as `curate-baseline`. This is the rollback point.
5. **Dispatch:** submit the pool job to the `curator` agent (prompt = repo-tracked constitution +
   task + fenced recall hits; `workdir` = the scope root). Wait on the ledger like `mxr` does.
6. **Diff audit (the actual enforcement):** `git status`/`diff --name-status` against baseline.
   ALLOWED: new files matching `^[a-z0-9][a-z0-9._-]*\.md$` (no slashes, no dot-segments,
   top-level only) and modification of exactly `index.md`. ANY other change — edits to existing
   briefs, `CLAUDE.md`, `.claude/**`, `.git/**`, deletions, renames, non-md creations —
   → `git reset --hard` to baseline and the run is reported **NONCOMPLIANT** with the offending
   diff. Compliant runs are committed `curate: <task slug>`.
7. **Report:** the agent's reply + a deterministic `OPERATIONS:` section generated FROM THE DIFF
   (the model's own claims are narrative only, never the audit record).

**Merge semantics under this guard:** the FILE op is dedupe-first, but merging into an existing
brief is **draft-only** — the curator writes `<target>-merge-draft.md` (a new file) + a proposal;
a human applies it. v1 mutative authority over existing content is exactly zero, enforced by
revert, not by trust.

## The folder→agent shape (revised)

An agent = a registry row; its policy = a **repo-tracked constitution**
(`src/runtime/prompts/curator_constitution.md`) injected at dispatch — versioned, PR-gated,
reviewable, and NOT writable by the agent (round-1 kilabz BLOCKER: policy inside the writable
corpus = persistent self-modification hole). `~/research/CLAUDE.md` stays a thin human-facing
usage note; the guard reverts any agent write to it.

- **Registry row:** `curator` — reach=CLI, authority=WORKSPACE_ACTOR, model `sonnet` (alias, not
  a frozen ID — registry precedent, noted), adapter `{kind: cli, argv: [claude, -p, --model,
  sonnet, --output-format, text, <tool flags — see enforcement test>], prompt_channel: stdin,
  env_passthrough: [CLAUDE_CODE_OAUTH_TOKEN], workdir: <scope root>}`, timeout_s 600.
- **Tool flags are a belt, not the boundary:** intended surface Read/Glob/Grep/Write/Edit, with
  an explicit disallow list (Bash, WebSearch, WebFetch, Task/agents, all MCP). Round 1 flagged
  `--allowedTools` semantics as pre-approval-not-confinement: the BUILD includes an
  **enforcement test** that invokes the real curator config headless, instructs it to run Bash /
  fetch a URL / edit `CLAUDE.md`, and asserts denial + guard revert. If the flag layer proves
  leaky, the diff guard is still the hard stop for writes, and no-Bash/no-network is re-verified
  per claude-CLI release.
- **Runner change:** `invoke_cli` honors `adapter.workdir` — **fail-closed**: declared but
  missing/non-canonical/outside the static scope roots → the run hard-errors; NO fallback to
  scratch cwd (round-1 kilabz MAJOR — cwd loads project config, so it's a trust boundary). Tests
  pin both directions (PR #39 scratch behavior unchanged when workdir is absent).

## Deterministic substrate

**Ingest** (`mxr knowledge-ingest --scope research`, pure Python, no LLM): walk root for `*.md`,
noise-globs excluded (`.venv*`, `__pycache__`, `.playwright-mcp`, `.claude`, `.git`, dotfiles).
Per file: title = first `#` heading (fallback filename); tags = frontmatter if parseable;
**doc_date** = `YYYY-MM-DD` filename prefix, else frontmatter `date:`, else NULL (round-1: the
citation contract needs a document date, `created_at` is ingest time); body decoded UTF-8
`errors='replace'`, NULs stripped, capped 900KB with truncation marker; `content_sha` over raw
bytes. Append-only under the advisory lock: current row same sha+status → skip; else INSERT.
File gone from disk → INSERT tombstone (status=archived, sha='absent'). A tsvector-overflow
insert error → harder truncate, one retry, logged. Files are the source of truth; Postgres is a
derived, rebuildable index.

**Recall** (`mxr recall --scope research "query" [--fenced]`): scope REQUIRED (no scope = error;
unknown scope = empty). Ladder: `websearch_to_tsquery` → zero hits → sanitized
`to_tsquery('tok:* & …')` prefix form (empty/all-stopword tokens skip the rung; query capped
512 chars) → zero hits → `ILIKE` substring with `%`/`_` escaped. Queries run against the
**active view only** (round-1: tombstones must be invisible). Rank `ts_rank_cd(tsv, q, 1)`;
`ts_headline` on the top-k only. `--fenced` nonce-fences every hit — REQUIRED on any path into a
prompt (curate.py uses it).

## Schema (`0009_knowledge.sql` + `schema.sql` mirror, lockstep)

```sql
CREATE TABLE IF NOT EXISTS knowledge_doc (
    id          uuid PRIMARY KEY,
    seq         bigserial,
    scope       text NOT NULL,
    path        text NOT NULL,            -- relative, traversal-checked at ingest
    title       text NOT NULL DEFAULT '',
    tags        text NOT NULL DEFAULT '',
    doc_date    date,                     -- from filename/frontmatter; NULL if unparseable
    body        text NOT NULL,
    content_sha text NOT NULL,            -- sha256 raw bytes; 'absent' = tombstone
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
CREATE OR REPLACE VIEW knowledge_doc_active AS
    SELECT * FROM knowledge_doc_current WHERE status = 'active';
```

Append-only mirrors `finding_outcome`: INSERT-only, computed current view. Idempotency =
compare-before-insert under the lock (NOT a unique index — a (scope,path,sha) unique index
swallows restore-after-archive of identical content, leaving a ghost tombstone current). 2-arg
`to_tsvector('english', …)` required (1-arg is not IMMUTABLE).

## Curator operations (constitution-defined, guard-enforced)

- **QUERY** — read the fenced recall hits, `Read` the top files, answer with `path (doc_date)`
  citations; state known / partially known / unknown. Propose (not perform) filing the answer
  back when it fills a real gap.
- **FILE** — dedupe-first against recall hits + index; a genuinely new topic → new
  `YYYY-MM-DD-<slug>.md` + `[[wikilinks]]` + index.md updated in the same run (mandatory step);
  an existing topic → `-merge-draft.md` + proposal (never edits the original).
- **LINT (v1 = deterministic-first)** — reports: exact duplicates (same sha), ghost `[[links]]`,
  index↔file-listing mismatches, orphans, un-indexed artifacts. Semantic judgments
  (near-duplicates, dead-direction candidates, gaps as concept+evidence+suggested-title) are
  clearly-labeled SUGGESTIONS, triaged P1/P2/P3, P3 orphans explicitly ignored. LINT works from
  `index.md` + recall metadata first and drills into at most a handful of files per pass
  (scales past the ~80KB the corpus is today). Contradictions are FLAGGED, never auto-resolved.
  Gaps never auto-create pages.

## Edge cases

- Empty corpus → ingest 0, recall exits 0 "no hits", curate still functions (empty fence).
- File >1MB / invalid UTF-8 / NULs → truncate-with-marker, decode-replace, strip; never a crash.
- Non-md artifacts (json/png/py) → not FTS-indexed; curator lists them in `index.md`.
- Malformed frontmatter → soft-parse fallbacks (heading/filename/NULL date).
- Over-specified query → the ladder + constitution guidance ("2–3 terms, OR-expand synonyms at
  query time" — replaces Postgres synonym-file machinery).
- Concurrent curate/ingest/recall → per-scope advisory lock; try-lock on read paths; append-only
  makes interleavings safe. Guard commits serialize on the lock too.
- Corpus root missing/not a dir/unknown scope → hard error, zero partial writes.
- Agent times out mid-write → guard's diff audit still runs on the job's terminal state; partial
  writes are reverted unless compliant.

## Security surface

- **Corpus text is untrusted** (briefs quote the web). Paths into prompts are fenced (`--fenced`
  everywhere curate.py injects). The curator reading its own corpus via `Read` is its normal
  evidence-gathering — the defense there is not fencing but the **authority bound**: no Bash, no
  network, no dispatch, and every write subject to revert. Worst case = a poisoned reply +
  noncompliant write that gets reverted; a poisoned COMPLIANT write (a new lying .md + index
  line) is visible in the deterministic OPERATIONS diff and trivially `git revert`-able.
- **Read-exposure residual:** Read/Glob/Grep can see hidden/noise files ingest excludes (e.g.
  `.playwright-mcp` logs may carry tokens). Mitigation: deny-path config where the CLI supports
  it + constitution rule; residual accepted for v1 because the agent has NO exfil channel (no
  network/Bash) — a token could only surface in its reply text, which lands in the local ledger/
  inbox. Flagged for the round-2 reviewers to re-judge.
- **Self-modification closed:** policy is repo-tracked + injected; agent writes to `CLAUDE.md` /
  `.claude/**` / `.git/**` are auto-reverted. (Round-1 kilabz BLOCKER — folded.)
- **Path traversal:** ingest realpath-checks every entry against the static root; stored paths
  relative; new-file names validated by the guard regex (no slashes/dot-segments).
- **Scope fail-closed:** static allowlist; write paths hard-error, read paths return empty.
- **Shell safety:** all verbs take input via `sys.argv`; nothing interpolated into shells.

## Files

- **Create:** `src/runtime/ledger/migrations/0009_knowledge.sql`, `src/runtime/knowledge.py`
  (pure walk/parse/sha/validate), `src/runtime/knowledgerecord.py` (ingest/recall verbs),
  `src/runtime/curate.py` (guard verb), `src/runtime/prompts/curator_constitution.md`, tests
  (pure + DB + guard + the tool-enforcement test), `~/research/index.md` (first curator run),
  `~/research` git init + baseline (deploy step, Jefe's folder — done with him).
- **Modify:** `src/runtime/ledger/schema.sql`, `src/runtime/cli.py` (verbs), `src/runtime/
  registry.py` (curator row), `src/runtime/runner.py` (fail-closed workdir), `~/research/
  CLAUDE.md` (thin usage note pointing at the constitution).

## Deliberately NOT built (each gated, per acting-rungs-gate-on-data)

- Scheduled tick / file-watcher → gate: on-demand use proves something is worth waking for.
- Auto-dispatching field research on gaps → gate: the authority ladder; v1 flags only.
- Agent-performed edits/moves/deletes of existing files → gate: a clean additive track record
  (the guard's compliance log IS that evidence, rung-style).
- pgvector / embeddings / chunking / synonym files / typed link ontologies → gate: a FELT
  "phrased-it-differently" miss (memory-second-brain §5); pg_trgm noted as the cheap substring
  upgrade if ILIKE ever hurts.
- Khoj / Onyx / LlamaIndex / Dataview-style adopted infra → wrong altitude (recon).
- Mini-side curator, phone recall, additional corpora → after `research/` proves the shape.

## Open calls — round-1 verdicts (both reviewers) + resolution

1. **Additive-Write vs read-only** → **Write ENABLED, guard-enforced** (oracle: yes; kilabz: only
   with hard enforcement — the diff-audit + revert is that enforcement).
2. **git init** → **MANDATORY** + baseline commit + wrapper-level diff audit (unanimous;
   "init alone is not rollback" folded — the guard does baseline/audit/commit).
3. **Recall freshness** → **YES**, specified: bounded ingest-primitive refresh, try-lock,
   best-effort with explicit staleness note.
4. **Model pin** → sonnet accepted; alias-not-frozen-ID noted (registry precedent).
5. **Standalone `knowledge_doc`** → **YES** (unanimous — derived FTS index, not the facts ledger).

## Review history

- **v0.1 → round 1 (2026-07-05):** oracle (agy/Gemini) 2 BLOCKER + 2 MAJOR + 1 MINOR + 1 NIT;
  kilabz (codex) 4 BLOCKER + 9 MAJOR + 3 MINOR + 1 NIT. FOLDED: recall paradox (converged) →
  guard pre-injection; constitution self-modification hole → repo-tracked policy; Write/Edit
  authority illusion (converged) → deterministic diff-audit + revert + draft-only merges;
  workdir fail-open → hard error; tombstones in current view → active view; missing doc_date;
  static scope allowlist; model-generated OPERATIONS → diff-generated; slug validation; UTF-8/
  NUL/tsvector-overflow handling; ladder bounds + ILIKE escaping; LINT → deterministic-first.
  REFUTED/PUSHED BACK: oracle's "fenced file-reader" (a workspace actor reads its own corpus as
  evidence; defense = authority bound + guard, and a fenced Read would break the job) and
  oracle's 13.5MB LINT math (corpus is ~80KB; the metadata-first rule folded anyway on
  principle); kilabz's "silently degrades to ad hoc Grep" (in-folder Grep at this scale is the
  deliberate v1 local search, now documented).
