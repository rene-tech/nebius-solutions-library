#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

asset_root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

python3 -m json.tool "${asset_root}/contracts/academic-assets.json" >/dev/null
python3 -m json.tool "${asset_root}/contracts/alphafold3-acceptance.template.json" >/dev/null
python3 -m json.tool "${asset_root}/contracts/pyrosetta-bindcraft-acceptance.template.json" >/dev/null
python3 -m json.tool "${asset_root}/kubernetes/private-cache-loader.template.json" >/dev/null
python3 -m json.tool "${asset_root}/schemas/license-acceptance.schema.json" >/dev/null
python3 -m json.tool "${asset_root}/schemas/readiness.schema.json" >/dev/null
python3 -m json.tool "${asset_root}/schemas/revocation.schema.json" >/dev/null
python3 -m json.tool "${asset_root}/schemas/stage-receipt.schema.json" >/dev/null
python3 -m unittest discover -v "${asset_root}/tests"
shellcheck "${asset_root}/scripts/build-private-layer.sh"
shellcheck "${asset_root}/scripts/ingest-approved-assets.sh"
shellcheck "${asset_root}/scripts/stage-private-cache.sh"
python3 "${asset_root}/scripts/academic_assets.py" \
  --contract "${asset_root}/contracts/academic-assets.json" \
  status --state-dir "${asset_root}/tests/nonexistent-private-state" \
  | python3 -m json.tool >/dev/null
