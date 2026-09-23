"""Recheck typed ligand/GB-PB workflow artifacts and per-frame arithmetic."""

import argparse
import csv
import hashlib
import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path

from fs2_amber import ENGINE_ID
from fs2_amber.advanced import validate_advanced
from fs2_amber.contracts import canonical, normalize
from fs2_gromacs.files import inventory


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024**2), b""):
            digest.update(block)
    return digest.hexdigest()


def binding_arithmetic(path):
    tables, model, component, columns = {}, None, None, None
    for row in csv.reader(path.open(newline="")):
        if not row:
            continue
        title = row[0].strip()
        if title in {"GENERALIZED BORN:", "POISSON BOLTZMANN:"}:
            model = "GB" if title == "GENERALIZED BORN:" else "PB"
        elif title.endswith(" Energy Terms"):
            component = title.removesuffix(" Energy Terms").upper()
            tables[(model, component)] = []
        elif title == "Frame #":
            columns = [name.removeprefix("DELTA ") for name in row[1:]]
        elif title.isdigit():
            values = [float(value) for value in row[1:]]
            if len(values) != len(columns) or not all(math.isfinite(value) for value in values):
                raise ValueError("missing or nonfinite native frame energy")
            tables[(model, component)].append(dict(zip(columns, values)))
    if set(tables) != {(model, component) for model in ("GB", "PB") for component in ("COMPLEX", "RECEPTOR", "LIGAND", "DELTA")} or any(len(rows) != 5 for rows in tables.values()):
        raise ValueError("both GB/PB must contain all four component tables and exactly five frames")
    summaries = []
    for model in ("GB", "PB"):
        maximum = 0.0
        for frame in range(5):
            complex_energy, receptor, ligand, delta = [tables[(model, component)][frame] for component in ("COMPLEX", "RECEPTOR", "LIGAND", "DELTA")]
            if not (complex_energy.keys() == receptor.keys() == ligand.keys() == delta.keys()):
                raise ValueError("component energy columns disagree")
            for key in complex_energy:
                maximum = max(maximum, abs(complex_energy[key] - receptor[key] - ligand[key] - delta[key]))
            for item in (complex_energy, receptor, ligand, delta):
                maximum = max(maximum, abs(item["TOTAL"] - item["G gas"] - item["G solv"]))
        if maximum > 1e-8:
            raise ValueError("binding component subtraction/total arithmetic differs by more than1e-8kcal/mol")
        summaries.append({"model": model, "frames": 5, "delta_total_mean_kcal_mol": statistics.mean(row["TOTAL"] for row in tables[(model, "DELTA")]), "max_arithmetic_residual_kcal_mol": maximum})
    return summaries


def validate(root):
    request = normalize(json.loads((root / "request.json").read_text()))
    result = json.loads((root / "result.json").read_text())
    manifest = json.loads((root / "fixture-manifest.json").read_text())
    job = request["jobs"][0]
    if result["status"] != "succeeded" or result["completed_steps"] != [step["id"] for step in job["steps"]]:
        raise ValueError("all requested typed stages must succeed")
    if result["engine_id"] != ENGINE_ID or result["recipe_sha256"] != hashlib.sha256(canonical({"request": request, "job": job["id"], "image": ENGINE_ID})).hexdigest():
        raise ValueError("native recipe or engine identity mismatch")
    if inventory(root / "data", max_bytes=request["max_output_bytes"]) != result["files"]:
        raise ValueError("native result inventory no longer matches outputs")
    if sha(root / "input.tar.gz") != manifest["input_sha256"] or sha(root / "request.json") != manifest["request_sha256"]:
        raise ValueError("frozen fixture changed")
    for item in manifest["files"]:
        if sha(root / "data" / item["path"]) != item["sha256"]:
            raise ValueError("native tool changed an immutable source input")
    scientific_outputs = []
    for step in job["steps"]:
        if step["kind"] in {"antechamber", "parmchk2", "mmpbsa"}:
            scientific_outputs.append({"kind": step["kind"], **validate_advanced(root / "data" / step["directory"], step)})
    if scientific_outputs[0]["atoms"] != 12 or scientific_outputs[0]["bonds"] != 12 or abs(scientific_outputs[0]["actual_charge_e"]) > 1e-10:
        raise ValueError("synthetic benzene ligand identity/charge differs")
    return {"status": "passed", "scientific_outputs": scientific_outputs, "binding_arithmetic": binding_arithmetic(root / "data/ras-raf/binding.csv"), "all_immutable_inputs_preserved": True, "entropy_included": False, "free_energy_convergence_claimed": False, "scientific_parameter_suitability_claimed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--fixed-coordinate-control", type=Path, required=True)
    parser.add_argument("--preserved-failure", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or "@sha256:" not in args.runtime_image:
        raise ValueError("new receipt path and exact immutable image are required")
    result = validate(args.workspace)
    accuracy = json.loads(args.fixed_coordinate_control.read_text())
    if accuracy["status"] != "passed":
        raise ValueError("fixed-coordinate numerical GB accuracy control did not pass")
    receipt = {"schema": "fs2-serve.nebius.ai/amber-tools-qualification/v1", "recorded_at": datetime.now(timezone.utc).isoformat(), "model_id": "amber", "status": "passed", "runtime_image": args.runtime_image, "engine_id": ENGINE_ID, "execution": "CPU-only exact private worker Docker command,network disabled,2CPU/6GiB", "input_sha256": sha(args.workspace / "input.tar.gz"), "request_sha256": sha(args.workspace / "request.json"), "result_sha256": sha(args.workspace / "result.json"), "validation": result, "fixed_coordinate_accuracy": {"path": str(args.fixed_coordinate_control), "sha256": sha(args.fixed_coordinate_control), "status": accuracy["status"], "terms": len(accuracy["tests"])}, "preserved_failures": [{"path": str(path), "sha256": sha(path)} for path in args.preserved_failure], "raw_evidence": str(args.workspace), "customer_ready": False, "customer_path_tested": False, "pb_reference_accuracy_not_inferred_from_gb": True}
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
