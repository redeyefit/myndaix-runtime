#!/bin/bash
# tailnet-watch — the LAB watches the FACTORY.
# LaunchAgent on the MacBook (macOS-only, hand-managed — NOT substrate-reconciled; the
# factory's rogue sweep does not patrol the lab). Probes the Mini over the tailnet every
# tick; 3 consecutive failures => macOS notification, re-alerted at most once per day,
# recovery notice on heal. Closes the 22-day-dark-Mini class (2026-07-18..08-09): the
# Mini's own liveness-canary was green the whole time because every LOCAL job was healthy —
# nobody was watching reachability from outside. Install: see substrate/lab/README.md.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

TW_HOST="${TW_HOST:-100.67.43.104}"          # jefes-mac-mini-1 (tagged factory node)
TW_PORT="${TW_PORT:-22}"
TS_BIN="${TW_TS_BIN:-/Applications/Tailscale.app/Contents/MacOS/Tailscale}"
NC_BIN="${TW_NC_BIN:-nc}"                     # overridable for hermetic tests
OSA_BIN="${TW_OSA_BIN:-osascript}"
FAIL_THRESHOLD="${TW_FAIL_THRESHOLD:-3}"      # consecutive ticks (~30 min at 600s)
REALERT_SECONDS="${TW_REALERT_SECONDS:-86400}"
STATE_DIR="${MYNDAIX_HOME:-$HOME/.myndaix}/state"
STATE_FILE="$STATE_DIR/tailnet-watch.state"
LOG_FILE="$STATE_DIR/tailnet-watch.log"

mkdir -p "$STATE_DIR"
tmp=""
trap 'rm -f "$tmp"' EXIT INT TERM

log() { printf '[%s] [tailnet-watch] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$LOG_FILE"; }

notify() {
  # $1 = message. Title/sound fixed; message is built ONLY from our own timestamps/host
  # (no external data enters the osascript string).
  "$OSA_BIN" -e "display notification \"$1\" with title \"MyndAIX tailnet-watch\" sound name \"Basso\"" >/dev/null 2>&1 || true
}

probe() {
  # Reachable if the ssh port answers OR tailscale ping succeeds (either proves the path).
  if "$NC_BIN" -z -G 5 "$TW_HOST" "$TW_PORT" >/dev/null 2>&1; then return 0; fi
  if [ -x "$TS_BIN" ] && "$TS_BIN" ping -c 1 --timeout=5s "$TW_HOST" >/dev/null 2>&1; then return 0; fi
  return 1
}

# ---- read state (own file, key=value; missing file = first run) ----
fails=0; alerted_at=0; first_fail_at=0
if [ -f "$STATE_FILE" ]; then
  v="$(grep '^fails=' "$STATE_FILE" | cut -d= -f2 || true)"
  [[ "$v" =~ ^[0-9]+$ ]] && fails=$((10#$v))
  v="$(grep '^alerted_at=' "$STATE_FILE" | cut -d= -f2 || true)"
  [[ "$v" =~ ^[0-9]+$ ]] && alerted_at=$((10#$v))
  v="$(grep '^first_fail_at=' "$STATE_FILE" | cut -d= -f2 || true)"
  [[ "$v" =~ ^[0-9]+$ ]] && first_fail_at=$((10#$v))
fi

now="$(date +%s)"

write_state() {
  tmp="$(mktemp "$STATE_DIR/.tailnet-watch.XXXXXX")"
  printf 'fails=%s\nalerted_at=%s\nfirst_fail_at=%s\n' "$1" "$2" "$3" > "$tmp"
  mv "$tmp" "$STATE_FILE"; tmp=""
}

if probe; then
  if [ "$alerted_at" -gt 0 ]; then
    downtime_min=$(( (now - first_fail_at) / 60 ))
    log "RECOVERED after ~${downtime_min}m down"
    notify "Mini is reachable again (was down ~${downtime_min} min)"
  fi
  [ "$fails" -gt 0 ] && log "reachable again after $fails failed tick(s) (below threshold)"
  write_state 0 0 0
else
  fails=$((fails + 1))
  [ "$first_fail_at" -eq 0 ] && first_fail_at="$now"
  log "probe FAILED ($fails consecutive; threshold $FAIL_THRESHOLD)"
  if [ "$fails" -ge "$FAIL_THRESHOLD" ] && [ $((now - alerted_at)) -ge "$REALERT_SECONDS" ]; then
    since="$(date -r "$first_fail_at" '+%H:%M %m/%d')"
    log "ALERT: Mini unreachable since $since"
    notify "Mini unreachable on the tailnet since $since — factory + backup mirror are dark"
    alerted_at="$now"
  fi
  write_state "$fails" "$alerted_at" "$first_fail_at"
fi
