"""Compose a single GROMACS+MPI release, preserving the captured live baseline.

Prepare the enhanced single-GPU successor first, so the MPI addition freezes
that successor in its qualification baseline. This edits source only; deployment
still compares the original captured live release in release_backend.py.
"""

import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys


def replace_pair(values, scheduling, candidates, images, evidence, recipe_digest):
    """Requalify both GROMACS identities atomically, preserving other App rows."""
    from prepare import digest, prepare

    baseline = copy.deepcopy(values)
    execution = baseline["scientificBatch"]["executionMap"]
    if isinstance(execution, str):
        execution = json.loads(execution)
        baseline["scientificBatch"]["executionMap"] = execution
    selected = {"gromacs", "gromacs-mpi"}
    if not selected.issubset({row["model_id"] for row in execution["models"]}):
        raise ValueError("Both predecessors must exist for a paired successor")
    # A frozen proof containing a replaced row is no longer valid. Only these
    # two Apps receive fresh qualification; every unrelated proof stays exact.
    execution["qualification_baselines"] = {
        sha: ids for sha, ids in execution["qualification_baselines"].items()
        if not selected.intersection(ids)
    }
    profiles = {}
    overlay = {}
    for model in ("gromacs", "gromacs-mpi"):
        profile, _, current, cm = prepare(
            baseline, scheduling, candidates[model], images[model], evidence[model],
            recipe_digest, replace_existing=True,
        )
        profiles[model] = profile
        for key, value in current.items():
            overlay.setdefault(key, {}).update(value)
            baseline[key].update(value)
        scheduling = cm["data"][baseline["scientificBatch"]["schedulingContractKey"]].encode()
    final_map = overlay["scientificBatch"]["executionMap"]
    final_digest = digest({"schema": final_map["schema"], "models": final_map["models"]})
    for profile in profiles.values():
        profile["qualification"]["execution_map_sha256"] = final_digest
    return profiles, overlay, cm


def replace_existing_pair(args):
    from prepare import ROOT, canonical, digest, runtime_receipt, source_recipe

    contracts = ROOT / "catalog/runtime/contracts"
    source_map = json.loads((contracts / "scientific-execution-map.json").read_text())
    values = json.loads((args.baseline / "values.json").read_text())
    captured = values["scientificBatch"]["executionMap"]
    if isinstance(captured, str):
        captured = json.loads(captured)
    comparable = lambda value: {key: val for key, val in value.items() if key != "snapshot_bundles"}
    if comparable(source_map) != comparable(captured):
        raise ValueError("Source map differs from the captured live release")
    images = {"gromacs": args.single_image, "gromacs-mpi": args.mpi_image}
    evidence = {model: runtime_receipt(images[model], directories, model_id=model)
                for model, directories in [("gromacs", args.single_result), ("gromacs-mpi", args.mpi_result)]}
    here = Path(__file__).parent
    candidates = {model: json.loads((here / file).read_text())["profile"] for model, file in
                  [("gromacs", "workload-profile.json"), ("gromacs-mpi", "mpi-workload-profile.json")]}
    recipe = source_recipe(ROOT)
    profiles, overlay, cm = replace_pair(values, (args.baseline / "scheduling.json").read_bytes(),
                                         candidates, images, evidence, digest(recipe))
    catalog_path = contracts / "scientific-workload-profiles.json"
    catalog = json.loads(catalog_path.read_text())
    catalog["profiles"] = [profiles.get(profile["model_id"], profile) for profile in catalog["profiles"]]
    final_map = overlay["scientificBatch"]["executionMap"]
    # Refuse to invalidate any other App's historical qualification.
    proofs = set(final_map["qualification_baselines"])
    final_digest = digest({"schema": final_map["schema"], "models": final_map["models"]})
    for profile in catalog["profiles"]:
        if profile["model_id"] not in profiles and profile.get("route_exposed"):
            if profile["qualification"]["execution_map_sha256"] not in proofs | {final_digest}:
                raise ValueError("Paired successor would invalidate another App's qualification")
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, value in [("activation.values.json", overlay), ("scheduling.configmap.json", cm),
                        ("runtime-receipts.json", evidence), ("source-recipe.json", recipe)]:
        (args.output / name).write_bytes(canonical(value) + b"\n")
    catalog_path.write_text(json.dumps(catalog, indent=2) + "\n")
    # Keep unrelated source rows (and their human-readable key order) intact.
    replacements = {row["model_id"]: row for row in final_map["models"] if row["model_id"] in profiles}
    for row in source_map["models"]:
        if row["model_id"] in replacements:
            replacement = replacements[row["model_id"]]
            row["execution_identity_sha256"] = replacement["execution_identity_sha256"]
            row["stages"][0]["image"] = replacement["stages"][0]["image"]
            if row != replacement:
                raise ValueError("Paired successor changed more than the declared image/recipe")
    source_map["qualification_baselines"] = final_map["qualification_baselines"]
    (contracts / "scientific-execution-map.json").write_text(json.dumps(source_map, indent=2) + "\n")
    print(json.dumps({"activation": str(args.output), "source_updated": True, "deployed": False,
                      "requalified_models": sorted(profiles)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--single-image", required=True)
    parser.add_argument("--mpi-image", required=True)
    parser.add_argument("--single-result", type=Path, action="append", required=True)
    parser.add_argument("--mpi-result", type=Path, action="append", required=True)
    parser.add_argument("--replace-mpi", action="store_true", help="Explicitly update an already active MPI App")
    args = parser.parse_args()
    os.umask(0o077)
    if args.replace_mpi:
        replace_existing_pair(args)
        return
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    prepare = str(Path(__file__).with_name("prepare.py"))
    single = args.output / "single"
    subprocess.run(
        [
            sys.executable,
            prepare,
            "--baseline",
            str(args.baseline),
            "--model",
            "gromacs",
            "--runtime-image",
            args.single_image,
            "--output",
            str(single),
            "--replace-existing",
            "--publish-catalog",
            *[
                item
                for directory in args.single_result
                for item in ("--gpu-result", str(directory))
            ],
        ],
        check=True,
    )
    baseline = copy.deepcopy(json.loads((args.baseline / "values.json").read_text()))
    overlay = json.loads((single / "activation.values.json").read_text())
    for key, value in overlay.items():
        baseline[key].update(value)
    composed = args.output / "composed-baseline"
    composed.mkdir(mode=0o700)
    (composed / "values.json").write_text(json.dumps(baseline))
    cm = json.loads((single / "scheduling.configmap.json").read_text())
    (composed / "scheduling.json").write_text(
        cm["data"][baseline["scientificBatch"]["schedulingContractKey"]]
    )
    subprocess.run(
        [
            sys.executable,
            prepare,
            "--baseline",
            str(composed),
            "--model",
            "gromacs-mpi",
            "--runtime-image",
            args.mpi_image,
            "--output",
            str(args.output / "combined"),
            "--publish-catalog",
            *(["--replace-existing"] if args.replace_mpi else []),
            *[
                item
                for directory in args.mpi_result
                for item in ("--gpu-result", str(directory))
            ],
        ],
        check=True,
    )
    print(
        json.dumps(
            {
                "activation": str(args.output / "combined"),
                "source_updated": True,
                "deployed": False,
            }
        )
    )


if __name__ == "__main__":
    main()
