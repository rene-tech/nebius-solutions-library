"""Offline canonical GROMACS audit: immutable inputs and untuned PME throughout.

Run after verified customer downloads have been materialized. XTC validation
uses the read-only low-level reader, so no offset cache alters the evidence.
This bounded 6,598-atom protocol validator is not a general convergence test.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import shlex
import statistics
import tarfile

from fs2_gromacs import NVIDIA_IMAGE, RESULT_SCHEMA
from fs2_gromacs.contracts import canonical, normalize, relative_path
from fs2_gromacs.files import digest_file, inventory


STAGES = {"minimize": 5000, "nvt": 50000, "npt": 50000, "production": 500000}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def fixed_command(command):
    require(command.count("-notunepme") == 1 and "-tunepme" not in command,
            "every requested and executed mdrun must explicitly disable PME tuning")


def native_parameters(text, stage, seed=None):
    """Reject temporary tuning even if the final grid returns to the original."""
    require(not re.search(r"timed with pme grid|PP/PME load balancing changed|optimal pme grid", text, re.I),
            "native log contains PME autotuning activity")
    lines = text.splitlines()
    headers = [i for i, line in enumerate(lines) if line.strip() == "Command line:"]
    require(len(headers) == 1, "missing or ambiguous native command line")
    fixed_command(shlex.split(lines[headers[0] + 1]))
    values = {}
    for key, value in re.findall(r"^\s*([A-Za-z][\w-]*)\s*=\s*(\S+)\s*$", text, re.M):
        values.setdefault(key.lower(), []).append(value)
    expected = {"fourier-nx": 64, "fourier-ny": 64, "fourier-nz": 64,
                "pme-order": 4, "rcoulomb": 1., "rvdw": 1., "ewald-rtol": 1e-5,
                "nsteps": STAGES[stage], "nstlist": 10, "rlist": 1.15}
    if stage != "minimize":
        expected.update({"dt": .002, "nstxout-compressed": 500, "nstenergy": 500,
                         "lincs-order": 8, "lincs-iter": 2, "ld-seed": seed})
    for key, target in expected.items():
        require(key in values and all(math.isclose(float(v), target, rel_tol=1e-9, abs_tol=1e-12)
                                      for v in values[key]), "native parameter differs: " + key)
    strings = {"coulombtype": "pme", "coulomb-modifier": "none", "vdw-modifier": "none",
               "dispcorr": "enerpres", "integrator": "steep" if stage == "minimize" else "sd"}
    if stage != "minimize":
        strings.update({"constraint-algorithm": "lincs", "pcoupl": "no" if stage == "nvt" else "c-rescale"})
        require(re.search(r"^\s*ref-t:\s+300\s*$", text, re.M) and
                re.search(r"^\s*tau-t:\s+1\s*$", text, re.M), "native Langevin temperature/friction differs")
        require("PME tasks will do all aspects on the GPU" in text, "missing actual GPU PME dispatch")
    for key, target in strings.items():
        require(key in values and all(v.lower() == target for v in values[key]), "native parameter differs: " + key)
    require("Finished mdrun" in text, "native run is incomplete")
    require("LINCS WARNING" not in text and "Fatal error:" not in text, "native numerical failure")
    performance = re.findall(r"^Performance:\s+(\S+)", text, re.M)
    return {"stage": stage, "requested_steps": STAGES[stage], "pme_grid": [64, 64, 64],
            "pme_order": 4, "coulomb_cutoff_nm": 1., "vdw_cutoff_nm": 1.,
            "autotuning_disabled": True, "autotuning_events": 0,
            "native_performance_ns_per_day": float(performance[-1]) if performance else None,
            "gpu_pme": stage != "minimize",
            "update_and_constraints": "CPU" if stage != "minimize" else "not dynamics"}


def energy_series(path, duration, *, density=True):
    text = path.read_text()
    labels = re.findall(r'^@\s+s\d+ legend "([^"]+)"', text, re.M)
    expected = ["Potential", "Total Energy", "Temperature", "Pressure"] + (["Density"] if density else [])
    require(labels == expected,
            "unexpected native energy columns")
    rows = [[float(v) for v in line.split()] for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith(("#", "@", "&"))]
    require(len(rows) == duration + 1 and all(len(row) == len(expected) + 1 and all(map(math.isfinite, row)) for row in rows),
            "non-finite or incomplete native energy series")
    require(all(row[0] == i for i, row in enumerate(rows)), "native energy cadence is not exactly 1 ps")
    # The initial boundary frame is retained, but excluded from production means
    # to use the same 1..1000 ps window as the other canonical engines.
    sampled = rows[1:]
    require(all(row[3] > 0 and (not density or row[5] > 0) for row in sampled), "nonphysical temperature or density")
    return {"rows": len(rows), "mean_window_ps": [1, duration],
            "temperature_mean_K": statistics.mean(row[3] for row in sampled),
            "pressure_mean_bar": statistics.mean(row[4] for row in sampled),
            "pressure_sample_sd_bar": statistics.stdev(row[4] for row in sampled),
            "density_mean_g_cm3": statistics.mean(row[5] for row in sampled) / 1000 if density else None,
            "density_unavailable_reason": None if density else "native fixed-volume NVT energy file omits density",
            "potential_mean_kcal_mol": statistics.mean(row[1] for row in sampled) / 4.184}


def trajectory(path, steps):
    import numpy as np
    from MDAnalysis.lib.formats.libmdaxdr import XTCFile

    count = 0
    with XTCFile(str(path), "r") as reader:
        for i, frame in enumerate(reader):
            require(frame.x.shape == (6598, 3) and np.isfinite(frame.x).all(), "invalid canonical atom coordinates")
            require(frame.box.shape == (3, 3) and np.isfinite(frame.box).all() and np.linalg.det(frame.box) > 0,
                    "invalid periodic cell")
            require(frame.step == i * 500 and math.isclose(frame.time, i, abs_tol=1e-5),
                    "native trajectory step/time cadence differs")
            count += 1
    require(count == steps // 500 + 1, "native trajectory is truncated or has extra frames")
    return {"file": path.name, "frames": count, "atoms": 6598, "first_time_ps": 0,
            "last_time_ps": steps * .002, "includes_initial_frame": True,
            "finite_coordinates_and_positive_cells": True}


def audit(fixture, workspace, receipt):
    request_path, result_path = workspace / "request.json", workspace / "result.json"
    require(request_path.read_bytes() == (fixture / "request.json").read_bytes(), "frozen request bytes differ")
    request = normalize(json.loads(request_path.read_text()))
    result = json.loads(result_path.read_text())
    client = json.loads((receipt / "receipt.json").read_text())
    status = json.loads((receipt / "status.json").read_text())
    op = status["operation"]
    require(client["state"] == "verified" and op["status"] == "succeeded" and status["batch"]["result_published"],
            "customer downloads are not successfully verified")
    require(client["operation_id"] == result["operation_id"] == op["id"], "operation identity differs")
    attempts = [a for s in status["batch"]["stages"] for a in s["attempts"]]
    require(attempts and all(a["resource_released"] for a in attempts), "compute resources remain allocated")
    require((result["schema"], result["status"], result["engine_id"]) == (RESULT_SCHEMA, "succeeded", NVIDIA_IMAGE),
            "native engine/schema/status differs")
    jobs = [j for j in request["jobs"] if j["id"] == result["job_id"]]
    require(len(jobs) == 1, "unknown native job")
    steps = jobs[0]["steps"]
    require(result["completed_steps"] == [s["id"] for s in steps], "native stages incomplete or reordered")
    recipe = hashlib.sha256(canonical({"request": request, "job": result["job_id"], "image": NVIDIA_IMAGE})).hexdigest()
    require(result["recipe_sha256"] == recipe, "native recipe differs")
    require([c["step_id"] for c in result["commands"]] == result["completed_steps"] and
            all(c["exit_code"] == 0 for c in result["commands"]), "native command coverage or exits differ")
    data = workspace / "data"
    require(inventory(data, max_bytes=request["max_output_bytes"]) == result["files"], "native inventory differs")
    bundle_sha = digest_file(fixture / "input.tar.gz")
    require(client["identity"]["source_sha256"] == bundle_sha, "uploaded archive differs")
    listed = {f["path"]: f for f in result["files"]}
    inputs = []
    with tarfile.open(fixture / "input.tar.gz", "r:gz") as archive:
        seen = set()
        for member in archive:
            if member.isdir():
                continue
            name = relative_path(member.name)
            require(member.isfile() and name not in seen and name in listed, "ambiguous/nonregular/missing input")
            seen.add(name)
            with archive.extractfile(member) as stream:
                sha = hashlib.file_digest(stream, "sha256").hexdigest()
            require((member.size, sha) == (listed[name]["size_bytes"], listed[name]["sha256"]),
                    "immutable input differs: " + name)
            inputs.append({"path": name, "sha256": sha, "size_bytes": member.size})
    require(len(inputs) == 9, "unexpected canonical input members")
    protocol = json.loads((data / "protocol.json").read_text())
    native = []
    for stage, count in STAGES.items():
        requested = [s for s in steps if s["id"] == stage]
        commands = [c for c in result["commands"] if c["step_id"] == stage]
        require(len(requested) == len(commands) == 1, "ambiguous canonical stage")
        fixed_command(requested[0]["args"])
        fixed_command(commands[0]["command"])
        log = data / f"{stage}.part0001.log"
        record = native_parameters(log.read_text(), stage, protocol.get(stage + "_seed"))
        record.update({"log_sha256": digest_file(log), "process_wall_seconds": commands[0]["wall_seconds"]})
        if stage != "minimize":
            require(commands[0]["checkpoint_step"] == count, "native checkpoint step differs")
            record["trajectory"] = trajectory(data / f"{stage}.part0001.xtc", count)
            record["thermodynamics"] = energy_series(data / f"{stage}-energy.xvg", int(count * .002), density=stage != "nvt")
        native.append(record)
    merged = trajectory(data / "canonical-production.xtc", STAGES["production"])
    return {"status": "passed", "model_id": "gromacs", "operation_id": op["id"], "job_id": result["job_id"],
            "engine_id": NVIDIA_IMAGE, "input_sha256": bundle_sha, "request_sha256": digest_file(request_path),
            "result_sha256": digest_file(result_path), "recipe_sha256": recipe, "immutable_input_files": inputs,
            "completed_steps": result["completed_steps"], "native_commands": len(result["commands"]),
            "output_files": len(listed), "output_bytes": sum(f["size_bytes"] for f in listed.values()),
            "resources_released": True, "stages": native, "merged_production": merged,
            "convergence_claimed": False, "gpu_snapshot_used": result["gpu_snapshot_used"],
            "customer_manifest_sha256": digest_file(receipt / "output-manifest.json"),
            "customer_receipt_sha256": digest_file(receipt / "receipt.json"),
            "customer_status_sha256": digest_file(receipt / "status.json")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("fixture", "workspace", "receipt", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists(), "preserve prior evidence; choose a new output path")
    record = audit(args.fixture, args.workspace, args.receipt)
    args.output.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({"status": record["status"], "operation_id": record["operation_id"],
                      "native_stages": len(record["stages"]), "output_files": record["output_files"]}))
