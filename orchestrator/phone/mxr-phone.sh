#!/bin/bash
# mxr-phone — the phone's ONLY door into the factory: an SSH forced-command wrapper
# exposing exactly four verbs (status | ask | get | reel) over the tailnet.
# Design: docs/phone-tailnet-surface-design.md (v0.2, cross-family review-hardened).
#
# Installed BY HAND to ~/.myndaix/bin/mxr-phone on the Mini (joins the hand-copied set;
# deploy-sync covers only the two orchestrator scripts). Wired via authorized_keys:
#   restrict,from="<phone-ts-ip>",command="/Users/jefe/.myndaix/bin/mxr-phone" ssh-ed25519 ...
#
# TRUST MODEL (design §3, stated honestly): this is a COMMAND-RESTRICTION boundary, not
# privilege isolation — it runs as the operator via sshd. The payload in
# SSH_ORIGINAL_COMMAND is UNTRUSTED dictated text: it is grammar-checked, byte-capped,
# control-rejected, and passed as ONE argv after `--`; it never touches eval, a subshell,
# or word-splitting. Every anomaly denies (exit 2) with a one-line reason on stdout
# (that's what the Shortcut displays).
set -euo pipefail

# ---- 0. ENV SCRUB FIRST (design B2): forced commands still inherit server env + shell
# startup effects. Kill the influence channels, then set truth with absolute paths.
unset BASH_ENV ENV CDPATH PYTHONSTARTUP PYTHONPATH NODE_OPTIONS RUBYOPT PERL5LIB \
      GIT_EXTERNAL_DIFF LD_PRELOAD DYLD_INSERT_LIBRARIES 2>/dev/null || true
for _v in $(compgen -v | grep '^MXR_' || true); do unset "$_v" 2>/dev/null || true; done
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# The live corpus is $HOME-rooted on every machine (the 2026-09-03 symlink decision):
export MYNDAIX_KNOWLEDGE_SCOPES="fitness=$HOME/fitness,company=$HOME/company"

# Test seams (MXRPHONE_* deliberately survives the ^MXR_ scrub above; sshd AcceptEnv is
# empty — asserted by test.sh --sshd — so a CLIENT can never set these).
MXR_BIN="${MXRPHONE_MXR:-$HOME/.local/bin/mxr}"
STATE="${MXRPHONE_STATE:-$HOME/.myndaix/state}"
CAP_ASK="${MXRPHONE_CAP_ASK:-50}";  CAP_GET="${MXRPHONE_CAP_GET:-100}"
CAP_REEL="${MXRPHONE_CAP_REEL:-5}"; CAP_STATUS="${MXRPHONE_CAP_STATUS:-60}"
for _c in CAP_ASK CAP_GET CAP_REEL CAP_STATUS; do
  [[ "${!_c}" =~ ^[0-9]+$ ]] || printf -v "$_c" '%s' 0   # non-numeric cap = fail closed
  printf -v "$_c" '%s' "$((10#${!_c}))"                  # octal-trap normalization
done
LOG="$STATE/mxr-phone.log"
MAX_PAYLOAD=2000
MAX_OUT=4096

mkdir -p "$STATE" 2>/dev/null || { printf 'denied: state dir unavailable\n'; exit 2; }

# ---- logging (design M2): sha-only, 0600, self-rotated; log-write failure = DENY ----
_logwrite(){ # _logwrite <line> ; rc!=0 on failure
  if [ ! -e "$LOG" ]; then ( umask 077; : > "$LOG" ) 2>/dev/null || return 1; fi
  printf '%s\n' "$1" >> "$LOG" 2>/dev/null || return 1
  # rotate: keep last 1000 lines (bounded disk, no logrotate dependency)
  if [ "$(wc -l < "$LOG" 2>/dev/null || echo 0)" -gt 1000 ]; then
    local _t; _t="$(mktemp "$STATE/.phlog.XXXXXX" 2>/dev/null)" || return 0
    tail -n 1000 "$LOG" > "$_t" 2>/dev/null && chmod 600 "$_t" 2>/dev/null && mv -f "$_t" "$LOG" 2>/dev/null || rm -f "$_t" 2>/dev/null || true
  fi
  return 0
}
log_or_deny(){ _logwrite "[$(date '+%Y-%m-%d %H:%M:%S')] $1" || { printf 'denied: logging unavailable\n'; exit 2; }; }

deny(){ log_or_deny "DENY $1"; printf 'denied: %s\n' "$2"; exit 2; }

# ---- output escape + answer-first truncation (design M1/L2) ----
emit(){ # stdin -> stdout, control/ANSI-stripped (keep \t\n), capped at MAX_OUT bytes
  local body
  body="$(LC_ALL=C tr -d '\000-\010\013\014\016-\037\177' | head -c $((MAX_OUT + 1)))"
  if [ "$(printf '%s' "$body" | wc -c)" -gt "$MAX_OUT" ]; then
    printf '%s' "$body" | head -c "$MAX_OUT"; printf '\n…[truncated]\n'
  else
    printf '%s\n' "$body"
  fi
}

# ---- bounded exec (design H6): perl alarm+exec — macOS has no timeout(1); the alarm
# kills the CHILD itself (one process, no orphans) — the cap_run house pattern. ----
command -v perl >/dev/null 2>&1 || { printf 'denied: perl missing (timeout guard unavailable)\n'; exit 2; }
run_bounded(){ perl -e 'alarm shift; exec @ARGV or exit 127' "$@"; }

# ---- global concurrency cap 2 (design H5): mkdir slots, pid-owned, stale-reaped ----
CONC=""
release_conc(){ if [ -n "$CONC" ] && [ "$(cat "$CONC/pid" 2>/dev/null || echo none)" = "$$" ]; then rm -rf "$CONC" 2>/dev/null; fi; return 0; }
trap 'release_conc' EXIT INT TERM
conc_acquire(){
  local d mt now
  for s in 1 2; do
    d="$STATE/.phone-conc$s"
    if mkdir "$d" 2>/dev/null; then printf '%s' "$$" > "$d/pid" 2>/dev/null || true; CONC="$d"; return 0; fi
    now="$(date +%s)"; mt="$(stat -f %m "$d" 2>/dev/null || echo "$now")"
    if [ $((now - mt)) -gt 300 ]; then rm -rf "$d" 2>/dev/null || true; if mkdir "$d" 2>/dev/null; then printf '%s' "$$" > "$d/pid" 2>/dev/null || true; CONC="$d"; return 0; fi; fi
  done
  return 1
}

# ---- per-verb caps (design H5): mkdir-locked check+increment BEFORE dispatch ----
cap_take(){ # cap_take <verb> <limit> <day|hour> ; rc!=0 = over cap (or lock starvation: fail closed)
  local verb="$1" limit="$2" window="$3" stamp cf lockd n i _t now mt
  case "$window" in hour) stamp="$(date +%Y%m%d%H)";; *) stamp="$(date +%Y%m%d)";; esac
  cf="$STATE/.phonecap-$verb-$stamp"; lockd="$STATE/.phonecap.lock"
  for i in $(seq 1 40); do
    if mkdir "$lockd" 2>/dev/null; then break; fi
    now="$(date +%s)"; mt="$(stat -f %m "$lockd" 2>/dev/null || echo "$now")"
    if [ $((now - mt)) -gt 60 ]; then rm -rf "$lockd" 2>/dev/null || true; continue; fi
    sleep 0.05
    [ "$i" -eq 40 ] && return 1
  done
  n="$(cat "$cf" 2>/dev/null || echo 0)"; [[ "$n" =~ ^[0-9]+$ ]] || n=0; n=$((10#$n))
  if [ "$n" -ge "$limit" ]; then rmdir "$lockd" 2>/dev/null || true; return 1; fi
  _t="$(mktemp "$STATE/.phcap.XXXXXX" 2>/dev/null)" || { rmdir "$lockd" 2>/dev/null || true; return 1; }
  printf '%s' "$((n + 1))" > "$_t" 2>/dev/null && mv -f "$_t" "$cf" 2>/dev/null || { rm -f "$_t" 2>/dev/null || true; rmdir "$lockd" 2>/dev/null || true; return 1; }
  rmdir "$lockd" 2>/dev/null || true
  return 0
}

# ---- 1. GRAMMAR (design §4): parse SSH_ORIGINAL_COMMAND; deny-by-default ----
cmd="${SSH_ORIGINAL_COMMAND:-}"
[ -n "$cmd" ] || deny "empty" "no command (use: status | ask <scope> <question> | get <job-id> | reel <topic>)"
if printf '%s' "$cmd" | LC_ALL=C grep -q '[[:cntrl:]]'; then deny "cntrl" "control characters are not accepted"; fi
verb="${cmd%% *}"
rest=""; [ "$cmd" != "$verb" ] && rest="${cmd#"$verb" }"

payload=""; scope=""; jid=""
validate_payload(){ # sets/validates $payload from $1 (VERBATIM — no word-splitting, no eval)
  payload="$1"
  [ -n "$payload" ] || deny "empty-payload" "empty payload"
  case "$payload" in
    -*) deny "leading-dash" "payload must not start with '-'";;
    " "*) deny "leading-space" "payload must not start with whitespace";;
  esac
  [ -n "${payload// /}" ] || deny "blank-payload" "payload is whitespace-only"
  local blen; blen="$(printf '%s' "$payload" | wc -c | tr -d ' ')"
  [ "$blen" -le "$MAX_PAYLOAD" ] || deny "oversize" "payload over ${MAX_PAYLOAD} bytes"
  printf '%s' "$payload" | iconv -f UTF-8 -t UTF-8 >/dev/null 2>&1 || deny "utf8" "payload is not valid UTF-8"
}
p_meta(){ printf 'sha=%s len=%s' "$(printf '%s' "$payload" | shasum -a 256 | cut -c1-12)" "$(printf '%s' "$payload" | wc -c | tr -d ' ')"; }

case "$verb" in
  status)
    [ -z "$rest" ] || deny "status-args" "status takes no arguments"
    cap_take status "$CAP_STATUS" hour || deny "cap-status" "status cap reached this hour"
    log_or_deny "RUN status"
    {
      printf 'factory status @ %s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
      printf 'liveness: %s\n' "$(tail -n 1 "$STATE/liveness-canary.out" 2>/dev/null || echo 'no data')"
      printf 'drift:    %s\n' "$(tail -n 1 "$STATE/drift-canary.out" 2>/dev/null || echo 'no data')"
      _lr="$(cat "$STATE/liveness-last-run" 2>/dev/null || echo 0)"; [[ "$_lr" =~ ^[0-9]+$ ]] || _lr=0
      if [ "$_lr" -gt 0 ]; then printf 'last liveness run: %ss ago\n' "$(( $(date +%s) - 10#$_lr ))"; fi
      _a="$(cat "$STATE/.phonecap-ask-$(date +%Y%m%d)" 2>/dev/null || echo 0)"
      _r="$(cat "$STATE/.phonecap-reel-$(date +%Y%m%d)" 2>/dev/null || echo 0)"
      printf 'phone today: ask %s/%s · reel %s/%s\n' "$_a" "$CAP_ASK" "$_r" "$CAP_REEL"
    } | emit
    log_or_deny "OK status rc=0"
    ;;

  ask)
    scope="${rest%% *}"
    [ "$rest" != "$scope" ] || deny "ask-args" "usage: ask <research|fitness|company> <question>"
    case "$scope" in research|fitness|company) : ;; *) deny "scope" "scope must be research|fitness|company";; esac
    validate_payload "${rest#"$scope" }"
    conc_acquire || deny "busy" "factory line busy (2 concurrent max) — try again in a moment"
    cap_take ask "$CAP_ASK" day || deny "cap-ask" "ask cap reached today"
    log_or_deny "RUN ask scope=$scope $(p_meta)"
    _o="$(mktemp "$STATE/.pho.XXXXXX")" ; _e="$(mktemp "$STATE/.phe.XXXXXX")"
    rc=0
    MXR_TIMEOUT_S=45 run_bounded 50 "$MXR_BIN" ask --scope "$scope" -- "$payload" >"$_o" 2>"$_e" || rc=$?
    if [ "$rc" -eq 0 ]; then
      emit < "$_o"
    else
      _jid="$(grep -o 'JOB_ID=[0-9a-f-]*' "$_e" 2>/dev/null | head -1 | cut -d= -f2 || true)"
      if [ -n "$_jid" ]; then
        printf 'still thinking — job %s\nrun Get Answer with: get %s\n' "${_jid:0:13}…" "$_jid"
      else
        { printf 'factory error (rc=%s):\n' "$rc"; tail -c 500 "$_e"; } | emit
      fi
    fi
    rm -f "$_o" "$_e" 2>/dev/null || true
    log_or_deny "OK ask rc=$rc"
    ;;

  get)
    jid="$rest"
    [[ "$jid" =~ ^[0-9a-f-]{8,40}$ ]] || deny "jid" "not a job id (8+ hex chars, hyphens ok)"
    conc_acquire || deny "busy" "factory line busy (2 concurrent max) — try again in a moment"
    cap_take get "$CAP_GET" day || deny "cap-get" "get cap reached today"
    log_or_deny "RUN get jid=${jid:0:13}"
    _o="$(mktemp "$STATE/.pho.XXXXXX")" ; _e="$(mktemp "$STATE/.phe.XXXXXX")"
    rc=0
    run_bounded 30 "$MXR_BIN" get --reply "$jid" >"$_o" 2>"$_e" || rc=$?
    case "$rc" in
      0) emit < "$_o";;
      3) printf 'still thinking — no reply yet; try again in a minute\n';;
      1) printf 'no such job\n';;
      *) { printf 'factory error (rc=%s):\n' "$rc"; tail -c 300 "$_e"; } | emit;;
    esac
    rm -f "$_o" "$_e" 2>/dev/null || true
    log_or_deny "OK get rc=$rc"
    ;;

  reel)
    validate_payload "$rest"
    cap_take reel "$CAP_REEL" day || deny "cap-reel" "reel cap reached today (5 paid renders/day)"
    # HONEST STUB (P2 plan): mx-engine is not deployed on the factory yet. No dispatch —
    # no ledger dead-jobs, no paid spend. Flip to a real `"$MXR_BIN" mx-engine -- "$payload"`
    # submit-and-return when the mx-engine lane deploys to the Mini.
    log_or_deny "RUN reel STUB $(p_meta)"
    printf 'reel is not yet available on the factory (mx-engine pending deploy) — your topic was NOT submitted.\n'
    log_or_deny "OK reel rc=0 (stub)"
    ;;

  *)
    deny "verb" "unknown verb (use: status | ask <scope> <question> | get <job-id> | reel <topic>)"
    ;;
esac
exit 0
