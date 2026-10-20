#!/usr/bin/env bash
# services/jobs/bin/call.sh, managed I/O (jobs 0.1.30): readiness first, then exactly one inference POST. The script
# runs as is with curl, sleep and the work directory stubbed (no network). Cases: a cold endpoint (probes without an
# answer, then ready) is waited for and posted to once; a POST without a response is an unknown outcome (exit 4, no
# second POST); a 5xx with a body is the endpoint's error (exit 5, one POST); 429 at the edge is asked again; a 4xx is
# the caller's error (exit 2); a refused probe (403) posts nothing (exit 2); not ready within the bound posts nothing
# (exit 5). Run by tools/check.sh.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
S="${CALL_SH:-$ROOT/services/jobs/bin/call.sh}"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
mkdir -p "$T/bin" "$T/work"
# the script's cwd is /work: run a copy that changes into the stub work directory instead
sed "s#^cd /work || exit 1#cd \"\$WORK\" || exit 1#" "$S" > "$T/call.sh"
# shellcheck disable=SC2016
grep -q 'cd "$WORK" || exit 1' "$T/call.sh" || fail "call.sh no longer starts with cd /work"
cat > "$T/bin/curl" <<'STUB'
#!/usr/bin/env bash
# scripted answers: GETS (readiness) and POSTS (inference) hold "code[:body]" words; -o names the body file
out=""; hdr=""; method=GET
while [ $# -gt 0 ]; do case "$1" in -o) out=$2; shift;; -D) hdr=$2; shift;; -X) method=$2; shift;; esac; shift; done
f="$LOG_DIR/$method.count"; n=$(( $(cat "$f" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$f"
if [ "$method" = POST ]; then set -- $POSTS; else set -- $GETS; fi
spec=${!n:-${@: -1}}; code=${spec%%:*}; body=${spec#*:}; [ "$body" = "$spec" ] && body=""
edge=0; case "$code" in *@edge) edge=1; code=${code%@edge};; esac   # "<code>@edge": the edge's own refusal (its header)
[ -n "$out" ] && printf '%s' "$body" > "$out"
[ -n "$hdr" ] && { printf 'HTTP/1.1 %s\r\n' "$code"; [ "$edge" = 1 ] && printf 'x-envoy-ratelimited: true\r\n'; printf '\r\n'; } > "$hdr"
[ "$code" = 000 ] && exit 7
[ "$method" = POST ] && printf '%s %s' "$code" "${#body}" || printf '%s' "$code"
STUB
cat > "$T/bin/sleep" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$T/bin/curl" "$T/bin/sleep"
run() { # <GETS> <POSTS> [READY_URL|-]: run the managed caller; prints "exit gets posts outcome http"
  # READY_MAX_WAIT is wall-clock in call.sh while sleep is a no-op here: the normal cases get a generous bound (the
  # scripted probes decide the outcome, never the clock); only the not-ready case sets a short one explicitly (WAIT)
  rm -rf "$T/work"; mkdir -p "$T/work"; rm -f "$T/GET.count" "$T/POST.count"
  local ready=${3:-https://ep.example/healthz}; [ "$ready" = - ] && ready=""
  (cd "$T" && WORK="$T/work" LOG_DIR="$T" GETS="$1" POSTS="$2" READY_URL="$ready" URL=https://ep.example/infer REQUEST_BODY='{"x":1}' \
     OPERATION=op-1 POD_NAME=pod-1 CALL_TIMEOUT=60 READY_MAX_WAIT="${WAIT:-600}" PATH="$T/bin:$PATH" bash "$T/call.sh" > "$T/log" 2>&1); local rc=$?
  echo "$rc $(cat "$T/GET.count" 2>/dev/null || echo 0) $(cat "$T/POST.count" 2>/dev/null || echo 0) $(jq -r '"\(.outcome) \(.http_status)"' "$T/work/out/call.json" 2>/dev/null || echo none)"
}
check() { # <name> <expected> <GETS> <POSTS> [READY_URL|-]: one run, its first result judged and reported as is
  local out; out=$(run "$3" "$4" "${5-https://ep.example/healthz}")
  [ "$out" = "$2" ] || fail "$1: got '$out', expected '$2'; $(tail -3 "$T/log" | tr '\n' '|')"
}
# 1. cold endpoint: two probes without an answer, then ready; the one POST succeeds
check "cold start then success" "0 3 1 ok 200" "000 000 200" "200:{\"ok\":1}"
[ "$(jq -c . "$T/work/out/response.json")" = '{"ok":1}' ] || fail "the response was not captured"
# 2. no response to the POST: unknown outcome, exit 4, never a second POST
check "no response" "4 1 1 unknown 0" "200" "000"
# 3. a 5xx without a body is unknown too; a 5xx with a body is the endpoint's error (exit 5), one POST either way
check "empty 503" "4 1 1 unknown 503" "200" "503"
check "500 with body" "5 1 1 endpoint_error 500" "200" "500:{\"error\":\"oom\"}"
# 4. a 429 PROVEN to come from the edge (x-envoy-ratelimited) is asked again; a model's own 408/429 is its answer,
#    taken once like any 4xx (exit 2, never a second POST)
check "edge 429 then 200" "0 1 3 ok 200" "200" "429@edge 429@edge 200:{}"
check "model 429" "2 1 1 client_error 429" "200" "429:{\"detail\":\"busy\"} 200:{}"
check "model 408" "2 1 1 client_error 408" "200" "408:{\"detail\":\"partial\"} 200:{}"
check "400" "2 1 1 client_error 400" "200" "400:{\"detail\":\"bad\"}"
# 5. a refused probe posts nothing (the key's error); not ready within the bound posts nothing (exit 5): the only
#    case the clock decides, so its bound is short and explicit
check "refused probe" "2 1 0 refused 403" "403:{\"detail\":\"no\"}" "200:{}"
out=$(WAIT=1 run "000" "200:{}"); read -r rc _ posts _ http <<< "$out"
if [ "$rc" != 5 ] || [ "$posts" != 0 ] || [ "$http" != 0 ]; then fail "not ready within the bound: $out"; fi
# 6. without READY_URL the POST is made directly, once
check "no readiness URL" "0 0 1 ok 200" "" "200:{}" -
echo "   ok call.sh managed: cold start waited then one POST; no response = unknown (exit 4, no re-POST); 5xx body = exit 5; edge 429 retried, model 408/429 taken once as a 4xx; refused/not-ready probes post nothing"
