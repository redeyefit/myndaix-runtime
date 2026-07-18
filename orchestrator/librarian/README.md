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

## The fence (why it's safe) — hardened per cross-family review r1
- `defaultMode: dontAsk` + `allow: []` + a deny-list of the **full current canonical non-Bash tool
  surface** by name (Read/Grep/Glob/Web*/MCP/Agent/Skill/Workflow/Task*/SendUserFile/… — everything
  except Bash). Bash is left un-denied ONLY because a settings `deny` overrides a hook `allow`, so
  `deny:["*"]` would kill Bash; the gate is Bash's sole allow-er.
- The recall-gate is **fail-closed**: every non-allow path (unparseable/malformed/non-string payload,
  wrong scope, dispatch, any other program) emits an explicit `deny` — never a bare return (which
  falls through to ALLOW under `dontAsk`). It allows ONLY `mxr ask --scope research|fitness "<safe q>"`.
- `mxr recall` is **not** allowed (raw snippets are unfenced); scope is allowlisted to research|fitness
  (a future sensitive scope can't auto-become phone-reachable); MCP is off (`CLAUDE_CODE_DISABLE_MCP=1`
  at launch + `disableClaudeAiConnectors`).
- Net: a poisoned corpus answer can't escalate — no file reads, no web, no dispatch, no other tool. This
  is the fenced-reads-WITHOUT-dispatch half of the shelved Watch design. 22/22 test.sh.

## Deploy (MacBook)

**1. Stage the workspace** (non-interactive — Mack does this):
```
mkdir -p ~/librarian/.claude
cp orchestrator/librarian/kit/CLAUDE.md      ~/librarian/CLAUDE.md
cp orchestrator/librarian/kit/settings.json  ~/librarian/.claude/settings.json
```

**2. Interactive — Jefe's hands (RC rejects long-lived tokens):**
- Launch from a **normal terminal** (so the session inherits `MYNDAIX_KNOWLEDGE_SCOPES` from `~/.zshrc`,
  needed for the `fitness` scope; `research` works regardless). **`CLAUDE_CODE_DISABLE_MCP=1` is
  load-bearing** — it loads ZERO MCP servers (settings' `disableClaudeAiConnectors` is belt):
  ```
  cd ~/librarian && CLAUDE_CODE_DISABLE_MCP=1 claude
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
