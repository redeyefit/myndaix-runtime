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

# 1. tree guard — FAIL CLOSED on a dirty tree (cross-family review BLOCKER). bootstrap-fetch did
#    reset --hard + clean -ffd, so any remaining tracked-change OR untracked file is a hand-edit /
#    tamper signal — and rendering/installing plists from a dirty tree could ship an unreviewed
#    descriptor. A `git status` ERROR (corrupt repo / stale lock) must ALSO fail closed — not read as
#    clean via `|| true` (cross-family review MAJOR). `--porcelain` includes untracked (not .venv).
if ! porcelain="$(git -C "$DEPLOY_CLONE" status --porcelain)"; then die "git status failed — unknown tree state"; fi
[[ -z "$porcelain" ]] || die "deploy clone NOT clean after reset+clean — refusing to converge:"$'\n'"$porcelain"
log "converging to $(git -C "$DEPLOY_CLONE" rev-parse --short HEAD)"

# 2. dep-sync — pip install when pyproject.toml changed OR the venv was just (re)created / is invalid.
#    A missing/corrupt .venv at an UNCHANGED pyproject would otherwise never be repaired: the
#    venv_source.sha still matches so the install is skipped (cross-family review MAJOR). Force it.
VENV="$DEPLOY_CLONE/.venv"
need_install=0
if [[ ! -x "$VENV/bin/pip" ]]; then
  log "creating venv $VENV"
  python3 -m venv "$VENV" || die "venv creation failed"
  need_install=1                      # a fresh (or repaired) venv has no packages yet
fi
cur_dep="$(shasum -a 256 "$DEPLOY_CLONE/pyproject.toml" | cut -d' ' -f1)"
dep_rec="$STATE_DIR/venv_source.sha"
[[ ! -f "$dep_rec" || "$(cat "$dep_rec" 2>/dev/null || true)" != "$cur_dep" ]] && need_install=1
if [[ "$need_install" == 1 ]]; then
  log "installing deps into venv"
  "$VENV/bin/pip" install -q -e "$DEPLOY_CLONE" || die "pip install failed"
  if ! { printf '%s\n' "$cur_dep" > "$dep_rec.tmp" && mv -f "$dep_rec.tmp" "$dep_rec"; }; then
    die "could not write venv_source.sha"
  fi
fi

# 3. install artifacts ATOMICALLY — render each role-matching plist, plutil-lint, mv into place.
mkdir -p "$LA_DIR"
ROLE_LABELS=()
for desc in "$SUBSTRATE_DIR"/plists/*.json; do
  # Distinguish role-check rc1 (does not apply to this role — skip) from rc2 (BROKEN descriptor).
  # Conflating them would silently drop a malformed descriptor from ROLE_LABELS, then orphan-prune
  # its label (cross-family review MAJOR). rc2 must fail closed.
  set +e; python3 "$SUBSTRATE_DIR/render_plist.py" role-check "$desc" "$MACHINE_ROLE"; rcheck=$?; set -e
  case "$rcheck" in
    0) : ;;                                                       # applies to this role
    1) continue ;;                                                # legitimately not this role
    *) die "descriptor error (role-check rc=$rcheck): $desc" ;;   # broken descriptor — fail closed
  esac
  label="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["label"])' "$desc")" \
    || die "cannot read label from $desc"
  ROLE_LABELS+=("$label")
  tmp="$(mktemp)"
  if ! python3 "$SUBSTRATE_DIR/render_plist.py" render "$desc" "$CONFIG_FILE" > "$tmp" 2>>"$STATE_DIR/reconcile.err"; then
    rm -f "$tmp"; die "render failed for $label (see $STATE_DIR/reconcile.err)"
  fi
  plutil -lint "$tmp" >/dev/null 2>&1 || { rm -f "$tmp"; die "rendered plist for $label failed plutil -lint"; }
  atomic_install "$tmp" "$LA_DIR/$label.plist" 0644
  log "installed plist: $label"
done

# 3.5 ORPHAN PRUNE (cross-family review CRITICAL): a label reconcile previously managed but no longer
#     in ROLE_LABELS (descriptor removed / role changed) must be torn down + its plist removed, else
#     it runs forever + resurrects on reboot, invisible to drift. SCOPED to state/managed_labels (what
#     reconcile ITSELF installed) — NEVER a wildcard over ai.myndaix.* (that would tear down the
#     unrelated jobs: audio-player, deadman, model-watch, … — the risk-#1 catastrophe).
MANAGED_REC="$STATE_DIR/managed_labels"
if [[ -f "$MANAGED_REC" ]]; then
  while IFS= read -r prev; do
    [[ -n "$prev" ]] || continue
    [[ "$prev" == "ai.myndaix.runtime" || "$prev" == "ai.myndaix.reconcile" ]] && continue
    still=0; for l in "${ROLE_LABELS[@]}"; do [[ "$l" == "$prev" ]] && { still=1; break; }; done
    if [[ "$still" == 0 ]]; then
      log "orphan prune: $prev no longer managed — bootout + remove plist"
      la_bootout "$prev"
      rm -f "$LA_DIR/$prev.plist"
    fi
  done < "$MANAGED_REC"
fi

# 4. RESTART serve (sole migration owner). Serve's plist is hand-managed in PR-1 (ownership
#    deferred), so we kickstart the EXISTING label — no bootout/bootstrap of the pool here.
if la_loaded ai.myndaix.runtime; then
  log "kickstarting serve (ai.myndaix.runtime) to apply migrations"
  la_kickstart ai.myndaix.runtime
else
  die "serve (ai.myndaix.runtime) is not loaded — it is hand-managed in PR-1; load it before converging"
fi

# 5. WAIT until serve is healthy AND migrated (fail-closed on timeout). Health = the pool runs
#    with a STABLE pid AND the migration head object exists. pid-stability is load-bearing: a
#    `state = running` snapshot is true at PID-spawn (before serve's async migrate()), and
#    to_regclass() is satisfied by a head object PERSISTED from a prior boot — so new code whose
#    migrate() crash-loops (respawning with a NEW pid each time) could transiently pass both. A
#    stable pid across a poll interval rules the crash-loop out; a single transient restart (the
#    known Mini asyncpg blip) still stabilizes on the recovered pid. (Adversarial review MED.)
HEAD_OBJ="$(cat "$SUBSTRATE_DIR/migration_head.txt")"
[[ -n "$HEAD_OBJ" ]] || die "migration_head.txt is empty"
# HEAD_OBJ is interpolated into SQL — require a STRICT unquoted-identifier so a malformed pin can't
# inject a false-green expression (cross-family review MAJOR). The probe runs ON_ERROR_STOP with a
# bounded statement_timeout so a hang/error can't stall the converge or read as satisfied.
[[ "$HEAD_OBJ" =~ ^[A-Za-z_][A-Za-z0-9_]{0,62}$ ]] || die "migration_head.txt not a plain identifier: '$HEAD_OBJ'"
deadline=$(( $(date +%s) + 120 )); prev_pid=""
while :; do
  pid="$(launchctl print "$LA_DOMAIN/ai.myndaix.runtime" 2>/dev/null | awk -F'= ' '/^[[:space:]]*pid =/{gsub(/[^0-9]/,"",$2); print $2; exit}')"
  head_ok="$(PGOPTIONS='-c statement_timeout=10s' psql "$MYNDAIX_DSN" -v ON_ERROR_STOP=1 -tAc "SELECT to_regclass('public.$HEAD_OBJ') IS NOT NULL" 2>/dev/null | tr -d '[:space:]')"
  [[ -n "$pid" && "$pid" == "$prev_pid" && "$head_ok" == "t" ]] && break
  prev_pid="$pid"
  [[ $(date +%s) -ge $deadline ]] && die "serve not healthy/stable / migration head '$HEAD_OBJ' not applied within 120s"
  sleep 3
done
log "serve healthy (pid $pid stable); migration head '$HEAD_OBJ' present"

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
  la_wait_gone "$label" 10 || true          # let the old job fully exit before re-bootstrap (avoid EBUSY)
  # one retry absorbs a transient launchctl EBUSY so a blip can't abort the converge mid-loop
  la_bootstrap "$LA_DIR/$label.plist" || { sleep 2; la_bootstrap "$LA_DIR/$label.plist"; } \
    || die "could not bootstrap $label"
  log "started tick: $label"
done
trap - EXIT   # ticks are back up — clear the autonomy-halt restore trap

# 7. VERIFY (post-restart): manifest clean + tree clean, else ALARM.
set +e
verify="$(python3 "$SUBSTRATE_DIR/manifest.py" check "$CONFIG_FILE" 2>&1)"; vrc=$?
set -e
if ! porcelain="$(git -C "$DEPLOY_CLONE" status --porcelain)"; then die "post-converge: git status failed — unknown tree state"; fi
if [[ "$vrc" -ne 0 || -n "$porcelain" ]]; then
  printf '%s\n' "$verify"
  [[ -n "$porcelain" ]] && printf 'tree dirty:\n%s\n' "$porcelain"
  die "post-converge verify FAILED — manifest/tree drift remains"
fi

# 8. COMMIT POINT — write the manifest receipt FIRST, then RUNNING_SHA as the FINAL commit marker
#    (cross-family review MAJOR: RUNNING_SHA is what --only-if-changed + drift trust as "fully
#    converged", so a manifest-build failure must NOT leave RUNNING_SHA claiming success).
sha="$(git -C "$DEPLOY_CLONE" rev-parse HEAD)"
# Record the labels reconcile now manages (drives the NEXT converge's orphan prune + manifest orphan
# detection). Written before RUNNING_SHA so a converge that reaches the commit point has an accurate set.
# Each write is an explicit if-block: `printf > tmp && mv` under set -e exempts the non-final && link,
# so a printf failure (disk full) would silently skip mv and proceed with stale state (the #89 class,
# cross-family review MAJOR). Fail closed on each.
if ! { printf '%s\n' "${ROLE_LABELS[@]}" > "$STATE_DIR/managed_labels.tmp" && mv -f "$STATE_DIR/managed_labels.tmp" "$STATE_DIR/managed_labels"; }; then
  die "failed to write managed_labels (converge incomplete, retry next poll)"
fi
if ! { python3 "$SUBSTRATE_DIR/manifest.py" build "$CONFIG_FILE" > "$STATE_DIR/manifest.json.tmp" && mv -f "$STATE_DIR/manifest.json.tmp" "$STATE_DIR/manifest.json"; }; then
  die "manifest build failed — NOT writing RUNNING_SHA (converge incomplete, retry next poll)"
fi
if ! { printf '%s\n' "$sha" > "$STATE_DIR/RUNNING_SHA.tmp" && mv -f "$STATE_DIR/RUNNING_SHA.tmp" "$STATE_DIR/RUNNING_SHA"; }; then
  die "failed to write RUNNING_SHA (converge incomplete, retry next poll)"
fi
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
