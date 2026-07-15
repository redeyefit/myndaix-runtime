#!/usr/bin/env bash
# drift-canary.sh — the loud smoke alarm (design §2.6). Runs `reconcile.sh --dry-run` on a
# cheap interval; if drift PERSISTS past a threshold, drops one alert into the operator
# inbox. It does NOT auto-fix — reconcile's own poll converges; the canary only shouts when
# convergence isn't happening (e.g. a broken reconcile, a stuck migration, a hand-edit).
set -euo pipefail
SUBSTRATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=substrate/lib.sh
source "$SUBSTRATE_DIR/lib.sh"
substrate_load_config

STATE_DIR="$MYNDAIX_HOME/state"
mkdir -p "$STATE_DIR"
STREAK_FILE="$STATE_DIR/drift-streak"
ALERTED_FILE="$STATE_DIR/drift-alerted"
THRESHOLD=2   # consecutive drifting checks before alerting (~2 intervals)

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
printf '%s\n' "$streak" > "$STREAK_FILE.tmp" && mv -f "$STREAK_FILE.tmp" "$STREAK_FILE"
log "canary: DRIFT (streak=$streak)"

if [[ "$streak" -ge "$THRESHOLD" && ! -e "$ALERTED_FILE" ]]; then
  msg="drift-canary: FACTORY drift persisting (${streak} checks). reconcile is not converging. Investigate.

$report"
  if [[ -n "${OPERATOR_INBOX:-}" && -d "$OPERATOR_INBOX" ]]; then
    alert="$OPERATOR_INBOX/drift-alert-$(date '+%Y%m%d%H%M%S').md"
    printf '%s\n' "$msg" > "$alert.tmp" && mv -f "$alert.tmp" "$alert"
    log "canary: alert dropped -> $alert"
  else
    log "canary: OPERATOR_INBOX unavailable ($OPERATOR_INBOX) — alert not delivered:"$'\n'"$msg"
  fi
  : > "$ALERTED_FILE"   # latch: one alert per drift streak (no per-interval spam)
fi
exit 0
