# MyndAIX Team Runtime

A thin, **deterministic** orchestrator that routes a unit of work to an agent (CLI or API),
runs it in **isolation**, captures the result, delivers a reply, and logs every state
transition to a durable **Postgres ledger**.

It replaces **openclaw** — which coupled comms + runtime + agents + config into one event
loop, so any single slow/failed part took the whole command center down (a ~3-hour, 6-fix
outage on 2026-06-21). The agents were never the problem and were never coupled to openclaw:
Codex, Gemini, and Claude all answer **direct local shell calls** with zero openclaw in the
path. This keeps the direct calls and deletes the wrapper.

**Status:** v0.4 — design build-ready (two cross-family review cycles, Codex + Gemini).
Scaffolding phase. Internal-first, built clean to release.

## The shape (full spec in `DESIGN.md`)
- **C0 capability model** — every agent has a `reach` (cli|api) and an `authority`
  (responder / workspace-actor / controller / composite). *Authority*, not reach, drives
  retry-safety, isolation, and dispatch rights.
- **C2 ledger** — a state machine (inbound_event / job / attempt / outbound / dead_letter)
  with **leases** (crash recovery) and **dedupe** (exactly-once-ish), not files.
- **C3 boundary** — transport is a **dumb pipe** over the ledger; it can never block on agent work.
- **C4 failure semantics** — authority-gated retry (mutating agents **never** auto-retry),
  process-group kill, admission limits (no runaway job trees).
- **C5 workspace isolation** — each file-mutating job runs in an ephemeral **git worktree**;
  the runner enforces the boundary. No concurrent-repo corruption.
- **Command API** — the **sole** writer to the ledger.

## The non-negotiable principle
Contracts deep + rigid; roster/roles/models/transport flexible data behind them. Every change
is **additive** (a registry row), never **structural** (a patch around the spine). The test:
*can you add an agent / swap a model / change transport without editing the spine?*

## Layout
```
DESIGN.md                  the spec (source of truth)
src/runtime/
  contracts.py             C0-C3 as Pydantic — the contracts in code
  registry.py              the agent roster as data
  command_api.py           the Command API verb interface (C4b)
  ledger/schema.sql        the C2 state machine
tests/
```

## Implemented vs next
- [x] Contracts (Pydantic), registry, Command-API interface, ledger schema
- [ ] Command-API implementation (asyncpg + FastAPI)
- [ ] Worker pool + runner (process-group kill, git-worktree isolation)
- [ ] One transport adapter (terminal/CLI first)
- [ ] One agent adapter end-to-end (codex)

Then let the build surface the rest — past the design, code finds more than review does.
