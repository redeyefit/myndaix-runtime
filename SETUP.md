# Setup

From a cold clone to `mxr <agent> "<task>"` returning a real model reply. Two ways to read this:

- **Kick the tires** (≈2 min, no Postgres, no API keys): steps 1–2.
- **Run it for real**: steps 1, 3–6, then [one machine vs. two](#one-machine-vs-two-machines).

Everything here was verified from a fresh clone of this repo.

## Prerequisites

| Need | For | Notes |
|---|---|---|
| **Python 3.11+** and **git** | everything | tested on 3.14 |
| **Postgres 16** | the real service (steps 3–6) | the zero-dep demos don't need it |
| **Node 18+** | installing the Claude/Codex CLIs | `agy` is a standalone binary, not npm |
| A provider account per agent | a real reply from that agent | Anthropic / OpenAI / Google (Antigravity) / Perplexity — only the ones you use |

## 1. Clone and install

```bash
git clone https://github.com/redeyefit/myndaix-runtime && cd myndaix-runtime
python3 -m venv .venv && source .venv/bin/activate
pip install -e .     # installs all deps from pyproject AND makes `runtime` importable
```

A venv avoids the PEP 668 `externally-managed-environment` error on modern macOS/Debian. After `pip install -e .` you can drop the `PYTHONPATH=src` prefix the demos show — the package is importable. If you'd rather not install, run from source with `PYTHONPATH=src python3 ...` and `pip install pydantic` (the zero-dep demos need only pydantic).

## Run it with Docker (Postgres + service, one command)

Have Docker? This is the fastest path to a running ledger + spine — it replaces the manual Postgres
setup (step 3) and the service (step 5):

```bash
docker compose up --build       # Postgres (schema auto-loaded) + the worker pool
```

Postgres is published on `localhost:5432` (db `runtime`, user/pass `runtime`/`runtime`), so the `mxr`
CLI on your host talks to the same ledger:

```bash
MYNDAIX_DSN=postgresql://runtime:runtime@localhost:5432/runtime mxr recon "latest stable Python release"
```

**What runs where:** the containerized worker runs the ledger and dispatches **API-reach agents**
(`recon`, when you start compose with `PERPLEXITY_API_KEY` set in your shell). The **CLI agents**
(`claude`, `codex`, `agy`) aren't installed in the image — run a `serve` on your *host* (where the CLIs
are authenticated) pointed at the same DSN, and both workers drain one queue. Install the CLIs per
step 4 below.

## 2. Kick the tires (zero-dep — no Postgres, no keys, no LLM)

```bash
python3 demo.py            # route a message through the spine and back
python3 demo.py --isolate  # an agent "fixes" a bug inside a throwaway git worktree (live repo untouched)
```

This proves the spine and the git-worktree isolation without any external dependency. The `--isolate` run shows the change as a reviewable diff that is never auto-merged. (The other demo flags — `--pool`, `--postgres`, `--terminal`, `--api` — need Postgres; see step 3.)

## 3. Postgres (the real ledger)

Install and start it:

```bash
# macOS:         brew install postgresql@16 && brew services start postgresql@16
# Debian/Ubuntu: sudo apt-get install -y postgresql && sudo service postgresql start
```

Create the ops database and load the state machine **once**, from the repo root:

```bash
createdb runtime
psql runtime < src/runtime/ledger/schema.sql
export MYNDAIX_DSN=postgresql://localhost/runtime
```

### Databases and env vars (a real gotcha — three names, on purpose)

| Env var | Used by | Default db | Why separate |
|---|---|---|---|
| `MYNDAIX_DSN` | the **service** (`runtime.serve`) and **`mxr`** (`runtime.cli`) | `runtime` | your live ops ledger |
| `LEDGER_TEST_DSN` | the **demos** and **tests** | `runtime_test` | they DROP and recreate the schema — must be a throwaway db |
| `LEDGER_DSN` | the HTTP API (legacy) | `runtime` | the API now reads `MYNDAIX_DSN` first; `LEDGER_DSN` still works |

Never point the demos/tests at your ops db. For the Postgres demos and tests, use a throwaway:

```bash
createdb runtime_test
export LEDGER_TEST_DSN=postgresql://localhost/runtime_test

python3 demo.py --pool       # N workers drain a queue + recover a crashed worker
python3 demo.py --postgres   # the SAME worker core, now Postgres-backed
python3 demo.py --terminal   # a dumb-pipe transport: a slow agent never blocks it
python3 demo.py --api        # the HTTP service: POST a job, GET its status + reply
```

## 4. Install the agent CLIs

This is what turns a job into a real model reply: the worker shells out to a local CLI (or, for `recon`, an HTTP API). **Install and authenticate each CLI on the machine that runs the pool** (`runtime.serve`) — the worker is what invokes it, so its credentials must live where `serve` runs. Install only the agents you'll use. Each is **install → authenticate → verify**.

| Agent(s) | Backend | Install | Verify |
|---|---|---|---|
| `mack`, `mini`, `lobster` | Claude Code | `npm install -g @anthropic-ai/claude-code` | `claude --version` |
| `kilabz`, `codex` | OpenAI Codex | `npm install -g @openai/codex` | `codex --version` |
| `oracle` | Antigravity (Gemini) | `curl -fsSL https://antigravity.google/cli/install.sh \| bash` | `agy --version` |
| `recon` | Perplexity (API) | *no CLI* — set `PERPLEXITY_API_KEY` | see below |

### Claude Code — `mack`, `mini`, `lobster`

```bash
npm install -g @anthropic-ai/claude-code
claude            # first run: OAuth sign-in (Pro/Max/Teams/Console) — or: export ANTHROPIC_API_KEY=...
claude --version  # prints a recent version (e.g. 2.1.x) — the runtime doesn't pin a CLI version
```

### OpenAI Codex — `kilabz`, `codex`

```bash
npm install -g @openai/codex   # the SCOPED package — NOT bare `codex` (an unrelated 2012 project)
codex             # first run: sign in with a ChatGPT account — or: `codex auth` with an OpenAI API key
codex --version   # prints a recent version (e.g. codex-cli 0.13x)
```

### Antigravity (Gemini) — `oracle`

The standalone `gemini` CLI's individual tier was retired in 2026; its successor is `agy`.

```bash
curl -fsSL https://antigravity.google/cli/install.sh | bash   # installs ~/.local/bin/agy (NOT npm, NOT `gemini`)
agy               # first run: OAuth wizard (Desktop, or Headless for SSH) — or: export ANTIGRAVITY_API_KEY=...
agy --version     # prints a recent version (e.g. 1.0.x)
```

Make sure `~/.local/bin` is on your `PATH` (and on the **service's** PATH — see step 6).

### Perplexity — `recon` (API, no CLI)

`recon` is an API-reach agent: there's no CLI to install, just a key in the environment.

```bash
# get a key from the Perplexity API platform, then:
export PERPLEXITY_API_KEY=pplx-...
```

The runtime reads the key from the environment **at dispatch time, never from the roster/config**. Keep it in a `chmod 600` env file you source before starting the service (step 6) — not in the repo.

## 5. Run the service and submit a task

```bash
# terminal 1 — the always-on worker pool (idle until a job arrives)
python3 -m runtime.serve            # --size N for N workers

# terminal 2 — submit a task, print the reply
python3 -m runtime.cli kilabz "one-line review: def add(a,b): return a-b"
```

The agent you name must have its CLI installed and authenticated in the **service's** environment (the pool is what runs it).

A convenience wrapper on your `PATH` so you can type `mxr <agent> "<task>"`:

```bash
cat > ~/.local/bin/mxr <<'EOF'
#!/bin/bash
export MYNDAIX_DSN="${MYNDAIX_DSN:-postgresql://localhost/runtime}"
export PYTHONPATH="/path/to/your/myndaix-runtime/src"   # <- set to YOUR clone's path
exec python3 -m runtime.cli "$@"
EOF
chmod +x ~/.local/bin/mxr

mxr recon "latest stable Python release"
```

`recon` needs `PERPLEXITY_API_KEY` in the **pool's** environment (step 4) — the shell you run `mxr` in
doesn't matter, since the agent runs inside `serve`. Export the key where `serve` runs and restart it.
(If you `pip install -e .` into a venv, drop the `PYTHONPATH` line and call that venv's `python`.)

## 6. Always-on service (macOS launchd)

The pool only invokes an agent when there's a job, so keeping it running is cheap. The full, corrected launchd plist (with the secrets-sourcing wrapper, the venv python path, and the explicit `PATH` a background process needs) is in **[docs/OPERATING.md](docs/OPERATING.md)**. Two non-obvious must-dos:

- **PATH**: launchd does *not* inherit your shell `PATH`. Set it explicitly in the plist, including `~/.local/bin` (where `agy` lives) and your npm-global bin — otherwise every job fails with the agent CLI "not found".
- **Secrets**: have the service source a `chmod 600` env file (e.g. `~/.myndaix/.secrets` with `export PERPLEXITY_API_KEY=...`) before exec'ing — keys stay out of the plist.

---

## One machine vs. two machines

### One machine (default)

Everything above runs on one box: Postgres, the worker pool, and the agent CLIs all local. That's the default and what the demos assume.

### Two machines (or N)

The **Postgres ledger is the only shared state** — workers self-coordinate purely through row locks (`FOR UPDATE SKIP LOCKED`), with no registration, leader election, or peer channel. So you can split the runtime across boxes, all pointed at one DSN:

- **DB host (machine A)** — runs Postgres, exposed to the other boxes on a *private* network only.
- **Worker box(es) (machine B…)** — run `runtime.serve` against the remote DSN. **The agent CLIs must be installed + authenticated here** (the worker is what shells out).
- **API box (optional)** — runs `uvicorn runtime.api:app` against the same DSN for the HTTP front door. Needs no agent CLIs.

**DB host:**

```bash
brew install postgresql@16 && brew services start postgresql@16
createdb runtime
psql runtime -c "CREATE ROLE runtime LOGIN PASSWORD 'STRONG_PW';"
psql runtime < /path/to/myndaix-runtime/src/runtime/ledger/schema.sql   # load ONCE, from one machine

# grant AFTER the tables exist — the schema is owned by the superuser that loaded it, so the
# runtime role needs table + sequence privileges or it can't read or write a single row:
psql runtime -c "GRANT USAGE ON SCHEMA public TO runtime;
                 GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO runtime;
                 GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO runtime;"
```

Expose it to the worker boxes — edit `postgresql.conf` (find it with `psql -c 'SHOW config_file;'`; on
Apple-Silicon brew it's `/opt/homebrew/var/postgresql@16/postgresql.conf`):

```conf
# this host's OWN Tailscale/LAN IP (its `tailscale ip -4`), not a worker's — never '*'
listen_addresses = 'localhost,100.x.x.x'
```

and **append** to `pg_hba.conf` (find it with `psql -c 'SHOW hba_file;'`). Append, don't replace — keep the
default local-socket entries or you lock yourself out of local admin; first match wins, so put this above
any catch-all `reject`:

```conf
# scram, scoped to the worker network (Tailscale CGNAT range) — never `trust`/`md5`
host  runtime  runtime  100.64.0.0/10  scram-sha-256
```

then `brew services restart postgresql@16` (reloads both files).

**Worker box(es):**

```bash
git clone https://github.com/redeyefit/myndaix-runtime && cd myndaix-runtime
python3 -m venv .venv && .venv/bin/pip install -e .
# install + authenticate the agent CLIs you'll route here (claude/codex/agy);
# set PERPLEXITY_API_KEY for recon
export MYNDAIX_DSN='postgresql://runtime:STRONG_PW@100.x.x.x:5432/runtime'
.venv/bin/python -m runtime.serve --size 8
```

Add more worker boxes the same way — **more `serve` processes against the same DSN is horizontal scale**, no coordination to configure.

**API box (optional):**

```bash
export MYNDAIX_DSN='postgresql://runtime:STRONG_PW@100.x.x.x:5432/runtime'
export MYNDAIX_API_KEYS='SECRET:alice:client'   # token:principal:role; empty => everything is 401
uvicorn runtime.api:app --host 0.0.0.0 --port 8080
```

**How submit/reply crosses machines:** a submit (CLI or HTTP) writes a `queued` job to the ledger; any worker on any box leases it; on completion the reply is written into the `outbound` table *in the same transaction* that marks the job done (transactional outbox); the submitter polls and reads it back. The submitter and the executing worker never talk directly — Postgres rows are the entire coordination and delivery channel.

**Rules for the multi-machine case (don't skip):**

- **Never expose Postgres to the internet; prefer an encrypted overlay.** Use Tailscale/WireGuard (the link is encrypted end-to-end) + `scram-sha-256`, scoped in `pg_hba.conf` to the worker IPs. `scram-sha-256` only protects the *password* — on a plain LAN, job prompts and replies travel in cleartext (asyncpg silently runs unencrypted if the server has no cert), so for a non-VPN LAN turn on server TLS (`ssl = on`) and append `?sslmode=require` to the worker DSN.
- **Load the schema exactly once, from one machine.** `schema.sql` has no `IF NOT EXISTS` guards, so a second load errors on the first existing object and stops (it won't double-create); to re-apply cleanly, `dropdb runtime && createdb runtime` and reload. Never wire a worker to create the schema on boot.
- **Secrets are per-worker-machine.** Each box needs its own authenticated CLIs / API keys. Keep the DB password and keys in a `chmod 600` env file, not the repo or a world-readable plist. If a provider ties auth to one login account, prefer a separate account per machine.
- **Repo-targeted builder jobs are local to the leasing worker.** A workspace-actor job runs in a git worktree on that worker's disk and its diff is a local path. For a first two-machine split, keep file-mutating builders on the box that has the repos checked out; run read-only agents (`kilabz`, `oracle`, `recon`) anywhere.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `externally-managed-environment` (PEP 668) | use a venv — step 1 |
| `ModuleNotFoundError: runtime` | `pip install -e .`, or prefix with `PYTHONPATH=src`; in the `mxr` wrapper point `PYTHONPATH` at *your* clone |
| `database "runtime_test" does not exist` | the demos/tests use `LEDGER_TEST_DSN`/`runtime_test`, separate from the service's `runtime` — `createdb runtime_test` |
| `mxr recon` → `missing API key in env: PERPLEXITY_API_KEY` | export the key in the **service's** environment (step 4) |
| under launchd, an agent CLI is "not found" | the service's `PATH` must include where the CLI lives (e.g. `~/.local/bin` for `agy`); launchd doesn't inherit your shell `PATH` |
| `unknown agent 'X'` | see the roster in `src/runtime/registry.py` |
