# Operating the runtime

The runtime runs as two pieces against one Postgres database: a long-lived worker-pool
**service** that drains the queue and runs jobs through the real agent CLIs, and a thin
**`mx` CLI** that submits a task and prints the agent's reply. You name the agent; the
runtime dispatches it durably and hands back the result — direct ops, no orchestrator in
the loop.

## One-time setup

```bash
createdb runtime
psql runtime < src/runtime/ledger/schema.sql
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

A convenient alias:

```bash
mx() { MYNDAIX_DSN=postgresql://localhost/runtime PYTHONPATH="$HOME/code/myndaix-runtime/src" \
       python3 -m runtime.cli "$@"; }
mx kilabz "review the diff in ..."
```

The agent it dispatches to must have its CLI installed and authenticated in the service's
environment (see the roster in `src/runtime/registry.py`).

## Always-on (macOS launchd)

The service is idle — just polling Postgres — until you submit work, so an always-on pool is
cheap: it only invokes an agent when there's a job to run. To keep it running across logins,
save this as `~/Library/LaunchAgents/ai.myndaix.runtime.plist` (fill in `<REPO>` and put the
agent CLIs on `PATH`), then `launchctl load -w ~/Library/LaunchAgents/ai.myndaix.runtime.plist`:

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
