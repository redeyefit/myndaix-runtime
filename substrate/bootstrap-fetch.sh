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
# half-written script. Bootout the mutating ticks and wait for them to actually exit
# BEFORE the reset. This is the EXPLICIT allowlist (risk #1) — never a wildcard. The
# read-only drift-canary is deliberately NOT quiesced (a transient half-read just makes it
# re-run; keeping it up preserves the smoke alarm if this converge later fails).
QUIESCE_LABELS=(ai.myndaix.controller ai.myndaix.automerge ai.myndaix.fix-sweep)
DOMAIN="gui/$(id -u)"
for label in "${QUIESCE_LABELS[@]}"; do
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
done
# Wait (bounded) for each to actually be gone — bootout can return before the process dies.
for label in "${QUIESCE_LABELS[@]}"; do
  deadline=$(( $(date +%s) + 30 ))
  while launchctl print "$DOMAIN/$label" >/dev/null 2>&1; do
    [[ $(date +%s) -ge $deadline ]] && { log "WARN: $label still up after 30s; proceeding"; break; }
    sleep 1
  done
done

log "fetching origin/main into $real_deploy"
git -C "$real_deploy" fetch --no-tags --prune origin '+refs/heads/main:refs/remotes/origin/main'
git -C "$real_deploy" reset --hard refs/remotes/origin/main
log "reset to $(git -C "$real_deploy" rev-parse --short HEAD)"

RECONCILE="$real_deploy/substrate/reconcile.sh"
[[ -x "$RECONCILE" ]] || [[ -f "$RECONCILE" ]] || die "reconcile.sh missing after reset: $RECONCILE"

export RECONCILE_BOOTSTRAPPED=1
export MYNDAIX_HOME
log "re-exec reconcile (bootstrapped)"
exec /bin/bash "$RECONCILE" "$@"
