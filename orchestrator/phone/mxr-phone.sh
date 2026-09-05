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
  # r3 MED-2: pass MYNDAIX_DSN through ONLY when non-empty — an exported-but-empty value
  # would override cli.py's absent-key default with "" (get() default fires on absence only).
  _dsn=()
  [ -n "${MYNDAIX_DSN:-}" ] && _dsn=(MYNDAIX_DSN="$MYNDAIX_DSN")
  exec /usr/bin/env -i MXRPHONE_CLEAN=1 \
    HOME="${HOME:-}" \
    PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
    SSH_ORIGINAL_COMMAND="${SSH_ORIGINAL_COMMAND:-}" \
    ${_dsn[@]+"${_dsn[@]}"} \
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

# legacy residue guard (r4 MAJOR-2/MED-3): pre-flock revisions used mkdir DIRECTORIES at
# these paths; a leftover dir would brick a conc slot forever (">>" cannot open a dir) or
# sit dead beside the new .lockf files. This surface has never been deployed, so there is
# no live old process to race — this is residue cleanup (crashed test runs, future
# half-copies), not online migration. rm failure = loud exit (set -e), never a silent skip.
for _legacy in "$STATE/.phone-conc1" "$STATE/.phone-conc2" "$STATE/.phonelog.lock" \
               "$STATE/.phjid.lock" "$STATE"/.phonecap-*.lock; do
  if [ -d "$_legacy" ]; then rm -rf "$_legacy"; fi
done

# ---- kernel locks (r3 HIGH-1): flock(2) via perl (already a hard dependency — see the
# run_bounded guard). The mkdir-lock + mv-eviction design was a TOCTOU class (r1 M-4/M-5,
# r2 MED-4, r3 HIGH-1 — every round found a narrower race in check-then-rename): a kernel
# lock has no stale state to evict — a crashed holder's fd dies with the process and the
# kernel releases the lock. One timeout knob, no second eviction constant to drift
# (r3 LOW-4). The exec'd child inherits the locked fd, so the lock spans exactly the
# critical section and vanishes on ANY exit path, including SIGKILL.
_locked(){ # _locked <lockfile> <timeout_s> <argv...> ; child's rc, 5=lock io, 6=lock timeout
  local lf="$1" to="$2"; shift 2
  # FD_CLOEXEC must be CLEARED before the exec (r4 MAJOR-1): perl close-on-execs every fd
  # above $^F (=2), so without the fcntl the kernel would close the fd — and release the
  # flock — the instant exec fires, leaving the critical section unlocked. Probed both
  # ways on the target /bin/bash+perl: default = contender acquires inside the child;
  # with the clear = contender blocks until the child exits.
  perl -MFcntl=:flock,F_GETFD,F_SETFD,FD_CLOEXEC -e '
    my ($lf, $to) = (shift @ARGV, shift @ARGV);
    open(my $f, ">>", $lf) or exit 5;
    $SIG{ALRM} = sub { exit 6 }; alarm($to);
    flock($f, LOCK_EX) or exit 5;
    alarm(0);
    my $fl = fcntl($f, F_GETFD, 0); defined $fl or exit 5;
    fcntl($f, F_SETFD, $fl & ~FD_CLOEXEC) or exit 5;
    exec @ARGV; exit 5;' "$lf" "$to" "$@"
}

# ---- logging (design M2; r1 L-13): sha-only, 0600, self-rotated, and SERIALIZED under a
# kernel lock so concurrent sessions can't lose lines to a create/rotate race.
# Log-write failure = DENY (fail closed). Snippet args are positional — never interpolated.
_logwrite(){ # _logwrite <line> ; rc!=0 on failure
  _locked "$STATE/.phonelog.lockf" 5 /bin/bash -c '
    set -uo pipefail
    LOG="$1"; STATE="$2"; line="$3"
    if [ ! -e "$LOG" ]; then ( umask 077; : > "$LOG" ) 2>/dev/null || exit 1; fi
    printf "%s\n" "$line" >> "$LOG" 2>/dev/null || exit 1
    if [ "$(wc -l < "$LOG" 2>/dev/null || echo 0)" -gt 1000 ]; then
      _t="$(mktemp "$STATE/.phlog.XXXXXX" 2>/dev/null)" || exit 0
      tail -n 1000 "$LOG" > "$_t" 2>/dev/null && chmod 600 "$_t" 2>/dev/null && mv -f "$_t" "$LOG" 2>/dev/null || rm -f "$_t" 2>/dev/null || true
    fi
    exit 0' _ "$LOG" "$STATE" "$1"
}
log_or_deny(){ _logwrite "[$(date '+%Y-%m-%d %H:%M:%S')] $1" || { printf 'denied: logging unavailable\n'; exit 2; }; }

deny(){ log_or_deny "DENY $1"; printf 'denied: %s\n' "$2"; exit 2; }

# ---- output escape + answer-first truncation (design M1/L2) ----
emit(){ # stdin -> stdout, control/ANSI-stripped (keep \t\n), capped at MAX_OUT bytes
  local body
  # r3 MED-3: the input is capped BEFORE the whole-buffer decode (perl -0777 would otherwise
  # slurp unbounded stdin into RAM). Stripping only ever REMOVES bytes, so 4x MAX_OUT of
  # headroom keeps the >MAX_OUT truncation marker honest for any real reply; a reply that is
  # mostly stripped garbage may lose the marker, which costs nothing but the ellipsis. A
  # pre-cap cut mid-multibyte-char decodes to U+FFFD, which the filter strips.
  body="$(head -c $((MAX_OUT * 4 + 8)) | perl -MEncode=decode,encode,FB_DEFAULT -0777 -ne '$s=decode("UTF-8",$_,FB_DEFAULT);$s=~s/[\x{0}-\x{8}\x{b}\x{c}\x{e}-\x{1f}\x{7f}-\x{9f}\x{fffd}]//g;print encode("UTF-8",$s)' | head -c $((MAX_OUT + 1)))"
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

# ---- global concurrency cap 2 (design H5; r1 M-4/M-6; r3 HIGH-1): slot = kernel flock
# on a permanent slot FILE, held on fd 8 for the session's lifetime. The perl helper
# flocks the INHERITED fd (">>&=" = fdopen, no dup) and exits — the lock lives on the
# open file description fd 8 keeps referencing, so it persists in THIS process and dies
# with it (any exit path, incl. SIGKILL: no stale slots, no reaper, no pid files).
# Children (run_bounded mxr calls) inherit fd 8, so a slot spans the session AND its
# in-flight work — exactly what the cap is bounding.
release_conc(){ exec 8>&- 2>/dev/null || true; return 0; }
trap 'release_conc' EXIT INT TERM
conc_acquire(){
  local s
  for s in 1 2; do
    exec 8>>"$STATE/.phone-conc$s" 2>/dev/null || continue
    if perl -MFcntl=:flock -e 'open(my $f, ">>&=", 8) or exit 1; flock($f, LOCK_EX|LOCK_NB) or exit 1; exit 0'; then
      return 0
    fi
  done
  exec 8>&- 2>/dev/null || true
  return 1
}

# ---- per-verb caps (design H5; r1 M-5/M-7/M-11): PER-VERB kernel lock (no cross-verb
# starvation), check+increment BEFORE dispatch. An EXISTING but UNREADABLE counter fails
# CLOSED — only a genuinely-absent file means zero.
cap_take(){ # cap_take <verb> <limit> <day|hour> ; rc!=0 = over cap / lock trouble / read failure
  local verb="$1" limit="$2" window="$3" stamp cf
  case "$window" in hour) stamp="$(date +%Y%m%d%H)";; *) stamp="$(date +%Y%m%d)";; esac
  cf="$STATE/.phonecap-$verb-$stamp"
  _locked "$STATE/.phonecap-$verb.lockf" 5 /bin/bash -c '
    set -uo pipefail
    cf="$1"; limit="$2"; STATE="$3"
    if [ -e "$cf" ]; then
      n="$(cat "$cf" 2>/dev/null)" || exit 1
    else
      n=0
    fi
    [[ "$n" =~ ^[0-9]+$ ]] || n=0; n=$((10#$n))
    [ "$n" -ge "$limit" ] && exit 1
    _t="$(mktemp "$STATE/.phcap.XXXXXX" 2>/dev/null)" || exit 1
    printf "%s" "$((n + 1))" > "$_t" 2>/dev/null && mv -f "$_t" "$cf" 2>/dev/null || { rm -f "$_t" 2>/dev/null || true; exit 1; }
    exit 0' _ "$cf" "$limit" "$STATE"
}

# ---- phone-jid registry (r1 H-3): `get` may only fetch jobs THIS surface issued — job
# ids are NOT an authorization boundary. Recorded on the ask-timeout path; 200-line cap.
jid_record(){ # jid_record <full-uuid> ; rc!=0 = record not durable (caller surfaces it, r2 MED-5)
  _locked "$STATE/.phjid.lockf" 5 /bin/bash -c '
    set -uo pipefail
    JIDS="$1"; STATE="$2"; jid="$3"
    ( umask 077; printf "%s\n" "$jid" >> "$JIDS" ) 2>/dev/null || exit 1
    if [ "$(wc -l < "$JIDS" 2>/dev/null || echo 0)" -gt 200 ]; then
      _t="$(mktemp "$STATE/.phjid.XXXXXX" 2>/dev/null)" || exit 0
      tail -n 200 "$JIDS" > "$_t" 2>/dev/null && chmod 600 "$_t" 2>/dev/null && mv -f "$_t" "$JIDS" 2>/dev/null || rm -f "$_t" 2>/dev/null || true
    fi
    exit 0' _ "$JIDS" "$STATE" "$1"
}
jid_known(){ grep -qx "$1" "$JIDS" 2>/dev/null; }

# ---- 1. GRAMMAR (design §4): parse SSH_ORIGINAL_COMMAND; deny-by-default ----
cmd="${SSH_ORIGINAL_COMMAND:-}"
[ -n "$cmd" ] || deny "empty" "no command (use: status | ask <scope> <question> | get <job-id> | reel <topic>)"
# r1 H-1: the control filter FAILS CLOSED — only "grep ran and counted zero" proceeds; a
# missing/broken grep (rc not in {0,1}) denies rather than waving the payload through.
set +e
_cn="$(printf '%s' "$cmd" | LC_ALL=C grep -c '[[:cntrl:]]' 2>/dev/null)"; _crc=$?
_c1_pat="$(printf '\302[\200-\237]')"
_c1="$(printf '%s' "$cmd" | LC_ALL=C grep -c "$_c1_pat" 2>/dev/null)"; _c1rc=$?
printf '%s' "$cmd" | iconv -f UTF-8 -t UTF-8 >/dev/null 2>&1; _utf8rc=$?
set -e
if [ "$_crc" -gt 1 ] || [ "$_c1rc" -gt 1 ] || [ "$_utf8rc" -ne 0 ] || [ "$_cn" != "0" ] || [ "$_c1" != "0" ]; then
  deny "cntrl" "control characters are not accepted"
fi
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
      # Branch on the cli's STABLE stderr markers only (marker fold): MXR_SYNC_TIMEOUT /
      # MXR_JOB_FAILED / MXR_JOB_DEAD — the human prose beside them may change freely,
      # and agent-controlled error text can't spoof a line-anchored marker match into
      # flipping a dead job back to "still thinking".
      _jid="$(grep -o 'JOB_ID=[0-9a-f-]*' "$_e" 2>/dev/null | head -1 | cut -d= -f2 || true)"
      if [ -n "$_jid" ] && grep -q '^MXR_SYNC_TIMEOUT$' "$_e" 2>/dev/null; then
        if jid_record "$_jid"; then
          printf 'still thinking — job %s\nrun Get Answer with: get %s\n' "${_jid:0:13}…" "$_jid"
        else
          rc=1
          printf 'factory error: could not record job id for later retrieval\n' | emit
        fi
      elif [ -n "$_jid" ] && ! grep -Eq '^MXR_JOB_(FAILED|DEAD)$' "$_e" 2>/dev/null && jid_record "$_jid"; then
        # Killed AFTER submit with no terminal marker (SIGALRM rc=142, crash, OOM): the
        # job is alive in the ledger — record the id NOW or the reply is orphaned forever
        # (get denies foreign ids). Record failure falls through to the plain error.
        printf 'factory hiccup (rc=%s) — the job may still finish\nrun Get Answer with: get %s\n' "$rc" "$_jid"
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
      3)
        # Stable-marker branch (marker fold): failed/dead and done-with-no-body are
        # TERMINAL — "still thinking" forever would burn the get cap polling a job
        # that can never answer.
        if grep -Eq '^MXR_JOB_(FAILED|DEAD)$' "$_e" 2>/dev/null; then
          { printf 'factory error (rc=%s):\n' "$rc"; tail -c 300 "$_e"; } | emit
        elif grep -q '^MXR_DONE_EMPTY$' "$_e" 2>/dev/null; then
          printf 'factory finished but produced no answer — ask again with more detail\n'
        else
          printf 'still thinking — no reply yet; try again in a minute\n'
        fi
        ;;
      1)
        # rc=1 WITHOUT the marker is not "no such job" — an unreachable ledger exits 1
        # too, and the authoritative-sounding lie makes the user discard a valid id.
        if grep -q '^MXR_NO_SUCH_JOB$' "$_e" 2>/dev/null; then
          printf 'no such job\n'
        else
          { printf 'factory error (rc=%s):\n' "$rc"; tail -c 300 "$_e"; } | emit
        fi
        ;;
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
