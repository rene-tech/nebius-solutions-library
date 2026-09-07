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

