#!/usr/bin/env bash
# drift-canary.sh — the loud smoke alarm (design §2.6). Runs `reconcile.sh --dry-run` on a
# cheap interval; if drift PERSISTS past a threshold, drops one alert into the operator
# inbox. It does NOT auto-fix — reconcile's own poll converges; the canary only shouts when
# convergence isn't happening (e.g. a broken reconcile, a stuck migration, a hand-edit).
# liveness-fire: every run logs >=1 stdout line unconditionally ("no drift" / "DRIFT"), so
# this job's .out mtime is execution evidence for liveness-canary's freshness check.
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

# Reverse watch (liveness-canary design): liveness-canary watches THIS job's recency like any
# declared job; here we watch ITS .out mtime back — mutual coverage, no third component, no
# cycle risk (each only READS the other's log mtime). Gated on its plist being installed past
# one full window (deploy grace); a stale watcher folds into this canary's existing streak+latch.
LIVENESS_OUT="$MYNDAIX_HOME/state/liveness-canary.out"
LIVENESS_PLIST="$HOME/Library/LaunchAgents/ai.myndaix.liveness.plist"
LIVENESS_MAX_AGE=1800   # 2x its 900s StartInterval
lnow="$(date +%s)"
lpm="$(stat -f %m "$LIVENESS_PLIST" 2>/dev/null || stat -c %Y "$LIVENESS_PLIST" 2>/dev/null || echo 0)"
lom="$(stat -f %m "$LIVENESS_OUT" 2>/dev/null || stat -c %Y "$LIVENESS_OUT" 2>/dev/null || echo 0)"
if [[ "$((10#$lpm))" -ne 0 ]] && (( lnow - 10#$lpm > LIVENESS_MAX_AGE )) && (( lnow - 10#$lom > LIVENESS_MAX_AGE )); then
  report="liveness-canary.out stale ($((lnow - 10#$lom))s; max ${LIVENESS_MAX_AGE}s) — the execution watcher is not running
$report"
  rc=1
fi

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
  msg="drift-canary: FACTORY drift persisting (${streak} checks). reconcile is not converging. Investigate.

$report"
  if [[ -n "${OPERATOR_INBOX:-}" && -d "$OPERATOR_INBOX" ]]; then
    alert="$OPERATOR_INBOX/drift-alert-$(date '+%Y%m%d%H%M%S').md"
    # Latch ONLY after the alert write actually succeeds — else a failed write (disk full) would
    # latch $ALERTED_FILE and silently suppress ALL future alerts (cross-family review MAJOR).
    if { printf '%s\n' "$msg" > "$alert.tmp" && mv -f "$alert.tmp" "$alert"; }; then
      : > "$ALERTED_FILE"
      log "canary: alert dropped -> $alert"
    else
      rm -f "$alert.tmp"
      log "canary: FAILED to write alert to $alert — will retry next interval (not latched)"
    fi
  else
    # Do NOT latch: the alert was NOT delivered. Latching a missing inbox at threshold would
    # permanently suppress the alert even after the inbox is restored (it only clears on a clean
    # dry-run) — a fail-open (r5 gate). Re-logging each interval is noisy-but-recoverable, and the
    # next interval retries delivery once the inbox returns, then latches on success.
    log "canary: OPERATOR_INBOX unavailable (${OPERATOR_INBOX:-<unset>}) — alert not delivered:"$'\n'"$msg"
  fi
fi
exit 0
