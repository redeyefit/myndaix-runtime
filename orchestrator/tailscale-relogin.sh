#!/bin/bash
# tailscale-relogin.sh — keep the FACTORY (Mac Mini) on the tailnet WITHOUT a human at the
# screen. The Tailscale node key expires (~180d default) and a logged-out/expired node drops
# off the tailnet and demands INTERACTIVE re-auth — which, on an offsite headless box, strands
# it (this happened twice on 2026-07-16). This daemon re-joins non-interactively with a stored
# auth key when it detects the node is logged out.
#
# SCOPE (deliberately narrow — this ONLY re-logs-in; it never reboots or reinstalls):
#   * It fixes the LOGGED-OUT / KEY-EXPIRED class (BackendState NeedsLogin/NoState/Stopped).
#   * It does NOT fix a ZOMBIE/dead tailscaled (CLI can't reach the local service) or the
#     outbound-TCP blackhole — those need a reboot (smart plug / power cycle); this daemon
#     DETECTS them and drops a distinct operator alert instead of futilely running `up`.
#
# PREREQUISITE (see docs/tailscale-relogin.md): the STANDALONE tailscaled (brew), NOT the
# App-Store GUI variant — only the standalone daemon supports headless `tailscale up --auth-key`,
# starts before login, and survives GUI trouble. Plus `tailscale set --operator=<user>` so this
# LaunchAgent can run `up` without sudo, and an auth key (ideally a no-expiry OAuth client
# secret, tag:factory) in ~/.myndaix/.secrets. Also DISABLE key expiry on the node in the admin
# console so the node key itself never times out — this daemon is the belt, not the only fix.
#
# SAFETY: never logs the key; fail-closed + loud on a missing key (no crash-loop); acts only
# after the logged-out state PERSISTS (transient-tolerant); rate-limited between attempts.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# ---- config (env-overridable; sane defaults) --------------------------------------------------
TS_CLI="${TS_RELOGIN_CLI:-$(command -v tailscale || echo /opt/homebrew/bin/tailscale)}"
SECRETS_FILE="${TS_RELOGIN_SECRETS:-$HOME/.myndaix/.secrets}"
KEY_VAR="${TS_RELOGIN_KEY_VAR:-TAILSCALE_AUTHKEY}"     # var name to read from .secrets
STATE_DIR="${TS_RELOGIN_STATE_DIR:-$HOME/.myndaix/state}"
OPERATOR_INBOX="${TS_RELOGIN_OPERATOR_INBOX:-$HOME/.myndaix/bridge/inbox/jefe}"
THRESHOLD="$(( 10#${TS_RELOGIN_THRESHOLD:-2} ))"      # consecutive logged-out checks before acting
COOLDOWN_S="$(( 10#${TS_RELOGIN_COOLDOWN_S:-600} ))"  # min seconds between `up` attempts (no hammering)
DRY="${TS_RELOGIN_DRY_RUN:-0}"                        # 1 = detect + log only, never run `up`
LOG="$HOME/.myndaix/orchestrator/tailscale-relogin.log"

mkdir -p "$STATE_DIR" "$(dirname "$LOG")" 2>/dev/null || true
STREAK_FILE="$STATE_DIR/ts-relogin-streak"
LAST_ATTEMPT_FILE="$STATE_DIR/ts-relogin-last-attempt"
ZOMBIE_ALERTED="$STATE_DIR/ts-relogin-zombie-alerted"

log(){ printf '[%s] [tailscale-relogin] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG" 2>/dev/null || true
       printf '[tailscale-relogin] %s\n' "$*"; }

now_s(){ date +%s; }

# read_int FILE — fail-closed numeric read (missing = 0; anything non-numeric = 0; base-10).
read_int(){ local v; v="$(cat "$1" 2>/dev/null || echo 0)"; [[ "$v" =~ ^[0-9]+$ ]] || v=0; printf '%s' "$(( 10#$v ))"; }

atomic_write(){ # atomic_write FILE VALUE — fail-closed (&&-chain exempt from set -e on the non-final link)
  if ! { printf '%s\n' "$2" > "$1.tmp.$$" && mv -f "$1.tmp.$$" "$1"; }; then
    rm -f "$1.tmp.$$"; die "could not write $1"; fi; }

die(){ log "ALARM: $*"; exit 1; }

drop_alert(){ # drop_alert PREFIX MESSAGE LATCH_FILE — one-shot operator alert, latched on success
  local prefix="$1" msg="$2" latch="$3" alert
  [[ -e "$latch" ]] && return 0   # already alerted for this episode
  if [[ -n "$OPERATOR_INBOX" && -d "$OPERATOR_INBOX" ]]; then
    alert="$OPERATOR_INBOX/${prefix}-$(date '+%Y%m%d%H%M%S')-$$.md"
    if { printf '%s\n' "$msg" > "$alert.tmp.$$" && mv -f "$alert.tmp.$$" "$alert"; }; then
      : > "$latch"; log "alert dropped -> $alert"
    else rm -f "$alert.tmp.$$"; log "FAILED to write alert (will retry next tick)"; fi
  else
    log "OPERATOR_INBOX unavailable — alert not delivered: $msg"
  fi; }

# ---- read node state -------------------------------------------------------------------------
# BackendState via JSON (python3 reads stdin; no shell interpolation into python — bash rule).
# CLI-connect failure => the local tailscaled is unreachable (zombie/not-running): NOT relogin-able.
[[ -x "$TS_CLI" ]] || die "tailscale CLI not found/executable at '$TS_CLI' (install standalone tailscaled — see docs)"

status_json="$("$TS_CLI" status --json 2>/dev/null || true)"
if [[ -z "$status_json" ]]; then
  # daemon unreachable — the reboot/plug case, not a relogin. Alert once, do NOT run `up`.
  drop_alert "ts-zombie-alert" \
    "tailscale-relogin: the local tailscaled is UNREACHABLE (CLI cannot connect). This is NOT a
logged-out state \`up\` can fix — it is a dead/zombie daemon or the outbound-TCP blackhole.
Recover by REBOOTING the Mini (smart plug / power cycle), then verify \`tailscale status\`." \
    "$ZOMBIE_ALERTED"
  log "tailscaled unreachable — cannot relogin (needs reboot); zombie alert handled"
  exit 0
fi
rm -f "$ZOMBIE_ALERTED"   # daemon reachable again — clear the zombie latch

state="$(printf '%s' "$status_json" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("BackendState",""))
except Exception: print("")' 2>/dev/null || true)"

case "$state" in
  Running)
    rm -f "$STREAK_FILE"
    log "node online (BackendState=Running)"
    exit 0 ;;
  NeedsMachineAuth)
    # key worked but device approval is required (device-approval is ON) — `up` won't clear it.
    drop_alert "ts-machineauth-alert" \
      "tailscale-relogin: node needs MACHINE AUTH (device approval). Approve jefes-mac-mini in the
Tailscale admin console (Google acct stevenfernandez83, NOT github). \`up\` cannot self-clear this." \
      "$STATE_DIR/ts-relogin-machineauth-alerted"
    log "NeedsMachineAuth — admin approval required (alerted)"
    exit 0 ;;
  Starting)
    log "node Starting — transient, will re-check next tick"
    exit 0 ;;
  NeedsLogin|NoState|Stopped|"")
    : ;;  # logged out / disconnected — the relogin path below
  *)
    log "unrecognized BackendState='$state' — treating as logged out"; ;;
esac

# ---- logged out: streak-gate, then relogin ---------------------------------------------------
streak="$(read_int "$STREAK_FILE")"
streak="$(( streak + 1 ))"
atomic_write "$STREAK_FILE" "$streak"
log "node logged out (BackendState='${state:-empty}') — streak=$streak/$THRESHOLD"
# new logged-out EPISODE: clear the recovered latch so the next successful relogin re-alerts.
rm -f "$STATE_DIR/ts-relogin-recovered-alerted"
[[ "$streak" -ge "$THRESHOLD" ]] || { log "below threshold — tolerating (may be a transient restart)"; exit 0; }

# rate-limit: no more than one `up` per COOLDOWN_S
last="$(read_int "$LAST_ATTEMPT_FILE")"
elapsed="$(( $(now_s) - last ))"
if [[ "$last" -gt 0 && "$elapsed" -lt "$COOLDOWN_S" ]]; then
  log "in cooldown (${elapsed}s < ${COOLDOWN_S}s since last attempt) — skipping this tick"
  exit 0
fi

# read the auth key (fail-closed + loud; NEVER logged). A missing secrets FILE or an empty
# key VAR are the same operator-actionable condition: logged out and no key to recover with.
AUTHKEY=""
if [[ -f "$SECRETS_FILE" ]]; then
  AUTHKEY="$(
    set +u
    # shellcheck disable=SC1090  # runtime-path secrets file; not statically resolvable
    source "$SECRETS_FILE" >/dev/null 2>&1 || true
    set -u
    printf '%s' "${!KEY_VAR:-}"
  )"
fi
if [[ -z "$AUTHKEY" ]]; then
  drop_alert "ts-nokey-alert" \
    "tailscale-relogin: node is logged out but NO usable $KEY_VAR is available in $SECRETS_FILE
(file missing or var empty) — cannot self-recover. Add a reusable/OAuth auth key (tag:factory
recommended) to re-enable auto-relogin." \
    "$STATE_DIR/ts-relogin-nokey-alerted"
  die "no usable $KEY_VAR in $SECRETS_FILE — logged out and cannot relogin (alerted)"
fi
rm -f "$STATE_DIR/ts-relogin-nokey-alerted"

atomic_write "$LAST_ATTEMPT_FILE" "$(now_s)"
if [[ "$DRY" == "1" ]]; then
  log "DRY-RUN: would run '$TS_CLI up --auth-key=<redacted> --hostname=$(hostname -s)'"
  exit 0
fi

log "attempting non-interactive relogin (tailscale up --auth-key=<redacted>)"
if "$TS_CLI" up --auth-key="$AUTHKEY" --hostname="$(hostname -s)" >/dev/null 2>&1; then
  rm -f "$STREAK_FILE"
  drop_alert "ts-recovered-alert" \
    "tailscale-relogin: node was logged out and has been AUTO-REJOINED to the tailnet via the
stored auth key. No action needed — informational." \
    "$STATE_DIR/ts-relogin-recovered-alerted"
  rm -f "$STATE_DIR/ts-relogin-recovered-alerted"   # informational one-shot; don't latch across episodes
  log "relogin SUCCEEDED — node rejoined the tailnet"
else
  log "relogin FAILED (tailscale up nonzero) — will retry after cooldown (${COOLDOWN_S}s)"
fi
exit 0
