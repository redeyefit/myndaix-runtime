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
| `hooks/recall-gate.{sh,py}` | PreToolUse Bash gate — allows ONLY `mxr ask --scope research\|fitness\|company "…"`; denies recall, dispatch, other programs, malformed payloads (crash-proof fail-closed). Stays in the repo; settings.json references it by absolute path |
| `test.sh` | 35-check gate smoke test (allow safe ask; deny injection/dispatch/other programs/wrong-scope/malformed payloads) |

## The fence (why it's safe) — hardened per cross-family review r1
- `defaultMode: dontAsk` + `allow: []` + a deny-list of the **full current canonical non-Bash tool
  surface** by name (Read/Grep/Glob/Web*/MCP/Agent/Skill/Workflow/Task*/SendUserFile/… — everything
  except Bash). Bash is left un-denied ONLY because a settings `deny` overrides a hook `allow`, so
  `deny:["*"]` would kill Bash; the gate is Bash's sole allow-er.
- The recall-gate is **fail-closed**: every non-allow path (unparseable/malformed/non-string payload,
  wrong scope, dispatch, any other program) emits an explicit `deny` — never a bare return (which
  falls through to ALLOW under `dontAsk`). It allows ONLY `mxr ask --scope research|fitness|company "<safe q>"`.
- `mxr recall` is **not** allowed (raw snippets are unfenced); scope is allowlisted to research|fitness|company
  (`company` = ~/company plan notes, non-sensitive; a future SENSITIVE scope can't auto-become phone-reachable); MCP is off (`CLAUDE_CODE_DISABLE_MCP=1`
  at launch + `disableClaudeAiConnectors`).
- Net: a poisoned corpus answer can't escalate — no file reads, no web, no dispatch, no other tool. This
  is the fenced-reads-WITHOUT-dispatch half of the shelved Watch design. 35/35 test.sh.

## Deploy (MacBook)

**1. Stage the workspace** (non-interactive — Mack does this):
```
mkdir -p ~/librarian/.claude
cp orchestrator/librarian/kit/CLAUDE.md      ~/librarian/CLAUDE.md
cp orchestrator/librarian/kit/settings.json  ~/librarian/.claude/settings.json
```

**2. Interactive — Jefe's hands (RC rejects long-lived tokens):**
- Launch from a **normal terminal**, PINNING the scope roots inline — do NOT rely on `~/.zshrc`
  (kilabz PR#110 LOW: an inherited env missing a root lets `mxr ask --scope company` pass the gate but
  fail at runtime as an unknown scope; `research` is hardcoded in the runtime, `fitness` + `company`
  need roots — same pins as keepalive/rc-wrapper.sh). **The MCP-off flags are load-bearing** —
  `--strict-mcp-config` (the proven control, as the curator uses) makes it ignore ALL inherited MCP
  servers; `CLAUDE_CODE_DISABLE_MCP=1` + settings' `disableClaudeAiConnectors` are belts:
  ```
  cd ~/librarian && CLAUDE_CODE_DISABLE_MCP=1 \
    MYNDAIX_KNOWLEDGE_SCOPES="fitness=$HOME/fitness,company=$HOME/company" claude --strict-mcp-config
  ```
  Residual (review r2, LOW): the gate validates the literal text `mxr`, not the resolved executable. If
  YOUR `~/.zshrc` defines an `mxr` alias/function, `mxr ask …` runs that. Not attacker-reachable (the
  session can't write files/shell config; the alias is your own → still the mxr wrapper), but keep the
  wrapper on PATH as the canonical `mxr`.
- If prompted, **`claude auth login`** (claude.ai OAuth — API keys / setup-tokens are rejected by RC).
- **Accept the workspace-trust dialog** for `~/librarian` (one-time; RC refuses to start in an untrusted folder).
- **Enable Remote Control** on this session and **pair your phone** (Claude app).

**3. Prove the loop** — from your phone, ask: *"what's my weekly training plan?"* → the session runs
`mxr ask --scope fitness "…"` → you get the cited answer. Try a research question too.

## Verify the fence locally (anytime)
```
bash orchestrator/librarian/test.sh     # 35/35
```
Or, in the live session, try `ls` or `cat ~/.myndaix/.secrets` — the gate denies it.

## Accepted residuals (3 review rounds; fail-open class is SEALED)
The security-critical fail-open class is closed (crash-proof gate — any malformed/exception payload denies).
These remain, none phone-reachable:
- **Inherited `~/.claude` hooks (r3 HIGH → verified benign):** the session inherits the operator's
  PreToolUse hooks, but they are ALL *restrictive* (branch-guard, destructive-blocker, linters,
  redact-memory) — none grants a capability, and deny-first precedence means the recall-gate's denies
  always win. They only ADD protection. Full isolation via a dedicated librarian `HOME` is blocked by RC
  needing claude.ai auth in HOME — a Mini/production follow-up (seed the auth into a clean HOME).
- **Scope roots are env-defined (r3 MED):** the gate allowlists the scope *names* research|fitness|company, but
  their filesystem roots come from `MYNDAIX_KNOWLEDGE_SCOPES` (`~/.zshrc`). A compromised operator env
  could remap them — NOT phone-reachable (the session can't edit `~/.zshrc`). Follow-up: pin the roots in
  a dedicated `LIBRARIAN_SCOPES` allowlist.
- **Slow-ask timeout (r3 MED):** Claude's Bash tool default (~120s) can drop a slow `mxr ask` before it
  returns (Read/task-output are denied, so no recovery). Bounded — asks are usually <30s. Follow-up: raise
  the session's Bash timeout if slow asks are lost.
- **Global `~/.claude/settings.json` (r7 HIGH, partial):** the fence validator certifies the workspace
  `settings.json` and now refuses a sibling `settings.local.json`, but Claude still merges the operator's
  GLOBAL `~/.claude/settings.json`. A `disableAllHooks` or a capability-granting hook there would weaken
  the fence — but it is the operator's OWN file, unwritable by the confined session (same trust boundary
  as the accepted inherited-hooks residual), so NOT phone-reachable. `--settings` only ADDS sources, so
  full isolation needs a dedicated `HOME` (the same Mini/production follow-up as the inherited-hooks item).
- **Validate→launch TOCTOU (r7 MED):** `preflight` validates `settings.json` immediately before the
  `claude` exec, but they are separate steps; a writer that swaps the file in that window is not caught.
  The confined session cannot write the workspace, so the only writer is the operator/deploy — same trust
  boundary as above, and the live path is a manual single launch. Follow-up (keepalive/Mini): hold a
  `flock` across validate+launch.

## Later (follow-ons, not v0)
- **Always-on:** graduate to the Mini + a launchd keepalive (adapt the Watch `rc-bootstrap`/`rc-wrapper`);
  needs the mx-ask code + corpus ingested + scopes registered on the Mini first.
- **More scopes:** register personal (sensitive tier) once its exclusion policy is set.
- **Telegram/iMessage** transport is the sanctioned *later* interface (RC now).
