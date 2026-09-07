"""Deployment hardware observations must describe the selected model itself."""

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_h100_fleet_bindings_reference_their_own_retained_measurements():
    compatibility = json.loads((ROOT / "catalog/profiles/model-accelerator-compatibility.json").read_text())
    for model_id, model in compatibility["models"].items():
        for runtime in model["runtimes"].values():
            for binding in runtime["bindings"]:
                evidence = binding.get("evidence", "")
                if not evidence.startswith("acceptance/h100-fleet/bionemo-structure/fragments/"):
                    continue
                path, digest = evidence.split("@sha256:")
                payload = (ROOT / path).read_bytes()
                measurement = json.loads(payload)
                assert measurement["model_id"] == model_id
                assert hashlib.sha256(payload).hexdigest() == digest
                assert measurement["qualification"]["semantic_status"] == "PASS-6-of-6"


def test_evo2_h100_qualification_is_bound_to_the_two_gpu_portable_image():
    compatibility = json.loads((ROOT / "catalog/profiles/model-accelerator-compatibility.json").read_text())
    runtimes = compatibility["models"]["evo2-40b"]["runtimes"]
    selected = runtimes["evo2-40b-upstream-portable"]
    record = json.loads((ROOT / "catalog/runtime/deployment-runtimes/evo2-40b-portable-h100.json").read_text())
    assert selected["runtime_ref"] == record["record"]["runtime"]["image"]["reference"]
    assert selected["requirements"]["gpu_count"] == 2
    assert selected["bindings"][0]["accelerator_class"] == "nvidia-h100-sxm5-80gb"
    path, digest = selected["bindings"][0]["evidence"].split("@sha256:")
    assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest
    archival = runtimes["catalog-canonical"]
    assert archival["requirements"]["gpu_count"] == 1
    assert all(not binding["enabled"] for binding in archival["bindings"])
