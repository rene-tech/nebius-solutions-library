#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
asset_root=$(cd "${script_dir}/.." && pwd)

: "${FS2_ACADEMIC_ASSET_STATE_DIR:?set to the owner-only private ingestion state directory}"
: "${FS2_ACADEMIC_ASSET_ID:?set to pyrosetta-bindcraft}"
: "${FS2_ACADEMIC_BASE_IMAGE:?set to a digest-pinned runtime image}"
: "${FS2_ACADEMIC_PRIVATE_IMAGE:?set to a tag in the contracted private registry}"
: "${FS2_PARENT_INTEGRATION_APPROVED:?set to yes only after parent integration review}"
: "${FS2_ACADEMIC_RUNTIME_UID:=65532}"
: "${FS2_ACADEMIC_RUNTIME_GID:=65532}"

if [[ ${FS2_PARENT_INTEGRATION_APPROVED} != yes ]]; then
  echo "parent integration approval is required" >&2
  exit 64
fi
if [[ ${FS2_ACADEMIC_ASSET_ID} != pyrosetta-bindcraft ]]; then
  echo "AlphaFold3 parameters must remain on a private volume and cannot be embedded" >&2
  exit 64
fi
if [[ ! ${FS2_ACADEMIC_BASE_IMAGE} =~ @sha256:[0-9a-f]{64}$ ]]; then
  echo "base image must be pinned by OCI digest" >&2
  exit 64
fi
if [[ ! ${FS2_ACADEMIC_PRIVATE_IMAGE} =~ ^cr\.eu-north1\.nebius\.cloud/e00akg9ndpx77eaexh/academic/[a-z0-9._/-]+:[a-zA-Z0-9._-]+$ ]]; then
  echo "target image must be a tag in the contracted private eu-north1 repository" >&2
  exit 64
fi
if [[ ! ${FS2_ACADEMIC_RUNTIME_UID} =~ ^[0-9]{1,10}$ ]] || \
   [[ ! ${FS2_ACADEMIC_RUNTIME_GID} =~ ^[0-9]{1,10}$ ]]; then
  echo "runtime UID and GID must be numeric" >&2
  exit 64
fi

resolved=$(python3 "${script_dir}/academic_assets.py" \
  --contract "${asset_root}/contracts/academic-assets.json" \
  resolve \
  --state-dir "${FS2_ACADEMIC_ASSET_STATE_DIR}" \
  --asset-id "${FS2_ACADEMIC_ASSET_ID}" \
  --for-private-layer)
asset_path=$(jq -er '.path' <<<"${resolved}")
asset_sha256=$(jq -er '.sha256' <<<"${resolved}")

metadata_file=$(mktemp)
cleanup() {
  : >"${metadata_file}"
  rm -f -- "${metadata_file}"
}
trap cleanup EXIT

docker buildx build \
  --file "${asset_root}/private-layer.Dockerfile" \
  --build-arg "BASE_IMAGE=${FS2_ACADEMIC_BASE_IMAGE}" \
  --build-arg "FS2_ASSET_ID=${FS2_ACADEMIC_ASSET_ID}" \
  --build-arg "FS2_ASSET_SHA256=${asset_sha256}" \
  --build-arg "FS2_RUNTIME_UID=${FS2_ACADEMIC_RUNTIME_UID}" \
  --build-arg "FS2_RUNTIME_GID=${FS2_ACADEMIC_RUNTIME_GID}" \
  --secret "id=licensed_asset,src=${asset_path}" \
  --tag "${FS2_ACADEMIC_PRIVATE_IMAGE}" \
  --metadata-file "${metadata_file}" \
  --provenance=true \
  --sbom=true \
  --push \
  "${asset_root}"

image_digest=$(jq -er '.["containerimage.digest"] | select(test("^sha256:[0-9a-f]{64}$"))' "${metadata_file}")
jq -n \
  --arg asset_id "${FS2_ACADEMIC_ASSET_ID}" \
  --arg artifact_sha256 "${asset_sha256}" \
  --arg repository "${FS2_ACADEMIC_PRIVATE_IMAGE%:*}" \
  --arg image_digest "${image_digest}" \
  --arg observed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  '{schema:"fs2-serve.nebius.ai/academic-image-receipt/v1",asset_id:$asset_id,artifact_sha256:$artifact_sha256,observed_at:$observed_at,repository:$repository,image_digest:$image_digest,visibility:"private",redistributable:false,asset_delivery_mode:"embedded-private-layer",contains_licensed_asset:true,builder:"docker-buildx-secret-mount"}' \
  >"${metadata_file}"

python3 "${script_dir}/academic_assets.py" \
  --contract "${asset_root}/contracts/academic-assets.json" \
  record \
  --state-dir "${FS2_ACADEMIC_ASSET_STATE_DIR}" \
  --asset-id "${FS2_ACADEMIC_ASSET_ID}" \
  --stage image \
  --receipt "${metadata_file}" >/dev/null

printf '{"asset_id":"%s","image_digest":"%s","state":"ImageReady"}\n' \
  "${FS2_ACADEMIC_ASSET_ID}" "${image_digest}"
