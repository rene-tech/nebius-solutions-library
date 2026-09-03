#!/usr/bin/env bash
# Offline checks for the Mosaic scientific-batch runtime image.
# No cluster, no GPU, no registry, no network beyond the local git object store.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${HERE}"

echo "== python compile =="
python3 -m py_compile runtime_entrypoint.py build_mosaic.py \
  qualification/render_plan.py qualification/submit_plan.py \
  qualification/validate_result.py tests/test_mosaic_runtime.py
echo "ok"

echo
echo "== image lock is valid JSON with the required sections =="
python3 - <<'PY'
import json
import pathlib

lock = json.loads(pathlib.Path("image-lock.json").read_text(encoding="utf-8"))
for section in (
    "schema", "source", "adapter", "image", "weight_policy",
    "external_artifacts", "artifact_delivery", "upstream_contract_defects",
):
    assert section in lock, section
print("sections ok:", ", ".join(sorted(lock)))
PY

echo
echo "== offline contract tests =="
python3 -m unittest discover -s tests -v

echo
echo "ALL OFFLINE CHECKS PASSED"
