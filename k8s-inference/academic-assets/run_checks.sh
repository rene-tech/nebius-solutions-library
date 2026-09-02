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
ASSET_ROOT="${asset_root}" python3 - <<'PY'
import json, subprocess
import os
from jsonschema import Draft202012Validator
root = os.environ["ASSET_ROOT"]
status = json.loads(subprocess.check_output(["python3", f"{root}/scripts/academic_assets.py", "--contract", f"{root}/contracts/academic-assets.json", "status", "--state-dir", f"{root}/tests/nonexistent-private-state"]))
schema = json.load(open(f"{root}/schemas/readiness.schema.json"))
errors = list(Draft202012Validator(schema).iter_errors(status))
assert not errors, "v2 readiness schema errors: " + "; ".join(str(e) for e in errors)
PY
python3 -m unittest discover -v "${asset_root}/tests"
shellcheck "${asset_root}/scripts/build-private-layer.sh"
shellcheck "${asset_root}/scripts/ingest-approved-assets.sh"
shellcheck "${asset_root}/scripts/stage-private-cache.sh"
python3 "${asset_root}/scripts/academic_assets.py" \
  --contract "${asset_root}/contracts/academic-assets.json" \
  status --state-dir "${asset_root}/tests/nonexistent-private-state" \
  | python3 -m json.tool >/dev/null
