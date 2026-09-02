#!/usr/bin/env bash
# Rebuild the owner-only readiness state from the current contract and the
# observed live evidence, then regenerate the catalog projection.
#
# Generations are bound to an exact contract digest, so any contract change
# fails closed with InvalidContract until the evidence is replayed against the
# new contract. That is the intended behaviour; this script is how an operator
# performs the replay in one documented step.
#
# It never handles licensed bytes: artifact paths arrive through environment
# references and only digests and identities are written.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
repo=$(cd "${here}/.." && pwd)
contract="${here}/contracts/academic-assets.json"
cli="${here}/scripts/academic_assets.py"

: "${FS2_ACADEMIC_STATE_DIR:?set FS2_ACADEMIC_STATE_DIR to the owner-only private state directory}"
: "${FS2_ACADEMIC_AF3_PATH:?set FS2_ACADEMIC_AF3_PATH to the verified AlphaFold 3 parameter object}"
: "${FS2_ACADEMIC_WHEEL_PATH:?set FS2_ACADEMIC_WHEEL_PATH to the verified PyRosetta wheel}"
: "${FS2_ACADEMIC_RECEIPT_DIR:?set FS2_ACADEMIC_RECEIPT_DIR to the directory holding observed stage receipts}"

generation="${1:-live-$(date -u +%Y%m%d-%H%M%S)}"

echo "replaying into generation ${generation}"
python3 "${cli}" --contract "${contract}" ingest \
  --state-dir-env FS2_ACADEMIC_STATE_DIR \
  --generation "${generation}" \
  --alphafold3-path-env FS2_ACADEMIC_AF3_PATH \
  --alphafold3-authorization "${here}/contracts/alphafold3-use-authorization.json" \
  --pyrosetta-bindcraft-path-env FS2_ACADEMIC_WHEEL_PATH \
  --pyrosetta-bindcraft-authorization "${here}/contracts/pyrosetta-bindcraft-use-authorization.json" \
  >/dev/null

for asset in alphafold3 pyrosetta-bindcraft; do
  for stage in cache install runtime; do
    receipt="${FS2_ACADEMIC_RECEIPT_DIR}/${asset}-${stage}.json"
    if [ -f "${receipt}" ]; then
      python3 "${cli}" --contract "${contract}" record \
        --state-dir "${FS2_ACADEMIC_STATE_DIR}" \
        --asset-id "${asset}" --stage "${stage}" --receipt "${receipt}" >/dev/null
      echo "recorded ${asset} ${stage}"
    fi
  done
done

python3 "${here}/scripts/project_catalog_readiness.py" \
  --contract "${contract}" \
  --state-dir "${FS2_ACADEMIC_STATE_DIR}" \
  --output "${repo}/catalog/runtime/contracts/academic-asset-readiness.json"
echo "regenerated the catalog projection"

python3 "${cli}" --contract "${contract}" status --state-dir "${FS2_ACADEMIC_STATE_DIR}"
