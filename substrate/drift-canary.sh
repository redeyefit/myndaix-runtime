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

# canary_emit SFILE AFILE PREFIX LABEL BODY — shared streak+latch+alert used by BOTH the
# config-drift watch and the (independent) liveness-execution watch. Each passes its OWN
# streak+latch files so a standing latch on one NEVER suppresses the other: a QUARANTINED hold
# keeps config drift latched for days, and the execution watcher dying under it must STILL
# alert (deep-audit P2 — the mutual watch must not share fate with config drift). Bumps the
# streak; at THRESHOLD drops ONE alert + latches on success (fail-closed writes, latch-after-write).
canary_emit() {
  local sfile="$1" afile="$2" prefix="$3" label="$4" body="$5" streak alert
  streak="$(cat "$sfile" 2>/dev/null || echo 0)"
  [[ "$streak" =~ ^[0-9]+$ ]] || streak=0
  streak=$(( 10#$streak + 1 ))
  # &&-chain is exempt from set -e on the non-final link — the #89 class; fail-closed write.
  if ! { printf '%s\n' "$streak" > "$sfile.tmp" && mv -f "$sfile.tmp" "$sfile"; }; then
    die "could not write $label streak"
  fi
  log "canary: $label (streak=$streak)"
  [[ "$streak" -ge "$THRESHOLD" && ! -e "$afile" ]] || return 0
  if [[ -n "${OPERATOR_INBOX:-}" && -d "$OPERATOR_INBOX" ]]; then
    alert="$OPERATOR_INBOX/${prefix}-$(date '+%Y%m%d%H%M%S').md"
    # Latch ONLY after the alert write succeeds — else a failed write (disk full) would latch and
    # silently suppress ALL future alerts (cross-family review MAJOR).
    if { printf '%s\n' "$body" > "$alert.tmp" && mv -f "$alert.tmp" "$alert"; }; then
      : > "$afile"
      log "canary: $label alert dropped -> $alert"
    else
      rm -f "$alert.tmp"
      log "canary: FAILED to write $label alert to $alert — will retry next interval (not latched)"
    fi
  else
    # Do NOT latch: the alert was NOT delivered (a fail-open if latched — r5 gate). Re-logging each
    # interval is noisy-but-recoverable; the next interval retries delivery, then latches on success.
    log "canary: OPERATOR_INBOX unavailable (${OPERATOR_INBOX:-<unset>}) — $label alert not delivered:"$'\n'"$body"
  fi
}

# Test-only seam (mirrors liveness-canary's LCTL): drive rc without a heavy real reconcile run so
# the independent liveness-watch can be exercised behaviorally. Live drift-canary never sets it.
if [[ -n "${DRIFT_CANARY_TEST_RC:-}" ]]; then
  rc="$DRIFT_CANARY_TEST_RC"; report="(test seam: reconcile --dry-run skipped)"
else
  set +e
  report="$(/bin/bash "$SUBSTRATE_DIR/reconcile.sh" --dry-run 2>&1)"; rc=$?
  set -e
fi

# ---- liveness-execution reverse watch (INDEPENDENT streak+latch) --------------------------
# liveness-canary watches THIS job's recency like any declared job; here we watch ITS .out mtime
# back — mutual coverage, no third component, no cycle risk (each only READS the other's log
# mtime). Its OWN streak+latch (NOT folded into config drift) so a standing drift latch can't
# mute "the execution watcher is dead" and vice versa (deep-audit P2). Gated on its plist being
# installed past one full window (deploy grace). Runs regardless of the config-drift outcome.
LW_STREAK_FILE="$STATE_DIR/liveness-watch-streak"
LW_ALERTED_FILE="$STATE_DIR/liveness-watch-alerted"
LIVENESS_OUT="$MYNDAIX_HOME/state/liveness-canary.out"
LIVENESS_PLIST="$HOME/Library/LaunchAgents/ai.myndaix.liveness.plist"
LIVENESS_MAX_AGE=1800   # 2x its 900s StartInterval
# mtime EPOCH seconds, &&-guarded (NOT `A || B` inside one substitution): GNU `stat -f` means
# --file-system and leaks a multiline block to stdout while exiting nonzero, which a `||` chain
# would capture as garbage and abort the arithmetic below (Linux CI). Emit only a form that won.
_mtime() { local m; m="$(stat -f %m "$1" 2>/dev/null)" && { printf '%s' "$m"; return 0; }; m="$(stat -c %Y "$1" 2>/dev/null)" && { printf '%s' "$m"; return 0; }; printf '0'; }
lnow="$(date +%s)"
lpm="$(_mtime "$LIVENESS_PLIST")"
lom="$(_mtime "$LIVENESS_OUT")"
if [[ "$((10#$lpm))" -ne 0 ]] && (( lnow - 10#$lpm > LIVENESS_MAX_AGE )) && (( lnow - 10#$lom > LIVENESS_MAX_AGE )); then
  canary_emit "$LW_STREAK_FILE" "$LW_ALERTED_FILE" "liveness-watch-alert" "liveness-watch DRIFT" \
    "drift-canary reverse watch: liveness-canary.out is stale ($((lnow - 10#$lom))s; max ${LIVENESS_MAX_AGE}s) — the execution watcher is not running. Every declared job's execution omission is now UNWATCHED. Investigate ai.myndaix.liveness (launchctl print $LA_DOMAIN/ai.myndaix.liveness)."
else
  rm -f "$LW_STREAK_FILE" "$LW_ALERTED_FILE" || log "canary: WARN could not clear liveness-watch streak/latch"
fi

# ---- config-drift watch -------------------------------------------------------------------
if [[ "$rc" -eq 0 ]]; then
  rm -f "$STREAK_FILE" "$ALERTED_FILE"
  log "canary: no drift"
  exit 0
fi
canary_emit "$STREAK_FILE" "$ALERTED_FILE" "drift-alert" "config DRIFT" \
  "drift-canary: FACTORY drift persisting. reconcile is not converging. Investigate.

$report"
exit 0
