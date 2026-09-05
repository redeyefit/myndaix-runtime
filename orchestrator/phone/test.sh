#!/bin/bash
# test.sh — mxr-phone forced-command wrapper. Two legs:
#   (default)  fixture leg: drives the wrapper via SSH_ORIGINAL_COMMAND with a RECORDING
#              stub mxr (argv logged one-per-line for byte-exact hostile-payload asserts).
#   --sshd     deploy-time leg (run ON the Mini): asserts the REAL sshd boundary — forced
#              command enforced, no shell/pty, AcceptEnv empty, env abuse inert.
# Run: bash orchestrator/phone/test.sh
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

SCRIPT="$(cd "$(dirname "$0")" && pwd)/mxr-phone.sh"
ROOT="$(mktemp -d /tmp/mxrphone-test.XXXXXX)"
trap 'rm -rf "$ROOT"' EXIT INT TERM
STATE="$ROOT/state"; mkdir -p "$STATE"
ARGV="$ROOT/mxr-argv.log"
PASS=0; FAIL=0
ok(){ PASS=$((PASS+1)); echo "  ok: $1"; }
bad(){ FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

# ---- recording stub mxr: one argv per line + a per-call record separator; canned outputs
# steered by MXRPHONE-test env (these survive the wrapper's ^MXR_ scrub by design). ----
STUB="$ROOT/mxr"
cat > "$STUB" <<'STUBEOF'
#!/bin/bash
{ printf -- '--CALL--\n'; printf '%s\n' "$@"; printf 'ENV_MXR_TIMEOUT_S=%s\n' "${MXR_TIMEOUT_S:-unset}"; printf 'ENV_BASH_ENV=%s\n' "${BASH_ENV:-unset}"; printf 'ENV_PYTHONPATH=%s\n' "${PYTHONPATH:-unset}"; } >> "${STUB_ARGV:?}"
case "${STUB_MODE:-answer}" in
  answer)  printf 'ANSWER: the launch color is teal\n[1] greeting.md\n';;
  timeout) printf -- '-> librarian  (job deadbeef)\nJOB_ID=deadbeef-0000-4000-8000-000000000001\nMXR_SYNC_TIMEOUT\ntimed out (is the pool running?)\n' >&2; exit 1;;
  jobfailed) printf -- '-> librarian  (job deadbeef)\nJOB_ID=deadbeef-0000-4000-8000-000000000001\nagent exploded\nMXR_JOB_FAILED\n(job failed)\n' >&2; exit 1;;
  spooffailed) printf -- '-> librarian  (job deadbeef)\nJOB_ID=deadbeef-0000-4000-8000-000000000001\nerror: request timed out mid-flight\nMXR_JOB_FAILED\n(job failed)\n' >&2; exit 1;;
  orphan)  printf -- '-> librarian  (job deadbeef)\nJOB_ID=deadbeef-0000-4000-8000-000000000001\n' >&2; exit 142;;
  reply)   printf 'RECOVERED ANSWER BODY\n';;
  noreply) printf 'no reply yet (job status: pending)\n' >&2; exit 3;;
  getfailed) printf 'MXR_JOB_FAILED\nno reply yet (job status: failed)\n' >&2; exit 3;;
  doneempty) printf 'MXR_DONE_EMPTY\nno reply yet (job status: done)\n' >&2; exit 3;;
  nojob)   printf 'MXR_NO_SUCH_JOB\nno such job\n' >&2; exit 1;;
  dbdown)  printf 'Traceback (most recent call last):\nConnectionRefusedError: [Errno 61] connect call failed\n' >&2; exit 1;;
  bigout)  head -c 9000 /dev/zero | tr '\0' 'A';;
  ansi)    printf 'clean \033[31mred\033[0m done\n';;
esac
exit 0
STUBEOF
chmod +x "$STUB"

run(){ # run "<SSH_ORIGINAL_COMMAND>" [extra env pairs as VAR=val args...]
  local cmd="$1"; shift
  env -i HOME="$ROOT" PATH="$PATH" SHELL=/bin/bash \
      SSH_ORIGINAL_COMMAND="$cmd" \
      MXRPHONE_MXR="$STUB" MXRPHONE_STATE="$STATE" STUB_ARGV="$ARGV" \
      "$@" bash "$SCRIPT" 2>&1
}
reset(){ rm -rf "$STATE"; mkdir -p "$STATE"; : > "$ARGV"; }
last_payload(){ tail -n +2 "$ARGV" | grep -v '^ENV_' | tail -n 1; }
no_dispatch(){ [ ! -s "$ARGV" ]; }

if [[ "${1:-}" == "--sshd" ]]; then
  # ================= deploy-time REAL-SSHD leg (design §8.4b) — run ON the Mini =========
  echo "sshd leg: asserting the real boundary on $(hostname)"
  command -v ssh >/dev/null || { echo "FAIL: no ssh"; exit 1; }
  # AcceptEnv: stock macOS ships 'AcceptEnv LANG LC_*' (/etc/ssh/sshd_config.d/100-macos.conf)
  # — locale-only passthrough, and inert against the wrapper's env -i trampoline anyway. A
  # strict empty-AcceptEnv gate dead-ended every fresh box on Apple's default (audit item 2),
  # so the gate flags any NON-locale pattern (a real env channel) instead. NOTE: an OS update
  # can rewrite 100-macos.conf — this gate re-checks the truth on every --sshd run.
  if sudo -n true 2>/dev/null; then _ae_lines="$(sudo sshd -T 2>/dev/null | grep -i '^acceptenv' || true)"
  else _ae_lines="$(grep -rhi '^[[:space:]]*AcceptEnv' /etc/ssh/sshd_config /etc/ssh/sshd_config.d 2>/dev/null || true)"; fi
  AE="$(printf '%s\n' "$_ae_lines" | awk '{for(i=2;i<=NF;i++) print $i}' | grep -vE '^(LANG|LC_)' | grep -c . || true)"
  [ "${AE:-0}" -eq 0 ] && ok "AcceptEnv carries no non-locale patterns (LANG/LC_* tolerated; env -i makes them inert)" || bad "AcceptEnv has $AE non-locale pattern(s) — real env channel open"
  # temp key wired to the wrapper via a forced command, loopback. Trap installed BEFORE
  # the append (r1 L-14): an interrupt can never strand the temp key; cleanup is
  # temp-file + atomic mv.
  K="$ROOT/k"; ssh-keygen -q -t ed25519 -N '' -f "$K"
  AK="$HOME/.ssh/authorized_keys"; touch "$AK"
  _ak_cleanup(){ # cleanup as a FUNCTION (deploy-sync precedent: never interpolate into a trap string)
    local t grc
    if [ -f "$K.pub" ] && t="$(mktemp "$HOME/.ssh/.ak.XXXXXX" 2>/dev/null)"; then
      # grep -v exits 1 when it selects ZERO lines (temp key was the only entry) — that is a
      # VALID filter result, not a failure; only rc>1 (real error) may skip the mv, else the
      # forced-command key would survive the test run (r2 LOW-7).
      grep -vF "$(cat "$K.pub")" "$AK" > "$t" 2>/dev/null; grc=$?
      if [ "$grc" -le 1 ]; then mv -f "$t" "$AK"; else rm -f "$t" 2>/dev/null; fi
    fi
    rm -rf "$ROOT"
  }
  trap '_ak_cleanup' EXIT
  LINE="restrict,command=\"$HOME/.myndaix/bin/mxr-phone\" $(cat "$K.pub")"
  printf '%s\n' "$LINE" >> "$AK"
  SSHOPTS=(-i "$K" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes)
  out="$(ssh "${SSHOPTS[@]}" localhost 'status' 2>/dev/null || true)"
  printf '%s' "$out" | grep -q 'factory status' && ok "forced command answers status" || bad "status via ssh: $out"
  out="$(ssh "${SSHOPTS[@]}" localhost 'bash -i' 2>/dev/null || true)"
  printf '%s' "$out" | grep -q 'denied' && ok "shell request hits the wrapper, denied" || bad "shell request: $out"
  ssh "${SSHOPTS[@]}" -t -t localhost 'status' >/dev/null 2>&1 && bad "pty allocated" || ok "pty denied (restrict)"
  out="$(ssh "${SSHOPTS[@]}" -o SetEnv=BASH_ENV=/tmp/evil localhost 'status' 2>/dev/null || true)"
  printf '%s' "$out" | grep -q 'factory status' && ok "SetEnv attempt inert" || bad "SetEnv broke the call: $out"
  n="$(grep -c 'mxr-phone' "$AK" || true)"; [ "$n" -le 1 ] && ok "no duplicate phone keys" || bad "$n phone key lines"
  echo; echo "=== $PASS passed, $FAIL failed ==="; [ "$FAIL" -eq 0 ]; exit $?
fi

# ============================== fixture leg ==============================================
echo "1. grammar — status happy path (seeded canary files)"
reset
printf '[x] liveness: all declared jobs alive\n' > "$STATE/liveness-canary.out"
printf '[x] canary: no drift\n' > "$STATE/drift-canary.out"
out="$(run status)"
printf '%s' "$out" | grep -q 'factory status' && ok "status header" || bad "status: $out"
printf '%s' "$out" | grep -q 'all declared jobs alive' && ok "liveness line" || bad "no liveness line"
no_dispatch && ok "status dispatches nothing to mxr" || bad "status dispatched"

echo "2. ask happy path — argv shape + scope + verbatim payload"
reset
out="$(run 'ask research what color is the launch')"
printf '%s' "$out" | grep -q 'ANSWER: the launch color is teal' && ok "answer relayed" || bad "ask out: $out"
grep -q '^ask$' "$ARGV" && grep -q '^--scope$' "$ARGV" && grep -q '^research$' "$ARGV" && grep -q '^--$' "$ARGV" && ok "argv: ask --scope research --" || bad "argv shape: $(cat "$ARGV")"
[ "$(last_payload)" = "what color is the launch" ] && ok "payload verbatim" || bad "payload: $(last_payload)"
grep -q '^ENV_MXR_TIMEOUT_S=45$' "$ARGV" && ok "ask wait pinned to 45s" || bad "MXR_TIMEOUT_S not 45"

echo "3. hostile payloads travel byte-verbatim (never eval'd, never split)"
for p in 'tell me about $(rm -rf /) attacks' 'what is `whoami` here' "quote ' and \" mix" 'double  space   payload'; do
  reset; run "ask research $p" >/dev/null
  if [ "$(last_payload)" = "$p" ]; then ok "verbatim: ${p:0:30}"; else bad "mangled: got '$(last_payload)' want '$p'"; fi
done

echo "4. deny-by-default grammar"
reset; out="$(run 'ask personal my secrets')";        printf '%s' "$out" | grep -q 'denied: scope' && ok "personal scope DENIED" || bad "personal: $out"
reset; out="$(run 'ask runtime x')";                  printf '%s' "$out" | grep -q 'denied: scope' && ok "runtime scope denied" || bad "runtime: $out"
reset; out="$(run 'rm -rf /')";                       printf '%s' "$out" | grep -q 'denied: unknown verb' && ok "unknown verb denied" || bad "verb: $out"
reset; out="$(run 'status extra')";                   printf '%s' "$out" | grep -q 'denied: status takes no' && ok "status args denied" || bad "status-args: $out"
reset; out="$(run 'ask research')";                   printf '%s' "$out" | grep -q 'denied' && ok "missing payload denied" || bad "no-payload: $out"
reset; out="$(run 'ask research    ')";               printf '%s' "$out" | grep -q 'denied' && ok "whitespace payload denied" || bad "ws: $out"
reset; out="$(run 'ask research --version')";         printf '%s' "$out" | grep -q "denied: payload must not start with '-'" && ok "leading dash denied" || bad "dash: $out"
reset; out="$(run "$(printf 'ask research bad\tchar')")"; printf '%s' "$out" | grep -q 'denied: control' && ok "control char REJECTED (not stripped)" || bad "cntrl: $out"
# LF is the one control byte a line-oriented grep can NEVER see (it IS the separator) —
# the gate is bytewise now (audit item 3)
reset; out="$(run "$(printf 'ask research line\nsmuggled second line')")"; printf '%s' "$out" | grep -q 'denied: control' && ok "embedded LF REJECTED (bytewise gate)" || bad "LF: $out"
reset; out="$(run "$(printf 'ask research trailing\r')")"; printf '%s' "$out" | grep -q 'denied: control' && ok "embedded CR REJECTED" || bad "CR: $out"
reset; out="$(run "ask research $(head -c 2100 /dev/zero | tr '\0' 'x')")"; printf '%s' "$out" | grep -q 'denied: payload over' && ok "oversize denied" || bad "oversize: $out"
no_dispatch && ok "no denied case reached mxr" || bad "a denied case dispatched: $(cat "$ARGV")"

echo "5. ask timeout -> JOB_ID handoff to get (+ jid recorded; r1 H-3/M-8)"
reset; out="$(run 'ask research a slow deep question' STUB_MODE=timeout)"
printf '%s' "$out" | grep -q 'still thinking — job deadbeef-0000' && ok "timeout returns job id" || bad "timeout: $out"
printf '%s' "$out" | grep -q 'get deadbeef-0000-4000-8000-000000000001' && ok "full jid for Get Answer" || bad "no full jid"
grep -qx 'deadbeef-0000-4000-8000-000000000001' "$STATE/phone-jids" 2>/dev/null && ok "jid recorded in the phone registry" || bad "jid not recorded"
reset; out="$(run 'ask research doomed question' STUB_MODE=jobfailed)"
printf '%s' "$out" | grep -q 'factory error' && ok "terminal job failure reported as ERROR, not still-thinking (M-8)" || bad "jobfailed: $out"
[ ! -s "$STATE/phone-jids" ] && ok "failed job NOT recorded as fetchable" || bad "dead jid recorded"

echo "5b. marker contract — ask lifecycle (audit marker fold)"
# a kill AFTER submit (SIGALRM 142, no terminal marker): the live job's id must be
# recorded and handed over, or the reply is orphaned forever (audit item 4)
reset; out="$(run 'ask research interrupted question' STUB_MODE=orphan)"
printf '%s' "$out" | grep -q 'factory hiccup' && ok "post-submit kill -> hiccup message with jid" || bad "orphan: $out"
printf '%s' "$out" | grep -q 'get deadbeef-0000-4000-8000-000000000001' && ok "orphaned jid handed to Get Answer" || bad "orphan no jid: $out"
grep -qx 'deadbeef-0000-4000-8000-000000000001' "$STATE/phone-jids" 2>/dev/null && ok "orphaned jid recorded (reply reachable)" || bad "orphan jid not recorded"
# agent-controlled prose saying "timed out" must NOT flip a marker-terminal job back
# to still-thinking/hiccup (audit item 9 — markers, not prose)
reset; out="$(run 'ask research spoofed question' STUB_MODE=spooffailed)"
printf '%s' "$out" | grep -q 'factory error' && ok "prose 'timed out' cannot spoof past MXR_JOB_FAILED" || bad "spoof: $out"
[ ! -s "$STATE/phone-jids" ] && ok "spoofed-failed jid NOT recorded" || bad "spoofed jid recorded"

echo "6. get verb — full-uuid grammar + phone-jid ownership (r1 H-3)"
JID='deadbeef-0000-4000-8000-000000000001'
seed_jid(){ mkdir -p "$STATE"; printf '%s\n' "$JID" > "$STATE/phone-jids"; }
reset; seed_jid; out="$(run "get $JID" STUB_MODE=reply)"
printf '%s' "$out" | grep -q 'RECOVERED ANSWER BODY' && ok "reply relayed" || bad "get: $out"
grep -q '^--reply$' "$ARGV" && ok "wrapper passes --reply" || bad "argv: $(cat "$ARGV")"
reset; seed_jid; out="$(run "get $JID" STUB_MODE=noreply)"; printf '%s' "$out" | grep -q 'still thinking' && ok "rc=3 -> still thinking" || bad "noreply: $out"
reset; seed_jid; out="$(run "get $JID" STUB_MODE=nojob)";  printf '%s' "$out" | grep -q 'no such job' && ok "rc=1 -> no such job" || bad "nojob: $out"

echo "6b. marker contract — get terminal states (audit marker fold)"
# failed/dead: rc=3 with a terminal marker is an ERROR, not eternal still-thinking
reset; seed_jid; out="$(run "get $JID" STUB_MODE=getfailed)"
printf '%s' "$out" | grep -q 'factory error' && ok "MXR_JOB_FAILED at get -> error, not still-thinking" || bad "getfailed: $out"
# done with no body: terminal "no answer", never polls forever (audit item 6)
reset; seed_jid; out="$(run "get $JID" STUB_MODE=doneempty)"
printf '%s' "$out" | grep -q 'produced no answer' && ok "MXR_DONE_EMPTY -> terminal no-answer message" || bad "doneempty: $out"
# ledger down: rc=1 WITHOUT MXR_NO_SUCH_JOB must NOT claim the id is unknown (audit item 5)
reset; seed_jid; out="$(run "get $JID" STUB_MODE=dbdown)"
printf '%s' "$out" | grep -q 'factory error' && ok "rc=1 sans marker -> factory error (not 'no such job' lie)" || bad "dbdown: $out"
printf '%s' "$out" | grep -q 'no such job' && bad "dbdown claimed no-such-job" || ok "no-such-job lie suppressed while ledger down"
reset; out="$(run "get $JID")";                             printf '%s' "$out" | grep -q 'denied: not a phone-issued job' && ok "FOREIGN jid denied (ownership)" || bad "foreign: $out"
no_dispatch && ok "foreign jid never reached mxr" || bad "foreign dispatched"
reset; out="$(run 'get deadbeef00')";                       printf '%s' "$out" | grep -q 'denied: not a job id' && ok "prefix jid denied (full uuid only)" || bad "prefix: $out"
reset; out="$(run 'get zznothex')";                         printf '%s' "$out" | grep -q 'denied: not a job id' && ok "bad jid denied pre-dispatch" || bad "jid: $out"
reset; out="$(run 'get $(reboot)')";                        printf '%s' "$out" | grep -q 'denied: not a job id' && ok "hostile jid denied" || bad "jid2: $out"

echo "7. reel stub — no dispatch, cap still charges"
reset; out="$(run 'reel gym motivation monday')"
printf '%s' "$out" | grep -q 'NOT submitted' && ok "honest stub message" || bad "reel: $out"
no_dispatch && ok "reel stub never reached mxr" || bad "reel dispatched!"
[ "$(cat "$STATE/.phonecap-reel-$(date +%Y%m%d)")" = "1" ] && ok "reel cap charged" || bad "cap not charged"
for i in 2 3 4 5; do run "reel topic $i" >/dev/null; done
out="$(run 'reel topic six')"; printf '%s' "$out" | grep -q 'denied: reel cap' && ok "6th reel denied (5/day)" || bad "reel cap: $out"

echo "8. ask/status caps + octal traps"
reset; printf '50' > "$STATE/.phonecap-ask-$(date +%Y%m%d)"
out="$(run 'ask research q')"; printf '%s' "$out" | grep -q 'denied: ask cap' && ok "ask cap enforced" || bad "askcap: $out"
reset; printf '60' > "$STATE/.phonecap-status-$(date +%Y%m%d%H)"
out="$(run status)"; printf '%s' "$out" | grep -q 'denied: status cap' && ok "status hourly cap enforced" || bad "statuscap: $out"
reset; printf '08' > "$STATE/.phonecap-ask-$(date +%Y%m%d)"   # octal trap
out="$(run 'ask research q')"; printf '%s' "$out" | grep -q 'ANSWER' && ok "leading-zero count read base-10 (8<50 -> allowed)" || bad "octal: $out"
reset; printf '3' > "$STATE/.phonecap-ask-$(date +%Y%m%d)"; chmod 000 "$STATE/.phonecap-ask-$(date +%Y%m%d)"
out="$(run 'ask research q')"; chmod 600 "$STATE/.phonecap-ask-$(date +%Y%m%d)" 2>/dev/null
printf '%s' "$out" | grep -q 'denied: ask cap' && ok "UNREADABLE counter fails CLOSED (M-7)" || bad "unreadable-cap: $out"

echo "8b. cap RMW under real concurrency (r4 MAJOR-1: the flock must HOLD across the exec)"
# 12 parallel sessions increment the status counter; a lock that releases at exec (the
# perl FD_CLOEXEC default) loses increments to the racy read-modify-write and this reads
# short. status takes no conc slot, so all 12 run truly concurrently.
reset; _stamp="$(date +%Y%m%d%H)"
for i in $(seq 1 12); do run status >/dev/null 2>&1 & done
wait
_c="$(cat "$STATE/.phonecap-status-$_stamp" 2>/dev/null || echo 0)"
[ "$_c" = "12" ] && ok "12 concurrent sessions -> counter exactly 12 (no lost increments)" || bad "lost increments: counter=$_c (want 12)"

echo "9. concurrency slots"
# slots are kernel flocks on permanent slot FILES (r3 HIGH-1) — a holder is a live process
# holding the lock, and a DEAD holder's slot frees itself (no reaper, nothing to backdate).
_hold_slot(){ # flock the slot file in a background process until killed; pid in $! — NOT
  # $(captured): the capture pipe would block on the bg holder's inherited stdout until
  # its sleep ended, i.e. the holders would be dead before the assertion ran.
  perl -MFcntl=:flock -e 'open(my $f, ">>", $ARGV[0]) or exit 1; flock($f, LOCK_EX) or exit 1; sleep 60' "$1" >/dev/null 2>&1 &
}
reset
_hold_slot "$STATE/.phone-conc1"; h1=$!
_hold_slot "$STATE/.phone-conc2"; h2=$!
sleep 0.3   # let both holders take their locks
out="$(run 'ask research q')"; printf '%s' "$out" | grep -q 'denied: factory line busy' && ok "both slots held -> busy" || bad "conc: $out"
kill -9 "$h1" 2>/dev/null; wait "$h1" 2>/dev/null
out="$(run 'ask research q')"; printf '%s' "$out" | grep -q 'ANSWER' && ok "SIGKILLed holder's slot immediately reusable (kernel lock, no stale state)" || bad "killed-holder conc: $out"
kill "$h2" 2>/dev/null; wait "$h2" 2>/dev/null

echo "10. log policy — sha-only, 0600, deny-on-unwritable"
reset; run 'ask research super secret gym numbers' >/dev/null
grep -q 'super secret' "$STATE/mxr-phone.log" && bad "payload text leaked into log" || ok "no payload text in log"
grep -q 'sha=' "$STATE/mxr-phone.log" && ok "sha recorded" || bad "no sha in log"
p="$(stat -f %Lp "$STATE/mxr-phone.log")"; [ "$p" = "600" ] && ok "log perms 0600" || bad "log perms $p"
reset; chmod a-w "$STATE"; out="$(run status)"; chmod u+w "$STATE"
printf '%s' "$out" | grep -q 'denied' && ok "unwritable state denies (fail-closed)" || bad "unwritable: $out"

echo "11. output hygiene — ANSI stripped, 4KB answer-first truncation"
reset; out="$(run 'ask research colorful' STUB_MODE=ansi)"
printf '%s' "$out" | LC_ALL=C grep -q $'\033' && bad "ESC survived" || ok "ANSI stripped"
reset; out="$(run 'ask research huge' STUB_MODE=bigout)"
printf '%s' "$out" | grep -q 'truncated' && ok "truncation marker" || bad "no truncation marker"
[ "$(printf '%s' "$out" | wc -c)" -lt 4300 ] && ok "output capped near 4KB" || bad "output $(printf '%s' "$out" | wc -c)B"

echo "12. env scrub — inherited env influence never reaches CHILDREN (the client-side
#    channel itself is killed by sshd AcceptEnv empty — asserted in the --sshd leg)"
reset
run 'ask research q' MXR_TIMEOUT_S=7 PYTHONPATH=/tmp/evil BASH_ENV=/tmp/evil >/dev/null
grep -q '^ENV_MXR_TIMEOUT_S=45$' "$ARGV" && ok "inherited MXR_TIMEOUT_S scrubbed (45 not 7)" || bad "scrub: $(grep ENV_MXR "$ARGV")"
grep -q '^ENV_BASH_ENV=unset$' "$ARGV" && ok "BASH_ENV not passed to children" || bad "BASH_ENV leaked: $(grep ENV_BASH "$ARGV")"
grep -q '^ENV_PYTHONPATH=unset$' "$ARGV" && ok "PYTHONPATH not passed to children" || bad "PYTHONPATH leaked: $(grep ENV_PY "$ARGV")"

echo; echo "=== $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
