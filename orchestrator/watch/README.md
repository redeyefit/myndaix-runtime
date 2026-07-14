# Watch — the Mini's phone-reachable remote-control agent

Design: `docs/always-on-agent-research.md` (v0.3-r2). This dir is the whole build: a keepalive
service + two typed read wrappers + a dispatch-gate hook + a lean identity kit. Watch is a
`claude remote-control` **server-mode** session on the Mini, supervised by launchd + tmux,
observing the runtime only through fenced read wrappers, dispatching only on a phone tap.

## Files
| File | Role |
|---|---|
| `ai.myndaix.rc-keepalive.plist.example` | LaunchAgent (RunAtLoad + 120s recheck; AbandonProcessGroup; WorkingDirectory=/Users/jefe/watch) |
| `rc-bootstrap.sh` | launchd entrypoint — idempotent tmux session on a pinned socket; park-aware; disk/protocol guards |
| `rc-wrapper.sh` | in-tmux supervisor loop — reachability gate, clean-env launch (HIGH-5), park-and-sleep (HIGH-1) |
| `watch-lib.sh` | shared: `sanitize_untrusted` fence (HIGH-2), narrow `watch_alert` iMessage ping, logging |
| `read-inbox.sh` | path-locked inbox/state reader → fenced output |
| `mxr-read.sh` | job-id-validated ledger reader → fenced output |
| `hooks/dispatch-gate.sh` + `.py` | PreToolUse Bash hook — positive-grammar dispatch gate (HIGH-3) |
| `kit/CLAUDE.md`, `kit/session_state.md` | identity kit → `/Users/jefe/watch/` |
| `kit/settings.json` | fail-closed permission posture → `/Users/jefe/watch/.claude/settings.json` |
| `test.sh` | local smoke test (17 checks) + the Mini-only LIVE checklist |

## Deploy runbook (Mini, as jefe — needs Jefe's hands)

**Prereqs / machine-prep (audit-first; most already true per the 2026-07-13 audit).**
Already good on the Mini: `sleep 0`, `displaysleep 0`, `disksleep 0`, `womp 1`, `tcpkeepalive 1`,
tmux 3.6b, claude 2.1.207, Ethernet. Set / decide:
- `sudo pmset -a powernap 0` (audit showed `powernap 1`).
- **FileVault + auto-login (Jefe's §2.6 call).** Audit: FileVault ON, no auto-login, 57d uptime.
  Branch A (unattended recovery): `sudo fdesetup disable` + set an auto-login user + `sudo pmset
  -a autorestart 1`. Branch B (status quo): leave ON, accept manual-unlock reboots. Either is
  coherent; Watch runs today under B.
- Verify login-keychain password == account password (else the keychain stays locked after
  auto-login and claude can't read its credential).
- `brew pin tmux`; set `DISABLE_AUTOUPDATER=1` for claude (pin 2.1.207 — RC flags churn).
- Messages.app signed in + Automation (TCC) granted to the launchd/tmux context — REQUIRED for
  `watch_alert` on macOS 26.2. Test a real send during prep before relying on it.

**Auth (Jefe, interactive — RC rejects long-lived tokens).**
`claude auth login` in a shell on the Mini (full-scope claude.ai OAuth; API keys / setup-token are
rejected by Remote Control). Shared Max quota for now; flip to a cheap Pro seat on the first
observed quota collision.

**Install (from the repo checkout on the Mini).**
```
cd ~/code/active/myndaix-runtime && git pull --ff-only          # branch/main carrying this dir
mkdir -p ~/watch/.claude ~/watch/bin ~/.local/state ~/.myndaix/orchestrator
cp orchestrator/watch/kit/CLAUDE.md ~/watch/CLAUDE.md
cp orchestrator/watch/kit/session_state.md ~/watch/session_state.md    # only if absent
cp orchestrator/watch/kit/settings.json ~/watch/.claude/settings.json
ln -sf "$PWD/orchestrator/watch/mxr-read.sh"   ~/watch/bin/mxr-read     # bare names on PATH
ln -sf "$PWD/orchestrator/watch/read-inbox.sh" ~/watch/bin/read-inbox
printf '%s' "<jefe's imessage handle>" > ~/watch/.alert-to      # narrow park-alert recipient
chmod 600 ~/watch/.alert-to                                     # (the wrapper reads THIS, not secrets/)
bash orchestrator/watch/test.sh                                 # 17 local checks must pass
```
Watch's PATH must include `~/watch/bin` (add it in `kit/settings.json` env or the shell that
launchd inherits) so the wrappers resolve as bare `mxr-read` / `read-inbox`.

**Accept the workspace-trust dialog for `/Users/jefe/watch`** (one-time; RC refuses to start in
an untrusted folder — "Workspace not trusted"): `cd ~/watch && claude` → accept "trust the files
in this folder" → `Ctrl-C`. Without this the wrapper's `claude` launches fast-fail and Watch parks.

**Load the service.**
```
cp orchestrator/watch/ai.myndaix.rc-keepalive.plist.example \
   ~/Library/LaunchAgents/ai.myndaix.rc-keepalive.plist
# edit the wrapper path in the plist if the checkout is not /Users/jefe/code/active/myndaix-runtime
launchctl load ~/Library/LaunchAgents/ai.myndaix.rc-keepalive.plist
```
Then work the LIVE checklist printed by `test.sh` (phone reconnect, H5 approval completeness,
iMessage round-trip, `/status`, idle-night usage). **H5 gate:** if the phone approval does NOT
show the full `mxr` command, flip to observe-only (remove the `Bash(mxr-read…)`→dispatch is
already hook-gated; drop nothing on reads, just don't rely on dispatch) until `mxr-safe` exists.

**Rollback (instant):** `launchctl unload ~/Library/LaunchAgents/ai.myndaix.rc-keepalive.plist`.

## Recovery from a park
Watch writes `~/watch/.parked` and sends one alert, then sleeps (keeps the pane alive). To
recover: `ssh mini` → `claude auth login` (refresh OAuth) → `rm ~/watch/.parked` →
`launchctl kickstart -k gui/$(id -u)/ai.myndaix.rc-keepalive`.
