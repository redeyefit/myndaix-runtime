#!/usr/bin/env bash
# drift-canary.sh — the loud smoke alarm (design §2.6). Probes outbound TCP, then runs
# `reconcile.sh --dry-run` on a cheap interval; if net-death or drift PERSISTS past a
# threshold, drops one alert into the operator inbox. It does NOT auto-fix — reconcile's
# own poll converges; the canary only shouts when convergence isn't happening (e.g. a
# broken reconcile, a stuck migration, a hand-edit, a blackholed network).
set -euo pipefail
SUBSTRATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=substrate/lib.sh
source "$SUBSTRATE_DIR/lib.sh"
substrate_load_config

STATE_DIR="$MYNDAIX_HOME/state"
mkdir -p "$STATE_DIR"
STREAK_FILE="$STATE_DIR/drift-streak"
ALERTED_FILE="$STATE_DIR/drift-alerted"
THRESHOLD=2   # consecutive failing checks before alerting (~2 intervals)

# deliver_alert PREFIX MSG LATCH — drop MSG into the operator inbox as PREFIX-<ts>.md.
# Latch ONLY after the alert write actually succeeds — else a failed write (disk full) would
# latch and silently suppress ALL future alerts (cross-family review MAJOR). Do NOT latch when
# the inbox is unavailable: latching an undelivered alert would permanently suppress it even
# after the inbox is restored (a fail-open — r5 gate). Re-logging each interval is
# noisy-but-recoverable; the next interval retries delivery, then latches on success.
deliver_alert() {
  local prefix="$1" msg="$2" latch="$3" alert
  if [[ -n "${OPERATOR_INBOX:-}" && -d "$OPERATOR_INBOX" ]]; then
    alert="$OPERATOR_INBOX/${prefix}-$(date '+%Y%m%d%H%M%S').md"
    if { printf '%s\n' "$msg" > "$alert.tmp" && mv -f "$alert.tmp" "$alert"; }; then
      : > "$latch"
      log "canary: alert dropped -> $alert"
    else
      rm -f "$alert.tmp"
      log "canary: FAILED to write alert to $alert — will retry next interval (not latched)"
    fi
  else
    log "canary: OPERATOR_INBOX unavailable (${OPERATOR_INBOX:-<unset>}) — alert not delivered:"$'\n'"$msg"
  fi
}

# ---- outbound-net probe (the Mini-strand alarm, 2026-07-16) -----------------
# A zombie VPN extension can blackhole ALL outbound TCP while ping+DNS still work
# (instant EADDRNOTAVAIL on every connect). Probe outbound TCP FIRST: on failure alert
# distinctly and SKIP the drift check — reconcile's fetch would fail anyway and read as
# endless false "drift". Any one URL succeeding = net is fine (a single site being down
# must not page).
NET_STREAK_FILE="$STATE_DIR/net-streak"
NET_ALERTED_FILE="$STATE_DIR/net-alerted"
read -r -a NET_PROBE_URLS <<< "${NET_PROBE_URLS:-https://github.com https://www.apple.com}"

net_errs=""
net_ok() {
  local url out
  for url in "${NET_PROBE_URLS[@]}"; do
    if out="$(curl -sS -m 10 --connect-timeout 6 -o /dev/null "$url" 2>&1)"; then
      return 0
    fi
    net_errs+="$url: $out"$'\n'
  done
  return 1
}

if net_ok; then
  rm -f "$NET_STREAK_FILE" "$NET_ALERTED_FILE"
else
  nstreak="$(cat "$NET_STREAK_FILE" 2>/dev/null || echo 0)"
  [[ "$nstreak" =~ ^[0-9]+$ ]] || nstreak=0
  nstreak=$(( 10#$nstreak + 1 ))
  if ! { printf '%s\n' "$nstreak" > "$NET_STREAK_FILE.tmp" && mv -f "$NET_STREAK_FILE.tmp" "$NET_STREAK_FILE"; }; then
    die "could not write net streak"
  fi
  log "canary: OUTBOUND NET DOWN (streak=$nstreak)"
  if [[ "$nstreak" -ge "$THRESHOLD" && ! -e "$NET_ALERTED_FILE" ]]; then
    deliver_alert "net-alert" "drift-canary: FACTORY outbound network DOWN (${nstreak} checks). All probe URLs unreachable:

${net_errs}
If ping/DNS work but no TCP connects (instant failures), this is the VPN-extension blackhole strand — recover by REBOOTING the machine, then verify Tailscale \"Use exit node\" is OFF." "$NET_ALERTED_FILE"
  fi
  exit 0   # a dead network is not drift — do not touch the drift streak
fi

set +e
report="$(/bin/bash "$SUBSTRATE_DIR/reconcile.sh" --dry-run 2>&1)"; rc=$?
set -e

if [[ "$rc" -eq 0 ]]; then
  # clean — reset the streak + clear any prior alert latch
  rm -f "$STREAK_FILE" "$ALERTED_FILE"
  log "canary: no drift"
  exit 0
fi

# drifting — bump the streak (base-10 normalize to dodge the octal trap)
streak="$(cat "$STREAK_FILE" 2>/dev/null || echo 0)"
[[ "$streak" =~ ^[0-9]+$ ]] || streak=0
streak=$(( 10#$streak + 1 ))
# Explicit fail-closed write (a `printf > tmp && mv` &&-chain is exempt from set -e on the non-final
# link — the #89 class; cross-family review MAJOR).
if ! { printf '%s\n' "$streak" > "$STREAK_FILE.tmp" && mv -f "$STREAK_FILE.tmp" "$STREAK_FILE"; }; then
  die "could not write drift streak"
fi
log "canary: DRIFT (streak=$streak)"

if [[ "$streak" -ge "$THRESHOLD" && ! -e "$ALERTED_FILE" ]]; then
  deliver_alert "drift-alert" "drift-canary: FACTORY drift persisting (${streak} checks). reconcile is not converging. Investigate.

$report" "$ALERTED_FILE"
fi
exit 0
