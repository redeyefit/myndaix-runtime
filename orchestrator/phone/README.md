# orchestrator/phone — the phone→tailnet surface

One tap or one dictation on the iPhone runs exactly one allowlisted factory action, from
anywhere, with **no AI vendor in the transport**: Shortcuts → tailnet SSH → the
`mxr-phone` forced-command wrapper on the Mini → `mxr`. Design (review-hardened v0.2):
`docs/phone-tailnet-surface-design.md`. Verbs:

| Verb | Does | Bound |
|---|---|---|
| `status` | one-screen factory health (canary tails + phone caps) | 60/hour |
| `ask <research\|fitness\|company> <question>` | cited answer from your corpus; >50s returns a job id | 50/day |
| `get <job-id>` | fetch the reply the iOS timeout abandoned (`mxr get --reply`) | 100/day |
| `reel <topic>` | STUB until mx-engine deploys to the factory (honest no-submit message) | 5/day |

Untrusted dictated text is grammar-checked, byte-capped (2000), control-REJECTED,
UTF-8-validated, and travels as ONE argv after `--` — never eval'd, never word-split.
Logs (`~/.myndaix/state/mxr-phone.log`, 0600, self-rotated) record sha256 prefixes,
never payload text.

## Install (Mini, as jefe — the wrapper joins the HAND-COPIED set; deploy-sync does not cover it)

```
cp orchestrator/phone/mxr-phone.sh ~/.myndaix/bin/mxr-phone && chmod 755 ~/.myndaix/bin/mxr-phone
bash orchestrator/phone/test.sh            # fixture leg: 48 checks
bash orchestrator/phone/test.sh --sshd     # REAL boundary leg (loopback sshd) — must pass before wiring the phone
```

Then wire the phone key: fill `authorized_keys.example` (phone tailnet IP + the pubkey
copied from the Shortcuts SSH action) and append that ONE line to
`~/.ssh/authorized_keys`. Tailscale admin: ACL the phone device → the Mini host, port
22 only (host, not `tag:factory` — the tag may widen).

## Shortcuts (iPhone — ~10 min, once)

Each is: **Shortcuts → + → Run Script Over SSH** — Host `100.67.43.104`, Port `22`,
User `jefe`, Authentication `SSH Key` (use the same key for all; its pubkey is the one
registered above). Shell input: none.

1. **Ask Research** — action 1: *Dictate Text*; SSH script: `ask research ` + Dictated Text
   (magic variable). Duplicate for **Ask Company** / **Ask Fitness** (scope hardcoded per
   Shortcut — the wrapper still requires it; design decision #2).
2. **Get Answer** — action 1: *Ask for Input* (text, "job id"); script: `get ` + input.
3. **Factory Status** — script: `status`. Add to a widget for one-tap health.

Show the SSH output with a *Show Result* / *Quick Look* action after the SSH step.

## Revocation drill (practice ONCE at install — design §7)

1. Delete the phone's line from `~/.ssh/authorized_keys` → the next Shortcut run must fail.
2. Tailscale admin → remove/disable the phone device (independent second lock).
3. Restore (re-add line / re-enable device); re-run **Factory Status**.
Rotation = new key in the Shortcuts action, swap the authorized_keys line, retire the old.

## Flip the reel stub (when the mx-engine lane deploys to the factory)

Replace the stub block in `mxr-phone.sh` (`case reel`) with a bounded submit-and-return:
`run_bounded 60 "$MXR_BIN" mx-engine -- "$payload"` and relay the JOB_ID line — the cap
(5/day) and payload validation already enforce the paid-verb policy.
