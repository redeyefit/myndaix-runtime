# Telegram Transport — DESIGN (v0.1)

_A new **transport adapter** on the existing ledger spine — NOT a new system. The runtime already
defines the transport contract (`transport/terminal.py`, the "dumb pipe over the ledger");
Telegram is a second implementation of exactly that interface, so the spine, admission checks,
outbox, and agent pool are untouched by construction. This is the biggest remaining Factory-OS
gap: phone access to the autonomous brain. Status: pre cross-family review. Follows the
new-systems rule (research → design → BOTH kilabz+oracle before code)._

## 1. What & why

Today Jefe reads verdicts only when Mack relays them from `~/.myndaix/bridge/inbox/jefe/`, and can
only reach the runtime from a terminal. The phone is the missing surface. A Telegram bot gives a
two-way channel over the tailnet: the brain pushes verdicts/alerts to Jefe's phone, and Jefe can
query status and dispatch a **safe, allowlisted** set of read-only work from anywhere.

**The load-bearing scope decision (v1): NOTIFY + READ-ONLY QUERY, not full command-and-control.**
An inbound Telegram message is untrusted text entering a system that can spend money, write code,
and merge PRs. So v1 deliberately mirrors how every other rung here was built — observe/notify
first (safe), act later (gated): v1 lets the phone (a) receive verdicts + alerts, and (b) dispatch
only a small allowlist of **read-only, non-spending** agents (status queries, `recon` research,
maybe a read-only review). Anything consequential — a paid media agent (higgsfield/stitcher), a
workspace-actor fix (codex), an auto-merge — is **NOT reachable from the phone in v1**; it waits
for the **approvals primitive** (§8, v2). "The human flips the switch" stays intact: the phone can
ask the brain to *look*, never (yet) to *act*.

Non-goals (v1): webhooks; inline keyboards/buttons; multi-user/group chats; media upload; any
agent with `Authority ∈ {WORKSPACE_ACTOR, CONTROLLER}` or any `non_idempotent`/paid agent;
arbitrary `to_agent` from the message; editing/deleting sent messages; a second autonomous git
writer.

## 2. Transport = polling, never a webhook (the NAT/Tailscale-forced, and safer, choice)

The Mini is behind NAT on the tailnet — it has **no public HTTPS endpoint**, which a Telegram
webhook requires. Long-polling (`getUpdates` with a timeout) is the correct and only fit: the bot
makes an **outbound** HTTPS call to `api.telegram.org` and Telegram holds it open until an update
arrives or the timeout elapses. No inbound port, no public surface, no reverse proxy — the attack
surface is strictly "what Telegram hands back to a poll we initiated." (Confirmed against current
Bot-API guidance: long-poll is the standard behind-NAT pattern; webhooks are for public hosts.)

Exactly-once ingest rides Telegram's own `update_id`: `getUpdates(offset=last+1)` **acknowledges**
everything below `offset`, and each update's `update_id` is monotonic per bot. So `update_id`
**is** the `dedupe_key` — the same restart-stable, never-colliding property the terminal transport
gets from `uuid4()`, but here it also gives crash-safe at-least-once delivery from Telegram +
exactly-once *processing* via the ledger's `inbound_event.dedupe_key UNIQUE`. Offset is advanced
only **after** `ingest_inbound` durably commits, so a crash between poll and commit re-delivers
(safe: the UNIQUE dedupe drops the duplicate).

## 3. Architecture (mirrors `TerminalTransport` exactly)

```
INBOUND (a poller task, one per bot account):
  loop:
    updates = GET api.telegram.org/bot<token>/getUpdates?offset=<last+1>&timeout=50
              &allowed_updates=["message"]           # ONLY message updates; ignore edited/callback/etc
    for u in updates (ascending update_id):
      m = u.message
      if m.chat.id NOT in CHAT_ALLOWLIST:  log+drop (fail-closed authN — §5)   ; continue
      if m.text is empty / not a text message:       drop (v1 = text only)      ; continue
      cmd, arg = parse_command(m.text)               # /ask, /status, /research, /help — §4
      if cmd not allowlisted:                         send usage hint; continue
      env = TransportEnvelope(transport="telegram", account=<bot>, sender_id=str(chat.id),
                              reply_target=f"telegram:{chat.id}", dedupe_key=str(update_id))
      event_id = ingest_inbound(env, arg)            # exactly-once
      submit_job(to_agent=AGENT_FOR[cmd], prompt=arg, inbound_event_id=event_id,
                 created_by=f"telegram:{chat.id}")    # NON-BLOCKING — returns immediately
      persist offset = update_id                      # ack ONLY after the durable commit above

OUTBOUND (a delivery task, mirrors TerminalTransport.run_delivery):
  loop (until stop, then drain):
    msg = claim_outbound("telegram")                  # transactional outbox — never lost
    POST api.telegram.org/bot<token>/sendMessage {chat_id: reply_target.split(":")[1], text: chunk}
    mark_outbound_sent(msg.id, provider_msg_id=<telegram message_id>)
```

Plus a **verdict bridge** (the highest-value half): the jefe-inbox verdict drop and controller
`_alert_jefe` also enqueue an `outbound` row with `transport="telegram"`, `reply_target=
"telegram:<jefe_chat>"` — so alerts/verdicts reach the phone through the SAME outbox path, no
special case. (Mack still relays substance in-terminal; this is additive.)

Runs as its own launchd job `ai.myndaix.telegram` (a poller + delivery loop), sourcing the bot
token from `~/.myndaix/.secrets` — **separate** from serve/controller/automerge so a poller crash
never touches the brain, and it can be disarmed independently (`rm $ORCH/TELEGRAM_ENABLED`).

## 4. Command grammar (v1 — closed allowlist, read-only)

| Command | Agent | Authority | Notes |
|---|---|---|---|
| `/status` | (none — direct ledger read) | — | queued/running counts, last review cursor, last N verdicts. No agent, no spend. |
| `/ask <q>` | `lobster` (sonnet) | CONTROLLER→**read-only prompt** | a general question; the prompt is fenced as untrusted; no tools that write. |
| `/research <q>` | `recon` | COMPOSITE (API, read-only) | perplexity research; `cost_budget` already caps it (registry). |
| `/help` | (none) | — | lists these commands. |

- The message text maps to a **fixed `to_agent`** per command — the phone NEVER supplies
  `to_agent`, so it can't address a workspace-actor or paid agent. Unknown/malformed command →
  a usage hint, never a dispatch.
- `/ask` and `/research` bodies are fenced as UNTRUSTED when they reach the agent (the review
  rung's nonce-fence discipline), and the agents chosen are read-only (`recon` is API-read-only;
  `lobster` here runs the plain `claude -p` reviewer/judge role, no file writes on this path).
- A per-chat **rate limit** (token-bucket, e.g. 20 msgs / 5 min, reuses the automerge/play-review
  cap pattern) bounds spend + abuse even from the allowlisted chat.

## 5. Security surface & failure modes (the core of this design)

- **AuthN = chat_id allowlist, fail-closed.** A public bot receives messages from anyone who finds
  its handle. The ONLY trust boundary is `m.chat.id ∈ CHAT_ALLOWLIST` (Jefe's chat id(s), from a
  `chmod 600` config, not the code). Everything else is logged-and-dropped BEFORE any ingest,
  submit, or reply — an unknown sender gets **no response at all** (no oracle for the bot's
  existence, no error that confirms the id). Empty allowlist → the poller refuses to start
  (fail-closed, like the API's empty-keys rule).
- **AuthZ = fixed command→agent map, read-only agents only.** Even the allowlisted chat cannot
  reach a spending or writing agent in v1 (§4). This is the property that makes an owned phone /
  a shoulder-surfed unlock non-catastrophic: worst case is a research query and a status read.
- **Injection.** Message text → job prompt → agent. Same defense as reviews: the body is wrapped
  in a nonce fence as UNTRUSTED DATA; the objective sits above the fence; the chosen agents have
  no write/merge/dispatch tools. A phone message cannot smuggle a `to_agent` or a shell command
  because it never parameterizes either — it only fills the `arg` slot of a fixed command.
- **Bot token = a bearer secret.** In `~/.myndaix/.secrets` (600), sourced into the launchd job's
  env, never in the plist, never logged. Token compromise lets an attacker *impersonate the bot*
  (send as it, read its updates) but the chat allowlist still gates who the runtime obeys, and the
  read-only agent map still bounds blast radius. Rotate via BotFather on suspicion.
- **Telegram is a third party in the path.** Inbound text and the bot token transit Telegram's
  cloud (unavoidable for any Telegram bot). Implication: **never send secrets or sensitive verdict
  bodies that shouldn't touch a third party** over this channel — the verdict bridge sends a
  SUMMARY + "details in the inbox", not raw diffs/keys. Documented limit, not a bug.
- **Availability / isolation.** The poller is its own launchd job; a hung `getUpdates`, a Telegram
  outage, or a poll crash cannot delay a review or wedge the brain (hard isolation, like the
  capture rung's fail-open). Bounded HTTP timeouts on every Bot-API call; the delivery loop's
  outbox means a reply enqueued during an outage is delivered on recovery, never lost.
- **Dedup / replay.** `update_id` UNIQUE dedupe_key makes a re-delivered update a no-op; offset
  advanced only post-commit means a crash re-processes at-least-once → the UNIQUE collapses it to
  exactly-once. A replayed old `update_id` (< offset) is never returned by `getUpdates` anyway.
- **Reply mis-routing.** `reply_target = "telegram:<chat_id>"` is derived from the INBOUND chat,
  and `claim_outbound("telegram")` only ever sends to a `telegram:` target — a reply can't leak to
  another transport or another chat. (Note: the audit's InboundIn NUL-guard gap does not apply —
  this transport builds the envelope in-process, not via the HTTP `/inbound` body.)

## 6. Edge cases

- **Long message / long reply.** Telegram caps `sendMessage` text at 4096 chars → the delivery
  loop chunks a long verdict into multiple sends (ordered), or truncates with "…(details in
  inbox)". Inbound over the agent body cap → reject with a hint.
- **`getUpdates` 409 (conflict).** Two pollers or a stale webhook → Telegram returns 409. Fail
  closed: log, back off, ensure only one poller + `deleteWebhook` on startup.
- **Bot added to a group.** `chat.id` won't be in the allowlist → dropped. (allowed_updates also
  excludes `my_chat_member` churn.)
- **Clock/token expiry.** Bot tokens don't expire; a revoked token → 401 on every call → the
  poller alerts (via the still-working inbox path) and exits non-zero for launchd to surface.

## 7. Borrow / build / reject

| Piece | Verdict |
|---|---|
| the transport interface (ingest/deliver over the ledger) | **REUSE** — `TerminalTransport` is the template, unchanged spine |
| long-poll `getUpdates` + `update_id` dedupe | **BORROW pattern** (Bot API idiom; ~a thin httpx loop) |
| chat_id allowlist authN + fixed command→agent authZ | **BUILD** (tiny, the whole security model) |
| a Telegram bot framework (python-telegram-bot, aiogram) | **REJECT for v1** — a raw `getUpdates`/`sendMessage` httpx loop is ~80 lines and adds no dependency/attack surface; frameworks bundle webhook servers, handlers, and update types we deliberately don't want. Revisit only if inline-keyboard approvals (v2) justify it. |
| webhook / public endpoint | **REJECT** — NAT + larger surface |
| the approvals primitive | **DEFER to v2** (§8) — do not build consequential-action dispatch until it exists |

## 8. The approvals primitive (v2 sketch — do NOT build yet)

When the phone should be able to trigger a consequential action (approve an auto-fix, greenlight a
paid render, merge a flagged PR), it goes through an **approvals table** — the missing Factory-OS
primitive:

```
approval { id, requested_by (agent/controller), action (enum), payload jsonb, state
           (pending|approved|denied|expired), decided_by, decided_at, ttl }
```

Flow: the brain (or a phone command) STAGES an `approval` in `pending`; the phone gets a message
with a short token; Jefe replies `/approve <token>` or `/deny <token>`; a deterministic executor
(not an LLM) performs the pre-registered action ONLY on `approved`, once, then marks it terminal.
Same asymmetry as the autonomy ladder: staging is cheap, execution is human-gated, TTL auto-denies.
This is where inline keyboards (approve/deny buttons) may earn the framework dependency. Designed
separately, cross-family reviewed, after v1 proves the transport is safe and useful.

## 9. Build plan (feature-flagged, each PR reviewed)

- **PR-A** `transport/telegram.py` (poller + delivery, raw httpx `getUpdates`/`sendMessage`),
  offset persistence (a tiny `telegram_offset` row or a state file), chat allowlist + command
  parser (pure, unit-tested: allowlist fail-closed, unknown-command, injection-in-arg, oversize).
- **PR-B** the launchd job `ai.myndaix.telegram.plist.example` + secret wiring + `TELEGRAM_ENABLED`
  arm flag + the verdict/alert bridge (enqueue a `telegram` outbound alongside the inbox drop).
- **PR-C** (v2, separate) the approvals table + `/approve`,`/deny` + the deterministic executor.

Deploy: install token → arm `touch $ORCH/TELEGRAM_ENABLED` → the poller starts; disarm = rm + the
launchd job exits no-op. v1 ships the notify + read-only-query value with zero write/spend surface.
