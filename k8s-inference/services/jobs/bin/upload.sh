#!/bin/bash
# jq expressions intentionally use single quotes: jq, rather than the shell, expands their variables.
# shellcheck disable=SC2016
# Uploader container: waits until the main container of this pod has ended, then syncs /work (minus
# the inputs) to OUTPUT_PREFIX with a STATUS.json and an attempt record (attempts/<pod>.json: the
# operation's history and GPU-seconds survive the pod, which preemption deletes), and on success
# deletes the job's PVC (a failed attempt keeps it for a resume). Always exits 0: the pod's phase
# follows the main container.
#
# "Ended" is detected two ways: the pod status through the Kubernetes API (gives the exit code) and,
# because the pod shares its PID namespace, the absence of any process outside this container and
# the sandbox's pause (the kubelet does not refresh the status of a terminating pod, so after a
# SIGTERM the API alone would leave us waiting until the grace period's SIGKILL). On SIGTERM
# (preemption, cancel) it uploads what exists at once, then waits for main to finish its own
# SIGTERM handling (checkpoint) and uploads again.
set -uo pipefail
: "${POD_NAME:?POD_NAME must be set}"
# UPLOAD_MODE=record (application-managed I/O, docs/JOBS.md "Execution options"): no bucket, no credentials; only the
# attempt record is kept under /work/attempts (the run moves its own data).
MODE="${UPLOAD_MODE:-sync}"; EXPORT=""; OUTPUT_PREFIX="${OUTPUT_PREFIX:-}"; ACK=""; CKPT_SEEN=""
API="https://${KUBERNETES_SERVICE_HOST}:${KUBERNETES_SERVICE_PORT}"
TOKEN_FILE=/var/run/secrets/kubernetes.io/serviceaccount/token; TOKEN=$(cat "$TOKEN_FILE"); CA=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt
POD_URL="$API/api/v1/namespaces/$POD_NAMESPACE/pods/$POD_NAME"
MAIN="${MAIN_CONTAINER:-main}"; EXIT=""; POD="{}"; SEEN=0; DIGEST_SEEN=0

pod() { TOKEN=$(cat "$TOKEN_FILE"); POD=$(curl -sS --cacert "$CA" -H "Authorization: Bearer $TOKEN" "$POD_URL" 2>/dev/null || echo '{}'); }
field() { jq -r --arg m "$MAIN" "$1" <<<"$POD"; }

main_pids() {    # every process whose ancestry ends neither at this shell nor at pid 1 (pause): main's (shared PID namespace)
  local d p root rest pp
  local -a stat_fields
  for d in /proc/[0-9]*; do
    p=${d#/proc/}; root=$p
    while :; do
      rest=$(cat "/proc/$root/stat" 2>/dev/null) || break
      rest=${rest##*) }; read -r -a stat_fields <<< "$rest"; pp=${stat_fields[1]}
      [ -z "${pp:-}" ] || [ "$pp" = 0 ] && break
      root=$pp
    done
    [ "$root" = "$$" ] && continue
    [ "$root" = 1 ] && continue
    [ -d "$d" ] && echo "$p"
  done
}

epoch_of() { date -u -D %Y-%m-%dT%H:%M:%SZ -d "$1" +%s 2>/dev/null || date -u -d "$1" +%s 2>/dev/null; }

kill_main() {    # TERM to every process of main, KILL after TERM_GRACE_S (30): the platform ends the attempt.
  # The runner is uid 10001 with every capability dropped (render.RUNNER_SECURITY_CONTEXT): it can signal only
  # processes of its own uid. A main of another uid (root) refuses every signal (EPERM): return 1 at once, DENIED_UID
  # names its uid, and the caller must not claim the termination (the Job deadline ends such an attempt)
  local pids p err sent=0; DENIED_UID=""; pids=$(main_pids); [ -n "$pids" ] || return 0
  for p in $pids; do
    if err=$(kill -TERM "$p" 2>&1); then sent=1
    else case "$err" in *"not permitted"*) DENIED_UID=$(awk '/^Uid:/{print $2}' "/proc/$p/status" 2>/dev/null) ;; esac; fi
  done
  [ "$sent" = 1 ] || { [ -z "$DENIED_UID" ] || return 1; return 0; }   # nothing signalled: all gone, or all denied
  sleep "${TERM_GRACE_S:-30}" & wait $!
  pids=$(main_pids | tr '\n' ' '); [ -n "$pids" ] || return 0
  # shellcheck disable=SC2086
  kill -KILL $pids 2>/dev/null; return 0
}

enforce_execution_timeout() {   # jobs 0.1.31: timeout_s counts from MAIN's ACTUAL start (its running.startedAt), never from
  # the Job's resume or the pod's creation (node and pull time are the Job deadline's start-up allowance); 0 = enforced now
  [ -n "${EXECUTION_TIMEOUT_S:-}" ] && [ "${TIMED_OUT:-0}" = 0 ] || return 1
  local s se now; s=$(field '.status.containerStatuses[]? | select(.name==$m) | .state.running.startedAt // empty'); [ -n "$s" ] || return 1
  se=$(epoch_of "$s") || return 1; [ -n "$se" ] || return 1; now=$(date -u +%s)
  [ $(( now - se )) -ge "$EXECUTION_TIMEOUT_S" ] || return 1
  REASON="timeout_s ${EXECUTION_TIMEOUT_S} exceeded $(( now - se ))s after main started at $s"; TIMED_OUT=1
  if kill_main; then REASON="$REASON; terminated by the platform"
  else REASON="$REASON; the runner (uid $(id -u)) cannot signal main's processes (uid ${DENIED_UID:-?}): the Job deadline ends the attempt"; fi
  echo "$REASON"; echo "$REASON" > /dev/termination-log 2>/dev/null; return 0
}

main_alive() {   # any process whose ancestry ends neither at this shell nor at pid 1 (pause) belongs to another container
  local d p root rest pp
  local -a stat_fields
  for d in /proc/[0-9]*; do
    p=${d#/proc/}; root=$p
    while :; do
      rest=$(cat "/proc/$root/stat" 2>/dev/null) || break
      rest=${rest##*) }; read -r -a stat_fields <<< "$rest"; pp=${stat_fields[1]}
      [ -z "${pp:-}" ] || [ "$pp" = 0 ] && break
      root=$pp
    done
    [ "$root" = "$$" ] && continue
    [ "$root" = 1 ] && continue
    [ -d "$d" ] && return 0
  done
  return 1
}

record() {   # $1 = status; the attempt as the uploader sees it now (main may still be running on SIGTERM)
  pod
  local started ended node pool image digest result ckpt
  started=$(field '.status.containerStatuses[]? | select(.name==$m) | (.state.terminated.startedAt // .state.running.startedAt // empty)')
  ended=$(field '.status.containerStatuses[]? | select(.name==$m) | .state.terminated.finishedAt // empty')
  node=$(field '.spec.nodeName // empty')
  pool=$(field '.spec.nodeSelector["serverless2.nebius/pool"] // empty')
  image=$(field '.spec.containers[]? | select(.name==$m) | .image // empty')
  digest=$(field '.status.containerStatuses[]? | select(.name==$m) | .imageID // empty')
  [ -n "$digest" ] && DIGEST_SEEN=1   # the exact runtime digest is on record from this record on
  # the application's finalization record (docs/JOBS.md "Attempts and finalization"): RESULT.json next to the
  # outputs names the committed result and checkpoint; without it an application-managed run is not delivered.
  # CHECKPOINT.json ({"checkpoint_ref": ..., "generation": N}) is the live pointer the application publishes while it
  # runs; the acknowledged pointer is written back to CHECKPOINT.ack.json
  local result="" ckpt="" gen="" cap=""
  if [ -f /work/RESULT.json ]; then
    result=$(jq -r '.result_ref // empty' /work/RESULT.json 2>/dev/null); ckpt=$(jq -r '.checkpoint_ref // empty' /work/RESULT.json 2>/dev/null)
    cap=$(jq -c '.capture // empty' /work/RESULT.json 2>/dev/null)   # the caller's capture manifest (docs/JOBS.md "Endpoint children")
  fi
  if [ -z "$ckpt" ] && [ -f /work/CHECKPOINT.json ]; then
    ckpt=$(jq -r '.checkpoint_ref // empty' /work/CHECKPOINT.json 2>/dev/null); gen=$(jq -r '.generation // empty' /work/CHECKPOINT.json 2>/dev/null)
  fi
  # the record goes next to the outputs; a volume this container cannot write (a misconfigured claim) must not stop
  # the record from reaching the API: it is kept under /tmp then and the upload step reports the volume
  REC=/work/attempts
  if ! mkdir -p "$REC" 2>/dev/null || ! [ -w "$REC" ]; then REC=/tmp/attempts; mkdir -p "$REC"; echo "WARN: /work is not writable by the uploader; records kept under $REC"; fi
  [ "$1" = started ] && ended="" || ended="${ended:-$(date -u +%FT%TZ)}"   # a start record has no end yet
  # the cause (bounded): the platform's own reason (timeout) first, else main's termination message (call.sh and
  # applications write it to /dev/termination-log), else the kubelet's reason (OOMKilled, Error); only for a failed
  # main, and persisted by the API so the cause survives the pod's deletion
  local rsn="${REASON:-}"
  if [ -z "$rsn" ] && [ "$1" != started ]; then
    rsn=$(field '.status.containerStatuses[]? | select(.name==$m) | .state.terminated // empty | select(.exitCode != 0) | (.message // .reason // empty)' | tr '\n' ' ' | sed 's/[[:space:]]*$//' | head -c 300)
  fi
  jq -cn --arg op "$OPERATION" --arg pod "$POD_NAME" --arg node "$node" --arg pool "$pool" --arg st "$1" --argjson exit "${EXIT:-null}" \
    --argjson gpus "${GPUS:-0}" --arg cls "${GPU_CLASS:-}" --arg s "$started" --arg e "$ended" --arg img "$image" --arg dg "$digest" \
    --arg exp "${EXPORT:-}" --arg res "$result" --arg ck "$ckpt" --arg gen "$gen" --arg mode "$([ "$MODE" = record ] && echo application || echo managed)" \
    --arg opmode "${OPERATION_MODE:-}" --arg model "${MODEL:-}" --arg cap "$cap" --arg rsn "$rsn" \
    '{operation:$op,pod:$pod,node:$node,pool:$pool,status:$st,exit_code:$exit,gpus:$gpus,gpu_class:$cls,started_at:$s,ended_at:$e,
      image:$img,image_digest:$dg,io_mode:$mode} + (if $exp != "" then {export:$exp} else {} end)
      + (if $rsn != "" then {reason:$rsn} else {} end)
      + (if $cap != "" then {capture:($cap|fromjson)} else {} end)
      + (if $opmode != "" then {mode:$opmode} else {} end) + (if $model != "" then {model:$model} else {} end)
      + (if $res != "" then {result_ref:$res} else {} end) + (if $ck != "" then {checkpoint_ref:$ck} else {} end)
      + (if $gen != "" then {checkpoint_generation:($gen|tonumber)} else {} end)' > "$REC/$POD_NAME.json"
  # the durable copy: the fleet API keeps attempts in its database (fenced by epoch; a stale pod gets 409). A terminal
  # record is retried; ACK holds the last HTTP status (200 = acknowledged; the work volume is released only then)
  ACK=""
  if [ -n "${API_URL:-}" ]; then
    # a start record is retried like a terminal one (jobs 0.1.29: one try stranded a healthy attempt behind a
    # transient 503, the App waits for that acknowledgement); 409 (fenced) and a definite auth rejection end it.
    # Bound of a start: 4 tries x 10 s timeout + 3 x 3 s = 49 s worst case here (+ the digest wait above, 30 s), then
    # once per poll (POLL_SECONDS) while main runs (docs/JOBS.md "Attempts and finalization"); a terminal record
    # waits 20 s between tries. The accepted or fenced answer alone becomes .ack.json (atomically: the application
    # reads it as the definite answer); a transient body is kept apart as .ack.transient.json, diagnostics only
    local tries=4 wait=20 tmo=20; [ "$1" = started ] && { wait=3; tmo=10; }
    for i in $(seq 1 $tries); do
      ACK=$(curl -sS -m "$tmo" -o "$REC/.ack.tmp" -w "%{http_code}" -X POST "$API_URL/internal/attempts" \
        -H "Authorization: Bearer $(cat "$TOKEN_FILE")" -H "Content-Type: application/json" --data-binary "@$REC/$POD_NAME.json" 2>/dev/null || echo 000)
      echo "attempt record $1 -> API $ACK"
      case "$ACK" in
        200|409|401|403) mv -f "$REC/.ack.tmp" "$REC/.ack.json"; break ;;
        *) [ -s "$REC/.ack.tmp" ] && mv -f "$REC/.ack.tmp" "$REC/.ack.transient.json" ;;
      esac
      [ "$i" -lt "$tries" ] && { sleep "$wait" & wait $!; }
    done
    if [ "$ACK" = 200 ] && [ -n "$gen" ]; then
      jq -c --arg g "$gen" '{generation:($g|tonumber), epoch:.epoch, checkpoint_ref:.checkpoint_ref, revision:.revision, acknowledged_at:(now|todate)}' \
        "$REC/.ack.json" > /work/CHECKPOINT.ack.json.tmp 2>/dev/null && mv /work/CHECKPOINT.ack.json.tmp /work/CHECKPOINT.ack.json
    fi
  fi
}

checkpoint_changed() {   # the application published a new checkpoint pointer (CHECKPOINT.json content changed)
  [ -f /work/CHECKPOINT.json ] || return 1
  local now; now=$(tr -d '\n' 2>/dev/null < /work/CHECKPOINT.json)
  [ "$now" != "$CKPT_SEEN" ] || return 1
  CKPT_SEEN="$now"; return 0
}

release_work() {   # $1 = delivered (true|false): the work volume / scratch leaf goes only with a delivery that is durable
  local delivered="$1"
  if [ "${SCRATCH:-}" = shared ]; then
    # a durable endpoint call on the tenant's shared scratch (scratch.sh): the leaf is emptied only with the delivery
    # acknowledged by the API and the capture proven intact (manifest, hash, RESULT.json); anything else keeps it
    # (export retry, recovery, retention), exactly as a kept volume; the shared claim is never deleted
    if [ "$delivered" = true ] && [ "${KEEP_SCRATCH:-false}" != "true" ] && /usr/local/bin/scratch.sh proof /work; then
      /usr/local/bin/scratch.sh cleanup /work 10001 || echo "scratch leaf ${SCRATCH_LEAF:-?} kept: cleanup refused (see above)"
    else
      echo "scratch leaf ${SCRATCH_LEAF:-?} kept: delivery not proven/acknowledged (export=$EXPORT ack=$ACK result=$([ -f /work/RESULT.json ] && echo yes || echo no)); POST /v1/operations/$OPERATION:export retries the delivery"
    fi
  elif [ -n "${PVC_NAME:-}" ] && [ "${KEEP_PVC:-false}" != "true" ] && [ "$delivered" = true ]; then
    curl -sS -o /dev/null -w "pvc delete %{http_code}\n" --cacert "$CA" -H "Authorization: Bearer $TOKEN" -X DELETE \
      "$API/api/v1/namespaces/$POD_NAMESPACE/persistentvolumeclaims/$PVC_NAME"
  elif [ -n "${PVC_NAME:-}" ]; then
    echo "volume $PVC_NAME kept: delivery not acknowledged (export=$EXPORT ack=$ACK result=$([ -f /work/RESULT.json ] && echo yes || echo no)); POST /v1/operations/$OPERATION:export retries the delivery"
  fi
}

captured_error_delivered() {   # jobs 0.1.32: an async endpoint caller's exit 2/5 with its capture delivered, proven and ACKed
  [ "$MODE" = record ] && [ "${OPERATION_MODE:-}" = async ] || return 1
  [ "$EXIT" = 2 ] || [ "$EXIT" = 5 ] || return 1
  [ -f /work/RESULT.json ] && jq -e '.export == "succeeded" and (.capture.sha256 // "") != ""' /work/RESULT.json > /dev/null 2>&1 || return 1
  if [ -n "${API_URL:-}" ]; then [ "$ACK" = 200 ] || return 1; fi
  /usr/local/bin/scratch.sh proof /work
}

upload() {   # $1 = status
  local st="$1"                                 # the positional parameters are replaced below (UPLOAD_EXCLUDES)
  cd /work || return
  if [ "$MODE" = record ]; then
    # application-managed I/O: the run (or the endpoint caller, docs/API.md "Endpoint calls") delivered its own output;
    # RESULT.json may say how that went (export: succeeded | failed), else the platform exported nothing (n/a). Read it
    # BEFORE the terminal record, so the one record carries the delivery: jobs 0.1.31 posted a preliminary terminal
    # record first, and a reader between the two saw "failed, export n/a" for a delivered capture (op-604f95bfb719109e,
    # 2026-10-20 21:53:52Z).
    EXPORT=$(jq -r '.export // "n/a"' /work/RESULT.json 2>/dev/null || echo n/a); [ -n "$EXPORT" ] || EXPORT="n/a"
    record "$st"; echo "RECORD $(date -u +%FT%TZ) status=$st export=$EXPORT (application-managed I/O)"; return
  fi
  record "$st"
  # multi-node runs (UPLOAD_SCOPE=rank0, docs/JOBS.md): every pod records its attempt, only rank 0 publishes the
  # results and STATUS.json (the ranks share nothing under /work; outputs are written by rank 0 by convention)
  if [ "${UPLOAD_SCOPE:-all}" = rank0 ] && [ "${NODE_RANK:-0}" != 0 ]; then
    echo "RECORD $(date -u +%FT%TZ) rank=$NODE_RANK status=$st -> $OUTPUT_PREFIX/attempts/$POD_NAME.json"
    aws s3 cp "${REC:-/work/attempts}/$POD_NAME.json" "${OUTPUT_PREFIX%/}/attempts/$POD_NAME.json" --no-progress 2>&1 | tail -n 2
    return
  fi
  echo "UPLOAD START $(date -u +%FT%TZ) status=$st -> $OUTPUT_PREFIX"
  # UPLOAD_EXCLUDES is a string of aws filters ("--exclude checkpoint/*", quotes allowed): split it like a shell
  # line with globbing OFF. Unquoted $UPLOAD_EXCLUDES expanded `checkpoint/*` against /work into `checkpoint/hf`,
  # which excludes that one entry and uploads everything below it: a multi-node run with shared checkpoints
  # pushed its 470 GB weight cache to the bucket (docs/VERIFICATION.md).
  set -f; eval "set -- ${UPLOAD_EXCLUDES:-}"
  aws s3 sync /work/ "${OUTPUT_PREFIX%/}/" --no-progress --exclude 'in/*' --exclude '.inputs-fetched' --exclude 'lost+found/*' --exclude 'STATUS.json' "$@" 2>&1 | tail -n 30
  local rc=${PIPESTATUS[0]}
  # verification: a second pass must find nothing left to copy (sizes), else the export is not durable (docs/JOBS.md)
  if [ "$rc" = 0 ] && [ -n "$(aws s3 sync /work/ "${OUTPUT_PREFIX%/}/" --dryrun --size-only --exclude 'in/*' --exclude '.inputs-fetched' --exclude 'lost+found/*' --exclude 'STATUS.json' "$@" 2>/dev/null | head -n 1)" ]; then rc=99; fi
  set +f
  if [ "$rc" = 0 ]; then EXPORT=succeeded; else EXPORT=failed; echo "UPLOAD FAILED $(date -u +%FT%TZ) rc=$rc: the volume is kept, retry the export (POST /v1/operations/$OPERATION:export)"; fi
  record "$st"                                  # the record now says how the export went (and reaches the API)
  aws s3 cp "${REC:-/work/attempts}/$POD_NAME.json" "${OUTPUT_PREFIX%/}/attempts/$POD_NAME.json" --no-progress 2>&1 | tail -n 1
  # STATUS.json is published last: its presence with export=succeeded is the durable-delivery marker
  printf '{"operation":"%s","attempt_pod":"%s","status":"%s","export":"%s","exit_code":%s,"finished_at":"%s"}\n' \
    "$OPERATION" "$POD_NAME" "$st" "$EXPORT" "${EXIT:-null}" "$(date -u +%FT%TZ)" > STATUS.json
  aws s3 cp STATUS.json "${OUTPUT_PREFIX%/}/STATUS.json" --no-progress 2>&1 | tail -n 1
  echo "UPLOAD END $(date -u +%FT%TZ) export=$EXPORT"
}
# shellcheck disable=SC2317,SC2329 # invoked by the TERM trap (diagnostic varies by ShellCheck version)
on_term() { echo "SIGTERM $(date -u +%FT%TZ): partial upload, then waiting for $MAIN to end"; EXIT=""; upload interrupted; }
trap on_term TERM

if [ "${1:-}" = start-record ]; then
  # the init step (jobs 0.1.31, docs/JOBS.md "Attempts and finalization"): main starts only behind its ACCEPTED start
  # record (200), so its first broker call never precedes its own record; no digest yet (the uploader republishes
  # with it). A fenced (409) or rejected (401/403) record ends the attempt here: this step fails, main never starts,
  # nothing is retried. A transient answer is retried (record()'s tries, then every 10 s) until START_RECORD_WAIT_S
  # (600), then the step fails and the Job's next pod records afresh. Without an API there is nothing to acknowledge.
  pod; deadline=$(( $(date +%s) + ${START_RECORD_WAIT_S:-600} ))
  while :; do
    record started
    case "$ACK" in
      200|"") echo "start record acknowledged: ${ACK:-no API}"; exit 0 ;;
      409|401|403) msg="start record refused ($ACK): main does not start" ;;
      *) [ "$(date +%s)" -lt "$deadline" ] && { sleep 10 & wait $!; continue; }
         msg="start record unacknowledged for ${START_RECORD_WAIT_S:-600}s (last answer ${ACK:-none}): main does not start" ;;
    esac
    echo "$msg"; echo "$msg" > /dev/termination-log 2>/dev/null; exit 1
  done
fi

gone=0
while true; do
  pod
  EXIT=$(field '.status.containerStatuses[]? | select(.name==$m) | .state.terminated.exitCode // empty')
  [ -n "$EXIT" ] && break
  if main_alive; then
    if [ "$SEEN" = 0 ]; then
      # the attempt is on record from its first second: a node that dies without warning (a stopped spot VM
      # sends no SIGTERM, measured on a B300 spot node) leaves this "started" record, which the
      # replacement attempt's upload of /work/attempts carries to the bucket; the copy here is best-effort
      # the kubelet reports main's runtime image digest (imageID) seconds after the start, sometimes after this
      # container already runs: wait for it, bounded, so the start record carries the exact digest the attempt runs
      # (docs/JOBS.md "Attempts and finalization"); a digest that appears later is republished below
      for _ in $(seq 1 "${DIGEST_WAIT_POLLS:-15}"); do
        [ -n "$(field '.status.containerStatuses[]? | select(.name==$m) | .imageID // empty')" ] && break
        sleep 2 & wait $!; pod
      done
      record started
      case "$ACK" in 200|409|401|403|"") START_ACK=1 ;; *) START_ACK=0 ;; esac   # "" = no API: nothing to acknowledge
      [ "$MODE" = record ] || aws s3 cp "${REC:-/work/attempts}/$POD_NAME.json" "${OUTPUT_PREFIX%/}/attempts/$POD_NAME.json" --no-progress > /dev/null 2>&1 || true
    fi
    SEEN=1; gone=0
    if [ "${START_ACK:-1}" = 0 ]; then       # the start is still unacknowledged (transient failures): once more per poll
      record started; case "$ACK" in 200|409|401|403) START_ACK=1 ;; esac
    fi
    enforce_execution_timeout && { sleep 3 & wait $!; continue; }   # main is being ended: its exit comes next
    checkpoint_changed && record started     # the live pointer, fenced and acknowledged through the API
    if [ "$DIGEST_SEEN" = 0 ] && [ -n "$(field '.status.containerStatuses[]? | select(.name==$m) | .imageID // empty')" ]; then
      record started                         # the digest arrived after the start record: published once, same attempt
    fi
  elif [ "$SEEN" = 1 ]; then
    gone=$((gone + 1))                        # main's processes are gone; the kubelet can take a while to post the exit code
    if [ "$gone" -ge "${EXIT_CODE_POLLS:-40}" ]; then
      echo "$MAIN ended, exit code not reported by the API after $gone polls; last status: $(field '.status.containerStatuses[]? | select(.name==$m) | .state' | tr -d '\n' | cut -c1-200)"
      EXIT=""; break
    fi
    sleep 3 & wait $!; continue
  fi
  sleep "${POLL_SECONDS:-10}" & wait $!      # interruptible by the trap
done
if [ "$EXIT" = "0" ]; then
  upload succeeded
  # the work volume goes only once delivery is durable: the export verified (managed), or RESULT.json present and not
  # reporting a failed export (application: a caller whose S3 delivery failed writes export=failed with the capture
  # kept for the transfer-only retry; that volume must stay), and the API acknowledged the final record when there
  # is an API (else the bucket record stands). The record itself was acknowledged either way: an acknowledged
  # "export failed" is not a delivery.
  delivered=false
  if [ "$MODE" = record ]; then
    [ -f /work/RESULT.json ] && jq -e '(.export // "n/a") != "failed"' /work/RESULT.json > /dev/null 2>&1 && delivered=true
  else
    [ "$EXPORT" = succeeded ] && delivered=true
  fi
  if [ -n "${API_URL:-}" ] && [ "$ACK" != 200 ]; then delivered=false; fi
  release_work "$delivered"
elif [ -z "$EXIT" ]; then
  upload interrupted
  echo "$MAIN interrupted: outputs uploaded, PVC ${PVC_NAME:-?} kept (resumes automatically after a preemption; POST /v1/operations/$OPERATION:resume after a cancel)"
else
  upload failed
  if captured_error_delivered; then
    # a trusted async endpoint caller whose main captured and DELIVERED the endpoint's error answer (exit 2: the
    # caller's 4xx, exit 5: the endpoint's 5xx): endpoint inference is not resumable and the capture IS the result, so
    # the volume/leaf is released like a delivered success (jobs 0.1.32). Exit 4 (unknown outcome), a partial or
    # failed export and every scientific run (mode run: its volume is for :resume) keep it; KEEP_* flags still win.
    echo "$MAIN exited $EXIT: the endpoint's error answer was captured, delivered and acknowledged; nothing to resume"
    release_work true
  else
    echo "$MAIN exited $EXIT: outputs uploaded, PVC ${PVC_NAME:-?} kept for a resume (POST /v1/operations/$OPERATION:resume)"
  fi
fi
exit 0
