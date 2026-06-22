# MyndAIX Team Runtime

A thin, **deterministic** orchestrator that routes a unit of work to an AI agent (CLI or API),
runs it in **isolation**, captures the result, delivers a reply, and logs every state transition
to a durable ledger. A clean-room replacement for a multi-agent runtime that coupled comms +
execution + config into one event loop — so any single slow part took the whole system down.

## See it work (zero setup)

```bash
PYTHONPATH=src python3 demo.py            # fast, deterministic (in-process echo agent)
PYTHONPATH=src python3 demo.py kilabz     # route to a REAL agent (Codex / GPT-5.5)
PYTHONPATH=src python3 demo.py --isolate  # an agent edits code in an isolated git worktree
```

The real-agent run routes a message through the spine to an actual GPT-5.5 process and back:

```
  submit_job  -> a87ea785  status=queued
  worker      -> processed 1 job(s)
  job a87ea785 -> status=done
  delivered replies:
    -> terminal:demo: 'Confirmed. I ran in `.../bridge` at `Mon Jun 22 11:53:52 PDT 2026`.'
  OK - the spine routed a message to an agent and returned a reply (job done).
```

No Postgres, no Docker, no API keys for the default demo — clone and run.

The `--isolate` run shows a workspace-actor editing buggy code **in an ephemeral git worktree** — the
live repo is never touched, and the change comes back as a reviewable diff artifact, never auto-merged:

```
  app.py before : 'def add(a, b):\n    return a - b  # bug'
  job ee152ab6 -> done (ran in an isolated git worktree)
  app.py AFTER  : 'def add(a, b):\n    return a - b  # bug'   <- LIVE REPO UNTOUCHED

  the agent's change, captured as a reviewable artifact (NOT auto-merged):
    -    return a - b  # bug
    +    return a + b
```

## Why it exists

It was born from a real outage: a multi-agent command center went down for ~3 hours, and **six
verified fixes each uncovered the next coupled failure** — an architecture that fights you. The
lesson became the design. The agents were never the problem — Codex, Gemini, and Claude all answer
direct local shell calls — only the *wrapper* around them was. This keeps the direct calls and
replaces the wrapper with a durable state machine.

## The architecture (full spec in [`DESIGN.md`](DESIGN.md))

The non-negotiable principle: **contracts are deep and rigid; the roster, models, and transport are
flexible data behind them.** Every change is *additive* (a registry row), never *structural* (a
patch around the spine). The test for every decision: *can you add an agent / swap a model / change
transport without editing the spine?*

- **C0 — capability model.** Each agent has a `reach` (cli|api) and an `authority`
  (`responder` / `workspace-actor` / `controller` / `composite`). *Authority*, not reach, drives
  retry-safety, isolation, and dispatch rights.
- **C1 — invocation.** One `invoke(agent, job) -> Result` for any agent. CLI agents spawn in their
  own process group with a hard timeout — bulletproof termination, no orphaned children.
- **C2 — ledger.** A state machine (inbound_event / job / attempt / outbound / dead_letter) with
  leases (crash recovery) and dedupe (exactly-once-ish) — not files. **Persistence is swappable
  behind the Command API: SQLite for this demo, Postgres for the concurrent production pool.**
- **C3 — comms boundary.** Transport is a dumb pipe over the ledger; it can *never* block on agent
  work — the original outage's root cause.
- **C4 — failure semantics.** Authority-gated retry (**mutating agents never auto-retry**),
  process-group kill, admission limits (no runaway job trees).
- **C5 — workspace isolation.** Each file-mutating job runs in an ephemeral **git worktree**;
  concurrent agents can't corrupt a shared repo.

## How it was designed (the process, not just the result)

The design was hardened by **two cross-family adversarial review cycles** — Codex (GPT-5.5) and
Gemini (3.1 Pro), each prompted to find what breaks — *before a line of code*. They caught real
blind spots a single reviewer misses: a filesystem-concurrency corruption risk, an under-modeled
ledger, and the capability-vs-`cli|api` distinction. The design evolved **v0.1 → v0.4** under that
fire; the `DESIGN.md` changelog and git history show the trail.

## Layout

```
DESIGN.md                    the spec + decision log (v0.1 -> v0.4)
demo.py                      runnable end-to-end demo
src/runtime/
  contracts.py               C0-C3 as Pydantic
  registry.py                the agent roster, as data
  command_api.py             the Command-API verb interface (sole ledger writer)
  runner.py                  C1 cli runner (process-group kill + timeout)
  worker.py                  the lease -> invoke -> result loop
  workspace.py               C5 ephemeral git-worktree isolation
  ledger/
    schema.sql               the Postgres state machine (production)
    sqlite_store.py          the SQLite store (demo)
tests/
  test_runner.py             deterministic runner tests
  test_workspace.py          git-worktree isolation tests
  test_worker.py             end-to-end worker + isolation test
```

## Status

Internal-first, built clean to release. **Working:** contracts, the C1 runner (tested), the ledger, and the
worker with **ephemeral git-worktree isolation wired into the loop** — a workspace-actor job runs in its own
worktree and returns its diff as an artifact, live repo untouched (tested + in the demo). Plus a runnable
end-to-end demo against real agents. **Next:** the async Postgres Command-API, a worker pool with leases, and
transport adapters (terminal first).
