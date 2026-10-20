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
echo "   ok upload.sh record(): 503/000 then 200 acknowledged (prior ack kept, transient body apart), 4 x transient recovered at the next poll, 409/401/403 never retried"
