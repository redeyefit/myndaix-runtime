#!/usr/bin/env bash
# deploy-sync.sh — deterministic sync + drift guard for the MANUALLY-COPIED, load-bearing
# orchestrator scripts that live OUTSIDE the repo (the autonomous-loop deploy surface).
#
# WHY: the autonomous review/fix loop runs two scripts from ~/.myndaix/orchestrator/ that are
# HAND-COPIED from the repo (a deliberate security boundary — play-fix.sh must be a real regular
# file the review worker can't hijack via a repo symlink). Nothing auto-syncs them, so a merged
# fix can silently NOT reach production. On 2026-07-24 PR#112 merged but the deployed play-fix.sh
# sat on the pre-flock version for 3 days, caught by luck not signal (see the runtime-deploy
# topology audit). This turns that silent, luck-dependent failure into a loud, deterministic one.
#
# Surface (audited — exactly these two files, everything else runs repo-direct):
#   ~/.myndaix/orchestrator/play-fix.sh      (autofix worker — anti-symlink security boundary)
#   ~/.myndaix/orchestrator/play-review.sh   (review worker, re-exec'd by the pre-push dispatcher)
#
# Modes:
#   --check       read-only: report SAME/DRIFT of each deployed copy vs <ref>; exit 1 on any drift.
#   --apply       sync each file from <ref> (atomic, backed up, hash-verified, regular-file-asserted);
#                 stamp ~/.myndaix/orchestrator/.deployed-sha with the deployed commit.
#   --preflight   ADVISORY (always exit 0): warn if deployed copies drift OR the repo working tree
#                 HEAD != the stamped deploy sha (branch-float). Safe to call at pool start.
#
#   deploy-sync.sh <mode> [ref]      ref defaults to origin/main
#
# House rules: bash-scripts.md — set -euo pipefail, PATH pin, atomic mv, quote all, no eval.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
DEST="${DEPLOY_SYNC_DEST:-$HOME/.myndaix/orchestrator}"   # overridable for tests
STAMP="$DEST/.deployed-sha"
FILES=(play-fix.sh play-review.sh)                        # the audited copied surface

mode="${1:-}"
ref="${2:-origin/main}"

log(){ printf '[%s] [deploy-sync] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die(){ log "ERROR: $*" >&2; exit 1; }

[[ -d "$REPO/.git" ]] || die "repo not a git dir: $REPO"
[[ -d "$DEST" ]]      || die "deploy dest missing: $DEST"

# blob sha of a path AT a git ref (the intended/committed content)
ref_blob(){ git -C "$REPO" rev-parse --verify --quiet "$ref:orchestrator/$1"; }
# blob sha of a file's CURRENT content on disk (git's own content hash — comparable to ref_blob)
disk_blob(){ git -C "$REPO" hash-object "$1" 2>/dev/null || echo "MISSING"; }

do_check(){
  local drift=0 f want have
  for f in "${FILES[@]}"; do
    want="$(ref_blob "$f")" || die "path not found at $ref: orchestrator/$f"
    have="$(disk_blob "$DEST/$f")"
    if [[ "$want" == "$have" ]]; then
      log "SAME  $f  ($want)"
    else
      log "DRIFT $f  deployed=$have  want($ref)=$want"; drift=1
    fi
  done
  return "$drift"
}

do_apply(){
  local f want tmp bak deployed_sha
  # refresh the tracking ref only when deploying from a remote (skip for local refs / tests)
  case "$ref" in origin/*) git -C "$REPO" fetch --quiet origin main 2>/dev/null || log "WARN: fetch failed — using local $ref" ;; esac
  deployed_sha="$(git -C "$REPO" rev-parse --verify "$ref")" || die "cannot resolve ref: $ref"
  for f in "${FILES[@]}"; do
    want="$(ref_blob "$f")" || die "path not found at $ref: orchestrator/$f"
    tmp="$DEST/.$f.tmp.$$"
    # materialize the EXACT committed blob (not the working tree — avoids dirty-tree bleed)
    git -C "$REPO" show "$ref:orchestrator/$f" > "$tmp" || { rm -f "$tmp"; die "git show failed for $f"; }
    chmod +x "$tmp"
    # verify BEFORE install — never place a mismatched file
    [[ "$(disk_blob "$tmp")" == "$want" ]] || { rm -f "$tmp"; die "hash mismatch building $f (refusing)"; }
    # backup any existing deployed copy (reversible)
    if [[ -e "$DEST/$f" ]]; then bak="$DEST/$f.bak-$(date '+%Y%m%d%H%M%S')"; cp -p "$DEST/$f" "$bak"; fi
    mv -f "$tmp" "$DEST/$f"                                # atomic rename (same fs)
    [[ ! -L "$DEST/$f" ]] || die "SECURITY: $f became a symlink — refusing"   # regular-file invariant
    [[ "$(disk_blob "$DEST/$f")" == "$want" ]] || die "post-install hash mismatch $f"
    log "APPLIED $f  ($want)"
  done
  printf '%s\n' "$deployed_sha" > "$STAMP"
  log "stamped .deployed-sha = $deployed_sha"
}

do_preflight(){
  # ADVISORY only — never fails the caller (pool start must not be bricked by this guard).
  local f want have head_sha stamped
  for f in "${FILES[@]}"; do
    want="$(ref_blob "$f" 2>/dev/null || echo '?')"; have="$(disk_blob "$DEST/$f")"
    [[ "$want" == "$have" ]] || log "PREFLIGHT WARN: deployed $f drifts from $ref (deployed=$have want=$want)"
  done
  if [[ -f "$STAMP" ]]; then
    stamped="$(cat "$STAMP" 2>/dev/null || true)"
    head_sha="$(git -C "$REPO" rev-parse --verify --quiet HEAD || echo unknown)"
    [[ "$head_sha" == "$stamped" ]] || \
      log "PREFLIGHT WARN: working-tree HEAD ($head_sha) != stamped deploy sha ($stamped) — pool may run un-deployed code (branch-float)"
  else
    log "PREFLIGHT WARN: no .deployed-sha stamp — run 'deploy-sync.sh --apply' to establish it"
  fi
  return 0
}

case "$mode" in
  --check)     do_check ;;
  --apply)     do_apply ;;
  --preflight) do_preflight ;;
  *) die "usage: deploy-sync.sh --check|--apply|--preflight [ref]" ;;
esac
