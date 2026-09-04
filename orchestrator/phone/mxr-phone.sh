#!/bin/bash
# mxr-phone — the phone's ONLY door into the factory: an SSH forced-command wrapper
# exposing exactly four verbs (status | ask | get | reel) over the tailnet.
# Design: docs/phone-tailnet-surface-design.md (v0.2). Review r1 (2026-09-03): all 14
# findings folded — see the H-/M-/L- tags inline.
#
# Installed BY HAND to ~/.myndaix/bin/mxr-phone on the Mini (joins the hand-copied set).
# Wired via authorized_keys:
#   restrict,from="<phone-ts-ip>",command="/Users/jefe/.myndaix/bin/mxr-phone" ssh-ed25519 ...
#
# TRUST MODEL (design §3, stated honestly): a COMMAND-RESTRICTION boundary, not privilege
# isolation. SSH_ORIGINAL_COMMAND is UNTRUSTED dictated text: grammar-checked, byte-capped,
# control-REJECTED, passed as ONE argv after `--`; never eval'd, never word-split. Every
# anomaly denies (exit 2) with a one-line reason on stdout (what the Shortcut displays).

# ---- 0a. ENV TRAMPOLINE (r1 H-2): bash sources $BASH_ENV during ITS OWN startup — an
# in-script unset is too late for this interpreter. Re-exec through env -i so bash starts
# in a clean room. Seams + SSH_ORIGINAL_COMMAND pass through EXPLICITLY (sshd AcceptEnv
# is empty — asserted by test.sh --sshd — so a CLIENT can never set any of these; the
# MXRPHONE_*/STUB_* seams exist for test.sh only).
if [ -z "${MXRPHONE_CLEAN:-}" ]; then
  exec /usr/bin/env -i MXRPHONE_CLEAN=1 \
    HOME="${HOME:-}" \
    PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
    SSH_ORIGINAL_COMMAND="${SSH_ORIGINAL_COMMAND:-}" \
    MXRPHONE_MXR="${MXRPHONE_MXR:-}" MXRPHONE_STATE="${MXRPHONE_STATE:-}" \
    MXRPHONE_CAP_ASK="${MXRPHONE_CAP_ASK:-}" MXRPHONE_CAP_GET="${MXRPHONE_CAP_GET:-}" \
    MXRPHONE_CAP_REEL="${MXRPHONE_CAP_REEL:-}" MXRPHONE_CAP_STATUS="${MXRPHONE_CAP_STATUS:-}" \
    STUB_ARGV="${STUB_ARGV:-}" STUB_MODE="${STUB_MODE:-}" \
    /bin/bash "$0" "$@"
fi
set -euo pipefail

# The live corpus is $HOME-rooted on every machine (the 2026-09-03 symlink decision):
export MYNDAIX_KNOWLEDGE_SCOPES="fitness=$HOME/fitness,company=$HOME/company"

MXR_BIN="${MXRPHONE_MXR:-$HOME/.local/bin/mxr}"
STATE="${MXRPHONE_STATE:-$HOME/.myndaix/state}"
CAP_ASK="${MXRPHONE_CAP_ASK:-50}";  CAP_GET="${MXRPHONE_CAP_GET:-100}"
CAP_REEL="${MXRPHONE_CAP_REEL:-5}"; CAP_STATUS="${MXRPHONE_CAP_STATUS:-60}"
for _c in CAP_ASK CAP_GET CAP_REEL CAP_STATUS; do
  [[ "${!_c}" =~ ^[0-9]+$ ]] || printf -v "$_c" '%s' 0   # non-numeric/empty cap = fail closed
  printf -v "$_c" '%s' "$((10#${!_c}))"                  # octal-trap normalization
done
LOG="$STATE/mxr-phone.log"
JIDS="$STATE/phone-jids"                                 # r1 H-3: phone-issued job registry
MAX_PAYLOAD=2000
MAX_OUT=4096

mkdir -p "$STATE" 2>/dev/null || { printf 'denied: state dir unavailable\n'; exit 2; }

# ---- atomic stale eviction (r1 M-4/M-5): mv is the ONE-WINNER op — a racing reaper's mv
# fails, so nobody ever deletes a rival's FRESH lock (mtime is re-checked here, atomically
# close to the mv; a recreated dir has a fresh mtime and simply fails the staleness gate).
_reap_dir(){ # _reap_dir <dir> <stale_s> ; rc0 = this caller evicted it
  local d="$1" now mt g
  now="$(date +%s)"; mt="$(stat -f %m "$d" 2>/dev/null || printf '%s' "$now")"
  [ $((now - mt)) -gt "$2" ] || return 1
  g="$d.reaped.$$"
  mv "$d" "$g" 2>/dev/null || return 1
  rm -rf "$g" 2>/dev/null || true
  return 0
}

# ---- logging (design M2; r1 L-13): sha-only, 0600, self-rotated, and SERIALIZED under a
# tiny mkdir lock so concurrent sessions can't lose lines to a create/rotate race.
# Log-write failure = DENY (fail closed).
_logwrite(){ # _logwrite <line> ; rc!=0 on failure
  local i lockd _t
  lockd="$STATE/.phonelog.lock"
  for i in $(seq 1 20); do
    if mkdir "$lockd" 2>/dev/null; then break; fi
    _reap_dir "$lockd" 10 && continue
    sleep 0.02
    [ "$i" -eq 20 ] && return 1
  done
  if [ ! -e "$LOG" ]; then ( umask 077; : > "$LOG" ) 2>/dev/null || { rmdir "$lockd" 2>/dev/null || true; return 1; }; fi
  if ! printf '%s\n' "$1" >> "$LOG" 2>/dev/null; then rmdir "$lockd" 2>/dev/null || true; return 1; fi
  if [ "$(wc -l < "$LOG" 2>/dev/null || echo 0)" -gt 1000 ]; then
    _t="$(mktemp "$STATE/.phlog.XXXXXX" 2>/dev/null)" || { rmdir "$lockd" 2>/dev/null || true; return 0; }
    tail -n 1000 "$LOG" > "$_t" 2>/dev/null && chmod 600 "$_t" 2>/dev/null && mv -f "$_t" "$LOG" 2>/dev/null || rm -f "$_t" 2>/dev/null || true
  fi
  rmdir "$lockd" 2>/dev/null || true
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

# ---- global concurrency cap 2 (design H5; r1 M-4/M-6): mkdir slots, pid-owned; a failed
# pid write = failed acquisition (never hold a slot the trap can't release).
CONC=""
release_conc(){ if [ -n "$CONC" ] && [ "$(cat "$CONC/pid" 2>/dev/null || echo none)" = "$$" ]; then rm -rf "$CONC" 2>/dev/null; fi; return 0; }
trap 'release_conc' EXIT INT TERM
conc_acquire(){
  local s d
  for s in 1 2; do
    d="$STATE/.phone-conc$s"
    if ! mkdir "$d" 2>/dev/null; then
      _reap_dir "$d" 300 || continue                     # fresh or contested: next slot
      mkdir "$d" 2>/dev/null || continue                 # lost the recreate race: next slot
    fi
    if printf '%s' "$$" > "$d/pid" 2>/dev/null; then CONC="$d"; return 0; fi
    rm -rf "$d" 2>/dev/null || true                      # r1 M-6: unownable slot = not acquired
  done
  return 1
}

# ---- per-verb caps (design H5; r1 M-5/M-7/M-11): PER-VERB mkdir lock (no cross-verb
# starvation), atomic stale eviction, check+increment BEFORE dispatch. An EXISTING but
# UNREADABLE counter fails CLOSED — only a genuinely-absent file means zero.
cap_take(){ # cap_take <verb> <limit> <day|hour> ; rc!=0 = over cap / lock starvation / read failure
  local verb="$1" limit="$2" window="$3" stamp cf lockd n i _t
  case "$window" in hour) stamp="$(date +%Y%m%d%H)";; *) stamp="$(date +%Y%m%d)";; esac
  cf="$STATE/.phonecap-$verb-$stamp"; lockd="$STATE/.phonecap-$verb.lock"
  for i in $(seq 1 40); do
    if mkdir "$lockd" 2>/dev/null; then break; fi
    _reap_dir "$lockd" 60 && continue
    sleep 0.05
    [ "$i" -eq 40 ] && return 1
  done
  if [ -e "$cf" ]; then
    if ! n="$(cat "$cf" 2>/dev/null)"; then rmdir "$lockd" 2>/dev/null || true; return 1; fi
  else
    n=0
  fi
  [[ "$n" =~ ^[0-9]+$ ]] || n=0; n=$((10#$n))
  if [ "$n" -ge "$limit" ]; then rmdir "$lockd" 2>/dev/null || true; return 1; fi
  _t="$(mktemp "$STATE/.phcap.XXXXXX" 2>/dev/null)" || { rmdir "$lockd" 2>/dev/null || true; return 1; }
  printf '%s' "$((n + 1))" > "$_t" 2>/dev/null && mv -f "$_t" "$cf" 2>/dev/null || { rm -f "$_t" 2>/dev/null || true; rmdir "$lockd" 2>/dev/null || true; return 1; }
  rmdir "$lockd" 2>/dev/null || true
  return 0
}

# ---- phone-jid registry (r1 H-3): `get` may only fetch jobs THIS surface issued — job
# ids are NOT an authorization boundary. Recorded on the ask-timeout path; 200-line cap.
jid_record(){ # jid_record <full-uuid>
  ( umask 077; printf '%s\n' "$1" >> "$JIDS" ) 2>/dev/null || true
  if [ "$(wc -l < "$JIDS" 2>/dev/null || echo 0)" -gt 200 ]; then
    local _t; _t="$(mktemp "$STATE/.phjid.XXXXXX" 2>/dev/null)" || return 0
    tail -n 200 "$JIDS" > "$_t" 2>/dev/null && chmod 600 "$_t" 2>/dev/null && mv -f "$_t" "$JIDS" 2>/dev/null || rm -f "$_t" 2>/dev/null || true
  fi
  return 0
}
jid_known(){ grep -qx "$1" "$JIDS" 2>/dev/null; }

# ---- 1. GRAMMAR (design §4): parse SSH_ORIGINAL_COMMAND; deny-by-default ----
cmd="${SSH_ORIGINAL_COMMAND:-}"
[ -n "$cmd" ] || deny "empty" "no command (use: status | ask <scope> <question> | get <job-id> | reel <topic>)"
# r1 H-1: the control filter FAILS CLOSED — only "grep ran and counted zero" proceeds; a
# missing/broken grep (rc not in {0,1}) denies rather than waving the payload through.
set +e
_cn="$(printf '%s' "$cmd" | LC_ALL=C grep -c '[[:cntrl:]]' 2>/dev/null)"; _crc=$?
set -e
if [ "$_crc" -ne 1 ] || [ "$_cn" != "0" ]; then deny "cntrl" "control characters are not accepted"; fi
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
      # r1 M-8: cli prints JOB_ID for EVERY submit and exits 1 for both sync-timeout and a
      # terminal failed/dead job. Only the TIMEOUT is "still thinking" — a terminal failure
      # must say so, or `get` polls a dead job forever.
      _jid="$(grep -o 'JOB_ID=[0-9a-f-]*' "$_e" 2>/dev/null | head -1 | cut -d= -f2 || true)"
      if [ -n "$_jid" ] && grep -q 'timed out' "$_e" 2>/dev/null; then
        jid_record "$_jid"                                # r1 H-3: only phone-issued jobs are get-able
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
    # Full uuid ONLY (the still-thinking message hands it over verbatim) — prefixes would
    # leak candidate ids via mxr's ambiguity listing (r1 H-3).
    [[ "$jid" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || deny "jid" "not a job id (use the full id from the still-thinking message)"
    jid_known "$jid" || deny "jid-foreign" "not a phone-issued job"
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
    # no ledger dead-jobs, no paid spend. Flip: bounded submit-and-return per the README
    # (and jid_record the submitted id so Get Answer can fetch it).
    log_or_deny "RUN reel STUB $(p_meta)"
    printf 'reel is not yet available on the factory (mx-engine pending deploy) — your topic was NOT submitted.\n'
    log_or_deny "OK reel rc=0 (stub)"
    ;;

  *)
    deny "verb" "unknown verb (use: status | ask <scope> <question> | get <job-id> | reel <topic>)"
    ;;
esac
exit 0
