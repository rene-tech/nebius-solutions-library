#!/usr/bin/env python3
"""Original standalone Preview2 inputs over existing public HTTP and MCP."""
import argparse
import asyncio
import hashlib
import importlib.util
import json
import logging
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
MEDIA = ROOT / "acceptance/h100-fleet/medical-media"
sys.path.insert(0, str(MEDIA))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PUBLIC = load(MEDIA / "public_verify.py", "fs2_of3_public_transport")
ENTRY = json.loads((ROOT / "catalog/runtime/deployment-runtimes/openfold3-portable-h100.json").read_text())
BINDING = ENTRY["record"]["semantic_validator"]
PACKAGED = ROOT / "catalog/runtime/packaged-repository"
SOURCE = PACKAGED / BINDING["source_path"]
FIXTURE = PACKAGED / BINDING["fixture_path"]
if (hashlib.sha256(SOURCE.read_bytes()).hexdigest() != BINDING["source_sha256"]
        or hashlib.sha256(FIXTURE.read_bytes()).hexdigest() != BINDING["fixture_sha256"]):
    raise ValueError("Original standalone OpenFold3 validator/fixture changed")
VALIDATOR = load(SOURCE, "fs2_of3_original_validator")
REQUEST_IDS = ("fs2-of3-public-a", "fs2-of3-public-b")


def cases_for(model):
    if model != "openfold3":
        raise ValueError("Standalone Preview2 only; OpenBind is a distinct model profile")
    fixture = VALIDATOR._read_fixture(FIXTURE)
    revision = ENTRY["record"]["model"]["source"]["revision"]
    payloads = [VALIDATOR._request_for_case(fixture, case) for case in REQUEST_IDS]
    return BINDING, [PUBLIC.AcceptanceCase(model, revision, "native", "predict-structure", payload,
        hashlib.sha256(PUBLIC.canonical_json(payload)).hexdigest(), "json-object") for payload in payloads]


def validate_pair(model, contract, paths, directory):
    del model, directory
    return {"status": "PASS", "validator": contract, "results": [
        VALIDATOR._validate_response(json.loads(path.read_bytes()), request_id)
        for path, request_id in zip(paths, REQUEST_IDS, strict=True)]}


def actual_runtime(args, operation, model):
    del model
    pods = json.loads(subprocess.check_output(["kubectl", "--kubeconfig", str(args.kubeconfig),
        "--context", args.context, "-n", "fs2-models", "get", "pods", "-o", "json"]))
    return PUBLIC.runtime_binding(pods["items"], operation, {"image": ENTRY["record"]["runtime"]["image"]["reference"],
        "model_revision": ENTRY["record"]["model"]["source"]["revision"]})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--kubeconfig", type=Path, default=Path("/home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/kubeconfig"))
    parser.add_argument("--context", default="k8s-inference-h100")
    parser.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args()
    args.models = ["openfold3"]
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(0 if asyncio.run(PUBLIC.verify(args, case_factory=cases_for,
        runtime_resolver=actual_runtime, pair_validator=validate_pair,
        report_schema="fs2-h100-openfold3-preview2-public-verification/v1")) else 1)
