#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

asset_root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd "${asset_root}/.." && pwd)

for document in \
  "${asset_root}"/contracts/*.json \
  "${asset_root}"/schemas/*.json \
  "${asset_root}"/evidence/*.json \
  "${asset_root}"/kubernetes/*.json; do
  python3 -m json.tool "${document}" >/dev/null
done

# The absent-state projection must satisfy the published readiness schema. The
# unit suite additionally proves that every reachable state, including every
# Invalid* diagnostic, satisfies it.
ASSET_ROOT="${asset_root}" python3 - <<'PY'
import json
import os
import subprocess

from jsonschema import Draft202012Validator

root = os.environ["ASSET_ROOT"]
status = json.loads(
    subprocess.check_output(
        [
            "python3",
            f"{root}/scripts/academic_assets.py",
            "--contract",
            f"{root}/contracts/academic-assets.json",
            "status",
            "--state-dir",
            f"{root}/tests/nonexistent-private-state",
        ]
    )
)
schema = json.load(open(f"{root}/schemas/readiness.schema.json"))
errors = list(Draft202012Validator(schema).iter_errors(status))
assert not errors, "readiness schema errors: " + "; ".join(str(error) for error in errors)
assert status["state"] == "Blocked"
assert status["formal_license_state"] == "Pending"
PY

# The committed catalog projection must validate against its published schema.
CATALOG_ROOT="${repo_root}/catalog/runtime" python3 - <<'PY'
import json
import os

from jsonschema import Draft202012Validator

root = os.environ["CATALOG_ROOT"]
document = json.load(open(f"{root}/contracts/academic-asset-readiness.json"))
schema = json.load(open(f"{root}/schema/academic-asset-readiness.schema.json"))
errors = list(Draft202012Validator(schema).iter_errors(document))
assert not errors, "catalog projection errors: " + "; ".join(str(error) for error in errors)
# Operational progress must never be reported as licence acceptance.
for model in document["models"]:
    assert model["delivery"]["embed_in_image"] is False
    assert model["formal_license_status"] == "FormalAcceptancePending"
assert document["formal_license_state"] == "Pending"
PY

python3 -m unittest discover -v "${asset_root}/tests"

for script in "${asset_root}"/scripts/*.sh; do
  shellcheck "${script}"
done

python3 "${asset_root}/scripts/academic_assets.py" \
  --contract "${asset_root}/contracts/academic-assets.json" \
  status --state-dir "${asset_root}/tests/nonexistent-private-state" \
  | python3 -m json.tool >/dev/null

# Nothing licensed, secret or owner-only may ever ship from this directory.
# Scan the working tree, not just tracked files, so an untracked convergence
# artifact cannot slip through. The scanner is a separate file so its own
# patterns can never match themselves.
python3 "${asset_root}/tests/scan_for_secrets.py" "${asset_root}"

echo "academic-assets checks passed"
