#!/usr/bin/env python3
"""Repackage validated windows as ordinary independent native workflow batches.

Local file operations only: no submission, credentials, native simulation or
cloud access. Science files are copied byte-for-byte; only input path routing
and native postprocessing of closed energy parts change.
"""
from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile

MD = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(MD / "gromacs/runtime"))
from fs2_gromacs import contracts
from fs2_gromacs.files import extract_inputs, inventory

GROUPS = (tuple(range(1, 9)), tuple(range(9, 17)), tuple(range(17, 24)))
STAGES = ("minimize", "nvt", "npt", "production")
REQUIRED_FILES = {"system.top", "start.gro", "dihedrals.ndx", "protocol.json", *(s + ".mdp" for s in STAGES)}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def record(path, relative_to=None):
    path = Path(path)
    return {"path": str(path.relative_to(relative_to)) if relative_to else str(path),
            "size_bytes": path.stat().st_size, "sha256": sha(path)}


def check_record(path, expected):
    require(not path.is_symlink() and path.is_file(), f"not a regular source file: {path}")
    require(path.stat().st_size == expected["size_bytes"] and sha(path) == expected["sha256"],
            f"source hash/size mismatch: {path}")


def archive(inputs, destination):
    """Byte-reproducible tar/gzip independent of source mtimes and owner."""
    with Path(destination).open("xb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w", format=tarfile.USTAR_FORMAT) as tar:
                for path in sorted(Path(inputs).rglob("*")):
                    require(not path.is_symlink(), "input symlinks are forbidden")
                    if path.is_dir():
                        continue
                    require(path.is_file(), "input must be regular")
                    name = path.relative_to(inputs).as_posix()
                    contracts.relative_path(name)
                    info = tarfile.TarInfo(name)
                    info.size = path.stat().st_size
                    info.mode = 0o644
                    with path.open("rb") as stream:
                        tar.addfile(info, stream)


def verify_archive(path, expected):
    """Exercise the actual runtime's safe extractor and inventory algorithms."""
    with tempfile.TemporaryDirectory(prefix="fs2-umbrella-bundle-") as directory:
        target = Path(directory) / "data"
        extract_inputs(path, target, max_bytes=sum(item["size_bytes"] for item in expected))
        actual = inventory(target, max_bytes=sum(item["size_bytes"] for item in expected))
    key = lambda row: row["path"]
    require(sorted(actual, key=key) == sorted(expected, key=key), "bundle differs from science input inventory")


def load_window(fixtures, index):
    name = f"window-{index:02d}"
    folder = fixtures / name
    require(not folder.is_symlink(), "fixture directory must not be a link")
    preparation = read(folder / "preparation.json")
    require(preparation["window_id"] == name, "preparation window identity differs")
    check_record(folder / "request.json", preparation["request"])
    check_record(folder / "input.tar.gz", preparation["bundle"])
    original = read(folder / "request.json")
    contracts.normalize(original)
    require([j["id"] for j in original["jobs"]] == [name], "source must contain exactly its one named window")
    source_input_root = Path(preparation["bundle"]["path"]).parent / "inputs"
    expected = []
    for entry in preparation["files"]:
        relative = Path(entry["path"]).relative_to(source_input_root).as_posix()
        contracts.relative_path(relative)
        path = folder / "inputs" / relative
        check_record(path, entry)
        expected.append({**entry, "path": relative})
    require(len({row["path"] for row in expected}) == len(expected), "duplicate science inventory entry")
    require({row["path"] for row in expected} == REQUIRED_FILES, "unexpected or missing original input files")
    actual = inventory(folder / "inputs", max_bytes=original["max_output_bytes"])
    require(actual == sorted(expected, key=lambda row: row["path"]), "source input inventory differs")
    verify_archive(folder / "input.tar.gz", expected)
    protocol = read(folder / "inputs/protocol.json")
    require(protocol["window_id"] == name, "protocol window identity differs")
    return {"name": name, "folder": folder, "request": original, "inputs": expected,
            "center_degrees": preparation["center_degrees"]}


def value_index(args, flag):
    require(args.count(flag) == 1, f"expected one explicit {flag}")
    index = args.index(flag) + 1
    require(index < len(args), f"missing value for {flag}")
    return index


def transform_job(job):
    """Strict allowlist of changes; retain native commands/selections otherwise."""
    result = copy.deepcopy(job)
    name, steps = job["id"], []
    require(name in {f"window-{i:02d}" for i in range(1, 24)}, "batch window is outside 01..23")
    expected_ids = ["prepare-minimize", "minimize", "prepare-nvt", "nvt", "energies-nvt",
                    "prepare-npt", "npt", "energies-npt", "prepare-production", "production",
                    "energies-production", "trajectory"]
    require([step["id"] for step in job["steps"]] == expected_ids, "unexpected source stage sequence")
    for original in job["steps"]:
        step = copy.deepcopy(original)
        require(step.get("directory", ".") == ".", "source steps must use isolated job cwd")
        if step["id"].startswith("prepare-"):
            require(step["command"] == "grompp", "preparation is not grompp")
            stage = step["id"].removeprefix("prepare-")
            args = step["args"]
            for flag, value in (("-f", stage + ".mdp"), ("-p", "system.top"), ("-n", "dihedrals.ndx")):
                index = value_index(args, flag)
                require(args[index] == value, f"unexpected original {stage} {flag}")
                args[index] = name + "/" + value
            index = value_index(args, "-c")
            previous = "start" if stage == "minimize" else STAGES[STAGES.index(stage) - 1]
            require(args[index] == previous + ".gro", "unexpected stage coordinate dependency")
            if stage == "minimize":
                args[index] = name + "/start.gro"
        elif step["id"].startswith("energies-"):
            require(step["command"] == "energy", "energy extraction command differs")
            stage = step["id"].removeprefix("energies-")
            index = value_index(step["args"], "-f")
            require(step["args"][index] == {"files": stage + "*.edr"}, "unexpected original energy selection")
            filename = stage + "-canonical.edr"
            steps.append({"id": "merge-energies-" + stage, "command": "eneconv",
                          "args": ["-f", {"files": stage + ".part*.edr"}, "-o", filename],
                          "expected_outputs": [filename]})
            step["args"][index] = filename
        steps.append(step)
    result["steps"] = steps
    return result


def validate_coverage(groups):
    flat = [index for group in groups for index in group]
    require([len(group) for group in groups] == [8, 8, 7], "expected 8/8/7 independent jobs")
    require(flat == list(range(1, 24)), "window coverage must be exactly 01..23 without duplicates")


def build(fixtures, output):
    fixtures, output = Path(fixtures).resolve(), Path(output).resolve()
    require(not output.exists(), "output must be a new directory; preserve previous variants")
    require(not output.is_relative_to(fixtures), "output may not be inside original fixtures")
    validate_coverage(GROUPS)
    windows = {index: load_window(fixtures, index) for group in GROUPS for index in group}
    common = {k: v for k, v in windows[1]["request"].items() if k != "jobs"}
    require(all({k: v for k, v in w["request"].items() if k != "jobs"} == common for w in windows.values()),
            "source workflow budgets/settings differ; refusing to silently choose one")
    output.mkdir(parents=True)
    receipt = {"schema": "fs2-alanine-umbrella-batches/v1", "status": "validated-not-submitted",
               "source_fixtures": str(fixtures), "source_script_sha256": sha(__file__),
               "runtime_contract_sha256": sha(contracts.__file__), "native_runs": 0, "cloud_writes": 0,
               "groups": [], "changes": ["grompp immutable input path prefixes only",
                   "eneconv explicit stage.part*.edr followed by single canonical energy input"],
               "limitations": ["No native execution or scheduler capacity claim follows from local validation.",
                   "eneconv duplicate-time frames use the later file; original parts remain retained.",
                   "Compute statistics from per-time XVG values; merged EDR cumulative sigma/E^2 metadata is not reliable.",
                   "Actual result/manifest size depends on native files and segment count; only input lower bounds are reported."]}
    for number, group in enumerate(GROUPS, 1):
        folder = output / f"batch-{number:02d}"
        inputs = folder / "inputs"
        inputs.mkdir(parents=True)
        request = {**copy.deepcopy(common), "jobs": []}
        sources, parity = [], []
        for index in group:
            window = windows[index]
            name, original = window["name"], window["folder"]
            provenance = folder / "original" / name
            provenance.mkdir(parents=True)
            for filename in ("request.json", "preparation.json", "input.tar.gz"):
                shutil.copyfile(original / filename, provenance / filename)
            for entry in window["inputs"]:
                path = inputs / name / entry["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original / "inputs" / entry["path"], path)
                check_record(path, entry)
                parity.append({"window": name, "source_path": entry["path"], "batch_path": name + "/" + entry["path"],
                               "sha256": entry["sha256"], "size_bytes": entry["size_bytes"], "byte_identical": True})
            job = transform_job(window["request"]["jobs"][0])
            request["jobs"].append(job)
            variant = {**copy.deepcopy(common), "jobs": [job]}
            contracts.normalize(variant)
            save(provenance / "request.transformed.json", variant)
            sources.append({"window": name, "center_degrees": window["center_degrees"],
                            "original_request": record(provenance / "request.json", output),
                            "original_bundle": record(provenance / "input.tar.gz", output),
                            "preparation": record(provenance / "preparation.json", output),
                            "transformed_request": record(provenance / "request.transformed.json", output)})
        normalized = contracts.normalize(request)
        save(folder / "request.json", request)
        save(folder / "request.normalized.json", normalized)
        expected = inventory(inputs, max_bytes=request["max_output_bytes"])
        archive(inputs, folder / "input.tar.gz")
        verify_archive(folder / "input.tar.gz", expected)
        count, size = len(expected), sum(row["size_bytes"] for row in expected)
        # A common immutable bundle is extracted separately for every job.
        # Every copied input is consequently also an output-inventory row.
        estimate = {"jobs": len(group), "input_files_per_job": count, "input_bytes_per_job": size,
                    "input_inventory_rows_across_jobs": count * len(group),
                    "replicated_input_bytes_across_jobs": size * len(group),
                    "input_inventory_json_bytes_per_job": len(contracts.canonical(expected)),
                    "actual_output_manifest_bytes": None, "native_output_bytes": None,
                    "not_a_complete_output_size_estimate": True}
        entry = {"id": folder.name, "windows": [w["window"] for w in sources], "sources": sources,
                 "science_file_parity": parity, "normalized_request_sha256": hashlib.sha256(contracts.canonical(normalized)).hexdigest(),
                 "request": record(folder / "request.json", output), "input": record(folder / "input.tar.gz", output),
                 "normalized_request": record(folder / "request.normalized.json", output),
                 "schema_validation": "passed", "runtime_archive_extraction": "passed", "transport_estimates": estimate}
        save(folder / "manifest.json", entry)
        receipt["groups"].append(entry)
    save(output / "receipt.json", receipt)
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build(args.fixtures, args.output)
    print(json.dumps({"status": result["status"], "output": str(args.output),
                      "batches": [{"id": r["id"], "windows": r["windows"],
                                   "request": r["request"], "input": r["input"],
                                   "transport_estimates": r["transport_estimates"]} for r in result["groups"]]}))
