#!/usr/bin/env bash
# skip-audit.sh — READ-ONLY report on what the push-review loop still owes you.
#
# play-review.sh records an unreviewed range as a `skipped-<slug>-<KEY>` file whose CONTENT is the
# range's base. Those files are the ONLY record that a range was never reviewed, and nothing ever
# reported on them: the backlog grew to 119 markers unseen, including 2 that pointed at a stale
# local main and 21 stranded in a namespace no push will ever walk. This closes that blind spot —
# it is the green/red check for the review loop's own coverage.
#
# NEVER mutates state. It classifies and reports; retiring a marker is a human decision, because
# deleting one silently asserts "this range WAS reviewed".
#
# Exit: 0 clean · 1 actionable findings · 2 usage/environment error.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

STATE="${PLAY_STATE:-$HOME/.myndaix/orchestrator/state}"
ROOTS="${SKIP_AUDIT_ROOTS:-$HOME/code/active}"
# Both mirror play-review.sh. They are re-declared, not sourced, because play-review.sh executes a
# review on load; if either drifts there, this report silently lies about prune and fold headroom.
PRUNE_DAYS="${PLAY_PRUNE_DAYS:-14}"      # play-review.sh:67 — the find -delete that reaps $STATE
FOLD_MAX_HOPS=10                         # play-review.sh:156 — fold_walk's `_hops -lt 10` bound
WARN_DAYS=3                              # how much runway before a prune counts as imminent

# Validated one by one rather than in an eval loop: `eval` is banned outright by rules/bash-scripts.md,
# and a loop saves nothing over three lines. 10# forces base 10 — $(( )) reads a leading zero as OCTAL,
# so an exported PLAY_PRUNE_DAYS=010 would silently mean 8 days of retention, not 10.
[[ "$PRUNE_DAYS" =~ ^[0-9]+$ ]] || { printf 'skip-audit: PLAY_PRUNE_DAYS must be numeric\n' >&2; exit 2; }
PRUNE_DAYS=$((10#$PRUNE_DAYS))
[[ "$PRUNE_DAYS" -gt "$WARN_DAYS" ]] || WARN_DAYS=0

[[ -d "$STATE" ]] || { printf 'skip-audit: no state dir at %s\n' "$STATE" >&2; exit 2; }
command -v git >/dev/null 2>&1 || { printf 'skip-audit: git not on PATH\n' >&2; exit 2; }

tmp="$(mktemp -d)" || { printf 'skip-audit: cannot create scratch\n' >&2; exit 2; }
trap 'rm -rf "$tmp"' EXIT INT TERM

# --- repo map: slug -> path, kind ------------------------------------------------------------
# play-review.sh derives its slug from `basename "$repo"` where $repo is `--show-toplevel`. In a
# linked worktree that is the WORKTREE's dirname, not the repo's — which is exactly how markers end
# up stranded. So the map deliberately records worktrees too, and marks them, so the report can
# name the strand instead of reporting an unresolvable slug.
map="$tmp/repos.tsv"; : > "$map"
# shellcheck disable=SC2086  # unquoted $ROOTS is INTENTIONAL: it is a space-separated root list
for root in $ROOTS; do
  for d in "$root"/*; do
    [[ -e "$d/.git" ]] || continue       # -e not -d: a linked worktree's .git is a FILE
    printf '%s\t%s\tmain\n' "$(basename "$d")" "$d" >> "$map"
    # `worktree list` prints the main worktree first; drop it, it is already recorded above
    git -C "$d" worktree list --porcelain 2>/dev/null | awk '/^worktree /{print $2}' | tail -n +2 \
      | while read -r wt; do
          printf '%s\t%s\tworktree:%s\n' "$(basename "$wt")" "$wt" "$(basename "$d")" >> "$map"
        done
  done
done

# Two single-field lookups rather than one row split on $IFS: a repo path containing a space would
# corrupt a `read`-based split, and silently mis-resolving a repo makes every downstream git query lie.
lookup_path(){ awk -F'\t' -v s="$1" '$1==s {print $2; exit}' "$map"; }
lookup_kind(){ awk -F'\t' -v s="$1" '$1==s {print $3; exit}' "$map"; }

# "$1^{commit}" is quoted: unquoted, bash treats ^{...} as literal-brace syntax (shellcheck SC1083)
# and the peel silently stops applying, so an annotated tag would compare as the wrong object.
trunk_of(){
  git -C "$1" rev-parse --verify --quiet "refs/remotes/origin/main^{commit}" \
    || git -C "$1" rev-parse --verify --quiet "refs/heads/main^{commit}" || true
}

anc(){ git -C "$1" merge-base --is-ancestor "$2" "$3" 2>/dev/null; }

# --- classify every marker --------------------------------------------------------------------
# row = class \t repo_slug \t slug \t marker \t content \t age_days \t extra
rows="$tmp/rows.tsv"; : > "$rows"
now="$(date +%s)"
for f in "$STATE"/skipped-*; do
  [[ -f "$f" ]] || continue                      # the unmatched glob itself when there are none
  name="$(basename "$f")"
  rest="${name#skipped-}"
  key="${rest##*-}"                              # a sha contains no '-', so this is the trailing sha
  if [[ ! "$key" =~ ^[0-9a-f]{40}$ ]]; then
    printf 'UNPARSEABLE\t-\t-\t%s\t-\t0\t-\n' "$name" >> "$rows"; continue
  fi
  slug="${rest%-$key}"
  repo_slug="${slug%%-refs-heads-*}"
  content="$(head -c 64 "$f" 2>/dev/null | tr -d '\r\n' || true)"
  age_d=$(( (now - $(stat -f %m "$f")) / 86400 ))

  path="$(lookup_path "$repo_slug")"
  if [[ -z "$path" ]]; then
    printf 'ORPHAN_SLUG\t%s\t%s\t%s\t%s\t%s\t-\n' "$repo_slug" "$slug" "$name" "$content" "$age_d" >> "$rows"; continue
  fi
  kind="$(lookup_kind "$repo_slug")"
  if [[ "$kind" == worktree:* ]]; then
    # a push from here recorded coverage under the WORKTREE's name; a later push of the same branch
    # from the main repo derives a different slug and never finds it -> the range is lost
    printf 'WORKTREE_STRAND\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$repo_slug" "$slug" "$name" "$content" "$age_d" "${kind#worktree:}" >> "$rows"; continue
  fi
  if [[ ! "$content" =~ ^[0-9a-f]{40}$ ]]; then
    printf 'CORRUPT\t%s\t%s\t%s\t%s\t%s\t-\n' "$repo_slug" "$slug" "$name" "$content" "$age_d" >> "$rows"; continue
  fi
  if ! git -C "$path" cat-file -e "${key}^{commit}" 2>/dev/null \
     || ! git -C "$path" cat-file -e "${content}^{commit}" 2>/dev/null; then
    printf 'OBJECTS_GONE\t%s\t%s\t%s\t%s\t%s\t-\n' "$repo_slug" "$slug" "$name" "$content" "$age_d" >> "$rows"; continue
  fi
  trunk="$(trunk_of "$path")"
  if [[ -z "$trunk" ]]; then
    printf 'NO_TRUNK\t%s\t%s\t%s\t%s\t%s\t-\n' "$repo_slug" "$slug" "$name" "$content" "$age_d" >> "$rows"; continue
  fi
  if anc "$path" "$key" "$trunk"; then
    # the whole skipped range is inside the trunk; it was reviewed on its way in. Retirable.
    printf 'MERGED\t%s\t%s\t%s\t%s\t%s\t-\n' "$repo_slug" "$slug" "$name" "$content" "$age_d" >> "$rows"; continue
  fi
  if anc "$path" "$content" "$trunk" && anc "$path" "$trunk" "$key"; then
    # base predates the trunk while the trunk sits inside the range: the range re-covers merged
    # commits, inflating the diff until it blows the cap and the review aborts entirely
    n="$(git -C "$path" rev-list --count "${content}..${trunk}" 2>/dev/null || true)"
    [[ "$n" =~ ^[0-9]+$ ]] || n=0
    if [[ "$((10#$n))" -gt 0 ]]; then
      printf 'PHANTOM\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$repo_slug" "$slug" "$name" "$content" "$age_d" "$((10#$n))" >> "$rows"; continue
    fi
  fi
  printf 'BACKLOG\t%s\t%s\t%s\t%s\t%s\t-\n' "$repo_slug" "$slug" "$name" "$content" "$age_d" >> "$rows"
done

total="$(wc -l < "$rows" | tr -d ' ')"
printf '=== skip-audit: %s markers in %s ===\n\n' "$total" "$STATE"
printf 'by class\n'
awk -F'\t' '{c[$1]++} END {for (k in c) printf "  %-16s %d\n", k, c[k]}' "$rows" | sort -k2 -rn
printf '\n'

findings=0

deep="$(awk -F'\t' -v lim="$FOLD_MAX_HOPS" \
  '$1!="UNPARSEABLE" {c[$3]++} END {for (k in c) if (c[k] > lim) printf "  %-52s %d markers\n", k, c[k]}' "$rows" | sort -k2 -rn)"
if [[ -n "$deep" ]]; then
  printf 'CHAIN DEEPER THAN fold_walk (bound %s hops) — the tail can never fold in:\n%s\n\n' "$FOLD_MAX_HOPS" "$deep"
  findings=1
fi

imminent="$(awk -F'\t' -v lim="$((PRUNE_DAYS - WARN_DAYS))" \
  '($1=="BACKLOG" || $1=="PHANTOM") && $6+0 >= lim {printf "  %-3sd  %-8s %s\n", $6, $1, $3}' "$rows" | sort -rn)"
if [[ -n "$imminent" ]]; then
  printf 'PRUNE IMMINENT (find -delete at %sd) — deleting these asserts they were REVIEWED:\n%s\n\n' "$PRUNE_DAYS" "$imminent"
  findings=1
fi

ph="$(awk -F'\t' '$1=="PHANTOM" {printf "  %s\n      swallows %s merged commit(s); base=%.12s\n", $3, $7, $5}' "$rows")"
if [[ -n "$ph" ]]; then
  printf 'PHANTOM BASE — range re-covers merged commits, so the review aborts over-cap:\n%s\n\n' "$ph"
  findings=1
fi

ws="$(awk -F'\t' '$1=="WORKTREE_STRAND" {printf "  %-44s (worktree of %s)\n", $2, $7}' "$rows" | sort -u)"
if [[ -n "$ws" ]]; then
  printf 'WORKTREE STRAND — play-review.sh keys state on basename(toplevel), so a push from a\n'
  printf 'linked worktree files its coverage under the WORKTREE name. A later push of the same\n'
  printf 'branch from the main repo derives a different slug and never finds it: the range is LOST.\n'
  printf 'The same basename also scopes the per-repo LOCK and the DAILY_CAP counter.\n%s\n\n' "$ws"
  findings=1
fi

orph="$(awk -F'\t' '$1=="ORPHAN_SLUG" {print "  " $2}' "$rows" | sort -u)"
if [[ -n "$orph" ]]; then
  printf 'ORPHAN SLUG — no repo or worktree on disk resolves this; unwalkable, awaiting prune:\n%s\n\n' "$orph"
fi

mg="$(awk -F'\t' '$1=="MERGED" {c++} END {print c+0}' "$rows")"
if [[ "$mg" -gt 0 ]]; then
  printf 'RETIRABLE — %s marker(s) whose tip is already in the trunk (reviewed on the way in).\n\n' "$mg"
fi

if [[ "$findings" -eq 0 ]]; then printf 'clean — nothing actionable.\n'; fi
exit "$findings"
