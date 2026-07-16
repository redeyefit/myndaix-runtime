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

# Autonomy-halt guard (cross-family review MAJOR): in a BOOTSTRAPPED converge, bootstrap-fetch has
# already quiesced the mutating ticks and cleared its own restore trap. Arm OUR restore trap NOW —
# BEFORE config-load / deploy-clone-assert — so a failure in that early window can't leave autonomy
# silently down. The restore uses only fixed labels + LA_DIR (no config), so it's valid this early.
# Cleared after step 6 restarts the ticks. Must match bootstrap-fetch QUIESCE_LABELS (test.sh asserts).
MUTATING_TICKS=(ai.myndaix.controller ai.myndaix.automerge ai.myndaix.fix-sweep)
LA_DIR="$HOME/Library/LaunchAgents"
reconcile_restore_ticks() {
  local l
  for l in "${MUTATING_TICKS[@]}"; do
    if [[ -f "$LA_DIR/$l.plist" ]]; then launchctl bootstrap "$LA_DOMAIN" "$LA_DIR/$l.plist" 2>/dev/null || true; fi
  done
}
[[ "$MODE" == converge && -n "${RECONCILE_BOOTSTRAPPED:-}" ]] && trap 'reconcile_restore_ticks' EXIT

substrate_load_config
STATE_DIR="$MYNDAIX_HOME/state"
mkdir -p "$STATE_DIR"

fetch_origin() {
  git -C "$DEPLOY_CLONE" fetch --no-tags --prune origin '+refs/heads/main:refs/remotes/origin/main'
}

# ---- --dry-run: the real drift detector (design §2.6) -----------------------------------
if [[ "$MODE" == dry-run ]]; then
  # A fetch failure means we CANNOT know the remote state — that is drift/UNKNOWN, never "clean"
  # (cross-family review MAJOR: "ambiguity => drift"). Comparing against a stale local ref would
  # false-green a machine that has fallen behind an unreachable origin.
  fetch_origin 2>/dev/null || { log "DRIFT: fetch failed — cannot determine remote state (UNKNOWN)"; exit 1; }
  set +e
  report="$(python3 "$SUBSTRATE_DIR/manifest.py" check "$CONFIG_FILE" 2>&1)"; rc=$?
  set -e
  printf '%s\n' "$report"
  if ! porcelain="$(git -C "$DEPLOY_CLONE" status --porcelain)"; then
    log "DRIFT: git status failed — unknown tree state"; exit 1
  fi
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
substrate_assert_deploy_clone   # restore trap already armed above (bootstrapped converge)

# Capture the LAST-GOOD SHA before any mutation — RUNNING_SHA is written last, so it only ever names
# a fully-converged SHA. This is what auto-revert (§2.8) reverts to on a health-gate failure.
PREV_GOOD="$(cat "$STATE_DIR/RUNNING_SHA" 2>/dev/null || echo none)"
HEAD_SHA="$(git -C "$DEPLOY_CLONE" rev-parse HEAD)"
prev_recoverable() { [[ "$PREV_GOOD" != none ]] && git -C "$DEPLOY_CLONE" cat-file -e "${PREV_GOOD}^{commit}" 2>/dev/null; }

# MIGRATION ADDITIVITY LINT on the prev_good..HEAD DELTA (auto-revert precondition, §2.8): a
# non-additive migration in this deploy would make a code-revert-against-the-applied-schema unsafe,
# so refuse to auto-deploy it (a contraction is a deliberate human-gated two-release change). Only
# the delta is linted — historical migrations may contain legitimate one-time contractions.
if prev_recoverable; then
  new_migs=()
  while IFS= read -r m; do [[ -n "$m" ]] && new_migs+=("$m"); done \
    < <(git -C "$DEPLOY_CLONE" diff --name-only --diff-filter=A "${PREV_GOOD}..HEAD" -- 'src/runtime/ledger/migrations/*.sql')
  if [[ ${#new_migs[@]} -gt 0 ]]; then
    ( cd "$DEPLOY_CLONE" && python3 "$SUBSTRATE_DIR/migration_lint.py" "${new_migs[@]}" ) \
      || die "NON-ADDITIVE migration in ${PREV_GOOD:0:8}..HEAD — refusing unattended auto-deploy (§2.8)"
  fi
fi

# --- install_artifacts: tree guard + dep-sync + render/install role plists + orphan prune. -------
# Re-runnable (called again after an auto-revert reset). Sets the global ROLE_LABELS.
install_artifacts() {
  local porcelain
  # 1. tree guard — FAIL CLOSED on a dirty/untracked tree (a git ERROR also fails closed, not `|| true`).
  if ! porcelain="$(git -C "$DEPLOY_CLONE" status --porcelain)"; then die "git status failed — unknown tree state"; fi
  [[ -z "$porcelain" ]] || die "deploy clone NOT clean — refusing to converge:"$'\n'"$porcelain"
  log "converging to $(git -C "$DEPLOY_CLONE" rev-parse --short HEAD)"

  # 2. dep-sync — install when pyproject changed OR the venv is fresh/invalid (repair a deleted venv).
  local VENV cur_dep dep_rec need_install=0
  VENV="$DEPLOY_CLONE/.venv"
  if [[ ! -x "$VENV/bin/pip" ]]; then log "creating venv $VENV"; python3 -m venv "$VENV" || die "venv creation failed"; need_install=1; fi
  cur_dep="$(shasum -a 256 "$DEPLOY_CLONE/pyproject.toml" | cut -d' ' -f1)"
  dep_rec="$STATE_DIR/venv_source.sha"
  [[ ! -f "$dep_rec" || "$(cat "$dep_rec" 2>/dev/null || true)" != "$cur_dep" ]] && need_install=1
  if [[ "$need_install" == 1 ]]; then
    log "installing deps into venv"
    "$VENV/bin/pip" install -q -e "$DEPLOY_CLONE" || die "pip install failed"
    { printf '%s\n' "$cur_dep" > "$dep_rec.tmp" && mv -f "$dep_rec.tmp" "$dep_rec"; } || die "could not write venv_source.sha"
  fi

  # 3. render + install role plists atomically. A SENTINEL-GATED job (the reconcile poll) installs
  #    ONLY when its sentinel exists — so unattended auto-deploy is an explicit opt-in (§2.8).
  mkdir -p "$LA_DIR"
  ROLE_LABELS=()
  local desc rcheck label sentinel tmp
  for desc in "$SUBSTRATE_DIR"/plists/*.json; do
    set +e; python3 "$SUBSTRATE_DIR/render_plist.py" role-check "$desc" "$MACHINE_ROLE"; rcheck=$?; set -e
    case "$rcheck" in
      0) : ;;
      1) continue ;;                                                # not this role
      *) die "descriptor error (role-check rc=$rcheck): $desc" ;;   # broken descriptor — fail closed
    esac
    label="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["label"])' "$desc")" || die "cannot read label from $desc"
    sentinel="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("requires_sentinel",""))' "$desc")" || die "cannot read $desc"
    if [[ -n "$sentinel" && ! -e "$MYNDAIX_HOME/$sentinel" ]]; then
      log "skip $label — sentinel '$sentinel' not set (unarmed)"; continue
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

  # 3.5 ORPHAN PRUNE — a previously-managed label no longer in ROLE_LABELS (descriptor removed / role
  #     changed / sentinel disarmed) is booted out + its plist removed. SCOPED to state/managed_labels
  #     (what reconcile itself installed) — NEVER a wildcard over ai.myndaix.* (risk-#1 catastrophe).
  local MANAGED_REC prev still l
  MANAGED_REC="$STATE_DIR/managed_labels"
  if [[ -f "$MANAGED_REC" ]]; then
    while IFS= read -r prev; do
      [[ -n "$prev" ]] || continue
      [[ "$prev" == "ai.myndaix.runtime" ]] && continue
      still=0; for l in "${ROLE_LABELS[@]}"; do [[ "$l" == "$prev" ]] && { still=1; break; }; done
      if [[ "$still" == 0 ]]; then
        log "orphan prune: $prev no longer managed — bootout + remove plist"
        # never bootout the poll label we may be running under; just remove its file if disarmed
        [[ "$prev" == "ai.myndaix.reconcile" ]] || la_bootout "$prev"
        rm -f "$LA_DIR/$prev.plist"
      fi
    done < "$MANAGED_REC"
  fi
}

# --- health_gate: restart serve + wait healthy+migrated + start ticks + verify. Returns 0/1 (NO die)
# so the caller can auto-revert. Reads migration_head.txt from the CURRENT tree each call (so after a
# revert it checks the reverted SHA's head object). psql CONNECT is bounded by PGCONNECT_TIMEOUT (lib.sh)
# so a flaky loopback fails-fast and the deadline logic actually fires (found live on the Mini cutover).
health_gate() {
  local HEAD_OBJ deadline prev_pid pid head_ok label vrc verify porcelain
  HEAD_OBJ="$(cat "$SUBSTRATE_DIR/migration_head.txt")"
  [[ "$HEAD_OBJ" =~ ^[A-Za-z_][A-Za-z0-9_]{0,62}$ ]] || die "migration_head.txt not a plain identifier: '$HEAD_OBJ'"
  # 4. restart serve (sole migration owner; hand-managed plist — kickstart the existing label)
  la_loaded ai.myndaix.runtime || { log "serve (ai.myndaix.runtime) not loaded"; return 1; }
  log "kickstarting serve to apply migrations"; la_kickstart ai.myndaix.runtime || { log "serve kickstart failed"; return 1; }
  # 5. WAIT for a STABLE serve pid AND the migration head object (pid-stability rules out a crash-loop).
  deadline=$(( $(date +%s) + 150 )); prev_pid=""
  while :; do
    pid="$(launchctl print "$LA_DOMAIN/ai.myndaix.runtime" 2>/dev/null | awk -F'= ' '/^[[:space:]]*pid =/{gsub(/[^0-9]/,"",$2); print $2; exit}')"
    head_ok="$(PGOPTIONS='-c statement_timeout=10s' psql "$MYNDAIX_DSN" -v ON_ERROR_STOP=1 -tAc "SELECT to_regclass('public.$HEAD_OBJ') IS NOT NULL" 2>/dev/null | tr -d '[:space:]')"
    [[ -n "$pid" && "$pid" == "$prev_pid" && "$head_ok" == "t" ]] && break
    prev_pid="$pid"
    [[ $(date +%s) -ge $deadline ]] && { log "health WAIT timeout: serve not stable / head '$HEAD_OBJ' absent (150s)"; return 1; }
    sleep 3
  done
  log "serve healthy (pid $pid stable); migration head '$HEAD_OBJ' present"
  # 6. start the ticks (never bootout the reconcile-poll self label)
  for label in "${ROLE_LABELS[@]}"; do
    [[ "$label" == "ai.myndaix.runtime" ]] && continue
    if [[ "$label" == "ai.myndaix.reconcile" ]]; then la_loaded "$label" || la_bootstrap "$LA_DIR/$label.plist"; continue; fi
    la_bootout "$label"; la_wait_gone "$label" 10 || true
    la_bootstrap "$LA_DIR/$label.plist" || { sleep 2; la_bootstrap "$LA_DIR/$label.plist"; } || { log "could not bootstrap $label"; return 1; }
    log "started tick: $label"
  done
  # 7. verify manifest + tree clean
  set +e; verify="$(python3 "$SUBSTRATE_DIR/manifest.py" check "$CONFIG_FILE" 2>&1)"; vrc=$?; set -e
  if ! porcelain="$(git -C "$DEPLOY_CLONE" status --porcelain)"; then log "verify: git status failed"; return 1; fi
  if [[ "$vrc" -ne 0 || -n "$porcelain" ]]; then
    printf '%s\n' "$verify"; [[ -n "$porcelain" ]] && printf 'tree dirty:\n%s\n' "$porcelain"
    log "post-converge verify FAILED — manifest/tree drift"; return 1
  fi
  return 0
}

# ---- converge with AUTO-REVERT (§2.8) ---------------------------------------------------------
install_artifacts
reverted=0
if health_gate; then
  converged_sha="$HEAD_SHA"
else
  log "HEALTH-GATE FAILED at ${HEAD_SHA:0:8}"
  if prev_recoverable; then
    log "AUTO-REVERT to last-good ${PREV_GOOD:0:8} (§2.8) — FACTORY must not stay on broken code"
    git -C "$DEPLOY_CLONE" reset --hard "$PREV_GOOD" && git -C "$DEPLOY_CLONE" clean -ffd || die "auto-revert reset failed — human needed"
    install_artifacts
    if health_gate; then
      converged_sha="$PREV_GOOD"; reverted=1
    else
      die "AUTO-REVERT to ${PREV_GOOD:0:8} ALSO failed — FACTORY needs a human (two-fault, serve down)"
    fi
  else
    die "health-gate failed and no recoverable last-good SHA to revert to (first converge?) — human needed"
  fi
fi
trap - EXIT   # converged (possibly reverted) — clear the autonomy-halt restore trap

# 8. COMMIT POINT — manifest receipt FIRST, then managed_labels, then RUNNING_SHA LAST (the last-good
#    marker). Each write is an explicit fail-closed if-block (a `printf > tmp && mv` &&-chain is exempt
#    from set -e on the non-final link — the #89 class).
if ! { printf '%s\n' "${ROLE_LABELS[@]}" > "$STATE_DIR/managed_labels.tmp" && mv -f "$STATE_DIR/managed_labels.tmp" "$STATE_DIR/managed_labels"; }; then
  die "failed to write managed_labels (converge incomplete, retry next poll)"
fi
if ! { python3 "$SUBSTRATE_DIR/manifest.py" build "$CONFIG_FILE" > "$STATE_DIR/manifest.json.tmp" && mv -f "$STATE_DIR/manifest.json.tmp" "$STATE_DIR/manifest.json"; }; then
  die "manifest build failed — NOT writing RUNNING_SHA (converge incomplete, retry next poll)"
fi
if ! { printf '%s\n' "$converged_sha" > "$STATE_DIR/RUNNING_SHA.tmp" && mv -f "$STATE_DIR/RUNNING_SHA.tmp" "$STATE_DIR/RUNNING_SHA"; }; then
  die "failed to write RUNNING_SHA (converge incomplete, retry next poll)"
fi
log "CONVERGED at ${converged_sha:0:8} — receipt written"

# Loud ALARM on an auto-revert: FACTORY is back on known-good code, but the bad SHA needs a human.
if [[ "$reverted" == 1 ]]; then
  log "ALARM: AUTO-REVERTED ${HEAD_SHA:0:8} -> ${converged_sha:0:8} (bad SHA failed the health gate) — investigate"
  if [[ -n "${OPERATOR_INBOX:-}" && -d "$OPERATOR_INBOX" ]]; then
    alert="$OPERATOR_INBOX/reconcile-revert-$(date '+%Y%m%d%H%M%S').md"
    { printf 'reconcile AUTO-REVERTED FACTORY from %s to %s: the new SHA failed the post-restart health gate.\nFACTORY is back on known-good code. Investigate the bad SHA before re-deploying.\n' "$HEAD_SHA" "$converged_sha" > "$alert.tmp" && mv -f "$alert.tmp" "$alert"; } || rm -f "$alert.tmp"
  fi
fi

# Non-blocking canary: a green mxr proves end-to-end drain, but a quota-drained pool is a FALSE
# health signal, so this NEVER fails the converge — warn only.
if command -v mxr >/dev/null 2>&1; then
  if MXR_TIMEOUT_S=60 mxr recon "reply READY" >/dev/null 2>&1; then
    log "canary: pool drained end-to-end"
  else
    log "WARN: canary did not confirm (pool busy / quota) — schema+health already verified"
  fi
fi
