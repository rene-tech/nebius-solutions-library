#!/usr/bin/env bash
# One documented step: verify approved academic artifacts and record readiness.
#
# Two independent axes:
#   * Use authorization  - the platform-owner grant that activates the academic
#     proof-of-concept path. Required. Committed receipts are used by default.
#   * Formal acceptance  - the licensor-required institutional attestation by a
#     named representative. Optional here, tracked separately, and never implied
#     by the operational path.
#
# Artifact locations are passed by environment reference so no licensed path ever
# appears in argv, logs, or receipts. No credentials are read or written.
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
asset_root=$(cd "${script_dir}/.." && pwd)
contract="${FS2_ACADEMIC_CONTRACT:-${asset_root}/contracts/academic-assets.json}"
cli="${script_dir}/academic_assets.py"

usage() {
  cat <<'USAGE'
Usage: ingest-approved-assets.sh

Required environment:
  FS2_ACADEMIC_ASSET_STATE_DIR  owner-only (mode 0700) private ingestion state directory
  FS2_ACADEMIC_GENERATION       new immutable generation ID
  FS2_AF3_FILE                  path to the approved AlphaFold 3 parameter object (af3.bin.zst)
  FS2_PYROSETTA_WHEEL_FILE      path to the approved PyRosetta cp310 wheel pinned by the contract

Optional environment:
  FS2_AF3_AUTHORIZATION         use-authorization receipt (default: the committed one)
  FS2_PYROSETTA_AUTHORIZATION   use-authorization receipt (default: the committed one)
  FS2_AF3_ACCEPTANCE            formal institutional acceptance receipt (separate axis)
  FS2_PYROSETTA_ACCEPTANCE      formal institutional acceptance receipt (separate axis)
  FS2_ACADEMIC_STAGE_CACHE      set to 1 to also stage into the tenant-private runtime claim
USAGE
}

if [[ ${1:-} == "-h" || ${1:-} == "--help" ]]; then
  usage
  exit 0
fi

: "${FS2_ACADEMIC_ASSET_STATE_DIR:?set to the owner-only private ingestion state directory}"
: "${FS2_ACADEMIC_GENERATION:?set to a new immutable generation ID}"
: "${FS2_AF3_FILE:?set to the approved af3.bin.zst path}"
: "${FS2_PYROSETTA_WHEEL_FILE:?set to the approved PyRosetta cp310 wheel path}"

af3_authorization="${FS2_AF3_AUTHORIZATION:-${asset_root}/contracts/alphafold3-use-authorization.json}"
pyrosetta_authorization="${FS2_PYROSETTA_AUTHORIZATION:-${asset_root}/contracts/pyrosetta-bindcraft-use-authorization.json}"

ingest_args=(
  ingest
  --state-dir-env FS2_ACADEMIC_ASSET_STATE_DIR
  --generation "${FS2_ACADEMIC_GENERATION}"
  --alphafold3-path-env FS2_AF3_FILE
  --alphafold3-authorization "${af3_authorization}"
  --pyrosetta-bindcraft-path-env FS2_PYROSETTA_WHEEL_FILE
  --pyrosetta-bindcraft-authorization "${pyrosetta_authorization}"
)

# Formal institutional acceptance stays optional and independent.
if [[ -n ${FS2_AF3_ACCEPTANCE:-} ]]; then
  ingest_args+=(--alphafold3-acceptance-env FS2_AF3_ACCEPTANCE)
fi
if [[ -n ${FS2_PYROSETTA_ACCEPTANCE:-} ]]; then
  ingest_args+=(--pyrosetta-bindcraft-acceptance-env FS2_PYROSETTA_ACCEPTANCE)
fi

python3 "${cli}" --contract "${contract}" "${ingest_args[@]}" >/dev/null

if [[ ${FS2_ACADEMIC_STAGE_CACHE:-0} == 1 ]]; then
  # One step: stage the pinned bytes onto the tenant-private claim, then build the
  # contracted installed tree for every asset that has one. A fresh cluster needs
  # both, so neither is optional here.
  for asset_id in $(jq -er '.assets | keys[]' "${contract}"); do
    FS2_ACADEMIC_ASSET_ID="${asset_id}" "${script_dir}/stage-private-cache.sh" >/dev/null
    if [[ $(jq -er --arg id "${asset_id}" '.assets[$id].delivery.install_mode' "${contract}") != none ]]; then
      FS2_ACADEMIC_ASSET_ID="${asset_id}" "${script_dir}/install-academic-tree.sh" >/dev/null
    fi
  done
fi

python3 "${cli}" --contract "${contract}" \
  status --state-dir-env FS2_ACADEMIC_ASSET_STATE_DIR
