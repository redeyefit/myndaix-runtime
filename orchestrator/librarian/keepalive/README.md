# Recall librarian — always-on keepalive (graduate piece C to the Mini)

Keeps the phone-reachable recall librarian (piece C) **always on** on the Mini: one supervised
`claude remote-control` session, confined to answering `mxr ask --scope research|fitness|company "…"`,
restarted if it dies, parked (not thrashed) if it can't come up. This is the second-brain rung-1
graduation — "reachable when the MacBook is awake" → "reachable 24/7 from the phone."

This is the **supervisor**. The **fence** it supervises is piece C (`orchestrator/librarian/`):
CLAUDE.md + `.claude/settings.json` (deny every non-Bash tool) + the recall-gate PreToolUse hook
(allow ONLY `mxr ask --scope research|fitness|company "<safe q>"`). The keepalive adds no capability — it
only keeps a session alive **inside** that fence.

## Design — borrowed from Watch, stripped for read-only recall

The **pattern** is borrowed from the Watch keepalive (on the `design/watch-v0.3` branch, which had
its own cross-family r1–r3 review): the tmux-on-a-pinned-socket supervisor, the idempotent bootstrap,
the fast-exit → park circuit, the clean auth-env boundary, the disk floor. But these files are
**net-new** and reviewed on their own merits (do NOT lean on Watch's review as coverage for this
code). **Stripped** everything the librarian doesn't have — it does not read untrusted files or
dispatch, so there is no read fence (`sanitize_untrusted`/`watch-scan.py`), no inbox/ledger read
wrappers, no dispatch gate. The recall-gate is the *sole* gate, and it is an **allowlist** (matcher
`"*"`): it fires for every tool and denies anything that is not a valid `mxr ask` — fail-closed for
any future tool, not dependent on the settings deny-list staying exhaustive (keepalive review r2).

| File | Role |
|---|---|
| `librarian-lib.sh` | `lib_log` (rotating, best-effort) + `lib_alert` (park-only iMessage; recipient EMPTY by default → log-only, honoring no-auto-texts) + `lib_validate_fence` (parse + smoke-run the gate before launch — the fail-closed CRITICAL guard). No read fence. |
| `rc-bootstrap.sh` | launchd entrypoint. Idempotent: ensure ONE tmux session `librarian` on a pinned socket running the wrapper, then exit. Fail-closed guards: parked / missing fence / low-or-unknown disk / tmux protocol mismatch → do not start. |
| `rc-wrapper.sh` | in-tmux supervisor loop. Clean minimal env (PATH with `~/.local/bin` for `mxr`, baked `MYNDAIX_KNOWLEDGE_SCOPES`, `CLAUDE_CODE_DISABLE_MCP=1`, stripped auth env) → reachability gate → disk floor → `preflight` (re-validate fence + canonical `mxr` before EVERY launch) → `claude remote-control --capacity 1 --spawn same-dir --permission-mode dontAsk --name librarian` in `~/librarian` → on exit classify; 3 sub-5s exits → PARK. |
| `ai.myndaix.librarian-rc.plist.example` | LaunchAgent (gui domain — RC needs the logged-in claude.ai OAuth). RunAtLoad + StartInterval=120 recheck; `AbandonProcessGroup=true` (load-bearing); WorkingDirectory pins identity+fence. |
| `test.sh` | supervisor smoke test (lib log/alert + bootstrap fail-closed guards + idempotency), no `claude` launched. The FENCE is tested by `orchestrator/librarian/test.sh`. |

### Data flow
phone (Claude app) → RC session in `~/librarian` → session runs `mxr ask --scope … "q"` (the ONLY
thing the recall-gate allows) → the `mxr` client dispatches a `librarian` RESPONDER to the Mini pool
→ cited answer returns to the phone. No file reads, no web, no other dispatch — a poisoned corpus
doc can at worst produce a wrong *answer*.

### Failure modes (and the response)
- **Session dies / crashes** → wrapper loop relaunches after a healthy run; bootstrap re-creates on
  the 120s timer if the whole session is gone.
- **Auth expired / RC refuses** → 3 sub-5s exits → PARK (marker + log + optional alert), pane stays
  alive so the operator can attach; bootstrap refuses to thrash. Recovery = re-auth, then `rm` the
  marker — the parked wrapper exits, the session ends, and the next 120s tick recreates it fresh (no
  kickstart needed).
- **Network down** → reachability gate backs off (does NOT count as a fast-exit / does not park).
- **Disk full / unknown** → bootstrap refuses to start; the wrapper re-checks the floor before EVERY
  relaunch and backs off (transient — self-heals via rotation/reaper; does not park). No-space class.
- **Fence not staged OR broken** (missing / malformed `settings.json`, non-executable or wrong hook
  path, a gate that doesn't deny) → `lib_validate_fence` parses + SMOKE-RUNS the gate (must deny `ls`,
  must deny a non-Bash tool, must allow a valid `mxr ask`); bootstrap AND wrapper refuse to start /
  park. Re-validated before EVERY relaunch (not once), so a mid-life fence break parks the next
  launch. Never serve an unconfined RC session — fail-closed (review r1 CRITICAL, r2 HIGH-3).
- **`mxr` not the canonical shim** (`$HOME/.local/bin/mxr` absent, or a different `mxr` on PATH) →
  wrapper parks (the gate allows the literal token `mxr`; a wrong binary must not run under it).
- **tmux upgraded under a running server** → protocol-mismatch park, never a second socket.
- **Always-listening RC exposure (the biggest NEW graduation risk).** On the MacBook the session
  existed only while awake with Jefe present; on the Mini it is a 24/7 RC endpoint bound to Jefe's
  claude.ai account. Anyone who compromises that account can pair a device and drive the librarian.
  The blast radius stays "wrong *answer*" ONLY as long as the fence holds — which is exactly why the
  gate is an allowlist (deny every non-`mxr-ask` tool structurally) rather than a deny-list that can
  go stale into a write-capable tool. **Mitigation: claude.ai account 2FA is now part of the
  librarian's blast radius.** The fence is the only thing between a paired device and the tool surface.
- **RC/JSONL transcript growth over months.** `librarian.log` self-rotates at 1MB, but RC's own
  session JSONL under `~/librarian` / `~/.claude` has no reaping here — the likeliest slow disk
  filler. The disk floor *refuses to relaunch* when low but never reclaims; prune transcripts
  periodically (or add a reaper) if this grows.
- **Silent re-park.** With `LIB_ALERT_IMESSAGE_TO` empty (house no-auto-texts default), a park logs
  but does not notify. If the root cause isn't fixed before `rm`-ing the marker, the librarian
  re-parks every ~2min **invisibly**. Set `~/.myndaix/orchestrator/librarian/.alert-to` to a phone
  number to make parks visible.

## Deploy (Mini, as `jefe`) — prerequisites Mack can do non-interactively

1. **Runtime code present** at `/Users/jefe/code/active/myndaix-runtime` (or edit the plist path) and
   the pool live (it already runs from `~/.myndaix/deploy/myndaix-runtime`).
2. **`mxr` on PATH**: `~/.local/bin/mxr` exists (DSN `postgresql://127.0.0.1/runtime`). The wrapper
   prepends `~/.local/bin`, so login-shell PATH is not required for the keepalive — but keep the shim
   there as the canonical `mxr`.
3. **Corpus ingested** on the Mini: `mxr knowledge-ingest --scope research`, `--scope fitness`, and
   `--scope company` (the last requires `~/company` present on the Mini — see the MVP copy note below).
4. **Stage the confined workspace** — and **rewrite the hook path for this machine**. The recall-gate
   stays in the repo; `settings.json` references it by ABSOLUTE path, hardcoded to the MacBook
   checkout (`/Users/stevenfernandez/...`). On the Mini it MUST point at the Mini checkout. If it
   doesn't, `lib_validate_fence` (run by both the bootstrap and the wrapper) smoke-runs the gate,
   finds the hook non-executable / non-denying, and **refuses to start / parks** — fail-closed, the
   session never comes up unconfined. Rewrite it so the librarian actually runs:
   ```
   RT=/Users/jefe/code/active/myndaix-runtime          # the Mini checkout that holds the hook
   mkdir -p ~/librarian/.claude
   cp "$RT/orchestrator/librarian/kit/CLAUDE.md" ~/librarian/CLAUDE.md
   sed 's#/Users/stevenfernandez/code/active/myndaix-runtime#'"$RT"'#g' \
       "$RT/orchestrator/librarian/kit/settings.json" > ~/librarian/.claude/settings.json
   # verify the rewritten hook path resolves + is executable:
   test -x "$(grep -oE '/Users/[^"]*recall-gate.sh' ~/librarian/.claude/settings.json)" && echo hook-ok
   ```
5. **Smoke test the supervisor**: `bash orchestrator/librarian/keepalive/test.sh` (17/17 local) and
   the gate: `bash orchestrator/librarian/test.sh` (35/35).

## Deploy — Jefe's hands (interactive: RC needs claude.ai OAuth)

6. **One-time trust + auth** (RC refuses an untrusted folder and rejects long-lived tokens):
   ```
   cd ~/librarian && CLAUDE_CODE_DISABLE_MCP=1 claude        # accept the workspace-trust dialog
   claude auth login                                         # if not already logged in (claude.ai)
   ```
   Then `exit`. (`~/.claude/.credentials.json` with `loggedIn:true, authMethod:claude.ai` = ready.)
7. **Install the keepalive** (create the log + socket dirs FIRST — launchd opens
   `StandardOutPath`/`StandardErrorPath` *before* the bootstrap runs, so a missing parent dir fails
   the job before any supervisor code executes):
   ```
   mkdir -p ~/.myndaix/orchestrator/librarian ~/.local/state
   cp orchestrator/librarian/keepalive/ai.myndaix.librarian-rc.plist.example \
      ~/Library/LaunchAgents/ai.myndaix.librarian-rc.plist
   launchctl load ~/Library/LaunchAgents/ai.myndaix.librarian-rc.plist
   ```
8. **Verify** the session came up:
   ```
   tmux -S ~/.local/state/librarian.tmux has-session -t librarian && echo alive
   tail ~/.myndaix/orchestrator/librarian/librarian.log
   ```
9. **HARD GATE — prove the fence live before trusting it 24/7** (do NOT skip; the entire confinement
   rests on `claude remote-control` loading the project `.claude/settings.json` + PreToolUse hook,
   which is confirmed from the Remote Control docs but must be verified on THIS box). Pair the phone
   (Claude app → the `librarian` session), then from the phone:
   - a research question → returns a **cited** answer (the loop works); and
   - `ls`, `cat ~/.myndaix/.secrets`, and any non-`mxr ask` request → **denied by the gate** (check
     `~/.myndaix/orchestrator/librarian/librarian.log` / the session shows the recall-gate fired).

   If a denied probe is NOT denied, `launchctl unload` immediately and re-check the deployed
   `~/librarian/.claude/settings.json` (matcher `"*"`, hook path executable) — do not leave it running.

## Rollback (instant)
```
launchctl unload ~/Library/LaunchAgents/ai.myndaix.librarian-rc.plist
tmux -S ~/.local/state/librarian.tmux kill-session -t librarian
```

## Accepted residuals (inherited from piece C; none phone-reachable)
- **Scope roots baked in the wrapper** (not `~/.zshrc`) — tighter than the piece-C prototype (the
  review flagged operator-env-defined scopes). A future sensitive scope must be added deliberately here.
- **Inherited `~/.claude` hooks** are all *restrictive* (branch-guard/destructive-blocker/linters);
  deny-first precedence means the recall-gate's denies always win. Full HOME isolation is a later
  hardening (seed claude.ai auth into a dedicated librarian HOME).
- **Slow-ask Bash timeout** — a slow `mxr ask` can exceed the session's default Bash timeout; asks
  are usually <30s. Follow-up if slow asks are observed lost.
- **MCP-off relies on the env var, not `--strict-mcp-config`** (review r2 MED). The piece-C interactive
  launch used `--strict-mcp-config` (the proven control); `remote-control` has no such flag, so the
  keepalive relies on `CLAUDE_CODE_DISABLE_MCP=1` + settings `disableClaudeAiConnectors:true`. That
  these fully suppress MCP under `remote-control` is unverified — part of the step-9 live gate: attempt
  an `mcp__*` tool from the phone and confirm the gate denies it (the allowlist denies `mcp__*` anyway,
  but confirm no MCP server even loads). The allowlist gate makes this belt, not load-bearing.
