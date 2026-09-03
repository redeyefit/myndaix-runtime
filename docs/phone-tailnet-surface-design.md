# Phone → Tailnet Surface — design v0.1

**Status:** DESIGN — pre cross-family review. Build gated on review + activation P0/P1
(merged review stack deployed, Mini pool re-canaried).
**What:** the vendor-free phone grip on the runtime — iPhone Shortcuts → tailnet SSH →
a forced-command wrapper on the Mini → allowlisted `mxr` verbs. Chosen over Claude
Remote Control as the PRIMARY phone surface (Jefe 2026-09-03: less vendor tie; RC
demoted to optional conversational bonus).

## 1. What it does & why

One tap or one dictation on the iPhone runs exactly one of three allowlisted actions
on the factory, from anywhere, with no AI vendor in the transport:

- `ask <scope> <question>` → `mxr ask --scope <scope> "<question>"` → cited answer back
  to the Shortcut. Scopes: `research|fitness|company` (mirror of the librarian fence;
  `personal` stays off by standing decision).
- `reel <topic>` → `mxr mx-engine "<topic>"` → narrated reel renders on the Mini;
  the Shortcut gets the submit/job line back (result lands in the jefe drop as usual).
- `status` → a bounded one-screen health summary (pool canary + liveness one-liners).

Transport: iPhone Tailscale app (WireGuard node on the existing tailnet) → SSH to the
Mini. Nothing public, no new daemon — sshd and the tailnet already exist.

## 2. Data flow

```
iPhone Shortcut (dictation/tap)
  └─ Run Script Over SSH  (Shortcuts' own ed25519 key)
       └─ tailnet (WireGuard, ACL: phone → mini:22 ONLY)
            └─ sshd on Mini → authorized_keys forced command
                 └─ ~/.myndaix/bin/mxr-phone   (THE trust boundary)
                      ├─ parse SSH_ORIGINAL_COMMAND strictly (verb + payload)
                      ├─ enforce caps + allowlist, log the call
                      └─ exec mxr <verb-mapped argv>   (payload as ONE argv, never shell)
                           └─ runtime pool → librarian / mx-engine / canary
```

## 3. Security surface (what's untrusted, what contains it)

| Threat | Containment |
|---|---|
| Lost/stolen phone | Key is `restrict,command=` — no shell, no pty, no forwarding; verbs are ask/reel/status with caps. Revoke = delete one authorized_keys line + remove the device in the Tailscale admin. |
| Dictated text = untrusted input | Payload is parsed, control-stripped, length-capped (≤2000 chars), then passed as a SINGLE argv to `mxr` — never interpolated, never eval'd, no shell metacharacter ever interpreted (same argv discipline as the librarian fence + mx-engine's topic-as-COPY precedent). Downstream, `ask` hits the tool-less librarian (worst case = wrong answer) and `reel` reaches an LLM as copy text, never shell. |
| Flooding / cost abuse | Per-verb daily caps in the wrapper (`reel` is PAID: default 5/day; `ask` 50/day; `status` uncapped), atomic count files under the wrapper's own state dir. Tailnet ACL bounds reach to one port on one host. |
| Wrapper bug | Fail-closed: any parse anomaly, unknown verb, bad scope, over-cap, or missing dependency → exit 2 with a one-line reason; nothing dispatched. Every call logged (ts, verb, payload first-80, rc) before AND after dispatch. |
| Mini compromise via this path | The forced command IS the boundary; sshd's `restrict` option set denies everything else regardless of what the client requests. |

**Account decision:** runs as `jefe` (mxr needs the ledger + pool socket). A dedicated
low-priv user was considered and rejected: it would need DB grants + secret access
anyway, buying nothing — the forced command is the privilege boundary, one layer
down from the librarian's hook fence and the same philosophy. RESIDUAL (accepted):
a bug in the ~100-line wrapper is a bug in a jefe-owned process; mitigated by the
hostile-input test suite and the verb allowlist's small surface.

## 4. Components

1. `orchestrator/phone/mxr-phone` — the forced-command wrapper (bash, ≤~120 lines,
   house rules: set -euo pipefail, argv-only, base-10 guards, atomic count writes,
   bounded `MXR_TIMEOUT_S` per verb — ask 240s, reel returns after submit, status 60s;
   output truncated to 4KB for the Shortcut display; over-truncation marked loudly).
2. `orchestrator/phone/authorized_keys.example` — one line:
   `restrict,command="/Users/jefe/.myndaix/bin/mxr-phone" ssh-ed25519 <SHORTCUTS-PUBKEY> iphone`
   (Shortcuts generates and holds its own key; only the pubkey leaves the phone.)
3. Tailnet ACL snippet (Tailscale admin, Jefe click): phone device → `tag:factory:22` only.
4. `orchestrator/phone/test.sh` — fixture suite driving the wrapper via
   `SSH_ORIGINAL_COMMAND` env: happy paths; injection attempts (`;`, `$()`,
   backticks, newlines, NULs, unicode confusables, 100KB payload); bad scope
   (`personal` MUST deny); unknown verb; over-cap; missing mxr (fail-closed);
   count-file corruption (octal trap).
5. Shortcuts (phone-side, no repo code): "Ask my brain" (dictate → `ask research …`),
   "Make a reel" (dictate → `reel …`), "Factory status" (tap → `status`).

## 5. Failure modes

- Pool down → wrapper's mxr call fails → clear one-liner back to the Shortcut
  ("factory unreachable — check liveness"), exit nonzero, nothing charged.
- Slow ask (> wait) → mxr's own timeout text incl. job id returns; answer stays
  recoverable in the ledger (`mxr get <jid>`).
- Tailnet down on either end → SSH fails at connect; Shortcut shows the error; no
  partial state anywhere.
- Wrapper state dir unwritable → caps unenforceable → FAIL CLOSED (deny, log to stderr).

## 6. What this deliberately does NOT build

- No new daemon, queue, or transport (sshd + tailnet already run; Telegram transport
  stays a separate deferred design; RC untouched as optional bonus).
- No team→phone direction (standing rule: supervised mirror sessions only).
- No push notifications; replies are synchronous to the Shortcut, artifacts land in
  the existing jefe drop.
- No new secrets: the phone key is an authorized_keys line, not a bearer token in
  `.secrets`; nothing added to the env allowlists.

## 7. Borrowed patterns / prior art

SSH forced-command + `restrict` = the standard scoped-automation substrate (passes
both substrate-check questions: official OpenSSH mechanism, stable for decades,
scoping is exactly its job). Librarian recall-gate = the allowlist philosophy and the
`personal`-deny precedent. mx-engine dispatch = paid-verb caps + topic-as-argv.
Rejected: HTTP endpoint on the Mini (new auth surface + daemon for no gain),
Tailscale Funnel (public exposure), dedicated Unix user (see §3).

## 8. Test & deploy plan

- `orchestrator/phone/test.sh` green locally (hostile suite above) + shellcheck/
  bash-check + review rounds before any install.
- Deploy (Mini, after activation P0/P1): install wrapper to `~/.myndaix/bin` via the
  reconcile-synced checkout + hand-add the authorized_keys line (secrets-adjacent =
  hand step, like all key material); ACL edit in the Tailscale admin (Jefe);
  Shortcuts built on the phone against the live wrapper.
- Live smoke: `status` from cellular (not wifi), one `ask`, one capped `reel`;
  then attempt an injection from a scratch Shortcut and watch it deny.

## 9. Open questions for review

1. Reel daily-cap default (5?) and whether `status` should require a cap too.
2. Should `ask` scope default to `research` when omitted, or hard-require the scope?
3. Wrapper log location: own file under `~/.myndaix/state/` vs syslog.
