# Tailscale auto-relogin — keeping the FACTORY reachable without a human at the screen

## What it does and why

The Mac Mini (FACTORY) is offsite and headless. Twice on 2026-07-16 it fell off the tailnet and
demanded **interactive** re-authentication — which stranded it, because nobody was there to click
"Log in". Two distinct causes stacked that day:

1. **Node key expiry** — Tailscale node keys expire (~180d default); an expired node silently
   drops off the tailnet and needs re-auth.
2. **Exit-node + zombie tailscaled** — blackholed all outbound TCP (a reboot-class failure).

`orchestrator/tailscale-relogin.sh` is a small LaunchAgent that closes cause **#1**: it detects a
logged-out node and re-joins non-interactively with a stored auth key. It is **one layer** of the
recovery stack, not the whole thing:

| Failure | Recovery layer |
|---|---|
| Logged out / key expired | **this daemon** (auto `tailscale up --auth-key`) |
| Node key expiry itself | **disable key expiry** in the admin console (do this too) |
| Zombie tailscaled / TCP blackhole | reboot — smart plug / power cycle (this daemon only DETECTS + alerts) |
| Detection of the blackhole | `substrate/drift-canary.sh` net-probe (PR #96) |

Deliberately **out of scope** (per the 2026-07-16 scope decision): a self-reboot watchdog. The
smart plug is a *safe manual* remote reboot; an autonomous root reboot actuator is a separate,
independently-reviewed design if we ever build it.

## Data flow

Every 5 min: `tailscale status --json` → `BackendState` →
- `Running` → healthy, reset streak, exit.
- `NeedsLogin` / `NoState` / `Stopped` → logged out. After the state **persists** past the streak
  threshold (transient-tolerant) and outside the attempt cooldown (no hammering), run
  `tailscale up --auth-key=<key> --hostname=<host>`. Success → drop an informational "rejoined"
  alert; failure → log and retry after cooldown.
- `NeedsMachineAuth` → device approval is required; `up` can't clear it → distinct operator alert.
- CLI can't reach the local service → **zombie/dead tailscaled** (the reboot case, NOT relogin) →
  distinct "reboot needed" alert; never runs `up`.

## Security surface

- The auth key is read from `~/.myndaix/.secrets` (`chmod 600`) into a variable and passed to
  `tailscale up --auth-key=…`. It is **never logged** (the command logs `<redacted>`); a test
  asserts the key value appears in no log.
- Fail-closed + loud on a missing/empty key: drops a `ts-nokey-alert` and exits non-zero rather
  than silently doing nothing.
- No `sudo`: with `tailscale set --operator=<user>` the LaunchAgent runs `up` as the user.

## Prerequisites (manual, one-time — the daemon can't do these itself)

1. **Swap App-Store Tailscale → standalone `tailscaled`.** The App-Store GUI variant drives auth
   through the GUI and can't do headless `tailscale up --auth-key`; it's also the variant that
   wedged twice. Install the standalone daemon:
   ```
   brew install tailscale
   sudo tailscaled install-system-daemon      # starts before login, survives GUI trouble
   ```
   (Then quit/remove the App-Store app so only one tailscaled runs.)
2. **Let the LaunchAgent run `up` without sudo:**
   ```
   sudo tailscale set --operator=$(whoami)
   ```
3. **Add a credential** to `~/.myndaix/.secrets` (`chmod 600`). Two options:

   **(a) OAuth client — recommended (no expiry).** OAuth-minted keys are ALWAYS tagged, so the
   tag must exist in the ACL first and be passed to `--advertise-tags`:
   - In **Access Controls**, define the tag owner:
     ```json
     "tagOwners": { "tag:factory": ["autogroup:admin"] }
     ```
   - **Settings → OAuth clients → Generate**, scope **Auth Keys → Write**, tag **tag:factory**.
   - Store BOTH the client secret and the tag — the daemon reads `TAILSCALE_TAGS` and passes it
     as `--advertise-tags` (required, or the OAuth `up` fails):
     ```
     TAILSCALE_AUTHKEY=tskey-client-...
     TAILSCALE_TAGS=tag:factory
     ```

   **(b) Reusable auth key — simpler, but expires (≤90d).** Non-ephemeral, reusable; no tag
   needed. Set a reminder to rotate before expiry:
   ```
   TAILSCALE_AUTHKEY=tskey-...
   ```

   Manual `up` (Phase 2) mirrors this — the OAuth path needs the tag flag:
   ```
   sudo tailscale up --auth-key='<secret>' --advertise-tags=tag:factory --hostname=jefes-mac-mini
   ```
4. **Disable key expiry** on `jefes-mac-mini` in the admin console
   (login.tailscale.com → **Google acct stevenfernandez83**, NOT github/redeyefit — that's a dead
   tailnet) so the node key itself never times out. This daemon is the belt; expiry-disable is the
   primary fix.

## Install

```
cp orchestrator/ai.myndaix.tailscale-relogin.plist.example \
   ~/Library/LaunchAgents/ai.myndaix.tailscale-relogin.plist
# replace {HOME} in the plist with the real home if needed
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.myndaix.tailscale-relogin.plist
```

Verify: `TS_RELOGIN_DRY_RUN=1 orchestrator/tailscale-relogin.sh` (detect + log, never acts), then
watch `~/.myndaix/orchestrator/tailscale-relogin.out`.

## Test

`orchestrator/test-tailscale-relogin.sh` — hermetic (stubs the `tailscale` CLI, throwaway homes):
healthy no-op, streak-gate, redacted+key-never-logged relogin, cooldown, fail-closed no-key alert,
zombie-daemon alert (no `up`), machine-auth alert. 19 checks; run before install.

## Failure modes

- **Key rotated/revoked** → `up` fails every cooldown, logs the failure; the node stays off until a
  valid key is restored (fail-closed, visible).
- **Reusable key expired** → same as revoked; this is why the OAuth-client (no-expiry) key is
  recommended.
- **Zombie tailscaled** → detected, alerted, NOT retried via `up` (a reboot is the only fix).
