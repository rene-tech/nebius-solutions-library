"""Compose single-cell onboarding from real runtime evidence and live state.

This publishes source contracts only. A runtime receipt permits testing the
hosted route; it does not assert customer readiness or scientific accuracy.
"""

import argparse
import copy
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "models/molecular-dynamics/gromacs/activation"))
sys.path.insert(0, str(ROOT / "models/molecular-dynamics/gromacs/runtime"))
sys.path.insert(0, str(ROOT / "components/control-plane/src"))
sys.path.insert(0, str(HERE / "runtime"))
from prepare import (  # noqa: E402 - shared composer uses the solution source tree
    digest,
    prepare,
    qualification_proofs,
    rebase_profile_qualifications,
    validate_profile_qualifications,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--runtime-receipts", type=Path, required=True)
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--runtime-pod", type=Path, required=True)
    parser.add_argument("--component-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--publish-catalog", action="store_true")
    parser.add_argument("--refresh-onboarding", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    values = json.loads((args.baseline / "values.json").read_text())
    captured = values["scientificBatch"]["executionMap"]
    contracts = ROOT / "catalog/runtime/contracts"
    source = json.loads((contracts / "scientific-execution-map.json").read_text())
    replacing = any(item["model_id"] == "scvi-scanvi" for item in captured["models"])
    if args.refresh_onboarding:
        existing_catalog = json.loads((contracts / "scientific-workload-profiles.json").read_text())
        existing = next(item for item in existing_catalog["profiles"] if item["model_id"] == "scvi-scanvi")
        if existing["state"] != "active" or existing["qualification"]["public_completion_receipt_sha256"]:
            raise ValueError("Never rewrite a qualified release as onboarding")
        if not replacing:
            source["models"] = [item for item in source["models"] if item["model_id"] != "scvi-scanvi"]
            source["qualification_baselines"] = {sha: ids for sha, ids in source.get("qualification_baselines", {}).items() if "scvi-scanvi" not in ids}
    elif replacing:
        raise ValueError("The live onboarding release requires --refresh-onboarding")
    if source["schema"] != captured["schema"] or source["models"] != captured["models"]:
        raise ValueError(
            "Source and actual live execution rows differ; do not overwrite them"
        )
    # The same rows can have extra valid historical projections in either
    # source or deployment. Verify both, retaining all existing proofs.
    captured["qualification_baselines"] = {
        **qualification_proofs(captured),
        **qualification_proofs(source),
    }
    result_path = args.runtime_receipts / "hlca-receipt.json"
    result = json.loads(result_path.read_text())
    components = json.loads(args.component_receipt.read_text())
    pod = json.loads(args.runtime_pod.read_text())
    image = args.runtime_image
    if (
        pod["spec"]["containers"][0]["image"] != image
        or pod["status"]["phase"] != "Succeeded"
        or pod["status"]["containerStatuses"][0]["state"]["terminated"]["exitCode"] != 0
    ):
        raise ValueError("Current component evidence requires the successful exact-image pod")
    if (
        result["status"] != "succeeded"
        or result["cells"] != 584944
        or not components["passed"]
    ):
        raise ValueError(
            "Require the successful real HLCA cohort plus restart/query evidence"
        )
    evidence = {
        "schema": "fs2-serve.nebius.ai/scvi-runtime-qualification/v1",
        "model_id": "scvi-scanvi",
        "runtime_image": image,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "customer_ready": False,
        "scientific_convergence_claimed": False,
        "tests": [
            {
                "case": "full-state-interruption-and-query-mapping",
                "pool": "h100-1x",
                "status": "succeeded",
                "runtime_image": image,
                "component_receipt_sha256": hashlib.sha256(args.component_receipt.read_bytes()).hexdigest(),
                "pod_receipt_sha256": hashlib.sha256(args.runtime_pod.read_bytes()).hexdigest(),
            },
        ],
        "prior_large_data_evidence": [
            {
                "case": "HLCA-core-584944",
                "runtime_image": "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/visual-science/scvi-scanvi@sha256:eb2835095574d290a90d289fc2790e49d984b5bb4fa4911f74156d7f172d77e5",
                "status": "succeeded",
                "gpu_name": result["gpu"],
                "input_sha256": result["input_sha256"],
                "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
                "cells": result["cells"],
                "elapsed_seconds": result["elapsed_seconds"],
            }
        ],
        "component_receipt_sha256": hashlib.sha256(
            args.component_receipt.read_bytes()
        ).hexdigest(),
    }
    from fs2_serve.scientific_batch.adapters.common import _RECIPE_SHARED_PATHS

    paths = set(_RECIPE_SHARED_PATHS) | {
        "components/control-plane/src/fs2_serve/scientific_batch/adapters/scvi_scanvi.py",
        "components/control-plane/src/fs2_serve/scientific_batch/native_workflows.py",
        "components/control-plane/src/fs2_serve/scientific_batch/gromacs_checkpoints.py",
        "models/visual-science/scvi-scanvi/Dockerfile.batch",
        "catalog/runtime/schema/scvi-workflow-request.schema.json",
    }
    paths.update(
        str(path.relative_to(ROOT)) for path in (HERE / "runtime/fs2_scvi").glob("*.py")
    )
    recipe = {
        "schema": "fs2-serve.nebius.ai/scvi-runtime-recipe/v1",
        "files": [
            {
                "path": path,
                "sha256": hashlib.sha256((ROOT / path).read_bytes()).hexdigest(),
            }
            for path in sorted(paths)
        ],
    }
    candidate = json.loads((HERE / "activation/workload-profile.json").read_text())[
        "profile"
    ]
    profile, row, overlay, cm = prepare(
        values,
        (args.baseline / "scheduling.json").read_bytes(),
        candidate,
        image,
        evidence,
        digest(recipe),
        replace_existing=replacing,
    )
    args.output.mkdir(parents=True, exist_ok=False)
    for name, value in (
        ("profile.json", profile),
        ("execution-row.json", row),
        ("activation.values.json", overlay),
        ("scheduling.configmap.json", cm),
        ("runtime-receipt.json", evidence),
        ("source-recipe.json", recipe),
    ):
        (args.output / name).write_text(json.dumps(value, indent=2) + "\n")
    if args.publish_catalog:
        catalog_path = contracts / "scientific-workload-profiles.json"
        catalog = json.loads(catalog_path.read_text())
        if args.refresh_onboarding:
            catalog["profiles"] = [item for item in catalog["profiles"] if item["model_id"] != "scvi-scanvi"]
        if any(item["model_id"] == "scvi-scanvi" for item in catalog["profiles"]):
            raise ValueError("Already published; prepare an explicit successor")
        desired = overlay["scientificBatch"]["executionMap"]
        catalog["profiles"] = rebase_profile_qualifications(
            catalog["profiles"], source, desired,
            replace_models=frozenset({"scvi-scanvi"}) if replacing else frozenset(),
        )
        catalog["profiles"].append(profile)
        validate_profile_qualifications(catalog["profiles"], desired)
        published = copy.deepcopy(desired)
        if "snapshot_bundles" in source:
            published["snapshot_bundles"] = source["snapshot_bundles"]
        else:
            published.pop("snapshot_bundles", None)
        catalog_path.write_text(json.dumps(catalog, indent=2) + "\n")
        (contracts / "scientific-execution-map.json").write_text(
            json.dumps(published, indent=2) + "\n"
        )
        receipt_path = contracts / "scientific-source-candidate-receipts.json"
        receipts = json.loads(receipt_path.read_text())
        if args.refresh_onboarding:
            receipts["receipts"] = [item for item in receipts["receipts"] if item["model_id"] != "scvi-scanvi"]
        if any(item["model_id"] == "scvi-scanvi" for item in receipts["receipts"]):
            raise ValueError("Existing source receipt requires explicit reconciliation")
        receipts["receipts"].append(
            {
                "model_id": "scvi-scanvi",
                "upstream_name": "scVI / scANVI",
                "backend_identity": "native-upstream",
                "status": "candidate",
                "qualification_state": "unqualified",
                "observation_method": "git-ls-remote-head",
                "source": {
                    k: v for k, v in profile["source"].items() if k != "classification"
                },
                "access_profile": "standard",
                "access_state": "not-required",
                "notes": "scvi-tools 1.5.0.post1 exact annotated tag, MIT license. Real H100 runtime tested; hosted qualification is separate.",
            }
        )
        receipt_path.write_text(json.dumps(receipts, indent=2) + "\n")
    print(
        json.dumps(
            {
                "state": "active-onboarding",
                "customer_ready": False,
                "retained_models": len(captured["models"]),
                "output": str(args.output),
            }
        )
    )


if __name__ == "__main__":
    main()
