"""Compose an additive native MD onboarding release from exact GPU evidence.

Uses the existing GROMACS release composer and baseline-preserving Helm path.
No cluster calls are made here. Runtime qualification permits hosted acceptance,
not a customer-ready claim; the public completion receipt remains absent.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE / "gromacs/runtime"))
sys.path.insert(0, str(HERE / "gromacs/activation"))
sys.path.insert(0, str(ROOT / "components/control-plane/src"))

from prepare import canonical, digest, prepare  # noqa: E402

MODELS = frozenset({"lammps", "namd"})


def source_recipe(root: Path, model: str) -> dict:
    from fs2_serve.scientific_batch.adapters.common import _RECIPE_SHARED_PATHS

    if model not in MODELS:
        raise ValueError("unregistered native engine")
    paths = set(_RECIPE_SHARED_PATHS) | {
        f"components/control-plane/src/fs2_serve/scientific_batch/adapters/{model}.py",
        "components/control-plane/src/fs2_serve/scientific_batch/adapters/native_md.py",
        "components/control-plane/src/fs2_serve/scientific_batch/native_workflows.py",
        "components/control-plane/src/fs2_serve/scientific_batch/gromacs_checkpoints.py",
        "components/control-plane/src/fs2_serve/scientific_batch/gromacs_storage.py",
        "components/control-plane/src/fs2_serve/scientific_batch/gromacs_storage_routes.py",
        "components/control-plane/src/fs2_serve/scientific_batch/artifact_bridge.py",
        "components/control-plane/src/fs2_serve/scientific_artifacts.py",
        "components/control-plane/src/fs2_serve/scientific_batch/workload_routes.py",
        f"catalog/runtime/schema/{model}-workflow-request.schema.json",
        f"models/molecular-dynamics/{model}/runtime/Containerfile",
        "models/molecular-dynamics/gromacs/runtime/fs2_gromacs/files.py",
        "models/molecular-dynamics/gromacs/runtime/fs2_gromacs/contracts.py",
    }
    paths.update(
        str(path.relative_to(root))
        for path in (root / f"models/molecular-dynamics/{model}/runtime/fs2_{model}").glob("*.py")
    )
    return {
        "schema": "fs2-serve.nebius.ai/native-md-runtime-recipe/v1",
        "model_id": model,
        "files": [
            {
                "path": path,
                "sha256": hashlib.sha256((root / path).read_bytes()).hexdigest(),
                "size_bytes": (root / path).stat().st_size,
            }
            for path in sorted(paths)
        ],
    }


def validate_evidence(value: dict, model: str) -> dict:
    if value.get("model_id") != model or model not in MODELS:
        raise ValueError("runtime receipt names another native engine")
    if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", value.get("runtime_image", "")):
        raise ValueError("runtime receipt must identify an immutable worker image")
    if value.get("customer_ready") is not False or not value.get("recorded_at"):
        raise ValueError("runtime receipt must not claim customer qualification")
    if value.get("status") != "passed":
        raise ValueError("runtime cohort is incomplete or failed; retain its evidence without activation")
    tests = value.get("tests", [])
    if not tests or any(test.get("status") not in {"passed", "succeeded"} for test in tests):
        raise ValueError("activation requires a completed successful native cohort")
    for test in tests:
        for key in ("case", "pool", "gpu_name", "driver"):
            if not isinstance(test.get(key), str) or not test[key]:
                raise ValueError(f"runtime test is missing {key}")
        for key in ("input_sha256", "result_sha256", "validation_sha256"):
            if not re.fullmatch(r"[a-f0-9]{64}", test.get(key, "")):
                raise ValueError(f"runtime test is missing exact {key}")
    if not any("H100" in test["gpu_name"] for test in tests):
        # The existing profile schema currently names its semantic receipt
        # h100_semantic_receipt_sha256. Do not put other-GPU evidence there.
        raise ValueError("this deployment's semantic receipt requires actual H100 evidence")
    return value


def compose(
    values: dict, scheduling: bytes, candidates: dict, evidence: dict, recipes: dict, *, replace_models=frozenset()
):
    if not set(replace_models) <= set(candidates):
        raise ValueError("replacement requires an explicit candidate and runtime receipt")
    baseline = copy.deepcopy(values)
    overlay, profiles = {}, {}
    # Replace explicit successors before adding new proof baselines which
    # contain them; unrelated App identities remain exactly as captured.
    for model in sorted(candidates, key=lambda model: (model not in replace_models, model)):
        receipt = validate_evidence(evidence[model], model)
        candidate = copy.deepcopy(candidates[model])
        tested_pools = {test["pool"] for test in receipt["tests"]}
        selected = [pool for pool in candidate["resources"]["compatible_pool_ids"] if pool in tested_pools]
        if not selected:
            raise ValueError("native receipt has no tested pool in this deployment")
        candidate["resources"]["compatible_pool_ids"] = selected
        profile, _, current, cm = prepare(
            baseline,
            scheduling,
            candidate,
            receipt["runtime_image"],
            receipt,
            digest(recipes[model]),
            replace_existing=model in replace_models,
        )
        profiles[model] = profile
        for key, value in current.items():
            overlay.setdefault(key, {}).update(value)
            baseline[key].update(value)
        scheduling = cm["data"][baseline["scientificBatch"]["schedulingContractKey"]].encode()
    final_map = overlay["scientificBatch"]["executionMap"]
    map_digest = digest({"schema": final_map["schema"], "models": final_map["models"]})
    for profile in profiles.values():
        profile["qualification"]["execution_map_sha256"] = map_digest
    return profiles, overlay, cm


def publish_catalog(profiles: dict, final_map: dict, captured_map: dict, *, replace_models=frozenset()) -> None:
    contracts = ROOT / "catalog/runtime/contracts"
    map_path = contracts / "scientific-execution-map.json"
    source_map = json.loads(map_path.read_text())

    def comparable(value):
        return {key: val for key, val in value.items() if key != "snapshot_bundles"}

    if comparable(source_map) != comparable(captured_map):
        raise ValueError("source execution map differs from captured deployment")
    catalog_path = contracts / "scientific-workload-profiles.json"
    catalog = json.loads(catalog_path.read_text())
    existing = set(profiles) & {item["model_id"] for item in catalog["profiles"]}
    if existing != set(replace_models):
        raise ValueError("native engine already exists; use an explicit successor workflow")
    receipts_path = contracts / "scientific-source-candidate-receipts.json"
    receipts = json.loads(receipts_path.read_text())
    if set(profiles) & {item["model_id"] for item in receipts["receipts"]} != set(replace_models):
        raise ValueError("native engine source receipt already exists; review its existing identity")
    for model in sorted(profiles):
        receipt = json.loads((HERE / model / "activation/source-candidate-receipt.json").read_text())
        expected_source = {key: value for key, value in profiles[model]["source"].items() if key != "classification"}
        if receipt["model_id"] != model or receipt["source"] != expected_source:
            raise ValueError("native source receipt and activated profile disagree")
        if model in replace_models:
            prior = next(item for item in receipts["receipts"] if item["model_id"] == model)
            if prior["source"] != receipt["source"]:
                raise ValueError("successor changes the native source; review that acquisition explicitly")
        else:
            receipts["receipts"].append(receipt)
    catalog["profiles"] = [
        profiles[item["model_id"]] if item["model_id"] in replace_models else item for item in catalog["profiles"]
    ]
    catalog["profiles"].extend(profiles[model] for model in sorted(set(profiles) - set(replace_models)))
    proofs = set(final_map.get("qualification_baselines", {}))
    final_digest = digest({"schema": final_map["schema"], "models": final_map["models"]})
    for profile in catalog["profiles"]:
        if profile.get("route_exposed") and profile["qualification"]["execution_map_sha256"] not in proofs | {
            final_digest
        }:
            raise ValueError("native addition would invalidate another App's qualification")
    # Live snapshot bundle paths are deployment data, not new repository data.
    published_map = copy.deepcopy(final_map)
    if "snapshot_bundles" in source_map:
        published_map["snapshot_bundles"] = source_map["snapshot_bundles"]
    else:
        published_map.pop("snapshot_bundles", None)
    catalog_path.write_text(json.dumps(catalog, indent=2) + "\n")
    map_path.write_text(json.dumps(published_map, indent=2) + "\n")
    receipts_path.write_text(json.dumps(receipts, indent=2, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--runtime-receipt", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--publish-catalog", action="store_true")
    parser.add_argument("--replace-existing-model", action="append", choices=sorted(MODELS), default=[])
    args = parser.parse_args()
    os.umask(0o077)
    evidence = {}
    for path in args.runtime_receipt:
        value = json.loads(path.read_text())
        model = value.get("model_id")
        if model in evidence:
            raise ValueError("duplicate native engine receipt")
        evidence[model] = validate_evidence(value, model)
    candidates = {
        model: json.loads((HERE / model / "activation/workload-profile.json").read_text())["profile"]
        for model in evidence
    }
    recipes = {model: source_recipe(ROOT, model) for model in evidence}
    values = json.loads((args.baseline / "values.json").read_text())
    captured_map = values["scientificBatch"]["executionMap"]
    if isinstance(captured_map, str):
        captured_map = json.loads(captured_map)
    profiles, overlay, cm = compose(
        values,
        (args.baseline / "scheduling.json").read_bytes(),
        candidates,
        evidence,
        recipes,
        replace_models=frozenset(args.replace_existing_model),
    )
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, value in [
        ("profiles.json", profiles),
        ("activation.values.json", overlay),
        ("scheduling.configmap.json", cm),
        ("runtime-receipts.json", evidence),
        ("source-recipes.json", recipes),
    ]:
        (args.output / name).write_bytes(canonical(value) + b"\n")
    if args.publish_catalog:
        publish_catalog(
            profiles,
            overlay["scientificBatch"]["executionMap"],
            captured_map,
            replace_models=frozenset(args.replace_existing_model),
        )
    print(
        json.dumps(
            {
                "activation": str(args.output),
                "models": sorted(profiles),
                "source_updated": args.publish_catalog,
                "deployed": False,
                "customer_ready": False,
            }
        )
    )


if __name__ == "__main__":
    main()
