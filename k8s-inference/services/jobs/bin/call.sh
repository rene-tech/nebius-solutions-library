#!/bin/bash
# Main container of an endpoint-call job (API mode `async`, docs/API.md "Endpoint calls").
#
# Managed I/O (no execution block): readiness first (READY_URL, below), then POST REQUEST_BODY to URL exactly once;
# the uploader exports out/ to the platform bucket. 408/429 PROVEN to come from the edge (edge_refusal: Envoy's
# x-envoy-ratelimited or the hook's admission refusal) are asked again with backoff, a model's own 408/429 is its
# answer like any other 4xx (exit 2, delivered once); no response (000, or a 5xx without a body: the connection
# broke or the edge gave up) is an unknown outcome (exit 4), never a second POST; a 5xx with a body is the endpoint's
# error (exit 5). The Job's podFailurePolicy turns 2/4/5 into an immediate FailJob (no pod retries). Before jobs
# 0.1.30 the managed path posted into a cold endpoint and re-posted on every 5xx/000 for as long as the Job lived.
#
# Application I/O (IO_MODE=application, a private binding mounted at PRIVATE_INPUT_DIR): the request comes from the
# binding (request.json, or input_url fetched at transfer time), the capture goes to the application's own
# destination (response_url, call_url: presigned PUT URLs; with refresh_url + refresh_token the caller asks the
# application for fresh URLs at every transfer, with its projected identity token when one is mounted), the stable
# result_ref is handed to the uploader's attempt record through RESULT.json. The capture is written to the retained
# work volume first (response.bin, then call.json last: the completion marker) and uploaded after; a failed upload
# leaves export=failed for the record (EXPORT_FAILED) and the capture on the volume for a transfer-only retry
# (TRANSFER_ONLY=1: no inference, upload what is captured; nothing complete -> exit 3). Readiness is probed first
# with an idempotent GET (READY_URL) so the one inference POST meets an endpoint that answers; the POST is made
# exactly once per operation (posted.json is the marker): no response means an unknown outcome (exit 4, FAILED),
# never a second POST; a response with any status is a capture and is delivered. The exit code is the inference
# outcome (0 = 2xx, 2 = 4xx, 5 = another status), on the first delivery and on every re-delivery; the delivery
# itself is reported through RESULT.json (export succeeded | failed).
#
# The URL is the endpoint's gateway hostname (TLS verified, key in AUTH_HEADER, ADMITTED = the API's admission token
# so the gateway hook counts the call once); CONNECT_TO (host:443:svc:443) routes the connection to the gateway's
# in-cluster Service instead of the public IP, while the TLS name and Host stay the public hostname.
set -uo pipefail
cd /work || exit 1
mkdir -p out
PRIV="${PRIVATE_INPUT_DIR:-}"

curl_auth=()
[ -n "${AUTH_HEADER:-}" ] && curl_auth+=(-H "Authorization: $AUTH_HEADER")
[ -n "${ADMITTED:-}" ] && curl_auth+=(-H "x-serverless2-admitted: $ADMITTED")
# the admission is bound to this operation and attempt (docs/API.md "Endpoint calls"): the gateway's hook verifies the
# token against the operation and checks that the operation is active, owned by this key, and that this attempt is
# its live pod; the headers name them, the token proves them
[ -n "${ADMITTED:-}" ] && [ -n "${OPERATION:-}" ] && curl_auth+=(-H "x-serverless2-operation: $OPERATION")
[ -n "${ADMITTED:-}" ] && [ -n "${POD_NAME:-}" ] && curl_auth+=(-H "x-serverless2-attempt: $POD_NAME")
[ -n "${CONNECT_TO:-}" ] && curl_auth+=(--connect-to "$CONNECT_TO")

backoff() {   # exponential, BACKOFF_BASE (5 s) x 2^n, capped at BACKOFF_MAX (300 s)
  local e=$1; [ "$e" -gt 6 ] && e=6
  local s=$(( ${BACKOFF_BASE:-5} * (1 << e) )); [ "$s" -gt "${BACKOFF_MAX:-300}" ] && s=${BACKOFF_MAX:-300}
  sleep "$s"
}

binding() { [ -n "$PRIV" ] && [ -f "$PRIV/$1" ] && cat "$PRIV/$1"; }

edge_refusal() {   # $1 = the response headers of a 408/429: true only for a PROVEN pre-execution refusal, the edge's
  # rate limit (Envoy sets x-envoy-ratelimited) or the gateway hook's admission refusal (X-Serverless2-Admission:
  # refused); a model can answer 408/429 itself after partial execution, so a bare status is never asked again
  [ -f "$1" ] && grep -qiE '^(x-envoy-ratelimited: *true|x-serverless2-admission: *refused)' "$1"
}

durable() {   # the named files (and the directory entries) reach stable storage before anything depends on them:
  # a node that dies right after a write must not leave a marker or capture that only the page cache had seen; on a
  # shared filesystem a stream that reached the server is not yet on disk either. busybox sync takes files (-f: the
  # containing filesystem); the whole-system sync is the fallback
  sync -f "$@" 2>/dev/null || sync -f . 2>/dev/null || sync
}

ready_pause() {   # readiness only: READY_BACKOFF_BASE (2 s) x 2^(n-1), capped at READY_BACKOFF_MAX (10 s); never the POST's backoff
  local e=$(( $1 - 1 )); [ "$e" -gt 4 ] && e=4
  local s=$(( ${READY_BACKOFF_BASE:-2} * (1 << e) )); [ "$s" -gt "${READY_BACKOFF_MAX:-10}" ] && s=${READY_BACKOFF_MAX:-10}
  sleep "$s"
}

refresh() {   # $1 = phase (input|output): fresh URLs from the application, or the static ones from the binding
  local url token body attempt_id
  url=$(binding refresh_url); token=$(binding refresh_token)
  if [ -z "$url" ]; then
    INPUT_URL=$(binding input_url); RESPONSE_URL=$(binding response_url); CALL_URL=$(binding call_url); return 0
  fi
  attempt_id="${POD_NAME:-$(hostname)}"
  body=$(jq -cn --arg op "${OPERATION:-}" --arg a "$attempt_id" --arg p "$1" '{operation_id:$op,attempt_id:$a,phase:$p}')
  local -a h=(-H "Content-Type: application/json" -H "Authorization: Bearer $token")
  [ -n "${IDENTITY_TOKEN_FILE:-}" ] && [ -r "$IDENTITY_TOKEN_FILE" ] && h+=(-H "X-Workload-Identity: $(cat "$IDENTITY_TOKEN_FILE")")
  local n=0 code
  while :; do
    code=$(curl -sS -o out/refresh.json -w '%{http_code}' -X POST "$url" "${h[@]}" --data-binary "$body" --max-time 60 2>out/refresh_error.txt) || code=000
    n=$((n + 1)); echo "$(date -u +%FT%TZ) refresh $1 attempt $n -> HTTP $code"
    case "$code" in
      2*) INPUT_URL=$(jq -r '.input_url // empty' out/refresh.json); RESPONSE_URL=$(jq -r '.response_url // empty' out/refresh.json)
          CALL_URL=$(jq -r '.call_url // empty' out/refresh.json); return 0;;
      401|403|404) echo "refresh refused ($code): $(head -c 300 out/refresh.json)"; return 1;;
    esac
    [ "$n" -ge 20 ] && return 1
    backoff $n
  done
}

put() {   # $1 = file, $2 = presigned URL, $3 = content type
  local n=0 code
  while :; do
    code=$(curl -sS -o out/put_error.txt -w '%{http_code}' -X PUT "$2" -H "Content-Type: $3" --data-binary "@$1" --max-time 900 2>&1) || code=000
    n=$((n + 1)); echo "$(date -u +%FT%TZ) upload $1 attempt $n -> HTTP $code"
    case "$code" in 2*) return 0;; 403|404) return 1;; esac   # an expired or wrong URL: ask for a fresh one
    [ "$n" -ge 5 ] && return 1
    backoff $n
  done
}

deliver() {   # upload the capture; with refresh, fresh URLs first, and once more after a 403 (expired URL)
  local _round
  for _round in 1 2; do
    refresh output || return 1
    if [ -z "${RESPONSE_URL:-}" ] || [ -z "${CALL_URL:-}" ]; then echo "no response_url/call_url to deliver to"; return 1; fi
    put response.bin "$RESPONSE_URL" "$(jq -r '.content_type // "application/octet-stream"' call.json)" \
      && put call.json "$CALL_URL" "application/json" && return 0
  done
  return 1
}

finish() {   # $1 = export state; RESULT.json is what the uploader's attempt record carries (upload.sh record mode)
  local ref; ref=$(binding result_ref)
  local wrote=0
  if [ "$1" = succeeded ]; then
    # the capture manifest (call.json, already durable) travels with the result record: the platform's uploader posts
    # it to the API (docs/JOBS.md "Endpoint children"); a re-delivery carries the ORIGINAL producer's manifest unchanged
    cap=$(jq -c '{sha256, content_type, size_bytes, http_status, capture_attempt_id: .attempt_id, posted_at: .timings.posted_at, completed_at: .timings.completed_at}' call.json 2>/dev/null || echo null)
    jq -cn --arg r "$ref" --arg a "${CAPTURE_ATTEMPT:-${POD_NAME:-}}" --argjson c "${cap:-null}" '{result_ref:$r, export:"succeeded", capture_attempt_id:$a} + (if $c != null and $c.sha256 != null then {capture:$c} else {} end)' > RESULT.json.tmp && wrote=1
  else
    [ -n "${2:-}" ] && echo "${2:0:300}" > /dev/termination-log 2>/dev/null   # the cause reaches the attempt (jobs 0.1.31)
    jq -cn --arg r "$ref" --arg e "${2:-}" '{export:"failed", error:$e} + (if $r != "" then {result_ref_pending:$r} else {} end)' > RESULT.json.tmp && wrote=1
  fi
  # the write itself is checked (jq's exit status, and the file parses as JSON: a short write leaves a non-empty
  # fragment), then the rename, then the sync
  if [ "$wrote" != 1 ] || ! jq -e . RESULT.json.tmp > /dev/null 2>&1 || ! mv RESULT.json.tmp RESULT.json || ! durable RESULT.json .; then
    # no export state may be claimed by a record that was not written, not renamed, or that a node loss could erase:
    # whatever is there is taken back and the caller ends retryable (exit 6); a fresh attempt delivers the kept
    # capture again (idempotent PUTs) and finishes
    rm -f RESULT.json RESULT.json.tmp; echo "RESULT.json could not be written durably: removed (no export state is claimed)"; return 1
  fi
  return 0
}

not_durable_exit() {   # $1 = what: a durability failure AFTER the POST: the capture (if any) stays, nothing is delivered,
  # the outcome is unknown (exit 4: never re-posted; nothing to export until a complete durable capture exists)
  echo "$1 could not be made durable: outcome unknown, not delivering, not posting again" | tee /dev/termination-log 2>/dev/null
  exit 4
}

# readiness first, with an idempotent GET through the same gateway (a cold or scaled-to-zero endpoint is woken by it and
# answers when it serves); the inference POST goes to an endpoint that answers, so the edge never cuts a cold start.
# The gateway and the activator HOLD the GET while the replica starts, so a probe that ends without an answer (000 after
# READY_TIMEOUT) says only "not yet": the probes are frequent and short (READY_TIMEOUT 30 s, pauses 2 s doubling to
# READY_BACKOFF_MAX 10 s), so a replica that becomes ready is seen within about 40 s, never minutes later (the previous
# exponential pause reached 320 s by the sixth probe). Bounded by READY_MAX_WAIT (default CALL_TIMEOUT) of wall-clock
# time: past it the call fails explicitly instead of running into the Job's deadline. The probe counter is its own: the
# inference POST's retry budget starts fresh and the POST is still made exactly once.
# Returns 0 ready, 1 refused (401/403: READY_CODE), 2 not ready within the bound (READY_CODE = the last answer).
wait_ready_one() {   # $1 = readiness URL, $2 = bound in seconds: one target's probes (the loop of jobs <= 0.1.33)
  READY_CODE=""; READY_PROBES=0; READY_WAITED=0
  [ -n "$1" ] || return 0
  local n=0 code ready_start=$SECONDS ready_max=$2
  while :; do
    code=$(curl -sS -o out/ready.txt -w '%{http_code}' "$1" "${curl_auth[@]}" --max-time "${READY_TIMEOUT:-30}" 2>out/ready_error.txt) || code=000
    n=$((n + 1)); echo "$(date -u +%FT%TZ) readiness probe $n -> HTTP $code"
    READY_CODE=$code; READY_PROBES=$n; READY_WAITED=$(( SECONDS - ready_start ))
    case "$code" in
      2*|405) return 0;;
      401|403) echo "readiness probe refused ($code): $(head -c 200 out/ready.txt)"; return 1;;
    esac
    if [ "$READY_WAITED" -ge "$ready_max" ]; then
      echo "endpoint not ready after ${READY_WAITED}s ($n probes, last HTTP $code): giving up before the inference"
      return 2
    fi
    ready_pause $n
  done
}

# Pre-send fallback across declared targets (jobs 0.1.35, docs/API.md "Variants"): TARGETS is a JSON list of
# {id, ready_url, url} in the order the API resolved (the first = READY_URL/URL as before; without TARGETS that one
# target is the list). Each target but the last is probed for at most TARGET_COLD_WAIT seconds, the last for what is
# left of READY_MAX_WAIT; the first that answers becomes URL and is recorded in out/target.json BEFORE any POST, so
# the one inference request goes to exactly one target and nothing was sent to the others. A refusal (401/403) is the
# key's and ends the call at once; nothing ready anywhere ends it with nothing sent (exit 5).
wait_ready() {
  local targets="" n i total id ready url bound rc waited_total=0 start=$SECONDS
  local ready_max=${READY_MAX_WAIT:-${CALL_TIMEOUT:-3600}}
  if [ -n "${TARGETS:-}" ]; then
    targets=$(printf '%s' "$TARGETS" | jq -c '.[]' 2>/dev/null) || { echo "TARGETS is not a JSON list; using READY_URL/URL"; targets=""; }
  fi
  [ -n "$targets" ] || targets=$(jq -cn --arg r "${READY_URL:-}" --arg u "${URL:-}" '{id:"primary", ready_url:$r, url:$u}')
  total=$(printf '%s\n' "$targets" | grep -c .); i=0
  while IFS= read -r t; do
    i=$((i + 1)); id=$(printf '%s' "$t" | jq -r '.id'); ready=$(printf '%s' "$t" | jq -r '.ready_url // ""'); url=$(printf '%s' "$t" | jq -r '.url')
    waited_total=$(( SECONDS - start ))
    if [ "$i" -lt "$total" ]; then bound=${TARGET_COLD_WAIT:-$ready_max}; [ $((ready_max - waited_total)) -lt "$bound" ] && bound=$((ready_max - waited_total)); else bound=$((ready_max - waited_total)); fi
    [ "$bound" -lt 0 ] && bound=0
    [ "$total" -gt 1 ] && echo "$(date -u +%FT%TZ) target $i/$total $id: probing for up to ${bound}s"
    wait_ready_one "$ready" "$bound"; rc=$?
    case "$rc" in
      0) URL=$url
         jq -cn --arg id "$id" --arg u "$url" --argjson i "$i" --argjson n "$total" --argjson p "$READY_PROBES" --argjson w "$READY_WAITED" \
           '{id:$id, url:$u, index:$i, of:$n, probes:$p, waited_s:$w}' > out/target.json 2>/dev/null
         [ "$total" -gt 1 ] && echo "$(date -u +%FT%TZ) target $id ready: the inference request goes there"
         return 0;;
      1) return 1;;
      2) [ "$i" -lt "$total" ] && echo "$(date -u +%FT%TZ) target $id not ready within ${READY_WAITED}s: nothing was sent to it; next target";;
    esac
  done <<< "$targets"
  return 2
}

if [ "${IO_MODE:-managed}" != application ]; then
  managed_end() {   # $1 = exit code, $2 = HTTP status, $3 = attempts, $4 = outcome, $5 = message: the record for the uploader
    jq -cn --argjson st "${2:-0}" --argjson n "${3:-0}" --arg o "$4" --arg e "$5" \
      '{http_status:$st, attempts:$n, outcome:$o} + (if $e != "" then {error:$e} else {} end)' > out/call.json
    [ -n "$5" ] && { echo "$5"; echo "$5" > /dev/termination-log 2>/dev/null; }
    exit "$1"
  }
  wait_ready; rc=$?
  [ "$rc" = 1 ] && managed_end 2 "$READY_CODE" 0 refused "the endpoint refused the call's key ($READY_CODE)"
  [ "$rc" = 2 ] && managed_end 5 "$READY_CODE" 0 not_ready "the endpoint did not become ready within ${READY_WAITED}s ($READY_PROBES probes, last HTTP $READY_CODE); nothing was sent"
  n=0
  while :; do
    code=$(curl -sS -o out/response.json -D out/headers.txt -w '%{http_code} %{size_download}' -X POST "$URL" -H 'Content-Type: application/json' \
           "${curl_auth[@]}" --data-binary "$REQUEST_BODY" --max-time "${CALL_TIMEOUT:-3600}" 2>out/last_error.txt) || code="000 0"
    read -r http size <<< "$code"
    n=$((n + 1))
    echo "$(date -u +%FT%TZ) inference POST $n -> HTTP $http (${size} bytes)"
    case "$http" in
      2*) managed_end 0 "$http" "$n" ok "";;
      408|429) if edge_refusal out/headers.txt; then   # proven pre-execution refusal at the edge: safe to ask again
                 [ "$n" -lt 30 ] && { backoff $n; continue; }
                 managed_end 5 "$http" "$n" rejected "the edge rejected the call $n times (HTTP $http)"
               fi
               cat out/response.json; managed_end 2 "$http" "$n" client_error "the endpoint itself answered HTTP $http (not the edge): delivered as is, not asked again";;
      4*) cat out/response.json; managed_end 2 "$http" "$n" client_error "the endpoint answered HTTP $http";;   # caller's error: FailJob
    esac
    if [ "$http" = 000 ] || [ "$size" = 0 ]; then
      # no response: the connection broke or the edge gave up before the first byte; the model may have run: unknown
      managed_end 4 "$http" "$n" unknown "inference outcome unknown: no response was captured (HTTP $http, $(head -c 200 out/last_error.txt | tr '\n' ' ')); not posting again"
    fi
    managed_end 5 "$http" "$n" endpoint_error "the endpoint answered HTTP $http"
  done
fi

# ---- application I/O ------------------------------------------------------------------------------------------
if [ -z "$PRIV" ] || [ ! -d "$PRIV" ]; then echo "application I/O needs the private binding (PRIVATE_INPUT_DIR)"; exit 2; fi

outcome_exit() {   # the inference outcome of a capture, independent of its delivery: 0 = 2xx, 2 = 4xx (the caller's
  local st=$1       # error), 5 = anything else the endpoint answered (its error); delivery is tracked by RESULT.json
  case "$st" in 2*) return 0;; 4*) return 2;; *) return 5;; esac
}

done_with() {   # $1 = HTTP status of the capture: exit with its outcome after a successful delivery
  local st=$1 rc; outcome_exit "$st"; rc=$?
  [ "$rc" = 0 ] || echo "endpoint answered HTTP $st (delivered)" > /dev/termination-log 2>/dev/null
  exit "$rc"
}

if [ "${TRANSFER_ONLY:-0}" = 1 ]; then
  # export retry: the capture of a previous attempt on this volume, complete only with its marker; the retry ends with
  # the captured outcome, so a delivered error never becomes a successful inference
  if [ -f call.json ] && [ -f response.bin ] && [ "$(jq -r '.capture_complete // false' call.json)" = true ]; then
    CAPTURE_ATTEMPT=$(jq -r '.attempt_id // empty' call.json)
    if deliver; then finish succeeded && done_with "$(jq -r '.http_status // 0' call.json)"; exit 6; fi
    finish failed "upload of the retained capture failed again" || true; exit 0
  fi
  echo "no complete capture on the work volume: nothing to export (inference is not repeated)"
  finish failed "no complete capture to export"; exit 3
fi

if [ -f call.json ] && [ "$(jq -r '.capture_complete // false' call.json)" = true ]; then
  echo "a complete capture exists already (a previous attempt of this operation): delivering it, not calling again"
  CAPTURE_ATTEMPT=$(jq -r '.attempt_id // empty' call.json)
  if deliver; then finish succeeded && done_with "$(jq -r '.http_status // 0' call.json)"; exit 6; fi
  finish failed "upload failed" || true; exit 0
fi

# the request: inline in the binding, or fetched at transfer time (a queued call may outlive a URL minted at submission)
refresh input || { finish failed "refresh of the input URL refused"; exit 2; }
if [ -n "${INPUT_URL:-}" ]; then
  n=0
  while :; do
    code=$(curl -sS -o request.bin -w '%{http_code}' "$INPUT_URL" --max-time 900 2>out/input_error.txt) || code=000
    n=$((n + 1)); echo "$(date -u +%FT%TZ) input fetch attempt $n -> HTTP $code"
    case "$code" in 2*) break;; 403|404) refresh input || true;; esac
    [ "$n" -ge 10 ] && { finish failed "the input could not be fetched (HTTP $code)"; exit 2; }
    backoff $n
  done
elif [ -f "$PRIV/request.json" ]; then
  cp "$PRIV/request.json" request.bin
else
  finish failed "the binding has neither request.json nor input_url"; exit 2
fi
# the bound request (input_sha256 in the binding: an endpoint child's immutable input reference, docs/JOBS.md "Endpoint
# children"): the bytes a renewable URL served must be exactly the bound ones, or nothing is posted (the caller's error)
bound_sha=$(binding input_sha256)
if [ -n "$bound_sha" ]; then
  have_sha=$(sha256sum request.bin | cut -d' ' -f1)
  if [ "$have_sha" != "$bound_sha" ]; then
    echo "the fetched request ($have_sha) is not the bound input ($bound_sha): not posting"
    finish failed "the request body does not match the bound input_sha256 ($have_sha != $bound_sha)"; exit 2
  fi
fi

# exactly one inference POST per operation: a previous attempt of this pod's Job that posted and died leaves
# posted.json without call.json; its outcome is unknown and is never re-posted (exit 4: the operation FAILS with that
# error, nothing to export; the application decides whether to submit again)
if [ -f posted.json ]; then
  echo "a previous attempt posted the request ($(jq -r '.attempt_id' posted.json) at $(jq -r '.posted_at' posted.json)) and left no capture: the inference outcome is unknown; not posting again"
  jq -cn --arg op "${OPERATION:-}" --arg a "$(jq -r '.attempt_id' posted.json)" '{operation_id:$op, attempt_id:$a, http_status:0, capture_complete:false, outcome:"unknown"}' > call.json.tmp && mv call.json.tmp call.json
  finish failed "inference outcome unknown: the request was posted by a previous attempt and no response was captured"
  echo "inference outcome unknown (posted, no response captured)" > /dev/termination-log 2>/dev/null
  exit 4
fi

# readiness first (wait_ready above): refused = the key's error (exit 2), not ready within the bound = exit 5 (FAILED)
wait_ready; rc=$?
[ "$rc" = 1 ] && { finish failed "the endpoint refused the call's key ($READY_CODE)"; exit 2; }
[ "$rc" = 2 ] && { finish failed "the endpoint did not become ready within ${READY_WAITED}s ($READY_PROBES probes, last HTTP $READY_CODE)"; exit 5; }

n=0
while :; do
  posted_epoch=$(date -u +%s)
  # the one-POST marker: written, renamed into place and on stable storage BEFORE the POST, each step checked (the
  # script runs without errexit): a marker that was not created, not renamed or not synced means NO POST. The attempt
  # ends retryable (exit 6) with the marker taken back (it names a POST that did not happen); a marker that could not
  # even be removed makes the next attempt refuse to post (exit 4)
  if ! jq -cn --arg a "${POD_NAME:-$(hostname)}" --argjson e "$posted_epoch" '{attempt_id:$a, posted_at:($e|todate)}' > posted.json.tmp \
     || ! [ -s posted.json.tmp ] || ! mv posted.json.tmp posted.json || ! [ -s posted.json ] || ! durable posted.json .; then
    rm -f posted.json posted.json.tmp
    finish failed "the one-POST marker could not be made durable; the request was NOT posted" || true
    echo "one-POST marker not durable: not posting (retryable)" > /dev/termination-log 2>/dev/null
    exit 6
  fi
  code=$(curl -sS -o response.bin -D headers.txt -w '%{http_code} %{time_starttransfer} %{time_total} %{size_download}' -X POST "$URL" \
         -H 'Content-Type: application/json' "${curl_auth[@]}" --data-binary @request.bin --max-time "${CALL_TIMEOUT:-3600}" 2>out/last_error.txt) || code="000 0 0 0"
  read -r http t_first t_total size <<< "$code"
  n=$((n + 1)); echo "$(date -u +%FT%TZ) inference POST $n -> HTTP $http (${t_total}s, ${size} bytes)"
  if { [ "$http" = 429 ] || [ "$http" = 408 ]; } && edge_refusal headers.txt && [ "$n" -lt 30 ]; then
    rm -f posted.json; backoff $n; continue   # PROVEN rejection at the edge before the model (edge_refusal): safe to ask again
  fi
  break
done
if [ "$http" = 000 ] || { [ "$size" = 0 ] && [ "${http:0:1}" = 5 ]; }; then
  # no response: the connection broke or the edge gave up before the first byte; the model may have run: unknown
  jq -cn --arg op "${OPERATION:-}" --arg a "${POD_NAME:-$(hostname)}" --argjson st "${http:-0}" --argjson e "$posted_epoch" \
     '{operation_id:$op, attempt_id:$a, http_status:$st, capture_complete:false, outcome:"unknown", timings:{posted_at:($e|todate)}}' > call.json.tmp && mv call.json.tmp call.json
  finish failed "inference outcome unknown: no response was captured (HTTP $http, $(head -c 200 out/last_error.txt | tr '\n' ' '))"
  echo "inference outcome unknown (HTTP $http, no response captured)" > /dev/termination-log 2>/dev/null
  exit 4
fi
ctype=$(grep -i '^content-type:' headers.txt | tail -1 | cut -d' ' -f2- | tr -d '\r')
sha=$(sha256sum response.bin | cut -d' ' -f1)
completed_epoch=$(date -u +%s)
jq -cn --arg op "${OPERATION:-}" --arg a "${POD_NAME:-$(hostname)}" --argjson st "${http:-0}" --arg ct "${ctype:-application/octet-stream}" \
   --argjson sz "$(stat -c %s response.bin)" --arg sha "$sha" --argjson p "$posted_epoch" --argjson c "$completed_epoch" --argjson tf "${t_first:-0}" --argjson tt "${t_total:-0}" \
   '{operation_id:$op, attempt_id:$a, http_status:$st, content_type:$ct, size_bytes:$sz, sha256:$sha,
     timings:{posted_at:($p|todate), first_byte_at:(($p + ($tf|floor))|todate), completed_at:($c|todate), duration_s:$tt}, capture_complete:true}' > call.json.tmp \
  || manifest_written=0
if [ "${manifest_written:-1}" != 1 ] || ! jq -e . call.json.tmp > /dev/null 2>&1; then
  # the manifest write itself failed (jq error or a short write leaving a fragment): the POST was made, no attempt may
  # trust a capture without its manifest: outcome unknown for good (posted.json stays), nothing delivered
  rm -f call.json.tmp
  finish failed "inference outcome unknown: the capture manifest could not be written (HTTP $http)" || true
  not_durable_exit "the capture manifest"
fi
if ! durable response.bin headers.txt .; then
  # the capture first, then its manifest: a manifest never names bytes that are not on stable storage. The POST was
  # made: the outcome is unknown for good (no manifest; posted.json stays), nothing is delivered, nothing re-posted
  rm -f call.json.tmp
  finish failed "inference outcome unknown: the captured response could not be made durable (HTTP $http)" || true
  not_durable_exit "the captured response"
fi
if ! [ -s call.json.tmp ] || ! mv call.json.tmp call.json; then
  # the manifest could not be written or renamed into place: the POST was made, the response may be there, but no
  # attempt can trust it without its manifest: outcome unknown for good (posted.json stays), nothing delivered
  rm -f call.json.tmp
  finish failed "inference outcome unknown: the capture manifest could not be written (HTTP $http)" || true
  not_durable_exit "the capture manifest"
fi
if ! durable call.json .; then
  # the bytes are durable, the manifest may not be: nothing is delivered now; a fresh attempt finds either the complete
  # manifest (and delivers without posting) or none (and refuses to post: posted.json). Retryable
  echo "capture manifest not durable: deferring delivery to a fresh attempt (retryable)" > /dev/termination-log 2>/dev/null
  exit 6
fi
CAPTURE_ATTEMPT="${POD_NAME:-$(hostname)}"
case "$http" in
  2*) ;;
  4*) echo "the endpoint answered $http (the caller's error): the response is captured and delivered as is";;
  *) echo "the endpoint answered $http (the endpoint's error): captured and delivered as is";;
esac
if deliver; then finish succeeded && done_with "$http"; exit 6; fi
finish failed "upload of the capture failed; the capture is kept on the work volume for POST /v1/operations/<id>:export" || true
exit 0
