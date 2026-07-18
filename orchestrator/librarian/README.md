# Recall librarian — phone-reachable front door (piece C)

The read-only, phone-reachable front door for `mxr ask` (second-brain rung-1, piece C). A confined
Claude session, reachable from your phone via **Remote Control**, that can do exactly ONE thing —
answer recall questions from your folders. Read-only, zero blast radius.

**v0 = MacBook, prove the loop** (per the north-star doc's "don't jump to the Mini yet"). Reachable
when the MacBook is awake; graduate to the always-on Mini (+ a launchd keepalive) once the loop works.

## What's here
| File | Role |
|---|---|
| `kit/settings.json` | fail-closed permission posture → `~/librarian/.claude/settings.json` (removes read/web/MCP/agent surfaces; the gate is the sole Bash allow-er) |
| `kit/CLAUDE.md` | the librarian identity → `~/librarian/CLAUDE.md` |
| `hooks/recall-gate.{sh,py}` | PreToolUse Bash gate — allows ONLY `mxr ask`/`mxr recall`, denies dispatch + everything else (stays in the repo; settings.json references it by absolute path) |
| `test.sh` | 15-check gate smoke test (allow safe recall; deny injection/dispatch/other programs) |

## The fence (why it's safe)
`defaultMode: dontAsk` + `allow: []` + a deny-list that REMOVES Read/Grep/Glob/Web*/MCP/Agent/Task/
Monitor/Cron*/RemoteTrigger/LSP by bare name. The only Bash the session can run is what the
recall-gate ALLOWS: an exact `mxr ask|recall --scope <scope> "<safe question>"`. No file reads, no web,
no dispatch to other agents, no standing loops. A poisoned corpus answer can't escalate — there's no
tool/dispatch/net channel. This is the fenced-reads-WITHOUT-dispatch half of the shelved Watch design.

## Deploy (MacBook)

**1. Stage the workspace** (non-interactive — Mack does this):
```
mkdir -p ~/librarian/.claude
cp orchestrator/librarian/kit/CLAUDE.md      ~/librarian/CLAUDE.md
cp orchestrator/librarian/kit/settings.json  ~/librarian/.claude/settings.json
```

**2. Interactive — Jefe's hands (RC rejects long-lived tokens):**
- Launch from a **normal terminal** (so the session inherits `MYNDAIX_KNOWLEDGE_SCOPES` from `~/.zshrc`,
  needed for the `fitness` scope; `research` works regardless):
  ```
  cd ~/librarian && claude
  ```
- If prompted, **`claude auth login`** (claude.ai OAuth — API keys / setup-tokens are rejected by RC).
- **Accept the workspace-trust dialog** for `~/librarian` (one-time; RC refuses to start in an untrusted folder).
- **Enable Remote Control** on this session and **pair your phone** (Claude app).

**3. Prove the loop** — from your phone, ask: *"what's my weekly training plan?"* → the session runs
`mxr ask --scope fitness "…"` → you get the cited answer. Try a research question too.

## Verify the fence locally (anytime)
```
bash orchestrator/librarian/test.sh     # 15/15
```
Or, in the live session, try `ls` or `cat ~/.myndaix/.secrets` — the gate denies it.

## Later (follow-ons, not v0)
- **Always-on:** graduate to the Mini + a launchd keepalive (adapt the Watch `rc-bootstrap`/`rc-wrapper`);
  needs the mx-ask code + corpus ingested + scopes registered on the Mini first.
- **More scopes:** register personal (sensitive tier) once its exclusion policy is set.
- **Telegram/iMessage** transport is the sanctioned *later* interface (RC now).
