#!/usr/bin/env bash
# reconcile.sh — the GitOps pull-reconcile converger (design §2.2).
#
#   reconcile.sh                 converge this machine to origin/main (factory; via Stage 0)
#   reconcile.sh --dry-run       NON-destructive drift report (any machine); exit 1 on drift
#   reconcile.sh --update-bootstrap   (re)install the static $MYNDAIX_HOME/bin/bootstrap-fetch
#
# The converge path is reached only AFTER bootstrap-fetch has fetched+reset the deploy
# clone and re-exec'd us with RECONCILE_BOOTSTRAPPED=1 (so we run the fresh code against
# the fresh schema). --dry-run NEVER triggers Stage 0 (no reset).
set -euo pipefail
SUBSTRATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=substrate/lib.sh
source "$SUBSTRATE_DIR/lib.sh"

MODE=converge
case "${1:-}" in
  --dry-run)          MODE=dry-run ;;
  --update-bootstrap) MODE=update-bootstrap ;;
  "")                 MODE=converge ;;
  *)                  die "unknown arg: ${1:-} (use --dry-run | --update-bootstrap)" ;;
esac

# ---- --update-bootstrap: install the STATIC fetcher (explicit, human-approved) ----------
if [[ "$MODE" == update-bootstrap ]]; then
  substrate_resolve_home
  mkdir -p "$MYNDAIX_HOME/bin"
  tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
  cp "$SUBSTRATE_DIR/bootstrap-fetch.sh" "$tmp"
  atomic_install "$tmp" "$MYNDAIX_HOME/bin/bootstrap-fetch" 0755
  trap - EXIT
  log "installed static bootstrap-fetch -> $MYNDAIX_HOME/bin/bootstrap-fetch"
  exit 0
fi

# ---- STAGE 0: static bootstrap dispatch (converge only; dry-run never resets) ------------
if [[ "$MODE" == converge && -z "${RECONCILE_BOOTSTRAPPED:-}" ]]; then
  substrate_resolve_home
  BF="$MYNDAIX_HOME/bin/bootstrap-fetch"
  [[ -x "$BF" ]] || die "static bootstrap-fetch missing ($BF) — run: reconcile.sh --update-bootstrap"
  log "dispatching Stage-0 bootstrap-fetch"
  exec /bin/bash "$BF"
fi

substrate_load_config
STATE_DIR="$MYNDAIX_HOME/state"
mkdir -p "$STATE_DIR"

fetch_origin() {
  git -C "$DEPLOY_CLONE" fetch --no-tags --prune origin '+refs/heads/main:refs/remotes/origin/main'
}

# ---- --dry-run: the real drift detector (design §2.6) -----------------------------------
if [[ "$MODE" == dry-run ]]; then
  fetch_origin 2>/dev/null || log "WARN: fetch failed — drift computed vs last-known origin"
  set +e
  report="$(python3 "$SUBSTRATE_DIR/manifest.py" check "$CONFIG_FILE" 2>&1)"; rc=$?
  set -e
  printf '%s\n' "$report"
  porcelain="$(git -C "$DEPLOY_CLONE" status --porcelain || true)"
  if [[ -n "$porcelain" ]]; then
    log "DRIFT: working tree not clean"; printf '%s\n' "$porcelain"; rc=1
  fi
  if [[ "$rc" -ne 0 ]]; then log "DRIFT detected"; exit 1; fi
  log "no drift"; exit 0
fi

# =========================================================================================
# CONVERGE (factory only; reached bootstrapped, so the tree is already reset to origin/main)
# =========================================================================================
[[ "$MACHINE_ROLE" == "factory" ]] || die "converge is factory-only (role=$MACHINE_ROLE); LAB uses --dry-run"
substrate_assert_deploy_clone

# 1. tree guard — post-reset the tree MUST be clean; a dirty tree is a loud hand-edit signal.
porcelain="$(git -C "$DEPLOY_CLONE" status --porcelain || true)"
[[ -n "$porcelain" ]] && log "WARN: deploy clone not clean after reset (hand-edit?):"$'\n'"$porcelain"
log "converging to $(git -C "$DEPLOY_CLONE" rev-parse --short HEAD)"

# 2. dep-sync — pip install only when pyproject.toml changed (venv is in-tree for PR-1).
VENV="$DEPLOY_CLONE/.venv"
if [[ ! -x "$VENV/bin/pip" ]]; then
  log "creating venv $VENV"
  python3 -m venv "$VENV" || die "venv creation failed"
fi
cur_dep="$(shasum -a 256 "$DEPLOY_CLONE/pyproject.toml" | cut -d' ' -f1)"
dep_rec="$STATE_DIR/venv_source.sha"
if [[ ! -f "$dep_rec" || "$(cat "$dep_rec" 2>/dev/null || true)" != "$cur_dep" ]]; then
  log "deps changed — pip install"
  "$VENV/bin/pip" install -q -e "$DEPLOY_CLONE" || die "pip install failed"
  printf '%s\n' "$cur_dep" > "$dep_rec.tmp" && mv -f "$dep_rec.tmp" "$dep_rec"
fi

# 3. install artifacts ATOMICALLY — render each role-matching plist, plutil-lint, mv into place.
LA_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$LA_DIR"
ROLE_LABELS=()
for desc in "$SUBSTRATE_DIR"/plists/*.json; do
  label="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["label"])' "$desc")"
  if ! python3 "$SUBSTRATE_DIR/render_plist.py" role-check "$desc" "$MACHINE_ROLE"; then
    continue
  fi
  ROLE_LABELS+=("$label")
  tmp="$(mktemp)"
  if ! python3 "$SUBSTRATE_DIR/render_plist.py" render "$desc" "$CONFIG_FILE" > "$tmp" 2>>"$STATE_DIR/reconcile.err"; then
    rm -f "$tmp"; die "render failed for $label (see $STATE_DIR/reconcile.err)"
  fi
  plutil -lint "$tmp" >/dev/null 2>&1 || { rm -f "$tmp"; die "rendered plist for $label failed plutil -lint"; }
  atomic_install "$tmp" "$LA_DIR/$label.plist" 0644
  log "installed plist: $label"
done

# 4. RESTART serve (sole migration owner). Serve's plist is hand-managed in PR-1 (ownership
#    deferred), so we kickstart the EXISTING label — no bootout/bootstrap of the pool here.
if la_loaded ai.myndaix.runtime; then
  log "kickstarting serve (ai.myndaix.runtime) to apply migrations"
  la_kickstart ai.myndaix.runtime
else
  die "serve (ai.myndaix.runtime) is not loaded — it is hand-managed in PR-1; load it before converging"
fi

# 5. WAIT until serve is healthy AND the migration head object exists (fail-closed on timeout).
HEAD_OBJ="$(cat "$SUBSTRATE_DIR/migration_head.txt")"
[[ -n "$HEAD_OBJ" ]] || die "migration_head.txt is empty"
deadline=$(( $(date +%s) + 120 ))
until launchctl print "$LA_DOMAIN/ai.myndaix.runtime" 2>/dev/null | grep -qE 'state = running' \
   && [[ "$(psql "$MYNDAIX_DSN" -tAc "SELECT to_regclass('public.$HEAD_OBJ') IS NOT NULL" 2>/dev/null | tr -d '[:space:]')" == "t" ]]; do
  if [[ $(date +%s) -ge $deadline ]]; then
    die "serve not healthy / migration head '$HEAD_OBJ' not applied within 120s"
  fi
  sleep 3
done
log "serve healthy; migration head '$HEAD_OBJ' present"

# 6. START the ticks (now new-code-against-new-schema). bootstrap-fetch quiesced the mutating
#    ticks; bring them + the canary up on the fresh plists. NEVER bootout ourselves (the
#    reconcile-poll label runs THIS process).
SELF_LABEL="ai.myndaix.reconcile"
for label in "${ROLE_LABELS[@]}"; do
  [[ "$label" == "ai.myndaix.runtime" ]] && continue   # not managed here
  if [[ "$label" == "$SELF_LABEL" ]]; then
    la_loaded "$label" || la_bootstrap "$LA_DIR/$label.plist"   # never bootout self
    continue
  fi
  la_bootout "$label"
  la_bootstrap "$LA_DIR/$label.plist"
  log "started tick: $label"
done

# 7. VERIFY (post-restart): manifest clean + tree clean, else ALARM.
set +e
verify="$(python3 "$SUBSTRATE_DIR/manifest.py" check "$CONFIG_FILE" 2>&1)"; vrc=$?
set -e
porcelain="$(git -C "$DEPLOY_CLONE" status --porcelain || true)"
if [[ "$vrc" -ne 0 || -n "$porcelain" ]]; then
  printf '%s\n' "$verify"
  [[ -n "$porcelain" ]] && printf 'tree dirty:\n%s\n' "$porcelain"
  die "post-converge verify FAILED — manifest/tree drift remains"
fi

# 8. COMMIT POINT — write RUNNING_SHA + the manifest receipt LAST (atomic).
sha="$(git -C "$DEPLOY_CLONE" rev-parse HEAD)"
printf '%s\n' "$sha" > "$STATE_DIR/RUNNING_SHA.tmp" && mv -f "$STATE_DIR/RUNNING_SHA.tmp" "$STATE_DIR/RUNNING_SHA"
python3 "$SUBSTRATE_DIR/manifest.py" build "$CONFIG_FILE" > "$STATE_DIR/manifest.json.tmp" \
  && mv -f "$STATE_DIR/manifest.json.tmp" "$STATE_DIR/manifest.json"
log "CONVERGED at ${sha:0:8} — receipt written"

# Non-blocking canary (design Q1): a green mxr proves end-to-end drain, but a quota-drained
# pool is a FALSE health signal, so this NEVER fails the converge — warn only.
if command -v mxr >/dev/null 2>&1; then
  if MXR_TIMEOUT_S=60 mxr recon "reply READY" >/dev/null 2>&1; then
    log "canary: pool drained end-to-end"
  else
    log "WARN: canary did not confirm (pool busy / quota) — schema+health already verified"
  fi
fi
