# MyndAIX Team Runtime

[![ci](https://github.com/redeyefit/myndaix-runtime/actions/workflows/ci.yml/badge.svg)](https://github.com/redeyefit/myndaix-runtime/actions/workflows/ci.yml)

A deterministic, Postgres-backed spine for multi-agent work. One Command API is the only thing that
writes the ledger, so agents lease, run, and reply through database row locks instead of files — and the
file-IPC bug classes (double-lease, lost update, duplicate delivery, a crashed worker's job stuck forever)
can't happen. I built it clean-room after a real multi-agent system went down for ~3 hours, when comms,
execution, and config were coupled into one event loop and any single slow part took the whole thing down.

```
   client / transport          ┌──────────────────────────────────┐
   (HTTP API · terminal) ─────▶ │  Command API   (the SOLE writer) │
         ▲                      └─────────────────┬────────────────┘
         │ reply                                  │ ingest · submit
         │ (outbox, decoupled)                    ▼
   ┌─────┴────────┐         ┌────────────────────────────────────────────┐
   │  transport   │ ◀────── │              Postgres ledger               │
   │ (dumb pipe)  │ deliver │  inbound_event · job · attempt · outbound  │
   └──────────────┘         │  status-guarded state machine, one lock    │
                            │  order (attempt→job), FOR UPDATE SKIP       │
                            │  LOCKED, transactional outbox              │
                            └──────────────────┬─────────────────────────┘
                              lease (SKIP LOCKED) │ complete / reclaim
                                                  ▼
                            ┌────────────────────────────────────────────┐
                            │      worker pool  (N workers + janitor)     │
                            │  heartbeats keep long jobs; crashed and     │
                            │  poison jobs are recovered, not lost        │
                            └──────────────────┬─────────────────────────┘
                                               ▼
                            runner → agent (CLI/API), killed as a process
                            group on timeout, file edits in an isolated
                            git worktree (the live repo is never touched)
```

The agents were never the problem — Codex, Gemini, and Claude all answer direct local shell calls. Only
the wrapper around them was. This keeps the direct calls and replaces the wrapper with a durable state machine.

## Run it

**Prerequisites:** Python 3.11+ and git — that's everything the zero-dep demos need. The Postgres demos
add a local Postgres; the optional real-agent demos add an agent CLI (Node 18+). All three are below.

> **Full install guide** — every agent CLI (install / auth / verify), the always-on service, and
> one-machine *and* two-machine deployment — is in **[SETUP.md](SETUP.md)**. What follows is the quick tour.

### 1. Clone and run (zero-dep — no Postgres, no API keys, no LLM)

```bash
git clone https://github.com/redeyefit/myndaix-runtime && cd myndaix-runtime
python3 -m venv .venv && source .venv/bin/activate   # avoids the PEP 668 error on modern macOS/Debian
pip install pydantic                                  # the zero-dep demos need only this
PYTHONPATH=src python3 demo.py            # route a message through the spine and back
PYTHONPATH=src python3 demo.py --isolate  # an agent fixes a bug in a throwaway git worktree
```

The `--isolate` run has a workspace-actor edit buggy code inside an ephemeral git worktree. The live repo
is never touched; the change comes back as a reviewable diff, never auto-merged:

```
  app.py before : 'def add(a, b):\n    return a - b  # bug'
  job ee152ab6 -> done (ran in an isolated git worktree)
  app.py AFTER  : 'def add(a, b):\n    return a - b  # bug'   <- LIVE REPO UNTOUCHED
  the agent's change, captured as a reviewable diff (not merged):
    -    return a - b  # bug
    +    return a + b
```

### 2. The Postgres demos (where the real concurrency lives)

```bash
pip install asyncpg fastapi uvicorn httpx

# macOS:           brew install postgresql@16 && brew services start postgresql@16
# Debian/Ubuntu:   sudo apt-get install -y postgresql && sudo service postgresql start

# these demos DROP and recreate the schema — use a THROWAWAY db, never your ops 'runtime'
createdb runtime_test
export LEDGER_TEST_DSN=postgresql://localhost/runtime_test

PYTHONPATH=src python3 demo.py --pool      # N workers drain a queue + recover a crashed worker
PYTHONPATH=src python3 demo.py --postgres  # the SAME worker core, now backed by Postgres
PYTHONPATH=src python3 demo.py --terminal  # a dumb-pipe transport: a slow agent never blocks it
PYTHONPATH=src python3 demo.py --api        # the HTTP service: POST a job, GET its status + reply
```

`--isolate` (SQLite) and `--postgres` call the *same* `worker.drain()` — only the ledger differs. Swapping
persistence behind the contract is the central thesis, and it's structural: one core runs both stores.

### 3. (Optional) Route to a real agent

The roster in `src/runtime/registry.py` maps each agent to a local CLI, so a real model is just a CLI
install + login away (needs Node 18+ and your own provider account). This needs no Postgres —
`demo.py <agent>` uses an in-memory store; only the agent's CLI must be installed and authenticated.
For example, `demo.py kilabz` runs a code-review agent through OpenAI's Codex CLI:

```bash
npm install -g @openai/codex          # the codex CLI
codex login                           # OAuth — or: export OPENAI_API_KEY=sk-...
PYTHONPATH=src python3 demo.py kilabz  # routes a real GPT-5.5 process through the spine
```

Every other agent works the same way — install its CLI, authenticate, and the adapter is already wired
(e.g. Claude Code: `npm install -g @anthropic-ai/claude-code`; the Gemini-backed `oracle` agent uses the
`agy` CLI; `recon` is an API agent that just needs `PERPLEXITY_API_KEY` in the environment). The full
per-agent install / auth / verify steps are in **[SETUP.md](SETUP.md#4-install-the-agent-clis)**.

## The design

The rule the whole thing follows: contracts are deep and rigid; the roster, models, and transport are
flexible data behind them. Every change is additive (a registry row), never structural (a patch around the
spine). The test for any decision is *can you add an agent, swap a model, or change transport without
editing the spine?*

- **C0 — capability model.** Each agent has a `reach` (cli|api) and an `authority`
  (responder / workspace-actor / controller / composite). Authority, not reach, drives retry-safety,
  isolation, and dispatch rights.
- **C1 — invocation.** One `invoke(agent, job) -> Result` for any agent. CLI agents run in their own
  process group with a hard timeout, so a stuck agent is killed cleanly with no orphaned children.
- **C2 — ledger.** A state machine (inbound_event / job / attempt / outbound / dead_letter) with leases
  for crash recovery and dedupe for exactly-once-ish ingest — not files. The same Command-API verbs are
  implemented over SQLite (the zero-dep demo) and asyncpg Postgres (production), behind one worker.
- **C3 — comms boundary.** A transport is a dumb pipe over the ledger and can never block on agent work,
  which was the original outage's root cause. Transport details stay in the envelope and never leak into
  agent fields (a chat platform's "group" flag silently dropping replies is what sank the prior system).
- **C4 — failure semantics.** Authority-gated retry (mutating agents never auto-retry), process-group
  kill, and admission limits so a job tree can't run away.
- **C5 — workspace isolation.** Every file-mutating job runs in an ephemeral git worktree, so concurrent
  agents can't corrupt a shared repo.

The Postgres ledger is where the real work is: one transaction per verb, a status-guarded compare-and-swap
on every transition, a single canonical lock order (attempt then job) so there's no ABBA deadlock,
`FOR UPDATE SKIP LOCKED` to hand distinct rows to racing workers, and a transactional outbox so a finished
job can't lose its reply. Full spec and decision log in [`DESIGN.md`](DESIGN.md).

## How I built it

Six slices — ledger, worker, concurrent pool, terminal transport, HTTP API, auth — and I pressure-tested
each one with Codex and Gemini before moving on. Different model families catch different things, and every
review surfaced a real bug my passing tests had missed:

| Slice | The bug the green tests missed | Now caught by |
|---|---|---|
| ledger | `cancel()` locked rows in the opposite order from every other verb — an ABBA deadlock | a 150-trial cancel-vs-finish race |
| worker pool | one poison job (bad binary, or an api agent) silently killed *every* worker; `gather(return_exceptions=True)` ate the tracebacks | a poison-job survival test |
| terminal transport | a per-process counter reused as a dedupe key misrouted a reply to the wrong sender after a restart | a restart / dedup test |
| HTTP auth | `created_by` doubled as the owner *and* a provenance tag, so a client keyed `id=human` could read every internal job | a provenance-collision test |

I confirmed each fix by reproducing the bug, not just trusting the green bar. For the deadlock I reverted
`cancel()` to the old lock order, watched the regression test fail on the first trial with a
`DeadlockDetectedError`, then re-applied the fix and watched it pass 150/150. Every fix here has a
regression test that fails without it.

## What works, what doesn't

**Built and tested (50 tests, all against real substrates — real Postgres, real subprocesses, real HTTP):**

- A ledger-agnostic worker: one core drives the SQLite demo store *or* the Postgres production store.
- The asyncpg Postgres ledger: all the Command-API verbs, **15 concurrency proofs** against a live Postgres
  (50 workers racing one lease, a 200-trial reclaim-vs-complete mutual-exclusion proof, exactly-once
  ingest/delivery, admission limits under 50 concurrent submits).
- A concurrent worker pool: no double-processing, crashed jobs recovered by the janitor, long jobs kept
  alive by heartbeats, and a poison job can't take down the fleet.
- A terminal transport that never blocks on an agent, with replies delivered fully decoupled.
- A FastAPI HTTP service with API-key auth: a client submits and reads only its own jobs (ownership is a
  namespaced `api:<id>`; reading someone else's job is a 404, not a 403, so ids never leak); admin reads any.
- An api-reach adapter (OpenAI-compatible chat): `recon` runs as a live Perplexity API agent through the
  *same* `invoke()` path as the CLI agents — the key comes from the environment, never the roster.

**Not built yet (deferred, named on purpose):**

- A redelivering chat transport (Slack, say) — where the idempotent-dispatch guard already in the
  ledger would start to pay off.
- The C4 admission budgets (cost/chain-TTL), composite authority, capability-gated routing — specified in
  `DESIGN.md`, not yet exercised in code.

## Tests

Each suite is a self-contained runner (no pytest config needed). The Postgres-backed suites need the DB
from the "Run it" section above:

```bash
# zero-dep
PYTHONPATH=src python3 tests/test_worker.py

# Postgres-backed (e.g. the 15 concurrency proofs) — throwaway db; the suite resets the schema
LEDGER_TEST_DSN=postgresql://localhost/runtime_test PYTHONPATH=src python3 tests/test_postgres_ledger.py
```

## Layout

```
DESIGN.md                    the spec + decision log (v0.1 -> v0.4)
demo.py                      the runnable demos above
src/runtime/
  contracts.py               C0-C3 as Pydantic models
  registry.py                the agent roster, as data
  command_api.py             the Command-API verb interface (sole ledger writer)
  api.py                     FastAPI HTTP surface + API-key auth
  runner.py                  C1 cli runner (process-group kill + timeout)
  worker.py                  the lease -> invoke -> result core (drives either store)
  pool.py                    concurrent worker pool (N workers + janitor + heartbeats)
  workspace.py               C5 ephemeral git-worktree isolation
  ledger/
    schema.sql               the Postgres state machine (DDL)
    postgres_store.py        the asyncpg ledger (production)
    sqlite_store.py          the SQLite store (zero-dep demo)
  transport/
    terminal.py              C3 terminal transport (dumb pipe over the ledger)
tests/                       50 tests across 8 suites
```
