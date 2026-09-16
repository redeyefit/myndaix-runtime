# DESIGN — mxr discuss

**Status:** Draft, awaiting cross-family design review
**Branch:** feat/mxr-discuss

## What

`mxr discuss "<topic>" --with <agent> [<agent>...]`

Parallel fan-out to named agents, barrier aggregation, labeled output. One command replaces
the current manual pattern of `MXR_REVIEW_GATE_BYPASS=1 mxr lobster "..."` × N agents +
SSH-routing oracle by hand + collecting replies separately.

```
$ mxr discuss "should we build the proposer now?" --with lobster oracle kilabz

[lobster]
...reply...

[oracle]
...reply...

[kilabz]
...reply...
```

## Why

Three friction points today:
1. Each agent is a separate manual call — no way to say "the team discusses this"
2. Oracle requires SSH routing to the Mini; callers own that routing
3. The review-gate hook fires on any `mxr kilabz/oracle` call; callers need `MXR_REVIEW_GATE_BYPASS=1`

## Build-vs-Adopt

- `mxr discuss` subcommand: **BUILD** — extends the existing CLI; no prior art covers
  this exact need
- Parallel dispatch: **BORROW** `asyncio.gather` (standard library)
- Oracle SSH routing: **BUILD** a `host` field in `AgentSpec` + a `_remote_fetch()` helper;
  the pattern already exists informally in every manual `ssh mini mxr oracle` call

## Key design decisions (from three-agent review, 2026-09-15)

**Ephemeral at the discuss level.** No new "discussion" job type, no discussion_id table.
Local-agent jobs land in the MacBook ledger as normal independent jobs (a side effect of
reusing the existing submission path). Oracle's job lands in the Mini's ledger. Nothing
correlates them — that's acceptable for v1. If you want a record, pipe stdout.

**Barrier aggregation.** Wait for all replies, then print labeled blocks together. Streaming
was considered and rejected: the use case is a synchronous design discussion you read
top-to-bottom; interleaved output adds complexity with no UX benefit. Per-agent timeout
(30s default) surfaces `[agent: TIMEOUT — reply retrievable via mxr get <jid>]` rather
than hanging.

**Registry `host` field for cross-machine routing.** Oracle's `agy` CLI lives only on the
Mini. Rather than special-casing oracle in the discuss command, `AgentSpec` gets an optional
`host: Optional[str] = None` field (None = local). When `discuss` sees `host="mini"`, it
dispatches via SSH instead of the local submission path. Adding agents to a different machine
in the future is a one-line registry change.

**Extract `_submit_and_fetch()`.** `run_job()` prints as a side effect and can't be called
concurrently for multiple agents without interleaved output. Extract a non-printing coroutine
`_submit_and_fetch(agent, topic) -> str | None` that submits and returns the reply text.
`run_job()` calls it internally and prints — existing callers unchanged.

For the SSH path: `_remote_fetch(host, agent, topic) -> str | None` wraps
`asyncio.create_subprocess_exec("ssh", host, f"~/.local/bin/mxr {agent} ...")` and captures
stdout. No changes to the remote machine — it's just `mxr` invoked via SSH as today.

## Data flow

```
mxr discuss "<topic>" --with lobster oracle kilabz

1. Validate all agent names against REGISTRY — fail fast before any dispatch
2. For each agent, build a dispatch task:
     local agents  → _submit_and_fetch(agent, topic)         # MacBook ledger
     host="mini"   → _remote_fetch("mini", agent, topic)     # ssh mini + capture stdout
3. asyncio.gather(*tasks, return_exceptions=True) with per-task 30s timeout
4. Print labeled blocks in --with order:
     [agent]
     <reply or TIMEOUT notice>
5. Exit 0 if ≥1 reply received; exit 1 if all timed out or failed
```

## Edge cases and failure modes

| Scenario | Handling |
|---|---|
| Unknown agent name in --with | Fail fast before any dispatch; print roster |
| One agent times out | Print `[agent: TIMEOUT — reply via mxr get <jid>]`; continue with others |
| Oracle SSH disconnect mid-wait | SSH subprocess exits non-zero; treat as timeout |
| Mini unreachable | SSH fails at connect; `[oracle: UNREACHABLE]` notice |
| All agents time out / fail | Exit 1, no output printed except timeout notices |
| `--with` contains duplicates | Deduplicate silently (same question twice to the same agent adds noise) |
| Single agent in --with | Works; no minimum N restriction |

## Security surface

**No new capabilities granted.** `discuss` routes to agents declared in the registry using
the same execution paths they already use. It does not accept free-form shell commands or
agent names outside the registry.

**Agent eligibility.** Restrict to `Authority.RESPONDER` and `Authority.CONTROLLER` agents
only (lobster, oracle, kilabz, recon, mack). Exclude `WORKSPACE_ACTOR` agents (mini, curator,
higgsfield, codex) — workspace actors execute code and mutate state; a discussion prompt is
not the right call shape for them and including them opens an unintended capability surface.

**SSH host is registry-declared, not caller-supplied.** The host routing (`host="mini"`) comes
from `AgentSpec`, not the command line. A caller cannot route a discussion to an arbitrary
host by passing a flag.

**Topic sanitization.** The topic is passed as a positional shell argument to `mxr` via
`asyncio.create_subprocess_exec` (not shell=True) — no injection surface. Same on the SSH
path: topic passed as a quoted single argument to the remote `mxr` invocation.

## Files to create / modify

| File | Change |
|---|---|
| `src/runtime/cli.py` | Add `discuss` subcommand; extract `_submit_and_fetch()` from `run_job()` |
| `src/runtime/registry.py` | Add `host: Optional[str] = None` to `AgentSpec`; set `host="mini"` on oracle |
| `src/runtime/contracts.py` | Verify `Authority` enum has the values referenced above (no change expected) |

No new files, no schema changes, no new launchd services.

## Dependencies

- Existing: `asyncio`, `argparse`, `PostgresLedger`, `REGISTRY`
- SSH: `ssh` binary must be on PATH (already required for current oracle usage)
- Mini reachable via Tailscale when oracle is in `--with` (existing requirement)

## Out of scope (v1)

- Multi-round debate (agents see each other's replies)
- Synthesis step (a fourth agent summarizes the N replies)
- `--format` / `--output` flags
- Scheduling or persisting discussions as a named artifact
- Routing discuss to the phone / RC surface

## Review checklist (pre-dispatch to xreview)

- [ ] No `--host` or free-form routing flag exposed to callers
- [ ] Authority filtering enforced (RESPONDER + CONTROLLER only)
- [ ] `asyncio.create_subprocess_exec` not `shell=True` on SSH path
- [ ] `_submit_and_fetch()` extraction leaves `run_job()` behavior unchanged
- [ ] Inbound event per-agent (uniqueness constraint not violated)
- [ ] Partial failure contract: print what arrived, note what timed out, exit code reflects reality
