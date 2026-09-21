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

# canary_emit SFILE AFILE PREFIX LABEL BODY [THRESHOLD] — shared streak+latch+alert used by the
# config-drift watch, the (independent) liveness-execution watch, and the DEV-tree drift watch.
# Each passes its OWN streak+latch files so a standing latch on one NEVER suppresses the other: a
# QUARANTINED hold keeps config drift latched for days, and the execution watcher dying under it
# must STILL alert (deep-audit P2 — the mutual watch must not share fate with config drift). Bumps
# the streak; at THRESHOLD drops ONE alert + latches on success (fail-closed writes,
# latch-after-write). THRESHOLD defaults to the config-drift $THRESHOLD; the DEV-tree watch passes
# its own (larger) value so brief hand-edits get live-dev grace before an alert.
canary_emit() {
  local sfile="$1" afile="$2" prefix="$3" label="$4" body="$5" threshold="${6:-$THRESHOLD}" streak alert
  streak="$(cat "$sfile" 2>/dev/null || echo 0)"
  [[ "$streak" =~ ^[0-9]+$ ]] || streak=0
  streak=$(( 10#$streak + 1 ))
  # &&-chain is exempt from set -e on the non-final link — the #89 class; fail-closed write.
  if ! { printf '%s\n' "$streak" > "$sfile.tmp" && mv -f "$sfile.tmp" "$sfile"; }; then
    die "could not write $label streak"
  fi
  log "canary: $label (streak=$streak)"
  [[ "$streak" -ge "$threshold" && ! -e "$afile" ]] || return 0
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

# dev_tree_drift DIR — echo a one-line human reason if the DEV tree is drifted (off main / ahead of
# origin/main / dirty), or NOTHING if it is clean at origin/main. READ-ONLY: only git symbolic-ref /
# rev-list / status, never a mutation, and NO explicit fetch here — the surrounding reconcile
# --dry-run already refreshed the deploy clone's origin this run, and a second network fetch in a
# DIFFERENT repo could hang the canary; "ahead" is judged against DEV_TREE's LAST-KNOWN origin/main
# (a normal push updates that ref locally, so it is honest for the factory's own commits). Always
# returns 0 (the caller drives the alert). ALWAYS runs in the caller's $(...) subshell, so the
# GIT_* unset below is scoped and never leaks to the main canary.
#   - Validity requires `git rev-parse --is-inside-work-tree` to print exactly `true`, NOT just
#     exit 0 and NOT `[[ -d "$dir/.git" ]]`: a linked git WORKTREE stores .git as a FILE (the old
#     dir-test falsely called it "missing"), while a BARE repo prints `false` with EXIT 0 (a bare
#     dir would then swallow the later status error and read as clean — cross-family review R2
#     regression). A missing / non-git / bare dir is itself a drift reason.
#   - GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE unset so an inherited hook env can't redirect the probe
#     to a DIFFERENT repo (cross-family review). status is --untracked-files=all (a config
#     status.showUntrackedFiles=no would otherwise hide stray untracked runtime code) and
#     --no-optional-locks (strict read-only; never take the index lock); a status ERROR (nonzero
#     rc) is reported as drift, never silently read as clean (cross-family review #2).
dev_tree_drift() {
  local dir="$1" branch ahead behind st strc
  unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE
  [[ "$(git -C "$dir" rev-parse --is-inside-work-tree 2>/dev/null)" == "true" ]] \
    || { printf 'is missing, bare, or not a git work tree (%s)' "$dir"; return 0; }
  branch="$(git -C "$dir" symbolic-ref --quiet --short HEAD 2>/dev/null || echo '(detached HEAD)')"
  if [[ "$branch" != "main" ]]; then
    printf 'is on %s, not main' "$branch"; return 0
  fi
  # rev-list FAILURE (origin/main ref missing) must read as DRIFT, never as "0 ahead = clean"
  # (review R4 P2: `|| echo 0` swallowed an unverifiable state). Non-numeric output ⇒ drift reason.
  ahead="$(git -C "$dir" rev-list --count origin/main..HEAD 2>/dev/null || true)"
  if ! [[ "$ahead" =~ ^[0-9]+$ ]]; then
    printf 'has no verifiable origin/main (rev-list failed — remote ref missing or repo corrupt)'; return 0
  fi
  if (( 10#$ahead > 0 )); then
    printf 'is %s commit(s) ahead of origin/main (unpushed local commits)' "$ahead"; return 0
  fi
  # BEHIND origin/main is drift too: a clean checkout sitting on an OLDER commit of main (ahead=0)
  # still ships STALE code — the "clean but never pulled" gap the ahead-only check missed (review
  # 68864 P2: origin/main..HEAD counts only ahead, so a fetched-not-pulled tree read as clean).
  # origin/main is already known-resolvable here (the ahead rev-list above would have failed and
  # returned otherwise), so this only fails on real corruption; non-numeric output ⇒ drift reason.
  behind="$(git -C "$dir" rev-list --count HEAD..origin/main 2>/dev/null || true)"
  if ! [[ "$behind" =~ ^[0-9]+$ ]]; then
    printf 'has no verifiable origin/main (rev-list failed — remote ref missing or repo corrupt)'; return 0
  fi
  if (( 10#$behind > 0 )); then
    printf 'is %s commit(s) behind origin/main (stale — a clean tree that was never pulled)' "$behind"; return 0
  fi
  # `|| strc=$?` disarms this script's set -e AND records the real rc — a bare `; strc=$?` would
  # never run: under set -e a failing substitution-assignment kills the $() subshell mid-function,
  # aborting the whole canary tick instead of reporting drift (review R6, verified live-reachable).
  strc=0
  st="$(git -C "$dir" --no-optional-locks status --porcelain --untracked-files=all 2>/dev/null)" || strc=$?
  if [[ "$strc" -ne 0 ]]; then
    printf 'git status errored (rc=%s) — tree state unverifiable' "$strc"; return 0
  fi
  if [[ -n "$st" ]]; then
    printf 'has uncommitted changes (dirty working tree)'; return 0
  fi
  return 0   # clean at origin/main — no reason emitted
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

# ---- DEV-tree drift watch (INDEPENDENT streak+latch) --------------------------------------
# The factory runs mxr / controller / orchestrator scripts from the DEV_TREE checkout (NOT the
# pull-only deploy clone). A DEV_TREE off clean main / ahead of origin / long-dirty silently ships
# WRONG code to the factory (root cause of the 09-14 six-day drift) and fail-OPENs the librarian
# recall-gate that is pinned to a tree path. ALERT-ONLY: this NEVER touches the tree — the human
# converges it. Own streak+latch (a standing DEV-tree latch must not mute config drift or the
# liveness watch, and vice versa) + its OWN larger threshold (DEV_TREE_DRIFT_THRESHOLD): a brief
# hand-edit gets live-dev grace; only PERSISTENT drift alerts. Gated on DEV_TREE being configured —
# labs never set it (their working tree is dirty by design), and drift-canary is a factory-only tick
# anyway, so in practice this runs only on the Mini. Runs regardless of the config-drift outcome.
# SINGLE-INSTANCE INVARIANT (all streak/latch/identity read-modify-write in this script relies on
# it): drift-canary runs ONLY as the launchd job ai.myndaix.drift-canary — launchd never overlaps
# invocations of a label, so ticks are serialized. Do NOT run concurrent manual instances (a
# manual run while the tick is live can race the cat→rm→mv sequences and the fixed .tmp names;
# reviews 73734/23749/34704 flag these — wontfix BECAUSE of this invariant, matching canary_emit's
# established fixed-suffix pattern). A one-off manual run while the launchd job is unloaded is fine.
DT_STREAK_FILE="$STATE_DIR/dev-tree-streak"
DT_ALERTED_FILE="$STATE_DIR/dev-tree-alerted"
DT_WATCHED_FILE="$STATE_DIR/dev-tree-watched"   # records WHICH tree the streak/latch belong to
if [[ -n "${DEV_TREE:-}" ]]; then
  # Bind the streak/latch to the watched checkout's identity: if DEV_TREE was RETARGETED (or the
  # watch was just re-enabled), a stale latch from the PREVIOUS tree must not suppress the new
  # tree's first alert (cross-family review). Reset state whenever the recorded path differs.
  # ORDER + die are load-bearing: CLEAR FIRST, COMMIT IDENTITY LAST — the identity file is the
  # "reset complete" marker, so ANY interruption (kill, failed write) leaves it mismatched and the
  # next run retries the whole reset (rm -f is idempotent). Both steps die LOUD on failure (like
  # canary_emit's streak write): warn-and-continue silently suppressed alerts while ticking exit-0
  # (review 2164 #3), and the write-identity-FIRST variant tore the other way — identity committed
  # + old latch left behind = the new tree's alerts suppressed under a matching identity (review
  # 23749: never commit the marker before the state it vouches for is actually clean).
  if [[ "$(cat "$DT_WATCHED_FILE" 2>/dev/null || true)" != "$DEV_TREE" ]]; then
    rm -f "$DT_STREAK_FILE" "$DT_ALERTED_FILE" \
      || die "could not clear dev-tree streak/latch for retarget"
    { printf '%s\n' "$DEV_TREE" > "$DT_WATCHED_FILE.tmp" && mv -f "$DT_WATCHED_FILE.tmp" "$DT_WATCHED_FILE"; } \
      || die "could not record dev-tree identity ($DT_WATCHED_FILE)"
  fi
  dt_reason="$(dev_tree_drift "$DEV_TREE")"
  if [[ -n "$dt_reason" ]]; then
    canary_emit "$DT_STREAK_FILE" "$DT_ALERTED_FILE" "dev-tree-alert" "DEV-tree DRIFT" \
      "drift-canary DEV-tree watch: $DEV_TREE $dt_reason. The factory runs mxr / controller / orchestrator scripts from this checkout — while it is off clean main the factory may ship WRONG code and the librarian recall-gate fail-OPENs. Converge it by hand: cd $DEV_TREE && git status; then restore a clean main at origin (git stash / commit+push, or git checkout main && git pull --ff-only). This alert is loud-only — nothing here mutates the tree." \
      "${DEV_TREE_DRIFT_THRESHOLD:-5}"
  else
    # die, not WARN: a failing clear here leaves a stale latch that would mute this SAME tree's
    # next drift, and the identity matches so nothing ever retries it — same silent-suppression
    # class as the retarget path (review 23749 series). Before dying, POISON the identity file
    # (best-effort rm): a matching identity is what would skip recovery, so breaking it forces the
    # next tick through the retarget reset, which retries the clear with its own die-loud path
    # (review 34704 P1: die alone left latch+identity intact across a transient failure — perms
    # recover, same tree re-drifts, alert muted while ticks exit 0). ACCEPTED RESIDUAL / WONTFIX
    # (reviews 68864/13355/36499/62436): if the WHOLE state/ dir loses write perm the poison rm ALSO
    # fails, so a matching identity + stale latch can mute the SAME tree's next episode AFTER perms
    # recover. But every tick DIES LOUD the entire time state/ is unwritable (liveness-canary sees the
    # sick tick), so the operator is alerted then; only a post-recovery re-drift before a clean tick
    # is the gap. A RESET_REQUIRED-sentinel fix was built and deliberately REVERTED: it requires a
    # chmod of the factory's state dir (never observed on a single-operator box) and each hardening
    # layer spawned a narrower edge case — an infinite fold on a never-fired path, not worth the
    # recovery-code complexity. Do NOT re-raise without a real-world trigger.
    rm -f "$DT_STREAK_FILE" "$DT_ALERTED_FILE" || {
      rm -f "$DT_WATCHED_FILE" 2>/dev/null || true
      die "could not clear dev-tree streak/latch (identity poisoned for retarget-reset recovery)"
    }
  fi
else
  # Watch DISABLED (DEV_TREE unset): clear any stale streak/latch/identity so a LATER re-enable
  # starts fresh — a standing latch from a prior watched tree would otherwise suppress the
  # re-enabled watch's first alert (cross-family review: the disable path must not preserve state).
  rm -f "$DT_STREAK_FILE" "$DT_ALERTED_FILE" "$DT_WATCHED_FILE" \
    || log "canary: WARN could not clear disabled dev-tree state"
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
