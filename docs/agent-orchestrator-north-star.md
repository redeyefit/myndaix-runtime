# Agent Orchestrator — North Star (personal + operational)

_Living sketch · v0.1 · 2026-07-13 (Mack + Jefe). One page on purpose — a compass, not a spec.
Extends [[north-star-autonomous-brain]] to the personal domain and a multi-interface front.
Nothing here is built; it anchors the build decisions that follow._

## The destination
**One always-on agent orchestrator**, reachable from wherever Jefe is, that spans every domain
and earns every capability **one trusted rung at a time** — on a self-learning spine, with
harnesses, evals, and trust gating each step. The controlled version of the "clawedbot" dream:
the machinery is identical to the sprawling insecure version — **the restraint is the product.**

## One thing, two ends
The **personal second-brain** and the **runtime orchestrator** are the same system approached
from two sides. They converge into one orchestrator over all of Jefe's domains. We're not
building two things.

## Domains (its reach)
`personal` · `fitness` (RedEyeFit) · `research` · `ask` · `runtime` · `FieldVision` · (…)
Each accessed **live — search AND control, not just view.**

## Roles (the physical shape)
- **MacBook** — where Jefe authors and works (primary surface).
- **Mini** — the always-awake **home** of the brain (reachable when the MacBook is shut).
- **Phone** — the away window.

Folders live on the MacBook today; the always-on version mirrors them to the Mini (Syncthing,
already running there). **Not jumping to the Mini yet.**

## Interface path (front doors, earned in order)
**Remote Control now** (Claude-native, zero-build, zero idle burn) → Telegram / iMessage /
Discord / other later (reviewed shelf designs already exist). RC is the seed; interfaces widen on
**need**, not speculation.

## Capability ladder (each rung earned by evals + trust)
1. **Recall** — search/answer over the folders. Read-only, zero blast radius. ← domain-1, rung-1.
2. **Capture** — jot / file a thought from the phone.
3. **Organize** — retag, link, reshape structure.
4. **Edit / act** — change docs, draft, produce. **Tap-approve each action + git-reversible folders.**
5. **Operate** — headless runs, web + scrape, cron jobs, test the FieldVision app/site, …

Read-only rungs ship free. **Every acting rung is gated:** explicit approval per action +
reversible + *measured* before it is ever trusted to run unattended.

## The method (already under us — not aspirational)
- **Harnesses** — xreview / the review gauntlets (proven: caught 4 rounds of real bugs this session).
- **Evals** — the outcomes ledger, per-family precision, the shadow precision dials.
- **Trust ladder** — shadow → armed; autonomy widened only on accrued labeled evidence.
- **Self-learning** — labeled ground truth → memory + dials + skills. **Jefe = ground truth.**

This vision names the summit those were already climbing.

## Ownership (own the data, rent the brain — for now)
Jefe owns the durable pieces from day one: the folders, their git history, the local recall
index. Claude is a **swappable** reasoning front-end bolted on top — keep that seam clean so
migration is cheap. **Flip the brain to local (Ollama) only when BOTH are true:** a local model
good enough at agentic file edit/act, AND a phone path that isn't a build-sprawl. Until then,
Claude+RC. (DeepSeek / Kimi are hosted clouds, not local — they don't serve the ownership motive.)

## The discipline (the whole game)
Every new domain and every new capability is a rung **earned by evals + trust** — never switched
on because it's possible. That restraint is the line between this and clawedbot.

## First concrete step — when we build (not yet)
**Recall librarian:** always-on, phone-reachable, **read-only** Claude over the folders, reusing
the curator's existing index. Zero blast radius. Proves the loop. Everything else is a later rung.

## Deliberately NOT yet
Jumping to the Mini · any non-RC interface · any acting/autonomy rung · a local-model migration ·
imposing a Notion-like schema (recall *without* the filing tax).
