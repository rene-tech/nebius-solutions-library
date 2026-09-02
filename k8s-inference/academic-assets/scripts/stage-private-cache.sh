#!/usr/bin/env bash
# Stage one verified academic artifact into the canonical tenant-private runtime claim.
#
# Delivery contract enforced here:
#   * bytes are written under the contracted non-root asset group;
#   * files are group-readable and never world-readable or writable (0440);
#   * directories are group-traversable only (0550);
#   * the loader runs as that group and never sets fsGroup, because kubelet fsGroup
#     ownership management rewrites the tree to group-writable 0660 and setgid 2775;
#   * a consuming runtime reads through supplementalGroups, not by matching the uid.
#
# Licensed bytes never reach argv or logs; only digests, sizes, modes and identities do.
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
asset_root=$(cd "${script_dir}/.." && pwd)

: "${FS2_ACADEMIC_ASSET_STATE_DIR:?set to the owner-only private ingestion state directory}"
: "${FS2_ACADEMIC_ASSET_ID:?set to a contracted asset ID}"
: "${FS2_ACADEMIC_KUBECONFIG:=/home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/kubeconfig}"
: "${FS2_ACADEMIC_KUBE_CONTEXT:=k8s-inference-h100}"

contract="${FS2_ACADEMIC_CONTRACT:-${asset_root}/contracts/academic-assets.json}"

if ! jq -er --arg id "${FS2_ACADEMIC_ASSET_ID}" '.assets[$id].model_id' "${contract}" >/dev/null 2>&1; then
  echo "asset ID is not in the contract" >&2
  exit 64
fi
if [[ ! -f ${FS2_ACADEMIC_KUBECONFIG} ]]; then
  echo "kubeconfig is missing" >&2
  exit 66
fi

kubectl_args=(--kubeconfig "${FS2_ACADEMIC_KUBECONFIG}" --context "${FS2_ACADEMIC_KUBE_CONTEXT}")

resolved=$(python3 "${script_dir}/academic_assets.py" \
  --contract "${contract}" resolve \
  --state-dir "${FS2_ACADEMIC_ASSET_STATE_DIR}" \
  --asset-id "${FS2_ACADEMIC_ASSET_ID}" \
  --for-tenant-volume)
asset_path=$(jq -er '.path' <<<"${resolved}")
asset_sha256=$(jq -er '.sha256' <<<"${resolved}")

asset_filename=$(jq -er --arg id "${FS2_ACADEMIC_ASSET_ID}" '.assets[$id].artifact.filename' "${contract}")
asset_size=$(jq -er --arg id "${FS2_ACADEMIC_ASSET_ID}" '.assets[$id].artifact.size_bytes' "${contract}")
asset_gid=$(jq -er --arg id "${FS2_ACADEMIC_ASSET_ID}" '.assets[$id].delivery.asset_gid' "${contract}")
file_mode=$(jq -er --arg id "${FS2_ACADEMIC_ASSET_ID}" '.assets[$id].delivery.file_mode' "${contract}")
directory_mode=$(jq -er --arg id "${FS2_ACADEMIC_ASSET_ID}" '.assets[$id].delivery.directory_mode' "${contract}")
asset_directory_mode=$(jq -er --arg id "${FS2_ACADEMIC_ASSET_ID}" '.assets[$id].delivery.asset_directory_mode' "${contract}")

# Fail closed on a delivery contract that would be unreadable or unsafe.
if [[ ! ${asset_gid} =~ ^[0-9]+$ ]] || ((asset_gid < 1 || asset_gid > 65535)); then
  echo "delivery asset_gid must be a non-root group id" >&2
  exit 65
fi
for mode in "${file_mode}" "${directory_mode}"; do
  if [[ ! ${mode} =~ ^0[0-7]{3}$ ]]; then
    echo "delivery modes must be octal strings" >&2
    exit 65
  fi
  if (((8#${mode} & 0007) != 0)); then
    echo "licensed bytes must never be world-accessible" >&2
    exit 65
  fi
  if (((8#${mode} & 0022) != 0)); then
    echo "licensed bytes must never be group or world writable" >&2
    exit 65
  fi
  if (((8#${mode} & 0040) == 0)); then
    echo "licensed bytes must be group readable so a runtime can mount them" >&2
    exit 65
  fi
done

namespace=$(jq -er '.runtime_cache.pvc_namespace' "${contract}")
pvc_name=$(jq -er '.runtime_cache.pvc_name' "${contract}")
tenant_id=$(jq -er '.tenant_id' \
  "${asset_root}/contracts/${FS2_ACADEMIC_ASSET_ID}-use-authorization.json")

# Deployment identity is observed, never read from the portable contract.
: "${FS2_ACADEMIC_PROJECT_ID:?set to the target project ID}"
: "${FS2_ACADEMIC_REGION:?set to the target region}"
: "${FS2_ACADEMIC_CLUSTER_ID:?set to the target Kubernetes cluster ID}"
project_id="${FS2_ACADEMIC_PROJECT_ID}"
region="${FS2_ACADEMIC_REGION}"
cluster_id="${FS2_ACADEMIC_CLUSTER_ID}"

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
  rm -f -- "${loader_manifest}" "${receipt_file}"
}
trap cleanup EXIT

if kubectl "${kubectl_args[@]}" -n "${namespace}" get pod "${loader_name}" >/dev/null 2>&1; then
  echo "task-owned loader pod already exists; inspect and remove it before retrying" >&2
  exit 73
fi

# The installer module is delivered to the loader so its bounded bootstrap can
# prepare a freshly provisioned, root-owned claim root before non-root staging.
kubectl "${kubectl_args[@]}" -n "${namespace}" create configmap academic-tree-installer \
  --from-file="install_tree.py=${script_dir}/install_tree.py" \
  --dry-run=client -o json |
  kubectl "${kubectl_args[@]}" apply -f - >/dev/null

jq --arg name "${loader_name}" --arg pvc "${pvc_name}" --arg ns "${namespace}" \
  --arg gid_string "${asset_gid}" --arg asset_id "${FS2_ACADEMIC_ASSET_ID}" \
  --arg asset_directory_mode "${asset_directory_mode}" \
  --argjson gid "${asset_gid}" \
  '.metadata.name = $name
   | .metadata.namespace = $ns
   | .spec.volumes[0].persistentVolumeClaim.claimName = $pvc
   | .spec.securityContext.runAsGroup = $gid
   | .spec.initContainers[0].command[6] = $gid_string
   | .spec.initContainers[0].command[8] = $asset_id
   | .spec.initContainers[0].command[10] = $asset_directory_mode
   | del(.spec.securityContext.fsGroup, .spec.securityContext.fsGroupChangePolicy)' \
  "${loader_template}" >"${loader_manifest}"
kubectl "${kubectl_args[@]}" create -f "${loader_manifest}" >/dev/null
loader_created=true
kubectl "${kubectl_args[@]}" -n "${namespace}" wait \
  --for=condition=Ready "pod/${loader_name}" --timeout=180s >/dev/null

destination="/private-cache/${FS2_ACADEMIC_ASSET_ID}/${asset_filename}"

verify_program='import hashlib,json,pathlib,stat,sys
p=pathlib.Path(sys.argv[1])
if not p.exists():
    print(json.dumps({"exists":False})); raise SystemExit(0)
h=hashlib.sha256(); total=0
with p.open("rb") as f:
    for chunk in iter(lambda: f.read(8*1024*1024), b""):
        h.update(chunk); total+=len(chunk)
info=p.stat(); parent=p.parent.stat()
print(json.dumps({"exists":True,
                  "mode":"0%o"%stat.S_IMODE(info.st_mode),
                  "gid":info.st_gid,
                  "parent_mode":"0%o"%stat.S_IMODE(parent.st_mode),
                  "parent_gid":parent.st_gid,
                  "sha256":h.hexdigest(),
                  "size_bytes":total},sort_keys=True,separators=(",",":")))'

upload_program='import hashlib,os,pathlib,sys
target=pathlib.Path(sys.argv[1]); expected=sys.argv[2]
expected_size=int(sys.argv[3]); file_mode=int(sys.argv[4],8); dir_mode=int(sys.argv[5],8)
target.parent.mkdir(parents=True,exist_ok=True)
if target.exists(): raise SystemExit("immutable destination already exists")
temporary=target.with_name("."+target.name+".uploading")
try:
    h=hashlib.sha256(); total=0
    with temporary.open("xb") as output:
        for chunk in iter(lambda: sys.stdin.buffer.read(8*1024*1024), b""):
            h.update(chunk); total+=len(chunk); output.write(chunk)
        output.flush(); os.fsync(output.fileno())
    if total != expected_size or h.hexdigest() != expected:
        raise SystemExit("uploaded content identity differs")
    temporary.chmod(file_mode); temporary.replace(target)
finally:
    if temporary.exists(): temporary.unlink()
os.chmod(target.parent, dir_mode)'

verified=$(kubectl "${kubectl_args[@]}" -n "${namespace}" exec "${loader_name}" -- \
  python3 -c "${verify_program}" "${destination}" 2>/dev/null)
if [[ $(jq -er '.exists' <<<"${verified}") == false ]]; then
  kubectl "${kubectl_args[@]}" -n "${namespace}" exec -i "${loader_name}" -- \
    python3 -c "${upload_program}" "${destination}" "${asset_sha256}" "${asset_size}" \
    "${file_mode}" "${directory_mode}" <"${asset_path}"
  verified=$(kubectl "${kubectl_args[@]}" -n "${namespace}" exec "${loader_name}" -- \
    python3 -c "${verify_program}" "${destination}" 2>/dev/null)
fi

if [[ $(jq -er '.sha256' <<<"${verified}") != "${asset_sha256}" ]] ||
  [[ $(jq -er '.size_bytes' <<<"${verified}") != "${asset_size}" ]] ||
  [[ $(jq -er '.mode' <<<"${verified}") != "${file_mode}" ]] ||
  [[ $(jq -er '.parent_mode' <<<"${verified}") != "${directory_mode}" ]] ||
  [[ $(jq -er '.gid' <<<"${verified}") != "${asset_gid}" ]] ||
  [[ $(jq -er '.parent_gid' <<<"${verified}") != "${asset_gid}" ]]; then
  echo "staged identity, ownership or mode differs from the delivery contract" >&2
  jq -c '{mode,parent_mode,gid,parent_gid,size_bytes}' <<<"${verified}" >&2
  exit 74
fi

pvc_uid=$(kubectl "${kubectl_args[@]}" -n "${namespace}" get pvc "${pvc_name}" \
  -o jsonpath='{.metadata.uid}')
volume_name=$(kubectl "${kubectl_args[@]}" -n "${namespace}" get pvc "${pvc_name}" \
  -o jsonpath='{.spec.volumeName}')
volume_handle=$(kubectl "${kubectl_args[@]}" get pv "${volume_name}" \
  -o jsonpath='{.spec.csi.volumeHandle}')
# A dynamically provisioned claim has no shared filesystem identity; record null
# rather than back-filling an unrelated filesystem.
filesystem_id=$(kubectl "${kubectl_args[@]}" -n "${namespace}" get pvc "${pvc_name}" \
  -o jsonpath='{.metadata.annotations.fs2\.nebius\.ai/filesystem-id}')
if [[ -z ${filesystem_id} ]]; then
  filesystem_argument=null
else
  filesystem_argument=$(jq -n --arg value "${filesystem_id}" '$value')
fi

jq -n \
  --arg asset_id "${FS2_ACADEMIC_ASSET_ID}" \
  --arg artifact_sha256 "${asset_sha256}" \
  --arg observed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg tenant_id "${tenant_id}" \
  --arg project_id "${project_id}" \
  --arg region "${region}" \
  --arg cluster_id "${cluster_id}" \
  --argjson filesystem_id "${filesystem_argument}" \
  --arg volume_handle "${volume_handle}" \
  --arg pvc_namespace "${namespace}" \
  --arg pvc_name "${pvc_name}" \
  --arg pvc_uid "${pvc_uid}" \
  --arg file_mode "${file_mode}" \
  --arg directory_mode "${directory_mode}" \
  --argjson asset_gid "${asset_gid}" \
  --argjson file_size_bytes "${asset_size}" \
  '{schema:"fs2-serve.nebius.ai/academic-cache-receipt/v3",
    asset_id:$asset_id, artifact_sha256:$artifact_sha256, observed_at:$observed_at,
    tenant_id:$tenant_id, institution_id:null,
    project_id:$project_id, region:$region, cluster_id:$cluster_id,
    filesystem_id:$filesystem_id, volume_handle:$volume_handle,
    pvc_namespace:$pvc_namespace, pvc_name:$pvc_name, pvc_uid:$pvc_uid,
    file_size_bytes:$file_size_bytes, file_mode:$file_mode,
    directory_mode:$directory_mode, asset_gid:$asset_gid, verified:true,
    runtime_mount_allowed:true, general_shared_cache:false}' >"${receipt_file}"

python3 "${script_dir}/academic_assets.py" --contract "${contract}" record \
  --state-dir "${FS2_ACADEMIC_ASSET_STATE_DIR}" \
  --asset-id "${FS2_ACADEMIC_ASSET_ID}" --stage cache --receipt "${receipt_file}" >/dev/null

printf '{"asset_id":"%s","artifact_sha256":"%s","state":"TenantCacheReady"}\n' \
  "${FS2_ACADEMIC_ASSET_ID}" "${asset_sha256}"
