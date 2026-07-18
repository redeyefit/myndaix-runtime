#!/usr/bin/env bash
# liveness-canary.sh — declared-vs-runtime execution reconciliation
# (docs/liveness-canary-design.md). Every 15 min: verify each job DECLARED in
# substrate/plists/*.json for this machine's role is actually ALIVE at runtime — label loaded
# in the launchd domain, last exit status healthy, fresh execution evidence (its .out mtime
# within the descriptor's liveness_max_gap_seconds) — and flag loaded ai.myndaix.* labels
# nobody declares (rogues). drift-canary covers CONFIG-level convergence; this covers the
# operational-omission class it can't see: installed AND loaded but never actually firing.
#
# READ-ONLY against launchd (print/list only — never bootstrap/bootout/kickstart). Alerts via
# the drift-canary streak+latch pattern into $OPERATOR_INBOX. Exits 0 always (the canary
# itself must not accumulate launchd failure state).
# liveness-fire: every run ends in an unconditional log line, so this job's own .out mtime is
# execution evidence for drift-canary's reverse watch (mutual coverage, no third component).
set -euo pipefail
SUBSTRATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=substrate/lib.sh
source "$SUBSTRATE_DIR/lib.sh"
substrate_load_config

INTERVAL=900        # MUST equal StartInterval in plists/ai.myndaix.liveness.json (test-asserted)
THRESHOLD=2         # consecutive divergent runs before alerting (~2 intervals)
STATE_DIR="$MYNDAIX_HOME/state"
mkdir -p "$STATE_DIR"
STREAK_FILE="$STATE_DIR/liveness-streak"
ALERTED_FILE="$STATE_DIR/liveness-alerted"
LAST_RUN_FILE="$STATE_DIR/liveness-last-run"
LA_DIR="$HOME/Library/LaunchAgents"
# Test seam ONLY: test.sh injects a stub so every divergence path runs behaviorally without
# touching live launchd. The live plist never sets this; read-only calls either way.
LCTL="${LIVENESS_LAUNCHCTL:-launchctl}"

# mtime EPOCH seconds (macOS stat -f, Linux-CI stat -c); missing file -> 0. The `|| echo 0`
# lives INSIDE the substitution so set -e -o pipefail can't kill the caller (bash rules).
mtime() { stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo 0; }

now="$(date +%s)"

# ---- self-grace (sleep/wake guard) ------------------------------------------------------
# If WE haven't run in > 2x our own interval, the machine was asleep/frozen — every job's
# evidence is equally stale. Touch + skip one tick so each job catches up, instead of a
# wake-up alert storm. .last_run is touched on EVERY normal run (Oracle re-review note 1).
last_run="$(mtime "$LAST_RUN_FILE")"; last_run=$((10#$last_run))
touch "$LAST_RUN_FILE"
if [[ "$last_run" -ne 0 ]] && (( now - last_run > 2 * INTERVAL )); then
  log "liveness: own last run $((now - last_run))s ago (>2x interval) — sleep/wake grace, skipping one tick"
  exit 0
fi

div=""; div_n=0
diverge() { div="${div}- ${1}"$'\n'; div_n=$((div_n + 1)); log "liveness: DIVERGENT ${1}"; }

# ---- declared set: ONE python3 pass over all descriptors (paths via argv) ---------------
# liveness_targets.py emits label/max_gap/sentinel/out_path per watched job and a per-file
# ERR line on a corrupt descriptor (fail-closed, never sinks the batch). Its own nonzero
# exit means the CONFIG is unreadable — the whole declared set is unverifiable.
if ! targets="$(python3 "$SUBSTRATE_DIR/liveness_targets.py" "$CONFIG_FILE" "$SUBSTRATE_DIR"/plists/*.json)"; then
  targets=""
  diverge "liveness_targets.py failed (invalid config?) — declared set unverifiable, remedy: run reconcile --dry-run"
fi

declared=$'\n'   # newline-framed membership string (bash-3.2 safe; no arrays)
while IFS=$'\t' read -r label max_gap sentinel out_path; do
  [[ -n "$label" ]] || continue
  if [[ "$label" == "ERR" ]]; then
    # fields here are: ERR <file> <reason> — an unwatchable descriptor IS the omission class
    diverge "descriptor $max_gap: $sentinel"
    continue
  fi
  declared="${declared}${label}"$'\n'
  # Belt behind the python-side validation: never interpolate an unvalidated label into a
  # launchctl target or an alert body (design §Security Surface).
  if ! [[ "$label" =~ ^ai\.myndaix\.[A-Za-z0-9._-]+$ ]]; then
    diverge "declared-set line carries an invalid label — refusing to query launchd with it"
    continue
  fi
  [[ "$max_gap" =~ ^[0-9]+$ ]] || { diverge "$label: non-numeric max gap in declared set"; continue; }
  max_gap=$((10#$max_gap))
  # Sentinel-gated + unarmed => the job is LEGITIMATELY unloaded — skip, not divergence.
  if [[ "$sentinel" != "-" && ! -e "$MYNDAIX_HOME/$sentinel" ]]; then continue; fi
  # Reconcile-grace, UNCONDITIONAL (not gated on a missing .out): a plist fresher than the
  # job's max gap was just (re)installed — the job hasn't had a full cycle yet.
  plist_m="$(mtime "$LA_DIR/$label.plist")"; plist_m=$((10#$plist_m))
  if [[ "$plist_m" -ne 0 ]] && (( now - plist_m <= max_gap )); then continue; fi
  # Loaded? Targeted print on the validated label only; a nonzero exit (incl. permission/SIP
  # quirks) = "not loaded" divergence, never a crash.
  if ! pr="$("$LCTL" print "$LA_DOMAIN/$label" 2>/dev/null)"; then
    diverge "$label: NOT LOADED — remedy: launchctl bootstrap $LA_DOMAIN $LA_DIR/$label.plist (or let reconcile converge)"
    continue
  fi
  # Last exit status: targeted parse; a format miss on a future macOS SKIPS this sub-check
  # (the loaded + freshness checks still stand) rather than false-alerting.
  ec="$(printf '%s\n' "$pr" | sed -n 's/^[[:space:]]*last exit code = \([0-9][0-9]*\).*/\1/p' | head -1 || true)"
  if [[ "$ec" =~ ^[0-9]+$ ]] && [[ "$((10#$ec))" -ne 0 ]]; then
    diverge "$label: last exit code = $((10#$ec)) — remedy: investigate $out_path"
  fi
  # Freshness: the job's stdout log mtime is the execution evidence (every descriptor's
  # program writes >=1 stdout line per fire — the liveness-fire invariant, test-asserted).
  out_m="$(mtime "$out_path")"; out_m=$((10#$out_m))
  if [[ "$out_m" -eq 0 ]]; then
    diverge "$label: NEVER RAN — no $out_path past the install grace — remedy: investigate why launchd isn't firing it"
  elif (( now - out_m > max_gap )); then
    diverge "$label: STALE — last execution evidence $((now - out_m))s ago (max ${max_gap}s) — remedy: investigate $out_path"
  fi
done <<< "$targets"

# ---- static hand-managed daemons (outside reconcile's managed set): liveness = pid present
# shellcheck disable=SC2043  # single-item list is intentional — the roster grows in place
for label in ai.myndaix.runtime; do
  declared="${declared}${label}"$'\n'
  if ! pr="$("$LCTL" print "$LA_DOMAIN/$label" 2>/dev/null)"; then
    diverge "$label: daemon NOT LOADED — remedy: launchctl bootstrap $LA_DOMAIN $LA_DIR/$label.plist"
  elif ! printf '%s\n' "$pr" | grep -qE '^[[:space:]]*pid = [0-9]+'; then
    diverge "$label: daemon loaded but NO RUNNING PID — remedy: launchctl kickstart -k $LA_DOMAIN/$label"
  fi
done

# ---- reverse sweep: loaded ai.myndaix.* minus declared minus static = rogue -------------
# Enumeration uses `launchctl list` (tab-delimited, stable); `print` output is never parsed
# for enumeration (brittle across macOS releases).
rogue_src="$("$LCTL" list 2>/dev/null | awk -F'\t' '$3 ~ /^ai\.myndaix\./ {print $3}' || true)"
while IFS= read -r label; do
  [[ -n "$label" ]] || continue
  [[ "$declared" == *$'\n'"$label"$'\n'* ]] && continue
  # Labels come from launchctl output — sanitize before embedding in the alert body.
  safe="$(printf '%s' "$label" | tr -cd 'A-Za-z0-9._-')"
  diverge "$safe: ROGUE — loaded in $LA_DOMAIN but not declared in substrate/plists/ — remedy: investigate, then launchctl bootout $LA_DOMAIN/$safe"
done <<< "$rogue_src"

# ---- streak + latch + alert (the drift-canary pattern, verbatim) ------------------------
if [[ "$div_n" -eq 0 ]]; then
  rm -f "$STREAK_FILE" "$ALERTED_FILE"
  log "liveness: all declared jobs alive"
  exit 0
fi

streak="$(cat "$STREAK_FILE" 2>/dev/null || echo 0)"
[[ "$streak" =~ ^[0-9]+$ ]] || streak=0
streak=$(( 10#$streak + 1 ))
# Explicit fail-closed write (a `printf > tmp && mv` &&-chain is exempt from set -e on the
# non-final link — the #89 class).
if ! { printf '%s\n' "$streak" > "$STREAK_FILE.tmp" && mv -f "$STREAK_FILE.tmp" "$STREAK_FILE"; }; then
  die "could not write liveness streak"
fi
log "liveness: $div_n divergence(s) (streak=$streak)"

if [[ "$streak" -ge "$THRESHOLD" && ! -e "$ALERTED_FILE" ]]; then
  msg="liveness-canary: declared-vs-runtime divergence persisting (${streak} checks). Jobs declared in substrate/plists/ are not executing as declared:

$div"
  if [[ -n "${OPERATOR_INBOX:-}" && -d "$OPERATOR_INBOX" ]]; then
    alert="$OPERATOR_INBOX/liveness-alert-$(date '+%Y%m%d%H%M%S').md"
    # Latch ONLY after the alert write succeeds — a failed write (disk full) must not
    # suppress all future alerts (drift-canary's exact rule).
    if { printf '%s\n' "$msg" > "$alert.tmp" && mv -f "$alert.tmp" "$alert"; }; then
      : > "$ALERTED_FILE"
      log "liveness: alert dropped -> $alert"
    else
      rm -f "$alert.tmp"
      log "liveness: FAILED to write alert to $alert — will retry next interval (not latched)"
    fi
  else
    # Do NOT latch: the alert was NOT delivered. Log-don't-latch; the next interval retries
    # delivery once the inbox returns, then latches on success (a lost alert must not be
    # suppressed forever).
    log "liveness: OPERATOR_INBOX unavailable (${OPERATOR_INBOX:-<unset>}) — alert not delivered:"$'\n'"$msg"
  fi
fi
exit 0
