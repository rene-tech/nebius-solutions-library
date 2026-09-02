#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
asset_root=$(cd "${script_dir}/.." && pwd)

: "${FS2_ACADEMIC_ASSET_STATE_DIR:?set to the owner-only private ingestion state directory}"
: "${FS2_ACADEMIC_GENERATION:?set to a new immutable generation ID}"
: "${FS2_AF3_FILE:?set to the approved af3.bin.zst path}"
: "${FS2_AF3_ACCEPTANCE:?set to the owner-only AF3 acceptance receipt path}"
: "${FS2_PYROSETTA_FILE:?set to the approved PyRosetta conda package path}"
: "${FS2_PYROSETTA_ACCEPTANCE:?set to the owner-only PyRosetta acceptance receipt path}"

contract="${asset_root}/contracts/academic-assets.json"
python3 "${script_dir}/academic_assets.py" \
  --contract "${contract}" \
  ingest \
  --state-dir-env FS2_ACADEMIC_ASSET_STATE_DIR \
  --generation "${FS2_ACADEMIC_GENERATION}" \
  --alphafold3-path-env FS2_AF3_FILE \
  --alphafold3-acceptance-env FS2_AF3_ACCEPTANCE \
  --pyrosetta-bindcraft-path-env FS2_PYROSETTA_FILE \
  --pyrosetta-bindcraft-acceptance-env FS2_PYROSETTA_ACCEPTANCE >/dev/null

for asset_id in alphafold3 pyrosetta-bindcraft; do
  FS2_ACADEMIC_ASSET_ID=${asset_id} "${script_dir}/stage-private-cache.sh" >/dev/null
done

python3 "${script_dir}/academic_assets.py" \
  --contract "${contract}" \
  status --state-dir-env FS2_ACADEMIC_ASSET_STATE_DIR
