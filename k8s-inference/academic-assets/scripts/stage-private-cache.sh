#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
asset_root=$(cd "${script_dir}/.." && pwd)

: "${FS2_ACADEMIC_ASSET_STATE_DIR:?set to the owner-only private ingestion state directory}"
: "${FS2_ACADEMIC_ASSET_ID:?set to alphafold3 or pyrosetta-bindcraft}"
: "${FS2_ACADEMIC_KUBECONFIG:=/home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/kubeconfig}"
: "${FS2_ACADEMIC_KUBE_CONTEXT:=k8s-inference-h100}"

case "${FS2_ACADEMIC_ASSET_ID}" in
  alphafold3|pyrosetta-bindcraft) ;;
  *)
    echo "asset ID must be alphafold3 or pyrosetta-bindcraft" >&2
    exit 64
    ;;
esac
if [[ ! -f ${FS2_ACADEMIC_KUBECONFIG} ]]; then
  echo "kubeconfig is missing" >&2
  exit 66
fi

kubectl_args=(--kubeconfig "${FS2_ACADEMIC_KUBECONFIG}" --context "${FS2_ACADEMIC_KUBE_CONTEXT}")
contract="${asset_root}/contracts/academic-assets.json"

resolved=$(python3 "${script_dir}/academic_assets.py" \
  --contract "${contract}" \
  resolve \
  --state-dir "${FS2_ACADEMIC_ASSET_STATE_DIR}" \
  --asset-id "${FS2_ACADEMIC_ASSET_ID}" \
  --for-shared-cache)
asset_path=$(jq -er '.path' <<<"${resolved}")
asset_sha256=$(jq -er '.sha256' <<<"${resolved}")
asset_filename=$(jq -er --arg id "${FS2_ACADEMIC_ASSET_ID}" '.assets[$id].artifact.filename' "${contract}")
asset_size=$(jq -er --arg id "${FS2_ACADEMIC_ASSET_ID}" '.assets[$id].artifact.size_bytes' "${contract}")
current=$(python3 "${script_dir}/academic_assets.py" \
  --contract "${contract}" status --state-dir "${FS2_ACADEMIC_ASSET_STATE_DIR}")
cache_already_ready=false
if [[ $(jq -er --arg id "${FS2_ACADEMIC_ASSET_ID}" \
  '.assets[] | select(.asset_id == $id) | .stages.private_cache' <<<"${current}") == ready ]]; then
  cache_already_ready=true
fi

namespace=$(jq -er '.private_cache.pvc_namespace' "${contract}")
pvc_name=$(jq -er '.private_cache.pvc_name' "${contract}")
loader_name="academic-asset-loader-${FS2_ACADEMIC_ASSET_ID}-${asset_sha256:0:8}"
loader_name=${loader_name:0:63}
loader_template="${asset_root}/kubernetes/private-cache-loader.template.json"
loader_manifest=$(mktemp)
receipt_file=$(mktemp)
loader_created=false
cleanup() {
  if [[ ${loader_created} == true ]]; then
    kubectl "${kubectl_args[@]}" -n "${namespace}" delete pod "${loader_name}" \
      --ignore-not-found --wait=false >/dev/null 2>&1 || true
  fi
  : >"${loader_manifest}"
  : >"${receipt_file}"
  rm -f -- "${loader_manifest}" "${receipt_file}"
}
trap cleanup EXIT

if kubectl "${kubectl_args[@]}" -n "${namespace}" get pod "${loader_name}" >/dev/null 2>&1; then
  echo "task-owned loader pod already exists; inspect and remove it before retrying" >&2
  exit 73
fi

kubectl "${kubectl_args[@]}" apply --server-side \
  --field-manager=fs2-academic-assets-ingestion \
  -f "${asset_root}/kubernetes/private-cache-pvc.yaml" >/dev/null

jq --arg name "${loader_name}" --arg pvc "${pvc_name}" \
  '.metadata.name = $name | .spec.volumes[0].persistentVolumeClaim.claimName = $pvc' \
  "${loader_template}" >"${loader_manifest}"
kubectl "${kubectl_args[@]}" create -f "${loader_manifest}" >/dev/null
loader_created=true
kubectl "${kubectl_args[@]}" -n "${namespace}" wait \
  --for=condition=Ready "pod/${loader_name}" --timeout=180s >/dev/null

destination="/private-cache/academic-assets/v1/${FS2_ACADEMIC_ASSET_ID}/${asset_sha256}/${asset_filename}"
verify_program='import hashlib,json,pathlib,stat,sys
p=pathlib.Path(sys.argv[1])
if not p.exists(): print("{\"exists\":false}"); raise SystemExit(0)
h=hashlib.sha256(); total=0; chunks=0
with p.open("rb") as f:
    for chunk in iter(lambda:f.read(8*1024*1024),b""):
        h.update(chunk); total+=len(chunk); chunks+=1
        if chunks % 16 == 0: print(".",file=sys.stderr,flush=True)
mode=stat.S_IMODE(p.stat().st_mode)
print(json.dumps({"exists":True,"mode":mode,"sha256":h.hexdigest(),"size_bytes":total},sort_keys=True,separators=(",",":")))'
upload_program='import hashlib,os,pathlib,sys
target=pathlib.Path(sys.argv[1]); expected=sys.argv[2]; expected_size=int(sys.argv[3])
target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
if target.exists(): raise SystemExit("immutable destination already exists")
temporary=target.with_name("."+target.name+".uploading")
try:
    h=hashlib.sha256(); total=0
    with temporary.open("xb") as output:
        for chunk in iter(lambda: sys.stdin.buffer.read(8*1024*1024),b""): h.update(chunk); total+=len(chunk); output.write(chunk)
        output.flush(); os.fsync(output.fileno())
    if total != expected_size or h.hexdigest() != expected: raise SystemExit("uploaded content identity differs")
    temporary.chmod(0o400); temporary.replace(target)
finally:
    if temporary.exists(): temporary.unlink()'

verified=$(kubectl "${kubectl_args[@]}" -n "${namespace}" exec "${loader_name}" -- \
  python3 -c "${verify_program}" "${destination}" 2>/dev/null)
if [[ $(jq -er '.exists' <<<"${verified}") == false ]]; then
  kubectl "${kubectl_args[@]}" -n "${namespace}" exec -i "${loader_name}" -- \
    python3 -c "${upload_program}" "${destination}" "${asset_sha256}" "${asset_size}" <"${asset_path}"
  verified=$(kubectl "${kubectl_args[@]}" -n "${namespace}" exec "${loader_name}" -- \
    python3 -c "${verify_program}" "${destination}" 2>/dev/null)
fi
if [[ $(jq -er '.sha256' <<<"${verified}") != "${asset_sha256}" ]] || \
   [[ $(jq -er '.size_bytes' <<<"${verified}") != "${asset_size}" ]] || \
   [[ $(jq -er '.mode' <<<"${verified}") != 256 ]]; then
  echo "private cache verification differs from the contract" >&2
  exit 74
fi
if [[ ${cache_already_ready} == true ]]; then
  printf '{"asset_id":"%s","artifact_sha256":"%s","state":"CacheReady"}\n' \
    "${FS2_ACADEMIC_ASSET_ID}" "${asset_sha256}"
  exit 0
fi

jq -n \
  --arg asset_id "${FS2_ACADEMIC_ASSET_ID}" \
  --arg artifact_sha256 "${asset_sha256}" \
  --arg observed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --argjson file_size_bytes "${asset_size}" \
  --slurpfile contract "${contract}" \
  '($contract[0].private_cache | del(.distribution_scope)) + {schema:"fs2-serve.nebius.ai/academic-cache-receipt/v1",asset_id:$asset_id,artifact_sha256:$artifact_sha256,observed_at:$observed_at,file_size_bytes:$file_size_bytes,verified:true}' \
  >"${receipt_file}"

python3 "${script_dir}/academic_assets.py" \
  --contract "${contract}" \
  record \
  --state-dir "${FS2_ACADEMIC_ASSET_STATE_DIR}" \
  --asset-id "${FS2_ACADEMIC_ASSET_ID}" \
  --stage cache \
  --receipt "${receipt_file}" >/dev/null

printf '{"asset_id":"%s","artifact_sha256":"%s","state":"CacheReady"}\n' \
  "${FS2_ACADEMIC_ASSET_ID}" "${asset_sha256}"
