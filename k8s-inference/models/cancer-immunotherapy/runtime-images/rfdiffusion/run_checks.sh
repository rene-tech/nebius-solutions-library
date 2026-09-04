#!/usr/bin/env bash
# Offline checks for the RFdiffusion scientific runtime image.
# No cluster, no GPU, no registry, no network.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${HERE}"

echo "== python compile =="
python3 -m py_compile runtime_entrypoint.py build_rfdiffusion.py fetch_verified.py \
  contract/generate_golden_argv.py \
  qualification/stage_checkpoint.py qualification/render_job.py \
  qualification/stage_target.py qualification/validate_result.py \
  tests/test_rfdiffusion_runtime.py tests/test_adapter_contract.py \
  tests/test_qualification_renderer.py
echo "ok"

echo
echo "== image lock is valid JSON with the required sections =="
python3 - <<'PY'
import json
import pathlib

lock = json.loads(pathlib.Path("image-lock.json").read_text(encoding="utf-8"))
for section in (
    "schema", "model_id", "owner_task", "source", "image", "adapter",
    "weight_policy", "external_artifacts", "artifact_delivery",
    "mount_contract", "qualification", "upstream_contract_defects",
):
    assert section in lock, section
print("sections ok:", ", ".join(sorted(lock)))
PY

echo
echo "== lock and runtime inputs agree, tag is derived from the pinned identities =="
python3 build_rfdiffusion.py --check

echo
echo "== adapter-to-image golden argv is current =="
python3 contract/generate_golden_argv.py --check

echo
echo "== offline contract tests (ResourceWarning is an error) =="
python3 -W error::ResourceWarning -m unittest discover -s tests -v

echo
echo "ALL OFFLINE CHECKS PASSED"
