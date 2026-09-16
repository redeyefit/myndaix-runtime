# DESIGN — mxr discuss

**Status:** v0.2 — post-review revision (oracle CRITICAL + 3 HIGHs addressed)
**Branch:** feat/mxr-discuss

## What

`mxr discuss "<topic>" --with <agent> [<agent>...]`

Parallel fan-out to named agents, barrier aggregation, labeled output. One command replaces
the current manual pattern of `MXR_REVIEW_GATE_BYPASS=1 mxr lobster "..."` × N agents +
SSH-routing oracle by hand + collecting replies separately.

```
$ mxr discuss "should we build the proposer now?" --with lobster oracle kilabz

─── [lobster] ───────────────────────────────────
...reply...

─── [oracle] ────────────────────────────────────
...reply...

─── [kilabz] ────────────────────────────────────
...reply...
```

## Why

Three friction points today:
1. Each agent is a separate manual call — no way to say "the team discusses this"
2. Oracle requires SSH routing to the Mini; callers own that routing
3. The review-gate hook fires on any `mxr kilabz/oracle` call; callers need `MXR_REVIEW_GATE_BYPASS=1`

## Build-vs-Adopt

- `mxr discuss` subcommand: **BUILD** — extends the existing CLI; no prior art covers this
- Parallel dispatch: **BORROW** `asyncio.gather` (standard library)
- Oracle SSH routing: **BUILD** a `host` field in `AgentSpec` + `_remote_fetch()` helper;
  the informal pattern `ssh mini mxr oracle` already exists — we're formalizing it

## Key design decisions

### Native subcommand (all three reviewers agree)

Implemented in `cli.py`. Reuses the registry, existing submission helpers, and the
`asyncio` event loop already required by `run_job()`. No wrapper script.

### Registry `host` field for cross-machine routing

`AgentSpec` gets `host: Optional[str] = None` (None = run locally). Oracle's entry gets
`host="mini"`. The discuss handler checks this field and picks the dispatch path. Adding
future remote agents is a one-line registry change. The `host` value is registry-declared —
callers cannot supply an arbitrary host via command-line flag.

### Two dispatch paths

**Local agents** (`host=None`): call `_submit_and_fetch(agent, topic)` — a non-printing
coroutine extracted from `run_job()`. It submits to the MacBook's ledger, polls, and
returns `DiscussResult`.

**Remote agents** (`host="mini"`): call `_remote_fetch("mini", agent, topic)` — spawns
`asyncio.create_subprocess_exec("ssh", ...)` and parses stdout. See SSH contract below.

### `DiscussResult` return type

```python
@dataclass
class DiscussResult:
    agent: str
    reply: str | None          # None = no usable reply
    job_id: str | None         # present if submission was acknowledged before timeout
    ledger: str                # "local" | "mini" — tells user which mxr get to run
    error: str | None          # TIMEOUT | UNREACHABLE | EMPTY | SSH_ERROR
```

The job handle is captured from `JOB_ID=<uuid>` on stderr *before* awaiting the reply, so
a timeout after submission always carries the job ID. A timeout before submission (connection
failure, queue rejection) produces `job_id=None` and `error="UNREACHABLE"` — the timeout
notice omits the `mxr get` hint in that case, never promises unretrievable recovery.

### SSH injection defense (oracle CRITICAL)

Topic is passed to the remote `mxr` invocation via `shlex.quote()`, and `--` precedes the
topic argument to prevent option injection:

```python
import shlex
remote_cmd = f"~/.local/bin/mxr {shlex.quote(agent)} -- {shlex.quote(topic)}"
proc = await asyncio.create_subprocess_exec(
    "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
    host, remote_cmd,
    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
)
```

`shlex.quote()` wraps the value in single quotes and escapes internal single quotes —
injection-safe for the remote shell. `--` terminates option parsing before the topic.
`BatchMode=yes` disables password/host-key prompts (fails noninteractively instead).
`ConnectTimeout=10` bounds the SSH handshake independently of the reply timeout.
`stdin=DEVNULL` ensures the subprocess never inherits the parent's stdin.

### Review-gate exemption

**Local path:** The `discuss` subcommand calls `_submit_and_fetch()` via Python directly —
no `mxr` subprocess is spawned, so no PreToolUse hook fires. No bypass flag needed.

**Remote path:** The SSH command string explicitly prepends `MXR_REVIEW_GATE_BYPASS=1`:

```python
remote_cmd = f"MXR_REVIEW_GATE_BYPASS=1 ~/.local/bin/mxr {shlex.quote(agent)} -- {shlex.quote(topic)}"
```

This is safe: the bypass value is a literal constant, not derived from user input. The bypass
is documented as the correct mechanism for non-review dispatches (commit-before-review.md).

### Barrier aggregation with per-task timeout

`asyncio.wait_for(coro, timeout=30)` wraps each participant. Python's `wait_for` re-raises
`asyncio.TimeoutError` after the timeout, but the inner coroutine finishes cancellation
before the caller proceeds — the 30 s is therefore NOT a hard wall-clock bound per task
(acknowledged). Total wall-clock for N tasks = up to 30 s × N in the worst case. For typical
3-agent discussions this is at most ~90 s; acceptable.

On `asyncio.TimeoutError`: attempt to terminate the SSH subprocess (`proc.terminate()`,
then `proc.kill()` after 5 s). Reap with `await proc.wait()` with a short bound.

On `KeyboardInterrupt` / `asyncio.CancelledError`: same teardown path; deliver any replies
already received before exiting.

### Timeout recovery notices

```
─── [oracle] ────────────────────────────────────
[TIMEOUT — job accepted, reply via: ssh mini mxr get <job_id> --reply]

─── [oracle] ────────────────────────────────────
[TIMEOUT — job not confirmed (connection failed)]
```

Only the first form appears when `job_id` is known. The second when `job_id` is None.
`mxr get` target is `ssh mini mxr get` for remote agents, bare `mxr get` for local ones.

### Ephemeral at the discuss level

No new "discussion" table, no `discussion_id` correlating the participant jobs. Local-agent
jobs land in the MacBook ledger as normal independent jobs. Oracle's job lands in the Mini's
ledger. Nothing correlates them — acceptable for v1. Pipe stdout to a file for a durable record.

### Inbound event uniqueness

Each `_submit_and_fetch()` call generates a fresh `uuid.uuid4()` dedupe key (same as the
existing `run_job()` path — see `cli.py:108`). The uniqueness constraint (`capture_candidate_pkey`
on `fingerprint`) is in the capture table, not on inbound events. The `inbound_event` table
has no uniqueness constraint on the dedupe key beyond what `ingest_inbound()` enforces. Each
participant gets its own inbound event row — no collision.

### Agent eligibility

Only `Authority.RESPONDER` and `Authority.CONTROLLER` agents are eligible:
- RESPONDER: oracle, kilabz, recon, lobster (as called via standard job submission)
- CONTROLLER: lobster (lobster is dual-authority; job submission path does NOT grant it
  the ability to spawn workspace-actor jobs via `discuss` — it receives the topic and replies
  like a responder in this context)

Rejected outright: `WORKSPACE_ACTOR` (mini, curator, higgsfield, codex, mack) — workspace
actors mutate state and should not be reached via a discussion prompt.

Preflight validates ALL agent names and authorities before submitting to ANY of them. A
mixed roster (some eligible, some not) is rejected entirely with a clear error message before
any dispatch. This prevents partial-submission state.

### Authority × route combinations

| Agent authority | host=None (local) | host="mini" (remote SSH) |
|---|---|---|
| RESPONDER | ✅ supported | ✅ supported |
| CONTROLLER | ✅ supported | ✅ supported |
| WORKSPACE_ACTOR | ❌ rejected at preflight | ❌ rejected at preflight |

Remote registry compatibility: `discuss` sends the same topic to the remote `mxr` using the
agent name as declared in the local registry. The remote machine's registry must have a
matching entry — verified trivially (oracle is the only remote agent, and the Mini's registry
is kept in sync with the main tree). If a name is missing on the remote, `mxr` returns a
non-zero exit; `_remote_fetch` surfaces this as `error="SSH_ERROR"`.

### Remote result parsing

The existing `mxr` CLI prints to stdout (reply text) and stderr (progress markers: `JOB_ID=`,
`MXR_DONE_EMPTY`, `MXR_SYNC_TIMEOUT`, `-> agent`). `_remote_fetch` captures both streams:

- **stderr**: scan for `JOB_ID=<uuid>` as it arrives (stream parse); store as `job_id`.
  Also watch for `MXR_DONE_EMPTY` and `MXR_SYNC_TIMEOUT` to distinguish empty/timeout from
  a reply.
- **stdout**: the complete reply body.
- **SSH exit code**: 0 = remote command exited 0; non-zero = remote command failed OR SSH
  transport error (SSH exits 255 for transport errors specifically — log this distinction).

### Labeled output trust boundary

Reply text is sanitized before printing: strip ANSI/VT100 escape sequences
(`re.sub(r'\x1b\[[0-9;]*[mGKHF]', '', body)`). Section headers use a fixed-width frame
(`─── [agent] ───...`) that is unlikely to collide with reply content; no additional framing
is needed given we control the label format and the reply is rendered in a terminal context.

## Data flow

```
mxr discuss "<topic>" --with lobster oracle kilabz

1. Validate all agent names + authorities against REGISTRY — reject entire call if any invalid
2. For each agent, build a DiscussTask:
     host=None  → _submit_and_fetch(agent, topic)   [MacBook ledger]
     host="mini" → _remote_fetch("mini", agent, topic)  [ssh + stdout capture]
3. results = await asyncio.gather(*[asyncio.wait_for(t, 30) for t in tasks],
                                   return_exceptions=True)
   — exceptions (TimeoutError, etc.) are caught per-task, not globally
4. For each result in --with order:
     if DiscussResult.reply: print header + reply
     else: print header + timeout/error notice
5. Exit 0 if ≥1 reply received; exit 1 if all failed
```

## Edge cases and failure modes

| Scenario | Handling |
|---|---|
| Unknown / ineligible agent name | Preflight rejects before any dispatch; print roster |
| Mixed eligible/ineligible roster | Entire call rejected at preflight, no partial dispatch |
| Agent times out (job accepted) | `[TIMEOUT — reply via: ssh mini mxr get <jid> --reply]` |
| Agent times out (not accepted) | `[TIMEOUT — job not confirmed (connection failed)]` |
| Oracle SSH host unreachable | `ConnectTimeout=10` expires → `[UNREACHABLE]` |
| SSH auth failure / host-key error | `BatchMode=yes` makes it noninteractive; SSH exits 255 |
| Oracle returns `MXR_DONE_EMPTY` | `[EMPTY — agent produced no reply]` |
| Reply contains forged headers | ANSI-stripped; content is indented after a fixed header line |
| `--with` contains duplicates | Deduplicate before preflight |
| Single agent in `--with` | Supported; functions as a bypass-free single dispatch |
| Ctrl-C during wait | Terminate all SSH subprocesses; print received replies; exit 130 |
| All agents fail/timeout | Exit 1 with per-agent notices |

## Security surface

- **SSH injection**: mitigated via `shlex.quote()` + `--` option terminator (see above)
- **No free-form host**: `host` is registry-declared, not caller-supplied
- **Authority filtering**: WORKSPACE_ACTOR rejected at preflight
- **Transitive delegation**: `discuss` submits a plain text prompt to the agent; the agent
  cannot spawn workspace-actor jobs via this path (that requires `submit_job` with a
  WORKSPACE_ACTOR `to_agent` — not triggered by a text reply)
- **BYPASS propagation**: explicit literal constant on SSH path; not derived from user input
- **SSH unattended**: `BatchMode=yes`, `ConnectTimeout=10`, `stdin=DEVNULL`

## Files to create / modify

| File | Change |
|---|---|
| `src/runtime/cli.py` | Add `discuss` subcommand; extract `_submit_and_fetch()` + `_remote_fetch()`; add `DiscussResult` |
| `src/runtime/registry.py` | Add `host: Optional[str] = None` to `AgentSpec`; set `host="mini"` on oracle |
| `src/runtime/contracts.py` | Verify `Authority` enum — no change expected |

No new files beyond the above, no schema changes, no new launchd services.

## Dependencies

- `asyncio`, `argparse`, `shlex`, `dataclasses` (stdlib)
- `PostgresLedger`, `REGISTRY` (existing runtime)
- `ssh` binary on PATH with BatchMode-compatible host config for Mini
- Mini reachable via Tailscale when oracle is in `--with`

## Out of scope (v1)

- Multi-round debate (agents see each other's replies)
- Synthesis step
- `--format` / `--output` flags
- Scheduling or persisting discussions as a named artifact
- Routing discuss to phone / RC surface

## Review checklist (pre-dispatch)

- [x] SSH injection: `shlex.quote()` + `--` on both agent name and topic
- [x] `BatchMode=yes`, `ConnectTimeout=10`, `stdin=DEVNULL` on SSH path
- [x] `MXR_REVIEW_GATE_BYPASS=1` prepended as literal constant on SSH command string
- [x] Local path uses Python submission directly — no hook fires
- [x] `DiscussResult` carries `job_id` before reply is awaited
- [x] `job_id` parsed from stderr `JOB_ID=` marker before reply wait
- [x] Timeout notice shows correct `mxr get` target (mini vs local)
- [x] All agents validated + authority-checked before any dispatch
- [x] Mixed eligible/ineligible roster rejected atomically
- [x] ANSI escape sequences stripped from reply text before printing
- [x] WORKSPACE_ACTOR agents rejected at preflight
- [x] No `shell=True` on any subprocess call
