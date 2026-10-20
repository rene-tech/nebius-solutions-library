#!/usr/bin/env bash
# services/jobs/bin/upload.sh record(): the API acknowledgement of an attempt record (jobs 0.1.29). The function
# under test is taken verbatim from the script; the kubelet, the API (curl) and the clock are stubbed. Cases: a start
# record behind a transient 503 is acknowledged on the next try without a checkpoint change; four transient failures
# leave it unacknowledged and the next poll recovers it; 409 (fenced) and 401/403 (rejected) are never retried; only
# an accepted or definite answer reaches attempts/.ack.json (a prior valid acknowledgement survives a transient
# answer, whose body goes to .ack.transient.json). Run by tools/check.sh.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
U="${UPLOAD_SH:-$ROOT/services/jobs/bin/upload.sh}"   # UPLOAD_SH: another copy of the script under test
T=$(mktemp -d); trap 'rm -rf "$T" /tmp/attempts/pod-upload-test.json /tmp/attempts/.ack.tmp' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
sed -n '/^record() {/,/^}/p' "$U" > "$T/record.sh"
[ -s "$T/record.sh" ] || fail "record() not found in upload.sh"
# the per-poll recovery of an unacknowledged start, verbatim from the main loop
# shellcheck disable=SC2016
sed -n '/if \[ "${START_ACK:-1}" = 0 \]; then/,/^    fi/p' "$U" > "$T/poll.sh"
[ -s "$T/poll.sh" ] || fail "the per-poll start recovery block not found in upload.sh"
# shellcheck disable=SC2016
grep -q 'case "$ACK" in 200|409|401|403|"") START_ACK=1' "$U" || fail "the first start record does not set START_ACK"

run() { # <expected calls> <expected ACK> <prior ack or -> <codes...>: record started against an API answering the codes in turn
  local want_calls=$1 want_ack=$2 prior=$3; shift 3
  bash -c '
    set -u
    PRIOR=$1; shift; CODES=("$@"); : > "$T/calls"
    pod() { :; }
    field() { echo ""; }
    sleep() { :; }
    mkdir() { [ "${2:-}" = /work/attempts ] && return 1; command mkdir "$@"; }   # the script falls back to /tmp/attempts
    calls() { wc -l < "$T/calls" | tr -d " "; }
    curl() { # the API: the body goes where -o says, like curl; a 200 carries an acknowledgement, anything else a detail
      echo x >> "$T/calls"; local n out="" code; n=$(calls); code=${CODES[$((n - 1))]:-${CODES[-1]}}
      while [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done
      [ "$code" = 000 ] && return 7
      if [ "$code" = 200 ]; then echo "{\"epoch\":1,\"checkpoint_ref\":\"c$n\"}" > "$out"; else echo "{\"detail\":\"answer $code\"}" > "$out"; fi
      printf "%s" "$code"; }
    MAIN=main; POD_NAME=pod-upload-test; OPERATION=op-1; MODE=sync; EXIT=""; DIGEST_SEEN=0; EXPORT=""
    API_URL=http://api.invalid; TOKEN_FILE=$T/token; echo t > "$TOKEN_FILE"
    cd "$T"
    rm -f /tmp/attempts/.ack.json /tmp/attempts/.ack.transient.json
    [ "$PRIOR" = - ] || { command mkdir -p /tmp/attempts; echo "$PRIOR" > /tmp/attempts/.ack.json; }
    . "$T/record.sh"
    record started > /dev/null
    case "$ACK" in 200|409|401|403|"") START_ACK=1 ;; *) START_ACK=0 ;; esac
    echo "$(calls) $ACK $START_ACK ack=$(cat /tmp/attempts/.ack.json 2>/dev/null || echo none) transient=$(cat /tmp/attempts/.ack.transient.json 2>/dev/null || echo none)"
    # the next poll: one more try while unacknowledged, none once acknowledged or rejected
    CODES=(200); : > "$T/calls"
    . "$T/poll.sh" > /dev/null
    echo "$(calls) $START_ACK ack=$(cat /tmp/attempts/.ack.json 2>/dev/null || echo none)"
  ' _ "$prior" "$@" > "$T/out" 2>"$T/err" || fail "record() exited non-zero for codes $*: $(cat "$T/err")"
  read -r calls ack start_ack rest < <(sed -n 1p "$T/out")
  [ "$calls" = "$want_calls" ] || fail "codes $*: $calls API calls, expected $want_calls"
  [ "$ack" = "$want_ack" ] || fail "codes $*: ACK=$ack, expected $want_ack"
  echo "$start_ack $rest | $(sed -n 2p "$T/out")"
}
export T
# 1. a transient 503 (with a JSON detail) then 200, no checkpoint change, a prior valid acknowledgement in place: the
#    503 body never touches .ack.json (the prior one stays until the 200 replaces it), it is kept as diagnostics
out=$(run 2 200 '{"epoch":1,"checkpoint_ref":"c0"}' 503 200)
[ "$out" = '1 ack={"epoch":1,"checkpoint_ref":"c2"} transient={"detail":"answer 503"} | 0 1 ack={"epoch":1,"checkpoint_ref":"c2"}' ] || fail "503 then 200: $out"
# 2. no answer (transport failure) then 200: acknowledged, no poll retry
out=$(run 2 200 - 000 200)
[ "$out" = '1 ack={"epoch":1,"checkpoint_ref":"c2"} transient=none | 0 1 ack={"epoch":1,"checkpoint_ref":"c2"}' ] || fail "000 then 200: $out"
# 3. four transient failures: unacknowledged after the batch, the prior acknowledgement untouched, the next poll
#    retries once and recovers
out=$(run 4 503 '{"epoch":1,"checkpoint_ref":"c0"}' 503 503 503 503)
[ "$out" = '0 ack={"epoch":1,"checkpoint_ref":"c0"} transient={"detail":"answer 503"} | 1 1 ack={"epoch":1,"checkpoint_ref":"c1"}' ] || fail "4 x 503: $out"
# 4. fenced (409) and rejected (401, 403): one call, the definite answer is the acknowledgement, never retried
for code in 409 401 403; do
  out=$(run 1 "$code" - "$code" 200)
  [ "$out" = "1 ack={\"detail\":\"answer $code\"} transient=none | 0 1 ack={\"detail\":\"answer $code\"}" ] || fail "$code: $out"
done
# 5. the bound: four tries, 10 s requests 3 s apart for a start record, 20 s / 20 s for a terminal one (docs/JOBS.md)
# shellcheck disable=SC2016
grep -q 'local tries=4 wait=20 tmo=20; \[ "$1" = started \] && { wait=3; tmo=10; }' "$U" || fail "start record bound changed; update docs/JOBS.md and this test"

# 6. jobs 0.1.31: the execution timer counts from MAIN's actual start (its running.startedAt), and ends main with TERM
#    then KILL only once timeout_s has elapsed after that start; never from the pod's or the Job's start
sed -n '/^main_pids() {/,/^}/p; /^epoch_of() {.*}$/p; /^kill_main() {/,/^}/p; /^enforce_execution_timeout() {/,/^}/p' "$U" > "$T/timer.sh"
[ "$(grep -c '^kill_main() {' "$T/timer.sh")" = 1 ] || fail "timer.sh extraction duplicated a function (an unterminated sed range)"
grep -q 'enforce_execution_timeout()' "$T/timer.sh" || fail "enforce_execution_timeout() not found in upload.sh"
timer() { # <EXECUTION_TIMEOUT_S> <main startedAt> <now epoch>: prints "rc killed TIMED_OUT reason"
  bash -c '
    set -u; . "$T/timer.sh"
    STARTED=$2; NOW=$3; EXECUTION_TIMEOUT_S=$1; MAIN=main; KILLED=0
    field() { case "$1" in *running.startedAt*) echo "$STARTED";; *) echo "";; esac; }
    date() { if [ "${1:-}" = -u ] && [ "${2:-}" = +%s ]; then echo "$NOW"; else command date "$@"; fi; }
    kill_main() { KILLED=1; [ "${DENY:-0}" = 1 ] && { DENIED_UID=0; return 1; }; return 0; }
    enforce_execution_timeout > /dev/null; rc=$?
    echo "$rc $KILLED ${TIMED_OUT:-0} ${REASON:-}"
  ' _ "$@" 2>/dev/null
}
# main started 2026-10-20T16:32:47Z = 1792513967; the Job resumed 16:32:00 and the pod waited: irrelevant to the timer
[ "$(timer 1200 2026-10-20T16:32:47Z 1792515166 | cut -d" " -f1-3)" = "1 0 0" ] || fail "timer fired 1199 s after main started"
out=$(timer 1200 2026-10-20T16:32:47Z 1792515167)
if [ "${out%% *}" != 0 ] || [ "$(echo "$out" | cut -d" " -f2-3)" != "1 1" ] || ! echo "$out" | grep -q 'timeout_s 1200 exceeded 1200s after main started'; then
  fail "timer did not end main at timeout_s after its start: $out"
fi
[ "$(timer 1200 "" 1792520000 | cut -d" " -f1-3)" = "1 0 0" ] || fail "timer must not fire while main has not started (no running.startedAt)"
# a main the runner cannot signal (another uid): the timer fires, claims no termination, names the Job deadline
out=$(DENY=1 timer 1200 2026-10-20T16:32:47Z 1792515167)
if [ "${out%% *}" != 0 ] || ! echo "$out" | grep -q 'cannot signal main.s processes (uid 0): the Job deadline ends the attempt' || echo "$out" | grep -q 'terminated by the platform'; then
  fail "timer must not claim a termination it could not deliver: $out"
fi
# kill_main itself: pid 1 (another uid) is denied at once without the grace wait; a same-uid child is ended
out=$(bash -c 'set -u; . "$T/timer.sh"; main_pids() { echo 1; }; sleep() { echo "slept $*"; }; kill_main; echo "rc=$? uid=${DENIED_UID:-}"' 2>/dev/null)
[ "$out" = "rc=1 uid=0" ] || fail "kill_main: a denied signal must return 1 at once with the uid: $out"
out=$(bash -c 'set -u; . "$T/timer.sh"; command sleep 300 & C=$!; main_pids() { kill -0 "$C" 2>/dev/null && echo "$C"; }; TERM_GRACE_S=1 kill_main; rc=$?; command sleep 0.2; kill -0 "$C" 2>/dev/null && alive=yes || alive=no; echo "rc=$rc alive=$alive"' 2>/dev/null)
[ "$out" = "rc=0 alive=no" ] || fail "kill_main: a same-uid main must be ended: $out"
[ "$(timer "" 2026-10-20T16:32:47Z 1792520000 | cut -d" " -f1-3)" = "1 0 0" ] || fail "no EXECUTION_TIMEOUT_S: no timer"
# 7. start-record mode: main starts only behind an accepted record (exit 0); 409/401/403 fail the step at once; a
#    transient answer is retried until START_RECORD_WAIT_S, then the step fails (exit 1); no loop, no upload
# the start-record block of the script, verbatim, with its mode test replaced so it runs when sourced
# shellcheck disable=SC2016
sed -n '/^if \[ "${1:-}" = start-record \]; then/,/^fi/p' "$U" | sed 's/^if \[ "${1:-}" = start-record \]; then/if true; then/' > "$T/start-record.sh"
grep -q 'record started' "$T/start-record.sh" || fail "start-record block not found in upload.sh"
run_start() { # <codes...>: exit code, calls, ACK
  bash -c '
    set -u; CODES=("$@"); : > "$T/calls"
    pod() { :; }; field() { echo ""; }; sleep() { :; }
    mkdir() { [ "${2:-}" = /work/attempts ] && return 1; command mkdir "$@"; }
    calls() { wc -l < "$T/calls" | tr -d " "; }
    curl() { echo x >> "$T/calls"; local n; n=$(calls); local code=${CODES[$((n - 1))]:-${CODES[-1]}}; local out=""; while [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done; [ -n "$out" ] && echo "{}" > "$out"; printf "%s" "$code"; }
    MAIN=main; POD_NAME=pod-upload-test; OPERATION=op-1; MODE=sync; EXIT=""; DIGEST_SEEN=0; EXPORT=""; API_URL=http://api.invalid; TOKEN_FILE=$T/token; echo t > "$TOKEN_FILE"
    START_RECORD_WAIT_S=${WAIT:-0}
    cd "$T"; . "$T/record.sh"
    . "$T/start-record.sh"
    echo "unreachable"
  ' _ "$@" > "$T/start.out" 2>/dev/null; echo "$? $(wc -l < "$T/calls" | tr -d " ")"
}
[ "$(run_start 200)" = "0 1" ] || fail "start-record: one accepted record then exit 0: $(run_start 200)"
grep -q 'start record acknowledged' "$T/start.out" || fail "start-record did not report its acknowledgement"
[ "$(run_start 409)" = "1 1" ] || fail "start-record: a fenced record fails the step at once: $(run_start 409)"
[ "$(run_start 403)" = "1 1" ] || fail "start-record: a rejected record fails the step at once: $(run_start 403)"
grep -q 'start record refused (403): main does not start' "$T/start.out" || fail "start-record: no refusal message"
[ "$(run_start 503 503 503 503)" = "1 4" ] || fail "start-record: transient answers past the bound fail the step: $(run_start 503 503 503 503)"
grep -q 'unacknowledged for 0s' "$T/start.out" || fail "start-record: no bound message"
[ "$(WAIT=100 run_start 503 503 503 503 200)" = "0 5" ] || fail "start-record: a transient answer within the bound is retried to acceptance: $(WAIT=100 run_start 503 503 503 503 200)"
# 8. the cause: a failed record without the platform's own REASON carries main's termination message, else the
#    kubelet's reason; a succeeded main carries none; the platform's reason wins
run_reason() { # <REASON> <main terminated state json>: the reason of the written record ("-" = none)
  bash -c '
    set -u; REASON=$1; TERM=$2
    pod() { :; }; sleep() { :; }
    mkdir() { [ "${2:-}" = /work/attempts ] && return 1; command mkdir "$@"; }
    curl() { printf 200; }
    POD="{\"status\":{\"containerStatuses\":[{\"name\":\"main\",\"state\":{\"terminated\":$TERM}}]}}"
    field() { jq -r --arg m "$MAIN" "$1" <<<"$POD"; }
    MAIN=main; POD_NAME=pod-upload-test; OPERATION=op-1; MODE=sync; EXIT=""; DIGEST_SEEN=0; EXPORT=""; API_URL=http://api.invalid; TOKEN_FILE=$T/token; echo t > "$TOKEN_FILE"
    cd "$T"; . "$T/record.sh"; record failed > /dev/null
    jq -r ".reason // \"-\"" /tmp/attempts/pod-upload-test.json
  ' _ "$@" 2>/dev/null
}
out=$(run_reason "" '{"exitCode":1,"message":"ValueError: bad ligand\n  at fold.py:12","reason":"Error"}')
[ "$out" = "ValueError: bad ligand   at fold.py:12" ] || fail "reason: main's termination message expected, got: $out"
out=$(run_reason "" '{"exitCode":137,"reason":"OOMKilled"}')
[ "$out" = OOMKilled ] || fail "reason: the kubelet reason expected without a message, got: $out"
out=$(run_reason "timeout_s 10 exceeded 10s after main started" '{"exitCode":143,"message":"killed"}')
[ "$out" = "timeout_s 10 exceeded 10s after main started" ] || fail "reason: the platform reason must win, got: $out"
out=$(run_reason "" '{"exitCode":0,"message":"done"}')
[ "$out" = - ] || fail "reason: a succeeded main carries no reason, got: $out"
out=$(run_reason "" "{\"exitCode\":1,\"message\":\"$(printf 'x%.0s' $(seq 1 400))\"}")
[ "${#out}" = 300 ] || fail "reason: bounded to 300 characters, got ${#out}"
# 9. application-managed I/O: upload() reads RESULT.json BEFORE its one terminal record (jobs 0.1.32): the record posted
#    carries export (and so the API never stores a preliminary "failed/n-a" row that a reader can take for the delivery)
# shellcheck disable=SC2016
sed -n '/^upload() {/,/^}/p' "$U" | sed 's#^  cd /work || return#  cd "$WORK" || return#; s#/work/RESULT.json#"$WORK/RESULT.json"#g' > "$T/upload-fn.sh"
grep -q 'cd "$WORK" || return' "$T/upload-fn.sh" || fail "upload() no longer starts with cd /work"
out=$(bash -c '
  set -u; WORK=$(mktemp -d); printf "%s" "$1" > "$WORK/RESULT.json"
  MODE=record; EXPORT=""; CALLS=0
  record() { CALLS=$((CALLS+1)); echo "record#$CALLS status=$1 export=${EXPORT:-unset}"; }
  . "$T/upload-fn.sh"; upload failed > "$WORK/log" 2>&1; grep "^record#" "$WORK/log"; rm -rf "$WORK"' _ '{"export":"succeeded","result_ref":"r"}' 2>/dev/null)
[ "$out" = "record#1 status=failed export=succeeded" ] || fail "application I/O: the one terminal record must carry the export read first: $out"
# 10. jobs 0.1.32: captured_error_delivered() releases only a trusted async caller's exit 2/5 with a delivered, proven,
#     acknowledged capture; exit 4, a run (mode run), a failed/partial export, a missing ACK or a failed proof keep the volume
sed -n '/^captured_error_delivered() {/,/^}/p' "$U" | sed 's#/work/RESULT.json#"$WORK/RESULT.json"#g; s#/usr/local/bin/scratch.sh proof /work#proof_stub#' > "$T/ced.sh"
grep -q 'proof_stub' "$T/ced.sh" || fail "captured_error_delivered() not found in upload.sh"
ced() { # <MODE> <OPERATION_MODE> <EXIT> <RESULT.json or -> <ACK> <proof rc>: prints rc of captured_error_delivered
  bash -c '
    set -u; WORK=$(mktemp -d); MODE=$1; OPERATION_MODE=$2; EXIT=$3; [ "$4" = - ] || printf "%s" "$4" > "$WORK/RESULT.json"; ACK=$5; API_URL=http://api.invalid
    proof_stub() { return '"$6"'; }
    . "$T/ced.sh"; captured_error_delivered; echo $?; rm -rf "$WORK"' _ "$1" "$2" "$3" "$4" "$5" 2>/dev/null
}
R='{"export":"succeeded","capture":{"sha256":"abc","http_status":422}}'
[ "$(ced record async 2 "$R" 200 0)" = 0 ] || fail "captured 4xx delivered+proven+ACKed must release"
[ "$(ced record async 5 "$R" 200 0)" = 0 ] || fail "captured 5xx delivered+proven+ACKed must release"
[ "$(ced record async 4 "$R" 200 0)" = 1 ] || fail "exit 4 (unknown outcome) must keep"
[ "$(ced record run 2 "$R" 200 0)" = 1 ] || fail "a scientific run (not an async caller) must keep its volume"
[ "$(ced sync async 2 "$R" 200 0)" = 1 ] || fail "managed I/O never releases through this path"
[ "$(ced record async 2 '{"export":"failed","capture":{"sha256":"abc"}}' 200 0)" = 1 ] || fail "a failed export must keep"
[ "$(ced record async 2 '{"export":"succeeded"}' 200 0)" = 1 ] || fail "no capture manifest must keep"
[ "$(ced record async 2 "$R" 503 0)" = 1 ] || fail "an unacknowledged record must keep"
[ "$(ced record async 2 "$R" 200 1)" = 1 ] || fail "a failed integrity proof must keep"
[ "$(ced record async 2 - 200 0)" = 1 ] || fail "no RESULT.json must keep"
echo "   ok upload.sh record(): timer from main start; start-record init step (200 releases main, 409/401/403 fail it, transients retried to the bound); the cause from main's termination message/reason; 503/000 then 200 acknowledged (prior ack kept, transient body apart), 4 x transient recovered at the next poll, 409/401/403 never retried"
