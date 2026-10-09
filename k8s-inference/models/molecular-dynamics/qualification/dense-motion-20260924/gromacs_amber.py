#!/usr/bin/env python3
"""Prepare and validate bounded real dense-motion continuations, never render.

Use the existing released typed scientific-batch client for submission. Frozen
delivery files are read-only; every derived directory and receipt must be new.
"""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import sys
import tarfile
from datetime import datetime, timezone

MD = Path(__file__).resolve().parents[2]
IMAGES = {
    "gromacs": "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/gromacs@sha256:14ffdae0f0389c7771bae8791c56a5e21736ece630bd11dfb0f3b6a78f8cd643",
    "amber": "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/amber-worker@sha256:1ca8115d4b4f899a282e449e05c59c1e6a6897fbb3ab8f889965ec73677d11fc",
}
CLIENT = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/lc@sha256:81a2f3b54a98299933d487ccca4e9eb3fbea3130257a5a818a83940127429a4d"
GROMACS_CHANGES = {"nsteps": "10000", "init-step": "500000", "tinit": "0", "nstxout-compressed": "10", "nstenergy": "10", "nstlog": "10"}
AMBER_CHANGES = {"nstlim": "10000", "ntpr": "10", "ntwx": "10", "ntwv": "10"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def record(path):
    path = Path(path)
    return {"path": str(path.resolve()), "size_bytes": path.stat().st_size, "sha256": sha(path)}


def save(path, value):
    with Path(path).open("x") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def mdp_values(text):
    values = {}
    for line in text.splitlines():
        line = line.split(";", 1)[0].strip()
        if not line:
            continue
        key, sep, value = line.partition("=")
        require(sep and key.strip().lower() not in values, "invalid or duplicate MDP key")
        values[key.strip().lower()] = value.strip()
    return values


def derive_mdp(text):
    before = mdp_values(text)
    require(before.get("nsteps") == "500000" and before.get("dt") == "0.002", "unexpected frozen GROMACS duration/timestep")
    require(before.get("continuation") == "yes" and before.get("gen-vel") == "no", "frozen GROMACS must preserve velocities")
    require(before.get("init-step", "0") == "0" and before.get("tinit", "0") == "0", "unexpected frozen GROMACS origin")
    after = {**before, **GROMACS_CHANGES}
    return "; Dense native continuation: 10000 new steps, 20 fs sampling.\n" + "".join(f"{key} = {value}\n" for key, value in after.items())


def mdin_values(text):
    values = {}
    for key, value in re.findall(r"\b([A-Za-z][A-Za-z0-9_]*)\s*=\s*([^,\s/]+)", text):
        require(key.lower() not in values, "duplicate AMBER MDIN key")
        values[key.lower()] = value
    return values


def derive_mdin(text):
    before = mdin_values(text)
    require(before.get("nstlim") == "500000" and before.get("dt") == "0.002", "unexpected frozen AMBER duration/timestep")
    require(before.get("irest") == "1" and before.get("ntx") == "5", "AMBER must use native position/velocity restart")
    derived = text
    for key, value in AMBER_CHANGES.items():
        derived, count = re.subn(rf"\b{key}\s*=\s*[^,\s/]+", f"{key}={value}", derived)
        require(count == 1, "missing or duplicate AMBER changed field")
    derived = "Dense 20 ps continuation; same native Langevin LFMiddle SCR NPT\n" + derived.split("\n", 1)[1]
    require(mdin_values(derived) == {**before, **AMBER_CHANGES}, "unapproved AMBER parameter change")
    return derived


def request(engine):
    common = {"schema": f"fs2-serve.nebius.ai/{engine}-workflow-request/v1", "threads": 8 if engine == "gromacs" else 1, "max_wall_seconds": 900, "max_output_bytes": 1073741824, "output_destination": "customer-bucket", "output_prefix": f"runs/dense-motion-20260924/{engine}"}
    if engine == "gromacs":
        common.update(checkpoint_minutes=1, segment_minutes=10)
        steps = [
            {"id": "prepare-dense", "command": "grompp", "args": ["-f", "dense.mdp", "-c", "source-final.gro", "-p", "system.top", "-t", "source-final.cpt", "-o", "dense.tpr"], "expected_outputs": ["dense.tpr"]},
            {"id": "dense", "command": "mdrun", "args": ["-s", "dense.tpr", "-deffnm", "dense", "-notunepme"], "restart_checkpoint": "source-final.cpt", "expected_outputs": ["dense.gro"]},
            {"id": "energies", "command": "energy", "args": ["-f", {"files": "dense*.edr"}, "-o", "dense-energy.xvg"], "stdin": "Potential\nTotal-Energy\nTemperature\nPressure\nDensity\n0\n", "expected_outputs": ["dense-energy.xvg"]},
            {"id": "trajectory", "command": "trjcat", "args": ["-f", {"files": "dense.part*.xtc"}, "-o", "dense-motion.xtc"], "expected_outputs": ["dense-motion.xtc"]},
            {"id": "check", "command": "check", "args": ["-f", "dense-motion.xtc"]},
        ]
    else:
        common["backend"] = "cuda-spfp"
        steps = [{"id": "dense", "kind": "pmemd", "input": "dense.in", "topology": "system.prmtop", "coordinates": "source-final.rst7", "task": "dynamics", "expected_nsteps": 10000, "output_prefix": "dense", "expected_outputs": ["dense.nc", "dense.mdvel", "dense.rst7"]}]
    common["jobs"] = [{"id": "dense-" + engine, "steps": steps}]
    return common


def archive(directory, path):
    with path.open("xb") as output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as bundle:
                for item in sorted(directory.iterdir()):
                    data = item.read_bytes()
                    info = tarfile.TarInfo(item.name)
                    info.size, info.mode, info.mtime = len(data), 0o644, 0
                    bundle.addfile(info, io.BytesIO(data))


def prepare(engine, delivery, output):
    source = delivery / "runs" / engine
    old_result = json.loads((source / "result.json").read_text())
    require(old_result["status"] == "succeeded", "frozen source did not succeed")
    original_md = [row for row in old_result["commands"] if row["step_id"] == ("production" if engine == "gromacs" else "production-001")][-1]
    if engine == "gromacs":
        require(original_md["checkpoint_step"] == 500000, "source CPT is not the final canonical production step")
    else:
        completed = original_md["validation"]
        require(completed["completion"]["completed_native_steps"] == 500000 and abs(completed["restart"]["time_ps"] - 1200) < 1e-6 and completed["restart"]["velocities"], "source AMBER restart is not the final velocity-bearing canonical state")
    original_inventory = {row["path"]: row for row in old_result["files"]}
    names = {"gromacs": {"system.top": "system.top", "source-final.gro": "production.gro", "source-final.cpt": "fs2-production.cpt", "source-production.tpr": "production.tpr", "source-production.mdp": "production.mdp"}, "amber": {"system.prmtop": "system.prmtop", "source-final.rst7": "production-001.rst7", "source-production.in": "production-001.in"}}[engine]
    output.mkdir(parents=True, exist_ok=False)
    inputs = output / "inputs"
    inputs.mkdir()
    provenance = {"schema": "fs2-dense-native-continuation/v1", "engine": engine, "worker_image": IMAGES[engine], "client_image": CLIENT, "source_operation": old_result["operation_id"], "source_result": record(source / "result.json"), "source_files": {}, "changes": GROMACS_CHANGES if engine == "gromacs" else AMBER_CHANGES, "integration_steps": 10000, "dt_ps": .002, "frame_interval_steps": 10, "frame_interval_ps": .02, "nonzero_frames": 1000, "native_time_origin_ps": 1000 if engine == "gromacs" else 1200, "native_step_origin": 500000 if engine == "gromacs" else 0, "canonical_to_native": "identity", "scientific_input_interpolation": False, "restart_scope": "Native CPT restores full checkpoint state through grompp -t and mdrun -cpi. No GPU bitwise-reproducibility claim." if engine == "gromacs" else "Native NetCDF restart restores positions, current velocities, box and time with irest=1/ntx=5; new Langevin RNG stream initializes with preserved ig=20260925. No exact stochastic restart claim."}
    for target, name in names.items():
        path = source / "data" / name
        measured = record(path)
        require(measured["sha256"] == original_inventory[name]["sha256"] and measured["size_bytes"] == original_inventory[name]["size_bytes"], "frozen native inventory mismatch")
        shutil.copyfile(path, inputs / target)
        provenance["source_files"][target] = measured
    if engine == "gromacs":
        (inputs / "dense.mdp").write_text(derive_mdp((inputs / "source-production.mdp").read_text()))
    else:
        (inputs / "dense.in").write_text(derive_mdin((inputs / "source-production.in").read_text()))
    save(inputs / "continuation-provenance.json", provenance)
    archive(inputs, output / "input.tar.gz")
    save(output / "request.json", request(engine))
    save(output / "preparation.json", {**provenance, "input_bundle": record(output / "input.tar.gz"), "request": record(output / "request.json"), "prepared_input_files": [record(path) for path in sorted(inputs.iterdir())], "source_after_sha256": {target: sha(source / "data" / name) for target, name in names.items()}})
    return {"engine": engine, "fixture": str(output), "input_sha256": sha(output / "input.tar.gz"), "request_sha256": sha(output / "request.json")}


def preflight(key_file, output):
    import httpx
    token = json.loads(key_file.read_text())["secret"]
    with httpx.Client(base_url="https://89.169.99.188", headers={"authorization": "Bearer " + token}, verify=True, trust_env=False, timeout=30) as client:
        me = client.get("/v1/me")
        profiles = client.get("/v1/scientific-models")
        require(me.status_code == profiles.status_code == 200, "live normal-key discovery unavailable")
    identity, rows = me.json(), profiles.json()["data"]
    selected = [row for row in rows if row["model_id"] in IMAGES]
    require({row["model_id"] for row in selected} == set(IMAGES), "both engines must be currently authorized/submittable")
    for row in selected:
        require(row["runtime_image_digest"].endswith(IMAGES[row["model_id"]].split("@")[-1]), "live runtime is not the frozen supported image")
    save(output, {"at": datetime.now(timezone.utc).isoformat(), "status": "exact-runtime-normal-key-discovery-passed", "identity": identity, "profiles": selected, "client_image": CLIENT, "no_upload_or_submission": True})
    return {"status": "passed", "engines": [row["model_id"] for row in selected], "receipt": str(output)}


def frame_schedule(times, steps, engine):
    import numpy as np
    require(len(times) in (1000, 1001), "dense trajectory must contain 1000 nonzero frames plus optional initial")
    initial = len(times) == 1001
    ordinal = np.arange(0 if initial else 10, 10001, 10)
    origin_time, origin_step = (1000., 500000) if engine == "gromacs" else (1200., 0)
    require(np.isfinite(times).all() and np.allclose(times, origin_time + ordinal * .002, atol=1e-4, rtol=0), "dense native times are missing, duplicated or not 20 fs apart")
    if engine == "gromacs":
        require(steps == (origin_step + ordinal).tolist(), "dense native GROMACS steps differ")
    else:
        require(all(step is None for step in steps), "unexpected AMBER step encoding; do not relabel it")
    return initial


def netcdf_scalar(variable):
    # A native AMBER restart time is rank zero; [:] is invalid for that shape.
    # Ellipsis accepts the scalar without inventing an extra dimension.
    return float(variable[...].item())


def gromacs_log(text):
    require("-notunepme" in text and "-cpi source-final.cpt" in text and "Finished mdrun" in text, "native full-checkpoint continuation/completion missing")
    require(not re.search(r"LINCS WARNING|Fatal error:|timed with pme grid|PP/PME load balancing changed|optimal pme grid", text, re.I), "native failure or PME tuning")
    values = {}
    for key, value in re.findall(r"^\s*([A-Za-z][\w-]*)\s*=\s*(\S+)\s*$", text, re.M):
        values.setdefault(key.lower(), []).append(value)
    numbers = {"init-step": 500000, "nsteps": 10000, "dt": .002, "nstxout-compressed": 10, "nstenergy": 10, "nstlog": 10, "fourier-nx": 64, "fourier-ny": 64, "fourier-nz": 64, "pme-order": 4, "rlist": 1.15, "rcoulomb": 1., "rvdw": 1., "ewald-rtol": 1e-5, "ld-seed": 20260925, "lincs-order": 8, "lincs-iter": 2}
    for key, target in numbers.items():
        require(key in values and all(abs(float(value) - target) <= 1e-10 * max(1, abs(target)) for value in values[key]), "native GROMACS parameter mismatch: " + key)
    for key, target in {"integrator": "sd", "pcoupl": "c-rescale", "coulombtype": "pme", "coulomb-modifier": "none", "vdw-modifier": "none", "dispcorr": "enerpres", "constraint-algorithm": "lincs"}.items():
        require(key in values and all(value.lower() == target for value in values[key]), "native GROMACS physical setting mismatch: " + key)
    require(re.search(r"^\s*ref-t:\s+300\s*$", text, re.M) and re.search(r"^\s*tau-t:\s+1\s*$", text, re.M), "native thermostat target/friction mismatch")
    require("NVIDIA H100" in text and "1 GPU selected" in text, "actual H100 single-GPU log identity missing")
    return {"parameters": numbers, "pme_autotuning": False, "native_checkpoint_supplied": True}


def validate(engine, fixture, workspace, customer, delivery):
    import numpy as np
    import parmed
    from scipy.io import netcdf_file
    sys.path.insert(0, str(MD / "comparison" / "analysis"))
    from native import frames
    from geometry import minimum_image
    result = json.loads((workspace / "result.json").read_text())
    wanted = json.loads((fixture / "request.json").read_text())
    require((workspace / "request.json").read_bytes() == (fixture / "request.json").read_bytes(), "materialized request differs")
    require(result["status"] == "succeeded" and result["completed_steps"] == [s["id"] for s in wanted["jobs"][0]["steps"]], "native workflow incomplete")
    status = json.loads((customer / "status.json").read_text())
    client = json.loads((customer / "receipt.json").read_text())
    platform = json.loads((customer / "result.json").read_text())
    require(status["operation"]["status"] == "succeeded" and status["batch"]["result_published"] and client["state"] == "verified", "customer operation/artifact verification incomplete")
    require(platform["execution_identity"]["runtime_image_digest"].endswith(IMAGES[engine].split("@")[-1]), "actual execution runtime image mismatch")
    require(result["operation_id"] == status["operation"]["id"], "native/public operation mismatch")
    data = workspace / "data"
    for item in result["files"]:
        path = data / item["path"]
        require(path.is_file() and path.stat().st_size == item["size_bytes"] and sha(path) == item["sha256"], "native output inventory mismatch")
    for original in (fixture / "inputs").iterdir():
        require(sha(original) == sha(data / original.name), "uploaded native input was changed")
    provenance = json.loads((data / "continuation-provenance.json").read_text())
    for name, source in provenance["source_files"].items():
        require(sha(source["path"]) == source["sha256"] == sha(data / name), "frozen source/restart changed")
    topology = delivery / "master" / "system.prmtop"
    structure = parmed.load_file(str(topology))
    require(len(structure.atoms) == 6598, "canonical topology atom count mismatch")
    selected = [bond for bond in structure.bonds if bond.atom1.atomic_number == 1 or bond.atom2.atomic_number == 1]
    pairs = np.array([[bond.atom1.idx, bond.atom2.idx] for bond in selected])
    distances = np.array([bond.type.req for bond in selected])
    require(len(pairs) == 6588, "canonical hydrogen/water constraint inventory differs")
    trajectory = data / ("dense-motion.xtc" if engine == "gromacs" else "dense.nc")
    times, native_steps, max_error, first_frame, last_frame = [], [], 0., None, None
    for frame in frames(trajectory, engine, .002):
        require(frame.positions.shape == (6598, 3) and np.isfinite(frame.positions).all() and np.isfinite(frame.cell).all() and np.linalg.det(frame.cell) > 0, "nonfinite coordinates or invalid periodic box")
        delta = minimum_image(frame.positions[pairs[:, 0]] - frame.positions[pairs[:, 1]], frame.cell)
        max_error = max(max_error, float(np.max(np.abs(np.linalg.norm(delta, axis=1) - distances))))
        times.append(frame.time_ps)
        native_steps.append(frame.step)
        if first_frame is None:
            first_frame = frame
        last_frame = frame
    initial = frame_schedule(times, native_steps, engine)
    require(max_error <= 1e-4, "dense trajectory constraint geometry exceeds 1e-4 A")
    commands = [command for command in result["commands"] if command["step_id"] == "dense"]
    require(len(commands) == 1 and commands[0]["exit_code"] == 0, "short dense segment unexpectedly split or failed")
    native_proof = {}
    if engine == "gromacs":
        require(commands[0]["checkpoint_step"] == 510000, "final native GROMACS CPT step mismatch")
        logs = list(data.glob("dense.part*.log"))
        require(len(logs) == 1, "ambiguous dense native GROMACS log")
        native_proof = gromacs_log(logs[0].read_text())
        if initial:
            source_last = None
            for frame in frames(delivery / "runs/gromacs/data/canonical-production.xtc", engine, .002):
                source_last = frame
            shift = minimum_image(first_frame.positions - source_last.positions, source_last.cell)
            require(np.max(np.abs(shift)) < 1e-4 and np.max(np.abs(first_frame.cell - source_last.cell)) < 1e-4, "GROMACS initial frame differs from frozen final state")
            native_proof["initial_vs_frozen_final_max_periodic_component_A"] = float(np.max(np.abs(shift)))
    else:
        require(sha(data / "system.prmtop") == sha(topology), "AMBER topology differs from canonical master")
        proof = commands[0]["validation"]
        completed = proof["completion"]
        require(commands[0]["backend"] == "cuda-spfp" and completed["native_completion"] and completed["completed_native_steps"] == 10000 and completed["natom"] == 6598 and abs(completed["final_time_ps"] - 1220) < 1e-5, "native AMBER completion mismatch")
        require(proof["restart"]["velocities"] and abs(proof["restart"]["time_ps"] - 1220) < 1e-5 and proof["exact_stochastic_restart_claimed"] is False, "native AMBER final restart invalid")
        mdout = (data / "dense.mdout").read_text()
        require("H100" in mdout and "is that the reported pressure is always 0" not in mdout and "VIRIAL" in mdout, "AMBER GPU or genuine SCR virial evidence missing")
        values = []
        for step, native_time in re.findall(r"NSTEP\s*=\s*(\d+)\s+TIME\(PS\)\s*=\s*([\d.]+)", mdout.split("A V E R A G E S")[0]):
            values.append((int(step), float(native_time)))
        require(values == [(step, round(1200 + step * .002, 3)) for step in range(10, 10001, 10)], "AMBER thermo native step/cadence mismatch")
        with netcdf_file(data / "dense.mdvel", "r", mmap=False) as velocities:
            velocity = velocities.variables["velocities"][:].copy()
            velocity_times = velocities.variables["time"][:].copy()
            require(velocity.shape == (1000, 6598, 3) and np.isfinite(velocity).all() and np.allclose(velocity_times, times, atol=1e-4, rtol=0), "dense AMBER velocity frames missing/nonfinite/misaligned")
        with netcdf_file(data / "source-final.rst7", "r", mmap=False) as restart:
            restart_time = netcdf_scalar(restart.variables["time"])
            require(abs(restart_time - 1200) < 1e-6 and np.isfinite(restart.variables["velocities"][:]).all(), "source native AMBER restart time/velocities invalid")
        native_proof = {"worker_native_completion": proof, "source_exact_restart_time_ps": restart_time, "native_step_counter_restarts_at_zero": True, "thermo_samples": len(values), "dense_velocity_frames": len(velocity_times), "velocity_units": "native Amber NetCDF; scale_factor retained, not discarded"}
    return {
        "schema": "fs2-dense-native-validation/v1", "status": "passed",
        "scope": "actual native 20 ps continuation, 1000 nonzero frames; not new ensemble convergence or performance qualification",
        "engine": engine, "operation_id": result["operation_id"], "worker_image": IMAGES[engine], "client_image": CLIENT,
        "execution_identity": platform["execution_identity"], "attempts": platform["attempts"],
        "native_commands": result["commands"], "native_steps": 10000, "atoms": 6598,
        "raw_frames": len(times), "nonzero_frames": 1000, "optional_initial_frame": initial,
        "first_native_time_ps": times[0], "last_native_time_ps": times[-1],
        "native_time_origin_ps": 1000 if engine == "gromacs" else 1200,
        "native_step_origin": 500000 if engine == "gromacs" else 0,
        "native_step_storage": "observed integer XTC steps" if engine == "gromacs" else "NetCDF has native time only; relative steps derived from checked 2 fs protocol and native MDOUT steps",
        "dt_ps": .002, "frame_interval_ps": .02, "canonical_to_native": "identity",
        "maximum_constraint_distance_error_A": max_error, "constraint_count": len(pairs),
        "native_proof": native_proof, "restart_scope": provenance["restart_scope"],
        "raw_trajectory": record(trajectory),
        "source_checkpoint": record(data / ("source-final.cpt" if engine == "gromacs" else "source-final.rst7")),
        "renderer": {
            "engine": engine, "trajectory": str(trajectory.resolve()),
            "trajectory_format": "XTC" if engine == "gromacs" else "NCDF",
            "topology": str((data / ("system.top" if engine == "gromacs" else "system.prmtop")).resolve()),
            "canonical_topology": str(topology.resolve()),
            "origin_step": 500000 if engine == "gromacs" else 0,
            "origin_time_ps": 1000 if engine == "gromacs" else 1200,
            "timestep_ps": .002, "production_steps": 10000, "output_every": 10,
            "canonical_to_native": "identity", "sample_window_relative_ps": [.02, 20.0],
            "interpolation_or_averaging": False,
        },
        "artifact_integrity": "all customer artifacts and native inventory verified",
        "native_files": [record(data / row["path"]) for row in result["files"]],
        "customer_result": record(customer / "result.json"), "customer_receipt": record(customer / "receipt.json"),
        "customer_status": record(customer / "status.json"), "source_helper": record(__file__),
        "scientific_convergence_claimed": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--engine", choices=IMAGES, required=True)
    prep.add_argument("--delivery", type=Path, required=True)
    prep.add_argument("--output", type=Path, required=True)
    live = sub.add_parser("preflight")
    live.add_argument("--key-file", type=Path, required=True)
    live.add_argument("--output", type=Path, required=True)
    audit = sub.add_parser("validate")
    audit.add_argument("--engine", choices=IMAGES, required=True)
    for name in ("fixture", "workspace", "customer", "delivery", "output"):
        audit.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(args.engine, args.delivery.resolve(), args.output.resolve())
    elif args.command == "preflight":
        result = preflight(args.key_file, args.output)
    else:
        require(not args.output.exists(), "validation output must be new")
        try:
            result = validate(args.engine, args.fixture, args.workspace, args.customer, args.delivery)
        except Exception as error:
            save(args.output, {"status": "failed", "engine": args.engine, "error_type": type(error).__name__, "error": str(error)})
            raise
        save(args.output, result)
        result = {"status": result["status"], "engine": args.engine, "receipt": record(args.output), "renderer": result["renderer"]}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
