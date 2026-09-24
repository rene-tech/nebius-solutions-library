"""Bind a complete, downloaded native umbrella cohort to WHAM input.

This is an additive manifest, not a scientific convergence verdict. Native data
are never edited. Missing, duplicated or segmented windows require inspection
rather than silently dropping observations.
"""
import argparse
import hashlib
import json
from pathlib import Path


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def assemble(native, delivery):
    records = {}
    for path in sorted(native.glob("*/window-??/result.json")):
        value = json.loads(path.read_text())
        name = path.parent.name
        if value["job_id"] != name or value["status"] != "succeeded" or name in records:
            raise ValueError("Failed, duplicated or misbound native window")
        records[name] = (path, value)
    expected = {f"window-{i:02d}" for i in range(24)}
    if set(records) != expected:
        raise ValueError("Require all24 native windows exactly once; missing=" + str(sorted(expected - set(records))))
    result = {"temperature_k": 300, "evidence_kind": "real-native-md", "windows": [],
              "unbiased": {"frames_csv": str(delivery / "analysis/gromacs/frames.csv"),
                           "frames_csv_sha256": digest(delivery / "analysis/gromacs/frames.csv")}}
    topology_digest = digest(delivery / "runs/gromacs/data/system.top")
    for index in range(24):
        name = f"window-{index:02d}"
        path, value = records[name]
        data = path.parent / "data"
        for entry in value["files"]:
            file = (data / entry["path"]).resolve()
            if not file.is_relative_to(data.resolve()) or not file.is_file():
                raise ValueError("Native inventory escapes data or references missing file")
            if file.stat().st_size != entry["size_bytes"] or digest(file) != entry["sha256"]:
                raise ValueError("Native bytes differ from the verified result inventory")
        production = [c for c in value["commands"] if c["step_id"] == "production"]
        if not production or any(c["exit_code"] for c in production) or production[-1]["checkpoint_step"] != 1000000:
            raise ValueError("Missing complete successful1,000,000step production")
        original_top = data / "system.top" if index == 0 else data / name / "system.top"
        if digest(original_top) != topology_digest:
            raise ValueError("Umbrella topology differs from original exact GROMACS reference")
        pulls = sorted(data.glob("production.part*_pullx.xvg"))
        if len(pulls) != 1:
            raise ValueError("Inspect native segmented pullx boundaries before constructing an explicit combined input")
        tpr, pullx = data / "production.tpr", pulls[0]
        result["windows"].append({
            "id": name, "center_degrees": -180 + 15 * index,
            "force_constant_kj_mol_rad2": 200, "status": "succeeded",
            "operation_id": value["operation_id"],
            "native_result": str(path.relative_to(native)), "native_result_sha256": digest(path),
            "tpr": str(tpr.relative_to(native)), "tpr_sha256": digest(tpr),
            "pullx": str(pullx.relative_to(native)), "pullx_sha256": digest(pullx),
            "production_start_ps": 0, "production_end_ps": 2000, "expected_dt_ps": .1,
            "native_production_wall_seconds": sum(c["wall_seconds"] for c in production),
            "reported_ns_per_day": [c["performance_ns_per_day"] for c in production],
        })
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--delivery", type=Path, required=True)
    args = parser.parse_args()
    native, delivery = args.native.resolve(), args.delivery.resolve()
    target = native / "windows.json"
    if target.exists():
        raise ValueError("Preserve previous manifests")
    result = assemble(native, delivery)
    with target.open("x") as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({"manifest": str(target), "windows": len(result["windows"]), "sha256": digest(target)}))


if __name__ == "__main__":
    main()
