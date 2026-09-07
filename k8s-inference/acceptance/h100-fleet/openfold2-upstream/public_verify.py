#!/usr/bin/env python3
"""Run both exact OpenFold2 structure probes over public HTTP and MCP."""

from __future__ import annotations

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
BIO = ROOT / "acceptance/h100-fleet/bionemo-structure"
sys.path.insert(0, str(MEDIA))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PUBLIC = load(MEDIA / "public_verify.py", "fs2_of2_public_transport")
BIO_PUBLIC = load(BIO / "public_verify.py", "fs2_of2_bio_runtime_binding")
ENTRY = json.loads(
    (ROOT / "catalog/runtime/deployment-runtimes/openfold2-portable-h100.json").read_text()
)
BINDING = ENTRY["record"]["semantic_validator"]
SOURCE = ROOT / BINDING["source_path"]
if not SOURCE.exists():
    SOURCE = ROOT / "catalog/runtime/packaged-repository" / BINDING["source_path"]
if hashlib.sha256(SOURCE.read_bytes()).hexdigest() != BINDING["source_sha256"]:
    raise ValueError("exact OpenFold2 validator bytes changed")
VALIDATOR = load(SOURCE, "fs2_of2_original_validator")
REQUEST_IDS = ("fs2-of2-public-a", "fs2-of2-public-b")


def cases_for(model):
    if model != "openfold2":
        raise ValueError("this runner accepts only standalone OpenFold2")
    probes = VALIDATOR.build_probes(REQUEST_IDS)
    revision = ENTRY["record"]["model"]["source"]["revision"]
    return BINDING, [
        PUBLIC.AcceptanceCase(
            model,
            revision,
            "native",
            "predict-structure",
            probe.payload,
            hashlib.sha256(PUBLIC.canonical_json(probe.payload)).hexdigest(),
            "json-object",
        )
        for probe in probes
    ]


def validate_pair(model, contract, paths, directory):
    del model, directory
    probes = VALIDATOR.build_probes(REQUEST_IDS)
    if paths[0].read_bytes() == paths[1].read_bytes():
        raise ValueError("OpenFold2 returned byte-identical structures for distinct sequences")
    return {
        "status": "PASS",
        "validator": contract,
        "results": [
            VALIDATOR.validate_response(
                json.loads(path.read_bytes()), probe.run_id, probe.sequence
            )
            for path, probe in zip(paths, probes, strict=True)
        ],
    }


def actual_runtime(args, operation, model):
    pods = json.loads(
        subprocess.check_output(
            [
                "kubectl",
                "--kubeconfig",
                str(args.kubeconfig),
                "--context",
                args.context,
                "-n",
                "fs2-models",
                "get",
                "pods",
                "-o",
                "json",
            ]
        )
    )
    return BIO_PUBLIC.runtime_binding(
        pods["items"], operation, BIO_PUBLIC.expected_runtime(model)
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument(
        "--kubeconfig",
        type=Path,
        default=Path(
            "/home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/kubeconfig"
        ),
    )
    parser.add_argument("--context", default="k8s-inference-h100")
    parser.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args()
    args.models = ["openfold2"]
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(
        0
        if asyncio.run(
            PUBLIC.verify(
                args,
                case_factory=cases_for,
                runtime_resolver=actual_runtime,
                pair_validator=validate_pair,
                report_schema="fs2-h100-openfold2-upstream-public-verification/v1",
            )
        )
        else 1
    )
