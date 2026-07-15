#!/usr/bin/env bash
# bootstrap-fetch — the STATIC Stage-0 fetcher (design §2.2). Canonical source lives here
# in the repo; the RUNNING copy is installed to $MYNDAIX_HOME/bin/bootstrap-fetch by
# `reconcile.sh --update-bootstrap` and is NEVER auto-overwritten by a reconcile. That
# separation is the whole point: a broken reconcile.sh (or config_parse.py) landing in
# origin/main can't brick the fetch — this file always resets the fix in.
#
# SELF-CONTAINED on purpose: it does NOT source lib.sh or run the clone's Python (both
# could be the very files a bad commit broke). It reads only DEPLOY_CLONE from config
# with a minimal, non-executing grep, hard-validates it, resets it to origin/main, then
# re-execs the (now-fresh) reconcile exactly once via the RECONCILE_BOOTSTRAPPED guard.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

log() { printf '[%s] [bootstrap-fetch] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die() { log "ALARM: $*" >&2; exit 1; }

# Re-exec loop guard: if we're already inside a bootstrapped invocation, something is
# wrong — never fetch+reset+re-exec a second time.
[[ -n "${RECONCILE_BOOTSTRAPPED:-}" ]] && die "RECONCILE_BOOTSTRAPPED already set — refusing to re-bootstrap"

MYNDAIX_HOME="${MYNDAIX_HOME:-$HOME/.myndaix}"
CONFIG_FILE="$MYNDAIX_HOME/config.env"
[[ -f "$CONFIG_FILE" ]] || die "no config.env at $CONFIG_FILE"

# Minimal, NON-executing extraction of exactly two keys. `cut`/`tr` only — never source.
read_key() {
  local key="$1" line val
  line="$(grep -E "^${key}=" "$CONFIG_FILE" | head -1 || true)"
  val="${line#*=}"
  # strip one layer of matching quotes
  val="${val%\"}"; val="${val#\"}"
  val="${val%\'}"; val="${val#\'}"
  printf '%s' "$val"
}

MACHINE_ROLE="$(read_key MACHINE_ROLE)"
DEPLOY_CLONE="$(read_key DEPLOY_CLONE)"
[[ -z "$DEPLOY_CLONE" ]] && DEPLOY_CLONE="$MYNDAIX_HOME/deploy/myndaix-runtime"

# Only the FACTORY auto-fetches+resets. LAB never runs this (its reconcile poll is not
# installed); a stray invocation on LAB must not reset a dev checkout.
[[ "$MACHINE_ROLE" == "factory" ]] || die "bootstrap-fetch only runs on a factory machine (role=$MACHINE_ROLE)"

# Hard-validate DEPLOY_CLONE before ANY reset — this is the path we `reset --hard`.
case "$DEPLOY_CLONE" in
  /*) : ;;                                   # absolute
  *)  die "DEPLOY_CLONE not absolute: $DEPLOY_CLONE" ;;
esac
[[ "$DEPLOY_CLONE" == *".."* ]] && die "DEPLOY_CLONE contains '..': $DEPLOY_CLONE"
[[ -d "$DEPLOY_CLONE/.git" ]] || die "DEPLOY_CLONE is not a git repo: $DEPLOY_CLONE"
top="$(git -C "$DEPLOY_CLONE" rev-parse --show-toplevel 2>/dev/null || true)"
real_deploy="$(cd "$DEPLOY_CLONE" && pwd)"
[[ "$top" == "$real_deploy" ]] || die "DEPLOY_CLONE is not a git worktree TOPLEVEL ($DEPLOY_CLONE -> $top)"

# QUIESCE-BRACKETS-THE-RESET (design §2.3, risk #2). Under Option A launchd runs the tick
# scripts DIRECTLY from the clone; a `reset --hard` rewrites those files in place (git
# working-tree writes are NOT atomic), so a mutating tick mid-read could execute a
# half-written script. Bootout the mutating ticks and PROVE they are gone BEFORE the reset.
# EXPLICIT allowlist (risk #1) — never a wildcard. The read-only drift-canary is deliberately
# NOT quiesced (a transient half-read just makes it re-run; keeping it up preserves the alarm).
# NOTE: this hardcoded list is asserted == the mutating-tick descriptors by substrate/test.sh.
QUIESCE_LABELS=(ai.myndaix.controller ai.myndaix.automerge ai.myndaix.fix-sweep)
DOMAIN="gui/$(id -u)"
LA_DIR="$HOME/Library/LaunchAgents"

# If we abort AFTER quiescing but BEFORE handing off to reconcile, RESTORE the ticks so a
# failed converge never leaves autonomy silently down (the drift-canary still shouts either way).
restore_ticks() {
  local l
  for l in "${QUIESCE_LABELS[@]}"; do
    if [[ -f "$LA_DIR/$l.plist" ]]; then launchctl bootstrap "$DOMAIN" "$LA_DIR/$l.plist" 2>/dev/null || true; fi
  done
}
trap 'restore_ticks' EXIT

for label in "${QUIESCE_LABELS[@]}"; do
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
done

# Wait — FAIL CLOSED — for each label to actually be gone (bootout can return before the process
# exits). Escalate SIGKILL after 30s; if a tick is STILL up, REFUSE the reset. Rewriting a live
# tick's scripts is the exact race the quiesce exists to prevent, so "couldn't prove it exited"
# must abort (retry next poll), never proceed. Matches lib.sh la_wait_gone + reconcile die-on-timeout.
for label in "${QUIESCE_LABELS[@]}"; do
  deadline=$(( $(date +%s) + 30 )); killed=0
  while launchctl print "$DOMAIN/$label" >/dev/null 2>&1; do
    if [[ $(date +%s) -ge $deadline ]]; then
      if [[ "$killed" == 0 ]]; then
        log "WARN: $label still up after 30s — SIGKILL escalate"
        launchctl kill -9 "$DOMAIN/$label" 2>/dev/null || true
        killed=1; deadline=$(( $(date +%s) + 10 ))
      else
        die "quiesce FAILED: $label still up after SIGKILL — refusing reset (would rewrite a live tick's scripts)"
      fi
    fi
    sleep 1
  done
done

# Abandoned-worker guard (H2): the controller detaches (abandon_process_group) a play-review/
# play-fix worker that outlives the tick. If one is running FROM the deploy clone (Option-A
# topology), reset --hard would rewrite its scripts mid-execution — refuse. (Latent until
# PLAY_SELF is injected; under the current topology the worker runs from $ORCH, outside the reset
# target — but fail-closed regardless.)
if pgrep -f "$real_deploy/orchestrator/play-" >/dev/null 2>&1; then
  die "quiesce FAILED: a worker is still running from the deploy clone — refusing reset (retry next poll)"
fi

log "fetching origin/main into $real_deploy"
# One retry absorbs a transient refs/remotes/origin/main.lock race with the un-quiesced canary's
# fetch (fetch-vs-fetch); a persistent failure aborts (fail-closed) and retries next poll.
git -C "$real_deploy" fetch --no-tags --prune origin '+refs/heads/main:refs/remotes/origin/main' \
  || { log "fetch failed — one retry"; sleep 2; git -C "$real_deploy" fetch --no-tags --prune origin '+refs/heads/main:refs/remotes/origin/main'; } \
  || die "git fetch failed twice (retry next poll)"
git -C "$real_deploy" reset --hard refs/remotes/origin/main
log "reset to $(git -C "$real_deploy" rev-parse --short HEAD)"

RECONCILE="$real_deploy/substrate/reconcile.sh"
[[ -f "$RECONCILE" ]] || die "reconcile.sh missing after reset: $RECONCILE"

# Hand off — reconcile owns restarting the ticks (its step 6). Clear our restore trap so the
# ticks aren't double-managed across the exec (exec replaces this process; EXIT would not fire
# anyway, but be explicit).
trap - EXIT
export RECONCILE_BOOTSTRAPPED=1
export MYNDAIX_HOME
log "re-exec reconcile (bootstrapped)"
exec /bin/bash "$RECONCILE" "$@"
