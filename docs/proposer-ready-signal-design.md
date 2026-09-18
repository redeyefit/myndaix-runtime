# proposer ready-signal — observe-only "a skill class is ready" email notifier

**v0.1 · build-ready.** A push-signal that tells a human when the auto-capture pipeline has a
skill class worth proposing — *without* turning on the proposer's PR-opening.

## Why

Capture (`CAPTURE_ENABLED`) records cross-family-agreed recurring findings; a class becomes
`state='ready'` once it clears the recurrence bar (`MIN_RECUR`/`MIN_EVENTS`). But with the S7
proposer OFF, a ready class **sits silently in the ledger** — nobody is signalled, because the
proposer *is* the signal (it opens a draft PR). Jefe should never have to watch a DB table.

This closes that gap at the lowest-risk rung: **observe only.** It reads the ready classes and
emails once per newly-ready class. The human then decides whether to arm the proposer / promote
a class to a real skill PR. No autonomy is widened — the proposer stays off.

## Data flow

```
launchd (daily) → proposer-signal.sh → runtime.proposersignal tick
   read state='ready' capture_candidate rows (SELECT, read-only)
   → dedup by fingerprint vs the seen-file
   → NEW fingerprints? compose + send one email
   → advance seen-file (atomic) ONLY for successfully-emailed classes
```

## Edge cases & failure modes

- **Repeat every tick** — dedup by `fingerprint` in an atomic-rewrite seen-file: one email per
  class until it leaves `ready`. A fingerprint that leaves then re-enters re-notifies (it
  recurred again — that is news).
- **Send fails** — the class is *not* written to the seen-file, so the next tick retries rather
  than silently dropping the signal. Read fail-OPEN (exit 0, never wedge launchd); deliver
  fail-CLOSED (no credential → no send).
- **Loosened credential** — the wrapper refuses a secret whose perms are not 600/400.
- **Concurrency** — daily single-shot, read-only; no lock needed (it never claims a candidate,
  so it can never race the proposer's CAS).

## Security surface

- **Untrusted:** nothing new — `rule_tag`/`repo_scope`/`path_glob` are already-sanitized ledger
  values, emitted into an email body (no shell, no argv-to-git). `repo_scope` is never passed to
  a git/gh command here (unlike the proposer — this path has no git side at all).
- **Stored/standing:** one send credential (Gmail app-password) in `~/.myndaix/.secrets/env/`,
  chmod 600, sourced only as that specific file. Its absence is the on/off gate.
- **Blast radius:** an email to yourself. Read-only on the ledger; opens nothing.

## What it deliberately does NOT build

- No PR, no proposer invocation, no ledger mutation (that is the proposer's job, still gated).
- No new notification service — reuses SMTP (the boring substrate); no Resend/API dependency.
- No dedup dead-letter — a lost signal simply re-fires next tick (the class is still `ready`).

## Fast loop

- `tests/test_proposer_signal.py` — module dedup contract (notify-once, only-new, retry-on-fail,
  prune-and-renotify), no DB/SMTP. `orchestrator/proposer-signal-test.sh` — wrapper gate branches.
