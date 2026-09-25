#!/usr/bin/env python3
"""Validate actual downloaded outputs using the pack's own MD analysis tool.

Can be rerun as independent jobs finish. Existing successful proofs are reused
only for the same analyzer and recipe. Partial coverage never qualifies a pack.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


def read(path):
    return json.loads(path.read_bytes())


def encoded(value):
    return (
        json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()


def main(args):
    os.umask(0o077)
    manifest = read(args.pack / "manifest.json")
    tool_path = "molecular-dynamics/analyze-md.py"
    tool = args.pack / tool_path
    entry = next(o for o in manifest["objects"] if o["path"] == tool_path)
    if hashlib.sha256(tool.read_bytes()).hexdigest() != entry["sha256"]:
        raise ValueError("analyzer_checksum_mismatch")
    args.output.mkdir(parents=True, exist_ok=True)
    identity = {
        "analyzer_sha256": entry["sha256"],
        "cohort": str(args.cohort.resolve()),
    }
    identity_path = args.output / "identity.json"
    if identity_path.exists() and read(identity_path) != identity:
        raise ValueError("validation_identity_changed_use_new_output")
    identity_path.write_bytes(encoded(identity))
    results, missing, failures = [], [], []
    for case in manifest["cases"]:
        if case["category"] != "molecular-dynamics":
            continue
        for recipe in read(args.pack / case["recipes"])["recipes"]:
            case_id, model = case["id"], recipe["model_id"]
            pair = {"case_id": case_id, "model_id": model}
            run = args.cohort / case_id / model
            receipt = run / "receipt.json"
            if not receipt.exists() or read(receipt)["state"] != "succeeded":
                missing.append(
                    {
                        **pair,
                        "state": read(receipt)["state"]
                        if receipt.exists()
                        else "not_started",
                    }
                )
                continue
            destination = args.output / case_id / model
            validation = destination / "validation.json"
            if not validation.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                process = subprocess.run(
                    [
                        sys.executable,
                        str(tool),
                        "--pack",
                        str(args.pack),
                        "--case",
                        case_id,
                        "--model",
                        model,
                        "--run",
                        str(run),
                        "--output",
                        str(destination),
                    ],
                    capture_output=True,
                    check=False,
                )
                (destination.parent / (model + "-analysis.log")).write_bytes(
                    process.stdout + process.stderr
                )
                if process.returncode:
                    failures.append(
                        {
                            **pair,
                            "state": "validation_failed",
                            "exit_code": process.returncode,
                        }
                    )
                    print(json.dumps(failures[-1]), flush=True)
                    continue
            proof = read(validation)
            if proof["recipe_sha256"] != hashlib.sha256(encoded(recipe)).hexdigest():
                raise ValueError("proof_recipe_mismatch")
            if proof["operation_id"] != read(receipt)["operation_id"]:
                raise ValueError("proof_operation_mismatch")
            results.append(proof)
            print(
                json.dumps(
                    {
                        **pair,
                        "state": proof["state"],
                        "server_elapsed_seconds": proof.get("server_elapsed_seconds"),
                    }
                ),
                flush=True,
            )
    report = {
        **identity,
        "results": results,
        "missing": missing,
        "failures": failures,
        "scope": "Exact customer MCP recipes, downloaded native outputs and bundled analysis; not a browser/agent or efficacy qualification.",
    }
    (args.output / "report.json").write_bytes(encoded(report))
    print(
        json.dumps({"passed": len(results), "missing": missing, "failures": failures}),
        flush=True,
    )
    return 1 if failures else 2 if missing and not args.allow_incomplete else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--cohort", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-incomplete", action="store_true")
    raise SystemExit(main(parser.parse_args()))
