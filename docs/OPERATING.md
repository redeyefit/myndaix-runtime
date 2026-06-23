# Operating the runtime

The runtime runs as two pieces against one Postgres database: a long-lived worker-pool
**service** that drains the queue and runs jobs through the real agent CLIs, and a thin
**`mxr` CLI** that submits a task and prints the agent's reply. You name the agent; the
runtime dispatches it durably and hands back the result — direct ops, no orchestrator in
the loop.

> First-time install (Python deps, Postgres, the agent CLIs, one machine and two) is in
> **[../SETUP.md](../SETUP.md)**. This doc is the day-to-day operating reference.

## One-time setup

Prerequisites: Python 3.11+, a running Postgres 16, and the runtime's deps installed
(`pip install -e .` from the repo root, or `pip install asyncpg fastapi uvicorn httpx pydantic`).
Run the schema load from the repo root (the path is relative):

```bash
createdb runtime
psql runtime < src/runtime/ledger/schema.sql   # from the repo root; load once
export MYNDAIX_DSN=postgresql://localhost/runtime
```

## Run the service

```bash
PYTHONPATH=src python3 -m runtime.serve            # foreground; Ctrl-C to stop (--size N for N workers)
```

## Submit a task

```bash
PYTHONPATH=src python3 -m runtime.cli kilabz "one-line review: def add(a,b): return a-b"
```

A convenient wrapper on your `PATH` (e.g. `~/.local/bin/mxr`):

```bash
#!/bin/bash
export MYNDAIX_DSN="${MYNDAIX_DSN:-postgresql://localhost/runtime}"
export PYTHONPATH="/path/to/your/myndaix-runtime/src"   # <- set to YOUR clone's path
exec python3 -m runtime.cli "$@"
```

then `mxr kilabz "review the diff in ..."`. Point `PYTHONPATH` at *your* checkout — a fresh
`git clone` lands in `./myndaix-runtime` wherever you ran it, so the path above is just an example.
(If you `pip install -e .` into a venv, drop the `PYTHONPATH` line and call that venv's `python`.)

The agent it dispatches to must have its CLI installed and authenticated in the service's
environment (see the roster in `src/runtime/registry.py`).

## Always-on (macOS launchd)

The service is idle — just polling Postgres — until you submit work, so an always-on pool is
cheap: it only invokes an agent when there's a job to run. To keep it running across logins,
save the plist below as `~/Library/LaunchAgents/ai.myndaix.runtime.plist`, then
`launchctl load -w ~/Library/LaunchAgents/ai.myndaix.runtime.plist`.

Fill in three things (and get them right — a background process is unforgiving):

1. **`<REPO>`** — the absolute path to your clone, and **`/Users/you`** — your real `$HOME`.
2. **`PATH`** — launchd does *not* inherit your shell `PATH`. List every dir holding an agent
   CLI: your npm-global bin (`claude`, `codex`) and `~/.local/bin` (`agy`). A missing dir means
   every job for that agent fails with the CLI "not found".
3. **Secrets** — to give the service API keys (e.g. `PERPLEXITY_API_KEY` for `recon`), don't put
   them in the plist. Point `ProgramArguments` at a small wrapper that sources a `chmod 600` env
   file first, e.g. a `runtime-serve.sh` containing:
   `set -a; . "$HOME/.myndaix/.secrets"; set +a; exec <REPO>/.venv/bin/python -m runtime.serve --size 4`
   — then the plist runs `/bin/bash <REPO>/runtime-serve.sh`. (The `.venv` path assumes you ran
   `python3 -m venv .venv && .venv/bin/pip install -e .`; otherwise call `python3` with `PYTHONPATH`.)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ai.myndaix.runtime</string>
  <key>ProgramArguments</key>
  <array>
    <string><REPO>/.venv/bin/python</string><string>-m</string><string>runtime.serve</string>
  </array>
  <key>WorkingDirectory</key><string><REPO></string>
  <key>EnvironmentVariables</key><dict>
    <key>MYNDAIX_DSN</key><string>postgresql://localhost/runtime</string>
    <key>PYTHONPATH</key><string><REPO>/src</string>
    <key>HOME</key><string>/Users/you</string>
    <key>PATH</key><string>/Users/you/.npm-global/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string><REPO>/.runtime.log</string>
</dict></plist>
```

Stop it with `launchctl unload ~/Library/LaunchAgents/ai.myndaix.runtime.plist`. Logs go to
`.runtime.log`. The launchd service runs agents non-interactively, so each agent CLI must be
authenticated in a way a background process can read (an env API key or a cached token).
