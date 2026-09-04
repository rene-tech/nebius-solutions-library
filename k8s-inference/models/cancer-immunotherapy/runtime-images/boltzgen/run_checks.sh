#!/usr/bin/env bash
# Offline source, image, adapter, localization, renderer and validator checks.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${HERE}"

for document in \
  image-lock.json \
  evidence/checkpoint-localization-receipt.json \
  evidence/h100-qualification-receipt.json \
  qualification/generated-plan-cold.json \
  qualification/generated-plan-prepared.json \
  qualification/manifest-template.json \
  qualification/request-template.json; do
  python3 -m json.tool "${document}" >/dev/null
done
python3 -m py_compile qualification/*.py tests/*.py

python3 - <<'PY'
import hashlib
import json
from pathlib import Path

lock = json.loads(Path("image-lock.json").read_text(encoding="utf-8"))
for name, key in (("Dockerfile", "dockerfile_sha256"), ("requirements.lock", "dependency_lock_sha256")):
    observed = hashlib.sha256(Path(name).read_bytes()).hexdigest()
    assert observed == lock["image"]["build_inputs"][key], (name, observed)
assert not lock["route_exposed"]
assert lock["qualification"]["state"] in {
    "pending-live-h100",
    "qualified-h100-design-stage",
}
if lock["qualification"]["state"] == "qualified-h100-design-stage":
    evidence = Path(lock["qualification"]["evidence_path"])
    assert hashlib.sha256(evidence.read_bytes()).hexdigest() == lock["qualification"]["evidence_sha256"]
    receipt = json.loads(evidence.read_text(encoding="utf-8"))
    assert receipt["status"] == "passed"
    for scenario in ("cold", "prepared"):
        assert receipt[scenario]["result"]["validator"].startswith("independent-")
print("image/build lock: PASS")
PY

PYTHONPATH="${HERE}/../../../../components/control-plane/src" \
  uv run --project "${HERE}/../../../../components/control-plane" \
  --with gemmi==0.7.5 --with numpy==2.2.6 \
  python -W error::ResourceWarning -m unittest discover -s tests -v

echo "ALL BOLTZGEN OFFLINE CHECKS PASSED"
