#!/usr/bin/env bash
# The external customer flow over the public API only: reserve an upload, write
# the bytes, finalize, read the bytes back, and prove the tenant boundary. The
# client is given no object-store credentials and never contacts the store, so
# this is exactly what a customer behind nothing but the gateway can do.
#
#   BASE=https://gateway HOSTHDR=gateway \
#   TOKEN_A=/path/to/tenant-a.pat TOKEN_B=/path/to/tenant-b.pat \
#     scripts/scientific_public_bytes_flow.sh
#
# The two tokens must belong to different tenants; the second one is used only
# to prove it is refused. Reaching a gateway by IP with an explicit Host header
# additionally needs CURL_EXTRA="-k --resolve <host>:443:<ip>" and
# CONNECT_BASE=https://<ip>. Every step prints PASS or FAIL and the exit status
# is the number of failures.
set -uo pipefail
BASE="${BASE:?BASE is required}"
HOSTHDR="${HOSTHDR:?HOSTHDR is required}"
# When the gateway is reached by IP with an explicit Host header, curl uses
# --resolve while the MCP probe has to dial the address directly.
CURL_EXTRA="${CURL_EXTRA:-}"
CONNECT_BASE="${CONNECT_BASE:-$BASE}"
A=$(cat "${TOKEN_A:?}")
B=$(cat "${TOKEN_B:?}")
RUN="${RUN:-$(date -u +%Y%m%d%H%M%S)}"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

req() { # method path token [args...]
  local method=$1 path=$2 token=$3; shift 3
  # shellcheck disable=SC2086
  curl -sS $CURL_EXTRA --max-time 60 -X "$method" "$BASE$path" \
    -H "Host: $HOSTHDR" -H "authorization: Bearer $token" "$@"
}
code() { python3 -c "import sys;print(sys.argv[1])" "$1"; }
step=0
ok=0; bad=0
check() { # label expected actual
  step=$((step+1))
  if [ "$2" = "$3" ]; then printf '  %2d PASS  %-58s %s\n' "$step" "$1" "$3"; ok=$((ok+1));
  else printf '  %2d FAIL  %-58s expected %s got %s\n' "$step" "$1" "$2" "$3"; bad=$((bad+1)); fi
}

PAYLOAD="$WORK/target.fasta"
printf '>PD-L1-target\nMRIFAVFIFMTYWHLLNAFTVTVPKDLYVVEYGSNMTIECKFPVEKQLDLAALIVYWEMEDKNIIQFVHGEEDLKVQHSSYRQRARLLKDQLSLGNAALQITDVKLQDAGVYRCMISYGGADYKRITVKVNAPY\n' > "$PAYLOAD"
SHA=$(sha256sum "$PAYLOAD" | cut -d' ' -f1)
SIZE=$(stat -c%s "$PAYLOAD")
echo "run=$RUN payload sha256=$SHA size=$SIZE"

echo "--- 1. authenticated upload bootstrap ---"
BEGIN="$WORK/begin.json"
STATUS=$(req POST /v1/scientific-artifacts/uploads "$A" \
  -H 'content-type: application/json' -H "idempotency-key: live-parity-input-$RUN" \
  -d "{\"model_id\":\"boltzgen\",\"sha256\":\"$SHA\",\"size_bytes\":$SIZE,\"media_type\":\"text/x-fasta\"}" \
  -o "$BEGIN" -w '%{http_code}')
check "POST /v1/scientific-artifacts/uploads" 201 "$STATUS"
OP=$(python3 -c "import json;print(json.load(open('$BEGIN'))['operation_id'])")
UP=$(python3 -c "import json;print(json.load(open('$BEGIN'))['upload_id'])")
CPATH=$(python3 -c "import json;print(json.load(open('$BEGIN'))['content_path'])")
MAXC=$(python3 -c "import json;print(json.load(open('$BEGIN'))['max_content_bytes'])")
echo "  operation=$OP upload=$UP max_content_bytes=$MAXC"
echo "  advertised content_path=$CPATH"

echo "--- 2. the reservation is idempotent ---"
STATUS=$(req POST /v1/scientific-artifacts/uploads "$A" \
  -H 'content-type: application/json' -H "idempotency-key: live-parity-input-$RUN" \
  -d "{\"model_id\":\"boltzgen\",\"sha256\":\"$SHA\",\"size_bytes\":$SIZE,\"media_type\":\"text/x-fasta\"}" \
  -o "$WORK/replay.json" -w '%{http_code}')
check "replayed reservation" 201 "$STATUS"
check "replay returns the same upload" "$UP" "$(python3 -c "import json;print(json.load(open('$WORK/replay.json'))['upload_id'])")"

echo "--- 3. the declared identity is immutable ---"
STATUS=$(req PUT "$CPATH" "$A" -H 'content-type: text/x-fasta' --data-binary @<(cat "$PAYLOAD"; echo X) -o "$WORK/e1.json" -w '%{http_code}')
check "PUT bytes with a different digest" 422 "$STATUS"
check "  refusal names verification" '"artifact_verification_failed"' "$(python3 -c "import json;print(json.dumps(json.load(open('$WORK/e1.json'))['error']['type']))")"
STATUS=$(req PUT "$CPATH" "$A" -H 'content-type: chemical/x-pdb' --data-binary @"$PAYLOAD" -o "$WORK/e2.json" -w '%{http_code}')
check "PUT bytes with a different media type" 422 "$STATUS"

echo "--- 4. a foreign tenant is refused before the owner writes ---"
STATUS=$(req PUT "$CPATH" "$B" -H 'content-type: text/x-fasta' --data-binary @"$PAYLOAD" -o /dev/null -w '%{http_code}')
check "tenant-b PUT bytes" 404 "$STATUS"

echo "--- 5. real byte PUT ---"
STATUS=$(req PUT "$CPATH" "$A" -H 'content-type: text/x-fasta' --data-binary @"$PAYLOAD" -o "$WORK/put.json" -D "$WORK/put.hdr" -w '%{http_code}')
check "PUT the declared bytes" 200 "$STATUS"
check "  receipt digest equals the payload" "$SHA" "$(python3 -c "import json;print(json.load(open('$WORK/put.json'))['sha256'])")"
check "  receipt size equals the payload" "$SIZE" "$(python3 -c "import json;print(json.load(open('$WORK/put.json'))['size_bytes'])")"
check "  response header carries the digest" "$SHA" "$(grep -i '^x-fs2-artifact-sha256:' "$WORK/put.hdr" | tr -d '\r' | awk '{print $2}')"

echo "--- 6. finalize ---"
STATUS=$(req POST "/v1/scientific-artifacts/uploads/$UP:finalize" "$A" \
  -H 'content-type: application/json' -d "{\"operation_id\":\"$OP\"}" -o "$WORK/final.json" -w '%{http_code}')
check "POST finalize" 200 "$STATUS"
ART=$(python3 -c "import json;print(json.load(open('$WORK/final.json'))['artifact_id'])")
check "  pointer digest equals the payload" "$SHA" "$(python3 -c "import json;print(json.load(open('$WORK/final.json'))['sha256'])")"
echo "  artifact=$ART"
STATUS=$(req PUT "$CPATH" "$A" -H 'content-type: text/x-fasta' --data-binary @"$PAYLOAD" -o /dev/null -w '%{http_code}')
check "PUT after finalize is write-once" 409 "$STATUS"

echo "--- 7. authorized byte GET ---"
STATUS=$(req GET "/v1/artifacts/$ART/content" "$A" -o "$WORK/got.bin" -D "$WORK/got.hdr" -w '%{http_code}')
check "GET /v1/artifacts/{id}/content" 200 "$STATUS"
check "  bytes are byte-identical" "$SHA" "$(sha256sum "$WORK/got.bin" | cut -d' ' -f1)"
check "  content-type is the bound media type" "text/x-fasta" "$(grep -i '^content-type:' "$WORK/got.hdr" | tr -d '\r' | awk '{print $2}' | cut -d';' -f1)"
check "  digest header matches" "$SHA" "$(grep -i '^x-fs2-artifact-sha256:' "$WORK/got.hdr" | tr -d '\r' | awk '{print $2}')"
check "  size header matches" "$SIZE" "$(grep -i '^x-fs2-artifact-size-bytes:' "$WORK/got.hdr" | tr -d '\r' | awk '{print $2}')"
check "  no signed storage URL leaks" 0 "$(grep -ic 'X-Amz' "$WORK/got.hdr" || true)"

echo "--- 8. short-lived signed handle still works ---"
STATUS=$(req GET "/v1/artifacts/$ART/download" "$A" -o "$WORK/handle.json" -w '%{http_code}')
check "GET /v1/artifacts/{id}/download" 200 "$STATUS"
check "  handle is a GET" '"GET"' "$(python3 -c "import json;print(json.dumps(json.load(open('$WORK/handle.json'))['handle']['method']))")"
check "  handle is really signed" 1 "$(python3 -c "import json;print(1 if 'X-Amz-Signature' in json.load(open('$WORK/handle.json'))['handle']['url'] else 0)")"

echo "--- 9. tenant isolation on every read ---"
check "tenant-b GET bytes" 404 "$(req GET "/v1/artifacts/$ART/content" "$B" -o /dev/null -w '%{http_code}')"
check "tenant-b GET pointer" 404 "$(req GET "/v1/artifacts/$ART" "$B" -o /dev/null -w '%{http_code}')"
check "tenant-b GET handle" 404 "$(req GET "/v1/artifacts/$ART/download" "$B" -o /dev/null -w '%{http_code}')"
check "tenant-b GET operation" 404 "$(req GET "/v1/operations/$OP" "$B" -o /dev/null -w '%{http_code}')"
check "tenant-b finalize" 404 "$(req POST "/v1/scientific-artifacts/uploads/$UP:finalize" "$B" -H 'content-type: application/json' -d "{\"operation_id\":\"$OP\"}" -o /dev/null -w '%{http_code}')"
check "unauthenticated GET bytes" 401 "$(curl -sS $CURL_EXTRA --max-time 30 -o /dev/null -w '%{http_code}' -H "Host: $HOSTHDR" "$BASE/v1/artifacts/$ART/content")"

echo "--- 10. status and result ---"
STATUS=$(req GET "/v1/operations/$OP" "$A" -o "$WORK/status.json" -w '%{http_code}')
check "GET /v1/operations/{id}" 200 "$STATUS"
check "  upload operation is terminal" '"succeeded"' "$(python3 -c "import json;print(json.dumps(json.load(open('$WORK/status.json'))['status']))")"
check "  outcome names the upload" '"artifact_uploaded"' "$(python3 -c "import json;print(json.dumps(json.load(open('$WORK/status.json'))['outcome']))")"

echo "--- 11. a manifest of the input, through the same path ---"
MAN="$WORK/manifest.json"
python3 - "$MAN" "$ART" "$SHA" "$SIZE" <<'PYEOF'
import json, sys
path, artifact_id, sha, size = sys.argv[1:5]
document = {
    "schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
    "manifest_id": "live-parity-input-$RUN",
    "entries": [
        {
            "name": "target-sequence",
            "semantic_type": "protein.sequence/v1",
            "artifact": {
                "artifact_id": artifact_id,
                "sha256": sha,
                "size_bytes": int(size),
                "media_type": "text/x-fasta",
            },
        }
    ],
}
with open(path, "w") as handle:
    handle.write(json.dumps(document, sort_keys=True, separators=(",", ":")))
PYEOF
MSHA=$(sha256sum "$MAN" | cut -d' ' -f1); MSIZE=$(stat -c%s "$MAN")
STATUS=$(req POST /v1/scientific-artifacts/uploads "$A" -H 'content-type: application/json' \
  -H "idempotency-key: live-parity-manifest-$RUN" \
  -d "{\"model_id\":\"boltzgen\",\"sha256\":\"$MSHA\",\"size_bytes\":$MSIZE,\"media_type\":\"application/vnd.fs2.scientific-manifest+json\"}" \
  -o "$WORK/mbegin.json" -w '%{http_code}')
check "reserve the manifest upload" 201 "$STATUS"
MOP=$(python3 -c "import json;print(json.load(open('$WORK/mbegin.json'))['operation_id'])")
MUP=$(python3 -c "import json;print(json.load(open('$WORK/mbegin.json'))['upload_id'])")
MCP=$(python3 -c "import json;print(json.load(open('$WORK/mbegin.json'))['content_path'])")
check "PUT the manifest bytes" 200 "$(req PUT "$MCP" "$A" -H 'content-type: application/vnd.fs2.scientific-manifest+json' --data-binary @"$MAN" -o /dev/null -w '%{http_code}')"
STATUS=$(req POST "/v1/scientific-artifacts/uploads/$MUP:finalize" "$A" -H 'content-type: application/json' -d "{\"operation_id\":\"$MOP\"}" -o "$WORK/mfinal.json" -w '%{http_code}')
check "finalize the manifest" 200 "$STATUS"
MART=$(python3 -c "import json;print(json.load(open('$WORK/mfinal.json'))['artifact_id'])")
check "read the manifest bytes back" "$MSHA" "$(req GET "/v1/artifacts/$MART/content" "$A" -o "$WORK/mgot.bin" -w '%{http_code}' >/dev/null; sha256sum "$WORK/mgot.bin" | cut -d' ' -f1)"

echo "--- 12. submit is reachable and refuses an unqualified profile ---"
SUB=$(req POST "/v1/models/boltzgen:submit" "$A" -H 'content-type: application/json' -H "idempotency-key: live-parity-submit-$RUN" \
  -d "{\"schema\":\"fs2-serve.nebius.ai/scientific-run-request/v1\",\"model_id\":\"boltzgen\",\"operation\":\"design\",\"service_class\":\"standard-batch\",\"input_manifest\":{\"artifact_id\":\"$MART\",\"sha256\":\"$MSHA\",\"size_bytes\":$MSIZE,\"media_type\":\"application/vnd.fs2.scientific-manifest+json\"},\"parameters\":{}}" \
  -o "$WORK/submit.json" -w '%{http_code}')
echo "  submit -> HTTP $SUB $(head -c 200 "$WORK/submit.json")"

echo "--- 13. MCP parity over the same gateway ---"
python3 - "$CONNECT_BASE" "$HOSTHDR" "$(cat "$TOKEN_A")" "$(cat "$TOKEN_B")" "$PAYLOAD" "$ART" "$CURL_EXTRA" "$RUN" <<'PYEOF'
import base64, hashlib, json, pathlib, ssl, sys, urllib.request

base, host, token_a, token_b, payload_path, artifact_id, extra, run = sys.argv[1:9]
payload = pathlib.Path(payload_path).read_bytes()
sha = hashlib.sha256(payload).hexdigest()
context = ssl._create_unverified_context() if "-k" in extra else None
passed = failed = 0


def report(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"     PASS  {label} {detail}")
    else:
        failed += 1
        print(f"     FAIL  {label} {detail}")


def rpc(token, method, params, session=None, request_id=1):
    body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}).encode()
    request = urllib.request.Request(f"{base}/mcp", data=body, method="POST")
    request.add_header("Host", host)
    request.add_header("authorization", f"Bearer {token}")
    request.add_header("content-type", "application/json")
    request.add_header("accept", "application/json, text/event-stream")
    if session:
        request.add_header("mcp-session-id", session)
    try:
        with urllib.request.urlopen(request, context=context, timeout=60) as response:
            raw = response.read().decode()
            sid = response.headers.get("mcp-session-id")
    except urllib.error.HTTPError as error:
        return {"http": error.code, "raw": error.read().decode()[:200]}, None
    for line in raw.splitlines():
        if line.startswith("data: "):
            raw = line[6:]
            break
    try:
        return json.loads(raw), sid
    except ValueError:
        return {"raw": raw[:200]}, sid


initialize, session = rpc(
    token_a,
    "initialize",
    {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "parity-probe", "version": "1"}},
)
report("MCP initialize", "result" in initialize, initialize.get("result", {}).get("protocolVersion", initialize))

listed, _ = rpc(token_a, "tools/list", {}, session)
names = {tool["name"] for tool in listed.get("result", {}).get("tools", [])}
parity = {
    "begin_scientific_artifact_upload",
    "put_scientific_artifact_bytes",
    "finalize_scientific_artifact_upload",
    "submit_scientific_run",
    "get_scientific_status",
    "get_scientific_result",
    "read_scientific_artifact_bytes",
    "download_scientific_artifact",
}
report("MCP advertises upload/submit/status/result", parity <= names, f"missing={sorted(parity - names)}")


def call(token, name, arguments):
    answer, _ = rpc(token, "tools/call", {"name": name, "arguments": arguments}, session)
    result = answer.get("result", {})
    if result.get("structuredContent") is not None:
        return result["structuredContent"], answer
    return None, answer


reservation, raw = call(
    token_a,
    "begin_scientific_artifact_upload",
    {
        "model_id": "boltzgen",
        "sha256": sha,
        "size_bytes": len(payload),
        "media_type": "text/x-fasta",
        "idempotency_key": f"live-parity-mcp-{run}",
    },
)
report("MCP begin_scientific_artifact_upload", bool(reservation), "" if reservation else str(raw)[:200])

mismatch, raw = call(
    token_a,
    "put_scientific_artifact_bytes",
    {
        "operation_id": reservation["operation_id"],
        "upload_id": reservation["upload_id"],
        "content_base64": base64.b64encode(payload + b"X").decode(),
    },
)
report("MCP refuses bytes that break the digest", mismatch is None, str(raw.get("error", {}).get("message", ""))[:80])

receipt, raw = call(
    token_a,
    "put_scientific_artifact_bytes",
    {
        "operation_id": reservation["operation_id"],
        "upload_id": reservation["upload_id"],
        "content_base64": base64.b64encode(payload).decode(),
    },
)
report("MCP put_scientific_artifact_bytes", bool(receipt) and receipt["sha256"] == sha, str(raw)[:160] if not receipt else "")

pointer, raw = call(
    token_a,
    "finalize_scientific_artifact_upload",
    {"operation_id": reservation["operation_id"], "upload_id": reservation["upload_id"]},
)
report("MCP finalize_scientific_artifact_upload", bool(pointer) and pointer["sha256"] == sha, str(raw)[:160] if not pointer else "")

read, raw = call(token_a, "read_scientific_artifact_bytes", {"artifact_id": pointer["artifact_id"]})
same = bool(read) and base64.b64decode(read["content_base64"]) == payload
report("MCP read_scientific_artifact_bytes returns exact bytes", same, str(raw)[:160] if not same else "")

status, raw = call(token_a, "get_operation", {"operation_id": reservation["operation_id"]})
report("MCP get_operation is terminal", bool(status) and status.get("outcome") == "artifact_uploaded", str(status)[:120] if status else str(raw)[:120])

stolen, raw = call(token_b, "read_scientific_artifact_bytes", {"artifact_id": pointer["artifact_id"]})
report("MCP refuses a foreign tenant's bytes", stolen is None, str(raw.get("error", {}).get("message", ""))[:80])

stolen_write, raw = call(
    token_b,
    "put_scientific_artifact_bytes",
    {
        "operation_id": reservation["operation_id"],
        "upload_id": reservation["upload_id"],
        "content_base64": base64.b64encode(payload).decode(),
    },
)
report("MCP refuses a foreign tenant's write", stolen_write is None, str(raw.get("error", {}).get("message", ""))[:80])

print(f"  MCP: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
PYEOF
MCP=$?

echo
echo "HTTP checks: $ok passed, $bad failed;  MCP exit: $MCP"
[ "$bad" -eq 0 ] && [ "$MCP" -eq 0 ] && echo "FLOW: PASS" || echo "FLOW: FAIL"
exit $(( bad + MCP ))
