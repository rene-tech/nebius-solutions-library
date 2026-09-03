#!/usr/bin/env bash
# shellcheck disable=SC2016  # jq programs use $id as a jq variable bound by --arg
# Reproducibly build the contracted installed tree for one academic asset.
#
# A fresh cluster has an empty, root-owned claim root, so this runs a bounded
# privileged init that prepares only that empty root, then a non-root installer
# that installs the pinned wheel, promotes it atomically, applies the contracted
# group-readable modes, and imports the result in place to prove it works.
#
# The installer logic lives in install_tree.py so it is unit-testable; this script
# only delivers it to the cluster and records the resulting receipt.
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
asset_root=$(cd "${script_dir}/.." && pwd)

: "${FS2_ACADEMIC_ASSET_STATE_DIR:?set to the owner-only private ingestion state directory}"
: "${FS2_ACADEMIC_ASSET_ID:?set to a contracted asset ID}"
: "${FS2_ACADEMIC_KUBECONFIG:=${KUBECONFIG:-${HOME}/.kube/config}}"
: "${FS2_ACADEMIC_KUBE_CONTEXT:=k8s-inference-h100}"
: "${FS2_ACADEMIC_INSTALLER_IMAGE:=docker.io/library/python:3.10-slim}"
: "${FS2_ACADEMIC_INSTALL_TIMEOUT:=900}"

contract="${FS2_ACADEMIC_CONTRACT:-${asset_root}/contracts/academic-assets.json}"
kubectl_args=(--kubeconfig "${FS2_ACADEMIC_KUBECONFIG}" --context "${FS2_ACADEMIC_KUBE_CONTEXT}")

asset_query() { jq -er --arg id "${FS2_ACADEMIC_ASSET_ID}" "$1" "${contract}"; }

install_mode=$(asset_query '.assets[$id].delivery.install_mode')
if [[ ${install_mode} == none ]]; then
  echo "asset has no contracted installed tree; nothing to install" >&2
  exit 0
fi

install_path=$(asset_query '.assets[$id].delivery.install_relative_path')
asset_gid=$(asset_query '.assets[$id].delivery.asset_gid')
file_mode=$(asset_query '.assets[$id].delivery.file_mode')
directory_mode=$(asset_query '.assets[$id].delivery.directory_mode')
asset_directory_mode=$(asset_query '.assets[$id].delivery.asset_directory_mode')
wheel_name=$(asset_query '.assets[$id].artifact.filename')
artifact_sha256=$(asset_query '.assets[$id].artifact.sha256')
distribution=$(asset_query '.assets[$id].runtime.offline_validation.expect_distribution')
version=$(asset_query '.assets[$id].runtime.offline_validation.expect_version')
namespace=$(jq -er '.runtime_cache.pvc_namespace' "${contract}")
pvc_name=$(jq -er '.runtime_cache.pvc_name' "${contract}")
tenant_id=$(jq -er '.tenant_id' \
  "${asset_root}/contracts/${FS2_ACADEMIC_ASSET_ID}-use-authorization.json")

job_name="academic-tree-installer-${FS2_ACADEMIC_ASSET_ID}-${artifact_sha256:0:8}"
job_name=${job_name:0:63}
job_manifest=$(mktemp)
receipt_file=$(mktemp)
cleanup() { rm -f -- "${job_manifest}" "${receipt_file}"; }
trap cleanup EXIT

kubectl "${kubectl_args[@]}" -n "${namespace}" create configmap academic-tree-installer \
  --from-file="install_tree.py=${script_dir}/install_tree.py" \
  --dry-run=client -o json |
  kubectl "${kubectl_args[@]}" apply -f - >/dev/null

asset_dir="/runtime/${FS2_ACADEMIC_ASSET_ID}"
install_command=$(printf '%s' "set -e
python3 /opt/installer/install_tree.py install \
  --wheel ${asset_dir}/${wheel_name} \
  --destination ${asset_dir}/${install_path} \
  --file-mode ${file_mode} --directory-mode ${directory_mode} --gid ${asset_gid}
echo INSTALL_RESULT_BEGIN
python3 /opt/installer/install_tree.py verify \
  --tree ${asset_dir}/${install_path} \
  --distribution ${distribution} --version ${version} \
  --file-mode ${file_mode} --directory-mode ${directory_mode} --gid ${asset_gid}
")

jq --arg name "${job_name}" --arg ns "${namespace}" --arg pvc "${pvc_name}" \
  --arg image "${FS2_ACADEMIC_INSTALLER_IMAGE}" --arg command "${install_command}" \
  --arg gid_string "${asset_gid}" --argjson gid "${asset_gid}" \
  --arg asset_id "${FS2_ACADEMIC_ASSET_ID}" \
  --arg asset_directory_mode "${asset_directory_mode}" \
  '.metadata.name = $name
   | .metadata.namespace = $ns
   | .spec.template.spec.volumes[0].persistentVolumeClaim.claimName = $pvc
   | .spec.template.spec.initContainers[0].image = $image
   | .spec.template.spec.initContainers[0].command[6] = $gid_string
   | .spec.template.spec.initContainers[0].command[8] = $asset_id
   | .spec.template.spec.initContainers[0].command[10] = $asset_directory_mode
   | .spec.template.spec.containers[0].image = $image
   | .spec.template.spec.containers[0].command[2] = $command
   | .spec.template.spec.containers[0].securityContext.runAsGroup = $gid' \
  "${asset_root}/kubernetes/tree-installer.template.json" >"${job_manifest}"

kubectl "${kubectl_args[@]}" -n "${namespace}" delete job "${job_name}" \
  --ignore-not-found --wait=true >/dev/null 2>&1 || true
kubectl "${kubectl_args[@]}" apply -f "${job_manifest}" >/dev/null
kubectl "${kubectl_args[@]}" -n "${namespace}" wait --for=condition=complete \
  "job/${job_name}" --timeout="${FS2_ACADEMIC_INSTALL_TIMEOUT}s" >/dev/null

logs=$(kubectl "${kubectl_args[@]}" -n "${namespace}" logs "job/${job_name}" -c install)
verify_json=$(printf '%s\n' "${logs}" | sed -n '/INSTALL_RESULT_BEGIN/,$p' | tail -n +2 | tail -1)
# The receipt is assembled field by field, so a field the verifier gained but the
# receipt never projected would only surface as a validation failure later.
required_verified_fields='["installed_distribution","installed_distribution_version","python_version","file_count","tree_manifest_algorithm","tree_manifest_sha256","tree_total_bytes","evidence_digest"]'
missing_fields=$(jq -r --argjson required "${required_verified_fields}" \
  '. as $observed | [$required[] | . as $field | select(($observed | has($field)) | not)] | join(",")' \
  <<<"${verify_json}" 2>/dev/null || echo "unparseable")
if [[ -n ${missing_fields} ]]; then
  echo "installer output is missing fields the receipt must project: ${missing_fields}" >&2
  exit 74
fi
if ! jq -e '.import_verified == true' <<<"${verify_json}" >/dev/null 2>&1; then
  echo "installed tree verification did not pass" >&2
  printf '%s\n' "${logs}" | tail -20 >&2
  exit 74
fi

jq -n \
  --arg asset_id "${FS2_ACADEMIC_ASSET_ID}" \
  --arg artifact_sha256 "${artifact_sha256}" \
  --arg observed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg tenant_id "${tenant_id}" \
  --arg install_relative_path "${install_path}" \
  --arg file_mode "${file_mode}" \
  --arg directory_mode "${directory_mode}" \
  --argjson asset_gid "${asset_gid}" \
  --argjson verified "${verify_json}" \
  '{schema:"fs2-serve.nebius.ai/academic-install-receipt/v3",
    asset_id:$asset_id, artifact_sha256:$artifact_sha256, observed_at:$observed_at,
    tenant_id:$tenant_id, institution_id:null,
    install_relative_path:$install_relative_path,
    installed_distribution:$verified.installed_distribution,
    installed_distribution_version:$verified.installed_distribution_version,
    python_version:$verified.python_version,
    file_count:$verified.file_count,
    tree_manifest_algorithm:$verified.tree_manifest_algorithm,
    tree_manifest_sha256:$verified.tree_manifest_sha256,
    tree_total_bytes:$verified.tree_total_bytes,
    file_mode:$file_mode, directory_mode:$directory_mode, asset_gid:$asset_gid,
    world_readable:false, atomic_promotion:true, import_verified:true,
    evidence_digest:$verified.evidence_digest}' >"${receipt_file}"

python3 "${script_dir}/academic_assets.py" --contract "${contract}" record \
  --state-dir "${FS2_ACADEMIC_ASSET_STATE_DIR}" \
  --asset-id "${FS2_ACADEMIC_ASSET_ID}" --stage install --receipt "${receipt_file}" >/dev/null

printf '{"asset_id":"%s","state":"InstallReady","version":%s}\n' \
  "${FS2_ACADEMIC_ASSET_ID}" "$(jq -c '.installed_distribution_version' <<<"${verify_json}")"
