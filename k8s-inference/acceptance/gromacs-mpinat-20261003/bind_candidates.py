"""Bind tested wrapper digests to their exact GROMACS source identities.

Mechanical catalog update only. No cloud deployment, hardware availability or
customer-readiness claim. Retain unchanged Apps and valid historical proofs.
"""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

SOLUTION = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("_gromacs_release_prepare",
    SOLUTION / "models/molecular-dynamics/gromacs/activation/prepare.py")
activation = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(activation)


def evidence(directories, *, mpi):
    tests, images = [], set()
    for directory in directories:
        raw = (directory / "receipt.json").read_bytes()
        receipt = json.loads(raw)
        result_raw = (directory / "workspace/result.json").read_bytes()
        result = json.loads(result_raw)
        validation = receipt.get("validation", {})
        if (receipt.get("worker_exit_code") != 0 or validation.get("status") != "passed"
                or validation.get("expected_failure_case") or result.get("status") != "succeeded"
                or validation.get("native_result_sha256") != hashlib.sha256(result_raw).hexdigest()
                or receipt.get("customer_path_tested") is not False):
            raise ValueError("Require passed exact-image native candidate evidence, not smoke/failed receipts")
        if mpi and not validation.get("checks", {}).get("all_rank_bindings_complete"):
            raise ValueError("MPI candidate lacks complete observed rank/device bindings")
        image = receipt["image"]
        if "@sha256:" not in image or not any(digest in receipt.get("image_id", "")
                for digest in receipt.get("allowed_image_digests", [])):
            raise ValueError("Runtime digest was not observed")
        capacity = json.loads((directory / "capacity-before.json").read_text())
        pool = capacity["labels"]["accelerator.fs2.nebius/pool-id"]
        images.add(image)
        tests.append({"case": directory.name, "pool": pool, "nodes": result.get("nodes", 1),
                      "gpus_per_node": result.get("gpus_per_node", receipt["gpus"]),
                      "receipt_sha256": hashlib.sha256(raw).hexdigest(),
                      "native_result_sha256": hashlib.sha256(result_raw).hexdigest(),
                      "input_sha256": receipt["input_sha256"], "request_sha256": receipt["request_sha256"],
                      "finished_at": receipt["finished_at"], "checks": validation["checks"]})
    if len(images) != 1 or not tests or not any(t["pool"].startswith("h100-") for t in tests):
        raise ValueError("Require one exact image and observed H100 native qualification")
    if not mpi and not any(t["pool"].startswith("l40s-") for t in tests):
        raise ValueError("Single-GPU successor needs both H100 and L40S evidence")
    return {"schema": "fs2-serve.nebius.ai/gromacs-runtime-qualification/v1",
            "runtime_image": next(iter(images)), "recorded_at": max(t["finished_at"] for t in tests),
            "tests": tests, "customer_ready": False, "customer_paths_tested": False,
            "scope": "Only the listed native shapes; new REST/MCP/agent qualification remains separate"}


def bind(catalog, execution, proofs, recipe_shas, *, active_deadline_seconds=None):
    models = set(proofs)
    if not models or not models.issubset({"gromacs", "gromacs-mpi"}):
        raise ValueError("Select one or both GROMACS Apps explicitly")
    if set(recipe_shas) != models:
        raise ValueError("Each selected App needs its own exact source recipe")
    if active_deadline_seconds is not None and not 60 <= active_deadline_seconds <= 606600:
        raise ValueError("Deadline must fit the seven-day budget and bounded export grace")
    before = copy.deepcopy(execution)
    result = copy.deepcopy(catalog)
    desired = copy.deepcopy(execution)
    rows = {row["model_id"]: row for row in desired["models"]}
    if (not models.issubset(rows)
            or not models.issubset({profile["model_id"] for profile in result["profiles"]})):
        raise ValueError("Selected App is missing from the catalog or execution map")
    for profile in result["profiles"]:
        model = profile["model_id"]
        if model not in models:
            continue
        proof = proofs[model]
        identity = profile["execution_identity"]
        identity.update(runtime_image_digest=proof["runtime_image"].rsplit("@", 1)[1],
                        runtime_recipe_sha256=recipe_shas[model],
                        workload_recipe_sha256=activation.digest(profile["workload"]))
        identity["execution_identity_sha256"] = activation.digest(
            {key: value for key, value in identity.items() if key != "execution_identity_sha256"})
        rows[model]["execution_identity_sha256"] = identity["execution_identity_sha256"]
        rows[model]["stages"][0]["image"] = proof["runtime_image"]
        if active_deadline_seconds is not None:
            rows[model]["stages"][0]["active_deadline_seconds"] = active_deadline_seconds
        profile["qualification"] = {"h100_semantic_receipt_sha256": activation.digest(proof),
            "public_completion_receipt_sha256": None, "scheduler_eligibility_receipt_sha256": None,
            "execution_map_sha256": None, "qualified_at": proof["recorded_at"]}
    desired["qualification_baselines"] = activation.rebase_qualification_baselines(before, desired, models)
    result["profiles"] = activation.rebase_profile_qualifications(result["profiles"], before, desired, models)
    final_digest = activation.digest({"schema": desired["schema"], "models": desired["models"]})
    for profile in result["profiles"]:
        if profile["model_id"] in models:
            profile["qualification"]["execution_map_sha256"] = final_digest
    activation.validate_profile_qualifications(result["profiles"], desired)
    return result, desired


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--single", type=Path, action="append")
    parser.add_argument("--mpi", type=Path, action="append")
    parser.add_argument("--mpi-cuda-aware", action="store_true",
                        help="Include the additive CUDA-aware Open MPI build recipe for the MPI App only")
    parser.add_argument("--evidence-output", type=Path, required=True)
    parser.add_argument("--active-deadline-seconds", type=int)
    args = parser.parse_args()
    if not args.single and not args.mpi:
        parser.error("select --single, --mpi, or both")
    if args.mpi_cuda_aware and not args.mpi:
        parser.error("--mpi-cuda-aware requires --mpi qualification evidence")
    proofs, recipes = {}, {}
    for model, directories, mpi in (("gromacs", args.single, False), ("gromacs-mpi", args.mpi, True)):
        if directories:
            proofs[model] = evidence(directories, mpi=mpi)
            recipes[model] = activation.source_recipe(SOLUTION, mpi_cuda_aware=mpi and args.mpi_cuda_aware)
    contracts = SOLUTION / "catalog/runtime/contracts"
    catalog_path, map_path = (contracts / name for name in (
        "scientific-workload-profiles.json", "scientific-execution-map.json"))
    catalog, execution = bind(json.loads(catalog_path.read_text()), json.loads(map_path.read_text()),
                              proofs, {model: activation.digest(recipe) for model, recipe in recipes.items()},
                              active_deadline_seconds=args.active_deadline_seconds)
    args.evidence_output.mkdir(parents=True, exist_ok=False)
    for name, value in (("runtime-proofs.json", proofs), ("source-recipes.json", recipes)):
        (args.evidence_output / name).write_text(json.dumps(value, indent=2) + "\n")
    catalog_path.write_text(json.dumps(catalog, indent=2) + "\n")
    map_path.write_text(json.dumps(execution, indent=2) + "\n")
    print(json.dumps({"models": sorted(proofs), "source_updated": True, "deployed": False,
                      "customer_ready": False, "evidence": str(args.evidence_output)}))


if __name__ == "__main__":
    main()
