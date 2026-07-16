# Runbook — Deploy the Inbox Assistant to the Mini

**Status:** NOT EXECUTED. This is the deploy contract — Jefe executes it top to bottom on the
Mini. Design: `docs/inbox-assistant-design.md`. One-time cloud setup (steps 1–6) happens on the
MacBook in a browser; the arm (step 7) is a normal merge — the substrate reconcile does the rest.

**Goal:** 3 Gmail inboxes → one 06:30 brief in the jefe drop, labels applied, reply drafts
created. **SEND IS NEVER CALLED** — v1 is drafts-only by code contract (`gmail.compose` permits
send; no send method is invoked anywhere). Everything reversible; per-account fail-closed.

## 1. GCP project + OAuth client (browser, MacBook)

1. **Log in as a redeyefit.com account FIRST, then create the project** (e.g. `inbox-assistant`).
   A project owned by the Workspace account keeps the "Internal" audience option open if we ever
   want it; harmless otherwise. Creating it under the consumer account forecloses that forever.
2. **Enable APIs:** APIs & Services → Library → enable **Gmail API** and **Google Drive API**.
3. **OAuth consent — Google Auth Platform → Audience page.** The console was reorganized in 2025;
   old screenshots/tutorials showing "OAuth consent screen" under APIs & Services lie
   (verified 2026-07, Google docs). Configure:
   - User type: **External**.
   - Scopes (Data Access page) — add exactly these four:
     ```
     https://www.googleapis.com/auth/gmail.readonly
     https://www.googleapis.com/auth/gmail.labels
     https://www.googleapis.com/auth/gmail.compose
     https://www.googleapis.com/auth/drive.file
     ```
   - **'Publish app'** → confirm past the "will require verification" warning. Status must read
     **'In production'**.
   - **NEVER leave it in Testing** — Testing-status refresh tokens expire after 7 days, which
     would silently kill all three accounts weekly (verified 2026-07, Google OAuth docs).
   - **NEVER submit for verification.** An unverified in-production app used only by its owner
     rides the personal-use exemption — that is exactly what keeps the restricted Gmail scopes
     free of the security assessment (verified 2026-07, Google OAuth verification FAQ). Submitting
     starts a process we do not want and cannot cheaply exit.
4. **Create the OAuth client:** Clients page → Create client → type **Desktop app**. The client
   secret is shown **ONCE** — post-June-2025 the console never redisplays it (verified 2026-07,
   Google Cloud release notes). Put `client_id` + `client_secret` straight into 1Password
   (step 3 below); do not park them in a file.

## 2. Workspace trust (optional but recommended)

Workspace Admin console (admin.google.com) → Security → API Controls → **App Access Control** →
add our client ID and mark it **Trusted**. Removes the unverified-app interstitial AND the
unverified-app user cap for the two redeyefit accounts (verified 2026-07, Google Workspace Admin
docs). The consumer account can't be trusted this way — it clicks through the interstitial once
at mint (step 4).

## 3. 1Password — ORDER OF OPERATIONS (grants are immutable; vault FIRST)

A service account's vault grants are fixed at creation — create the vault and items BEFORE the
service account, or you re-create the SA (verified 2026-07, 1Password docs).

1. **Create custom vault `Automation`.** Service accounts cannot read built-in Personal vaults
   (verified 2026-07, 1Password docs) — a custom vault is mandatory, not hygiene.
2. **Create the items** (the vault contract the code reads via `op read`):
   - `gmail-oauth-client` — fields `client_id`, `client_secret` (from step 1.4).
   - `gmail-rt-<email>` — ONE item per account, item name is literally `gmail-rt-` + the full
     email address; field `refresh_token` (filled in step 4).
   - `notion-inbox-assistant` — field `token` (filled in step 6; only if the Notion mirror is on).
3. **THEN create the service account**, read-only on that one vault:
   ```
   op service-account create inbox-assistant --vault Automation:read_items
   ```
4. **The SA token is shown once.** Store a copy in 1Password AND install it on the Mini's login
   Keychain (the tick script exports it as `OP_SERVICE_ACCOUNT_TOKEN` from there):
   ```
   security add-generic-password -a "$USER" -s 'op.inbox-assistant.token' -w '<token>'
   ```
   **Caveat:** after an unattended reboot without auto-login the login keychain is locked and the
   tick fails (`op` errors) until someone logs in. Known trade-off; the brief just skips days.

## 4. Mint the refresh tokens (MacBook — a browser is required)

Run `scripts/mint_gmail_refresh_token.py` once per account (3x), logged into that account in the
browser. The consumer account hits "Google hasn't verified this app" → **Advanced → Go to app
(unsafe)** — expected, click through. The redeyefit accounts skip the interstitial if step 2 was
done. Paste each refresh token into its `gmail-rt-<email>` vault item.

**Scopes are frozen at mint** — the token carries exactly the scopes consented at mint time.
`drive.file` is in the list NOW deliberately, even before the Drive mirror is armed: adding a
scope later means re-minting all three tokens. Do not trim the scope list to "what we use today."

## 5. Mini config — `~/.myndaix/config.env`

Add the `INBOX_*` keys (parsed, never sourced; strict `KEY=value`). Injected into the tick via
the plist env:

```
INBOX_ACCOUNTS=jefe@redeyefit.com,ops@redeyefit.com,stevenfernandez83@gmail.com
# 1Password vault name (default 'Automation')
INBOX_OP_VAULT=Automation
# first-run bounded backfill window (default 90)
INBOX_BACKFILL_DAYS=90
# whose Drive receives the brief mirror; MUST be one of INBOX_ACCOUNTS; empty = Drive mirror off
INBOX_DRIVE_ACCOUNT=jefe@redeyefit.com
# the database shared with the integration (step 6); empty = Notion mirror off
INBOX_NOTION_DB=<notion-database-id>
# empty = ping off
INBOX_IMESSAGE_TO=+1XXXXXXXXXX
```

The parser is strict `KEY=value` — **inline `#` comments are NOT supported** (a trailing comment
fails validation, and config_parse exit 2 is a hard reconcile ALARM). Comments go on their own
lines, as above.

- `INBOX_ACCOUNTS` empty/absent = component off (tick exits 0). That's the kill switch.
- Dry-run any time: `MYNDAIX_INBOX_DRY_RUN=1` — no labels, no drafts, no deliveries, no cursor
  writes; brief prints to stdout.

## 6. Notion (only if the mirror is on)

notion.so → My integrations → create an **internal** integration → token →
`op://Automation/notion-inbox-assistant/token`. Then share the target database with the
integration (database → ••• → Connections → add it — an unshared database is invisible to the
token). `INBOX_NOTION_DB` = the database id from the database URL.

## 7. Arm

1. Merge the PR to `main`. The Mini's reconcile poll converges within 15 min (no SSH deploy step).
2. Verify the job landed:
   ```
   launchctl print gui/$(id -u)/ai.myndaix.inbox-assistant
   ```
3. After the first 06:30 run, read `{MYNDAIX_HOME}/orchestrator/inbox-assistant.out` and confirm
   the brief in `~/.myndaix/bridge/inbox/jefe/`. **The first run does a bounded backfill**
   (`INBOX_BACKFILL_DAYS`, default 90) — expect it to be slow and the first brief to be big;
   subsequent runs are incremental off the historyId cursor.

## 8. Failure modes cheat-sheet

| Symptom | Meaning | Action |
|---|---|---|
| `invalid_grant` on one account | That account's refresh token died — usually a password change; Google revokes Gmail-scope tokens on password change, this is EXPECTED behavior, not a bug (verified 2026-07, Google OAuth docs) | Re-mint that one token (step 4), update its vault item. Other accounts keep working (per-account fail-closed). |
| `historyId` 404 | Google expired the cursor | Nothing — the tick auto-resyncs via bounded backfill and re-seeds the cursor. |
| `op: not signed in` / `op` nonzero | Keychain locked (post-reboot, no login) or SA token problem | Log in on the Mini; verify `security find-generic-password -s op.inbox-assistant.token` exists; worst case re-issue the SA token (step 3.4). |
| Brief missing | Anything upstream | Read `{MYNDAIX_HOME}/orchestrator/inbox-assistant.out` — every failure logs there. |
