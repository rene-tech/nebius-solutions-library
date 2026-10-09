"""Bounded, source-bound native 20 ps NAMD/LAMMPS continuation qualification.

prepare never changes the frozen delivery. discover is read-only. submit uses
the exact released customer CLI and its durable idempotent receipt. No runtime,
deployment, resource policy, credential or original trajectory is modified.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
from datetime import datetime, timezone
import gzip
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import tarfile

CLIENT = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/lc@sha256:81a2f3b54a98299933d487ccca4e9eb3fbea3130257a5a818a83940127429a4d"
WORKERS = {
    "namd": "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/namd-worker@sha256:30df40215f7df047463cf8e37f1fb80457b73fbb7ae8f681b9cb40d980e5f14e",
    "lammps": "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/lammps-worker@sha256:e4e21f952285134be263c9ea3f1f06ce461fdca2409622b568b186b12f7f199c",
}
START = {"namd": 605000, "lammps": 600000}
STEPS, CADENCE, ATOMS = 10000, 10, 6598


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    with Path(path).open("x") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def verify_runtime(models, engine, image):
    rows = [row for row in models.get("data", []) if row.get("model_id") == engine]
    if len(rows) != 1 or rows[0].get("state") != "active" or rows[0].get("runtime_image_digest") != image.split("@", 1)[1]:
        raise ValueError("caller-visible active runtime digest differs from frozen worker")
    return rows[0]


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError("expected exactly one native source directive: " + old)
    return text.replace(old, new)


def source_file(delivery, inventory, relative):
    path = delivery / relative
    item = inventory.get(relative)
    if (not item or path.is_symlink() or not path.is_file()
            or path.stat().st_size != item["bytes"] or sha(path) != item["sha256"]):
        raise ValueError("frozen source inventory mismatch: " + relative)
    return path


def bundle(inputs, target):
    with target.open("xb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as gz:
        with tarfile.open(fileobj=gz, mode="w") as archive:
            for path in sorted(inputs.rglob("*")):
                if path.is_file():
                    info = archive.gettarinfo(str(path), str(path.relative_to(inputs)))
                    info.uid = info.gid = info.mtime = 0
                    info.uname = info.gname = ""
                    info.mode = 0o644
                    with path.open("rb") as source:
                        archive.addfile(info, source)


def prepare(delivery, output, engine):
    delivery, output = Path(delivery).resolve(), Path(output).resolve()
    if delivery == output or delivery in output.parents:
        raise ValueError("new output must be outside frozen delivery")
    inventory = {r["path"]: r for r in read(delivery / "delivery-manifest.json")["files"]}
    case = read(source_file(delivery, inventory, "case.json"))
    if case["engines"][engine]["worker_image"] != WORKERS[engine]:
        raise ValueError("frozen worker image differs from qualified continuation image")
    output.mkdir(parents=True, exist_ok=False)
    inputs = output / "inputs"
    data = inputs / "alanine" if engine == "namd" else inputs
    data.mkdir(parents=True)
    retained = []

    def copy(relative, name=None):
        source = source_file(delivery, inventory, relative)
        target = data / (name or source.name)
        shutil.copyfile(source, target)
        retained.append({"source": relative, "sha256": sha(source), "bytes": source.stat().st_size,
                         "input": str(target.relative_to(inputs))})
        return target

    prefix = f"runs/{engine}/data/" + ("alanine/" if engine == "namd" else "")
    for name in ("protocol.json", "master-manifest.json"):
        copy(prefix + name)
    original_result = source_file(delivery, inventory, f"runs/{engine}/result.json")
    result = read(original_result)
    if result["status"] != "succeeded" or result["completed_steps"] != ["minimize", "nvt", "npt", "production"]:
        raise ValueError("source workflow not fully completed")
    if engine == "namd":
        for name in ("system.prmtop", "system.rst7", "system.pdb"):
            copy(prefix + name)
        for suffix in ("coor", "vel", "xsc"):
            copy(prefix + "production." + suffix, "origin." + suffix)
        xsc = [line for line in (data / "origin.xsc").read_text().splitlines() if line.strip() and not line.startswith("#")]
        if len(xsc) != 1 or int(xsc[0].split()[0]) != START[engine]:
            raise ValueError("source NAMD XSC timestep differs")
        original = copy(prefix + "production.namd", "source-production.namd").read_text()
        config = original
        for directive in ("outputEnergies", "outputPressure", "DCDfreq", "XSTfreq"):
            config = replace_once(config, f"{directive} 500\n", f"{directive} {CADENCE}\n")
        (data / "dense.namd").write_text(config)
        step = {"id": "dense", "mode": "dynamics", "directory": "alanine", "config": "dense.namd",
                "gpu_mode": "resident", "steps": STEPS, "segment_steps": STEPS, "first_step": START[engine],
                "output_prefix": "dense", "restart": {"coordinates": "origin.coor", "velocities": "origin.vel", "cell": "origin.xsc"},
                "expected_outputs": ["dense.coor", "dense.vel", "dense.xsc", "dense.part000001.dcd"]}
        request = {"schema": "fs2-serve.nebius.ai/namd-workflow-request/v1", "threads": 4,
                   "max_wall_seconds": 1800, "max_output_bytes": 536870912, "output_destination": "platform-artifacts",
                   "output_prefix": "qualification/dense-motion-20260924/namd", "jobs": [{"id": "dense-motion", "steps": [step]}]}
        restart_note = "Coordinates, velocities and XSC cell/piston state restored together at step 605000. Original seed 20260925 retained, but restart files do not clone Langevin RNG; no bitwise stochastic continuation claim. No temperature initialization or equilibration is repeated."
    else:
        for name in ("system-shake.lmp", "adapter-manifest.json", "restart-coefficients.inc", "nonbonded.inc", "constraints.inc"):
            copy(prefix + name)
        copy(prefix + "production.restart", "origin.restart")
        progress = source_file(delivery, inventory, prefix + "production-progress.txt")
        if progress.read_text().strip() != str(START[engine]) or result["commands"][-1].get("native_restart_step") != START[engine]:
            raise ValueError("source LAMMPS native restart/progress timestep differs")
        original = copy(prefix + "production-resume.in", "source-production-resume.in").read_text()
        thermo = copy(prefix + "thermo.inc", "source-thermo.inc").read_text()
        (data / "thermo.inc").write_text(replace_once(thermo, "thermo 500\n", "thermo 10\n"))
        script = replace_once(original, "read_restart production.restart\n", "read_restart origin.restart\n")
        script = replace_once(script, "dump coordinates all custom 500 production.${fs2_segment}.lammpstrj", "dump coordinates all custom 10 dense.${fs2_segment}.lammpstrj")
        script = replace_once(script, "run 600000 upto\n", "run 610000 upto\n")
        script = replace_once(script, "write_restart production.restart\n", "write_restart dense.restart\n")
        script = replace_once(script, "file production-progress.txt", "file dense-progress.txt")
        (data / "dense.in").write_text(script)
        (data / "dense-resume.in").write_text(replace_once(script, "read_restart origin.restart\n", "read_restart dense.restart\n"))
        # Exact text extraction, not rewritten floating-point coordinates. Bind
        # to the full prior artifact; never apply the old step-396000 exception.
        prior = source_file(delivery, inventory, prefix + "production.2.lammpstrj")
        with prior.open() as stream:
            last = list(deque(stream, maxlen=ATOMS + 9))
        if last[0].strip() != "ITEM: TIMESTEP" or int(last[1]) != START[engine]:
            raise ValueError("source final trajectory record differs from checkpoint")
        (data / "origin-closed.lammpstrj").write_text("".join(last))
        retained.append({"source": str(prior.relative_to(delivery)), "sha256": sha(prior), "bytes": prior.stat().st_size,
                         "input": "origin-closed.lammpstrj", "transformation": "exact final complete text record only",
                         "derived_sha256": sha(data / "origin-closed.lammpstrj")})
        step = {"id": "dense", "input": "dense.in", "expected_outputs": ["dense.restart", "dense-progress.txt", "dense.1.lammpstrj"],
                "continuation": {"input": "dense-resume.in", "restart_file": "dense.restart", "progress_file": "dense-progress.txt", "target_step": START[engine] + STEPS}}
        request = {"schema": "fs2-serve.nebius.ai/lammps-workflow-request/v1", "backend": "kokkos-cuda", "threads": 4,
                   "segment_seconds": 900, "max_wall_seconds": 1800, "max_output_bytes": 2147483648,
                   "output_destination": "platform-artifacts", "output_prefix": "qualification/dense-motion-20260924/lammps",
                   "jobs": [{"id": "dense-motion", "steps": [step]}]}
        restart_note = "Native binary state restored; original pair/PPPM/thermal/constraints/integrate fix IDs and coefficients recreated. Langevin seed 20260925 retained but RNG resets. Native SHAKE position setup may adjust step-600000 coordinates; this new boundary must be measured and independently validated, not accepted using old step-396000 evidence."
    save(output / "request.json", request)
    fixture = {"schema": "fs2-dense-native-continuation/v1", "engine": engine, "worker_image": WORKERS[engine],
               "source_operation_id": result["operation_id"], "source_result_sha256": sha(original_result),
               "delivery_manifest_sha256": sha(delivery / "delivery-manifest.json"), "source_directory": str(delivery),
               "start_step": START[engine], "final_step": START[engine] + STEPS, "native_origin_time_ps": START[engine] * .002,
               "steps": STEPS, "timestep_fs": 2, "output_every_steps": CADENCE, "nonzero_frames": 1000,
               "cadence_ps": .02, "duration_ps": 20, "atoms": ATOMS, "restart_semantics": restart_note,
               "source_files": retained, "input_files": [{"path": str(p.relative_to(inputs)), "sha256": sha(p), "bytes": p.stat().st_size} for p in sorted(inputs.rglob("*")) if p.is_file()]}
    bundle(inputs, output / "input.tar.gz")
    fixture.update(input_sha256=sha(output / "input.tar.gz"), request_sha256=sha(output / "request.json"))
    save(output / "fixture.json", fixture)
    return fixture


async def discover_inside(fixture, output):
    """Run only under the pinned client interpreter; no uploads/submissions."""
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from jsonschema import Draft202012Validator
    sys.path.insert(0, "/opt/bionemo")
    helper = runpy.run_path("/opt/bionemo/invoke-scientific-batch.py")
    f = read(fixture / "fixture.json")
    headers = {"authorization": "Bearer " + os.environ["SCIENTIFIC_MODELS_API_KEY"]}
    async with httpx2.AsyncClient(headers=headers, timeout=90, trust_env=False) as http:
        async with Client(streamable_http_client(os.environ["SCIENTIFIC_MODELS_MCP_URL"], http_client=http)) as client:
            names = [tool.name for tool in (await client.list_tools()).tools]
            if "submit_" + f["engine"] + "_workflow" not in names:
                raise ValueError("required caller-scoped typed tool unavailable")
            discovery = await helper["call"](client, "get_model_schema", {"model_id": f["engine"], "protocol": "scientific-batch-v1"})
            schema = helper["scientific_contract"](discovery)["input_schema"]["properties"]["parameters"]
            Draft202012Validator(schema).validate(read(fixture / "request.json"))
            models = await helper["call"](client, "list_scientific_models", {})
    save(output / "model-contract.json", discovery)
    save(output / "scientific-models.json", models)
    # Persist caller-visible identity and inspect it before admitting work. Some
    # contract versions publish image identity only in scientific discovery.
    runtime = verify_runtime(models, f["engine"], f["worker_image"])
    result = {"status": "schema-and-access-verified", "engine": f["engine"], "expected_worker_image": f["worker_image"],
              "exact_image_visible_in_discovery": True, "caller_visible_runtime": runtime,
              "caller_fingerprint": hashlib.sha256(os.environ["SCIENTIFIC_MODELS_API_KEY"].encode()).hexdigest(),
              "observed_at": datetime.now(timezone.utc).isoformat(), "uploads_or_runs_submitted": False}
    save(output / "discovery-receipt.json", result)
    print(json.dumps(result))


def customer(args):
    fixture, output = args.fixture.resolve(), args.output.resolve()
    key = read(args.key_file)["secret"]
    if args.previous_receipt and read(args.previous_receipt / "receipt.json")["identity"]["caller_fingerprint"] != hashlib.sha256(key.encode()).hexdigest():
        raise ValueError("credential differs from existing ordinary qualified owner")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = {**os.environ, "SCIENTIFIC_MODELS_API_KEY": key, "SCIENTIFIC_MODELS_MCP_URL": args.mcp_url}
    f = read(fixture / "fixture.json")
    if sha(fixture / "input.tar.gz") != f["input_sha256"] or sha(fixture / "request.json") != f["request_sha256"]:
        raise ValueError("frozen dense input changed")
    cmd = ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "--entrypoint", "/opt/scientific-client/bin/python",
           "--env", "SCIENTIFIC_MODELS_API_KEY", "--env", "SCIENTIFIC_MODELS_MCP_URL",
           "--mount", f"type=bind,src={fixture},dst=/input,readonly", "--mount", f"type=bind,src={output},dst=/receipt"]
    if args.command == "discover":
        cmd += ["--mount", f"type=bind,src={Path(__file__).resolve()},dst=/dense.py,readonly", CLIENT,
                "/dense.py", "_discover", "--fixture", "/input", "--output", "/receipt"]
    else:
        preflight = read(args.preflight / "discovery-receipt.json")
        if preflight["engine"] != f["engine"] or preflight["caller_fingerprint"] != hashlib.sha256(key.encode()).hexdigest() or not preflight["exact_image_visible_in_discovery"]:
            raise ValueError("matching caller/source/image preflight required before submission")
        model = f["engine"]
        cmd += [CLIENT, "/opt/bionemo/invoke-scientific-batch.py", "--model", model, "--tool", f"submit_{model}_workflow",
                "--operation", "run-workflow", "--source", "/input/input.tar.gz", "--parameters", "/input/request.json",
                "--entry-name", f"{model}-inputs", "--semantic-type", f"{model}-input-bundle/v1", "--media-type", "application/x-tar",
                "--compression", "gzip", "--output", "/receipt", "--idempotency-key", f"dense-motion-20260924-{model}-{f['input_sha256'][:24]}",
                "--display-name", f"{model.upper()} native 20 ps dense continuation", "--wait-seconds", "1800", "--poll-seconds", "5"]
    log = output.with_name(output.name + ".client.log")
    with log.open("ab") as stream:
        os.chmod(log, 0o600)
        code = subprocess.run(cmd, env=env, stdout=stream, stderr=subprocess.STDOUT).returncode
    print(json.dumps({"mode": args.command, "engine": f["engine"], "exit_code": code, "private_log": str(log)}))
    return code


def observe(args):
    operation = read(args.receipt / "receipt.json")["operation_id"]
    from uuid import UUID
    operation = str(UUID(operation))
    base = ["kubectl", "--kubeconfig", str(args.kubeconfig), "--context", args.context, "-n", "fs2-models"]
    raw = json.loads(subprocess.check_output(base + ["get", "pods", "-l", "fs2.nebius.ai/operation-id=" + operation, "-o", "json"], text=True))
    records = []
    for pod in raw["items"]:
        if pod["metadata"]["labels"].get("fs2.nebius.ai/operation-id") != operation:
            raise ValueError("unexpected unrelated pod")
        containers = [{"name": c["name"], "image": c["image"], "resources": c.get("resources", {})} for c in pod["spec"]["containers"]]
        row = {"name": pod["metadata"]["name"], "uid": pod["metadata"]["uid"], "labels": pod["metadata"]["labels"],
               "node": pod["spec"].get("nodeName"), "phase": pod["status"]["phase"], "containers": containers,
               "container_statuses": pod["status"].get("containerStatuses", []), "gpu_observation": None}
        runner = next((c for c in containers if c["resources"].get("limits", {}).get("nvidia.com/gpu")), None)
        if row["phase"] == "Running" and runner:
            probe = subprocess.run(base + ["exec", row["name"], "-c", runner["name"], "--", "nvidia-smi",
                "--query-gpu=name,uuid,driver_version", "--format=csv,noheader"], capture_output=True, text=True)
            row["gpu_observation"] = {"exit_code": probe.returncode, "stdout": probe.stdout.strip(), "stderr": probe.stderr.strip()}
        records.append(row)
    result = {"schema": "fs2-owned-dense-pod-observation/v1", "operation_id": operation,
              "observed_at": datetime.now(timezone.utc).isoformat(), "pods": records, "changes_made": False}
    save(args.output, result)
    print(json.dumps({"operation_id": operation, "pods": [{"name": r["name"], "phase": r["phase"], "gpu": r["gpu_observation"]} for r in records]}))


def require_patterns(text, patterns):
    for pattern in patterns:
        if not re.search(pattern, text, re.M):
            raise ValueError("required native setting missing: " + pattern)


def frame_schedule(steps, times, origin, *, initial):
    """Native timestamps are observed, never filled in from frame indexes."""
    import numpy as np
    expected = list(range(origin if initial else origin + CADENCE, origin + STEPS + 1, CADENCE))
    if steps != expected or len(times) != len(expected):
        raise ValueError("native frame count/step schedule differs from 1,000 positive 20 fs samples")
    error = float(np.max(np.abs(np.asarray(times) - np.asarray(expected) * .002)))
    if not np.isfinite(error) or error > 1e-3:
        raise ValueError("native frame timestamps differ from declared timestep")
    return {"native_frames": len(steps), "positive_frames": 1000, "initial_frame": initial,
            "first_step": steps[0], "last_step": steps[-1], "origin_step": origin,
            "origin_time_ps": origin * .002, "duration_ps": 20, "cadence_ps": .02,
            "maximum_native_time_roundoff_ps": error}


def inventory_audit(engine, fixture, workspace):
    """Reuse engine normalization and independently bind every returned byte."""
    contract = importlib.import_module(f"fs2_{engine}.contracts")
    package = importlib.import_module(f"fs2_{engine}")
    request = contract.normalize(read(fixture / "request.json"))
    result = read(workspace / "result.json")
    expected = hashlib.sha256(contract.canonical({"request": request, "job": "dense-motion", "image": package.ENGINE_ID})).hexdigest()
    if (result["status"] != "succeeded" or result["schema"] != package.RESULT_SCHEMA
            or result["engine_id"] != package.ENGINE_ID or result["job_id"] != "dense-motion"
            or result["recipe_sha256"] != expected or result["completed_steps"] != ["dense"]):
        raise ValueError("result does not bind the exact successful requested native recipe")
    if len(result["commands"]) != 1 or result["commands"][0]["exit_code"] != 0 or result["commands"][0]["step_id"] != "dense":
        raise ValueError("expected exactly one successful continuation process, no hidden retry")
    data, seen, total = workspace / "data", set(), 0
    for item in result["files"]:
        name = item["path"]
        path = data / name
        if (name in seen or path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(data.resolve())
                or path.stat().st_size != item["size_bytes"] or sha(path) != item["sha256"]):
            raise ValueError("result file inventory mismatch: " + name)
        seen.add(name)
        total += item["size_bytes"]
    provenance = read(fixture / "fixture.json")
    if sha(fixture / "input.tar.gz") != provenance["input_sha256"] or sha(fixture / "request.json") != provenance["request_sha256"]:
        raise ValueError("dense immutable fixture changed")
    for item in provenance["input_files"]:
        path = data / item["path"]
        if item["path"] not in seen or sha(path) != item["sha256"] or path.stat().st_size != item["bytes"]:
            raise ValueError("immutable native input changed: " + item["path"])
    return result, {"recipe_sha256": expected, "verified_files": len(seen), "verified_bytes": total,
                    "immutable_input_files": len(provenance["input_files"])}


def constraint_model(engine, data, master):
    import numpy as np
    import parmed
    topology = parmed.load_file(str(master / "system.prmtop"))
    if len(topology.atoms) != ATOMS:
        raise ValueError("master atom count differs")
    if engine == "namd":
        if sha(data / "system.prmtop") != sha(master / "system.prmtop"):
            raise ValueError("NAMD topology differs from canonical master")
        selected = [b for b in topology.bonds if min(b.atom1.mass, b.atom2.mass) < 2]
        pairs = np.asarray([[b.atom1.idx, b.atom2.idx] for b in selected], dtype=int)
        distances = np.asarray([b.type.req for b in selected])
        masses = np.asarray([a.mass for a in topology.atoms])
        path = data / "system.prmtop"
    else:
        from adapter import read_data
        proof = read(data / "adapter-manifest.json")
        path = data / "system-shake.lmp"
        if proof["adapted_data_sha256"] != sha(path) or proof["master_topology_sha256"] != sha(master / "system.prmtop"):
            raise ValueError("LAMMPS adapted topology/master binding differs")
        _, table = read_data(path)
        types = set(map(str, proof["shake_bond_types"]))
        water_hh = {frozenset((a, b)) for _, a, b in proof["water_atom_ids_O_H_H"]}
        selected = [r for r in table["Bonds"] if r[1] in types or frozenset(map(int, r[2:])) in water_hh]
        pairs = np.asarray([[int(r[2]) - 1, int(r[3]) - 1] for r in selected])
        coefficients = {r[0]: float(r[3]) for r in table["Bond Coeffs"]}
        distances = np.asarray([coefficients[r[1]] for r in selected])
        type_mass = {r[0]: float(r[1]) for r in table["Masses"]}
        masses = np.asarray([type_mass[r[2]] for r in sorted(table["Atoms"], key=lambda r: int(r[0]))])
    if pairs.shape != (6588, 2) or masses.shape != (ATOMS,) or len(np.unique(np.sort(pairs, axis=1), axis=0)) != 6588:
        raise ValueError("canonical 6,588-constraint model differs")
    if not np.allclose(masses, [a.mass for a in topology.atoms], atol=1e-6, rtol=0):
        raise ValueError("native atom/mass ordering differs from master")
    return path, masses, pairs, distances


def validate_native_settings(engine, data, command, origin):
    log = data / command["log"]
    text = log.read_text()
    if engine == "namd":
        from fs2_namd.worker import binary_vectors, xsc_step
        if (command["configured_first_step"] != origin or command["checkpoint_step"] != origin + STEPS
                or command["gpu_mode"] != "resident" or command["atoms"] != ATOMS
                or command["random_seed"] != 20260925 or command["timestep_fs"] != 2):
            raise ValueError("native NAMD segment/restart settings differ")
        folder = data / "alanine"
        for base, step in (("origin", origin), ("dense", origin + STEPS)):
            if xsc_step(folder / (base + ".xsc")) != step or any(binary_vectors(folder / (base + s)) != ATOMS for s in (".coor", ".vel")):
                raise ValueError("NAMD complete binary restart state differs")
        wrapper = (folder / "fs2-dense-part000001.namd").read_text()
        require_patterns(wrapper, [r'^binCoordinates "origin.coor"$', r'^binVelocities "origin.vel"$', r'^extendedSystem "origin.xsc"$', rf"^firsttimestep {origin}$", r"^run 10000$"])
        if re.search(r"(?im)^\s*(temperature|reinitvels)\s", wrapper):
            raise ValueError("NAMD continuation reinitializes velocities")
        require_patterns(text, [r"Info: Running with GPU-resident mode", r"Info: TIMESTEP\s+2$", r"Info: CUTOFF\s+10$",
            r"Info: PME GRID DIMENSIONS\s+64 64 64$", r"Info: PME INTERPOLATION ORDER\s+4$", r"Info: PME TOLERANCE\s+1e-05$",
            r"Info: PME CALCULATION WILL BE PERFORMED ON GPU", r"Info: LANGEVIN TEMPERATURE\s+300$",
            r"Info: LANGEVIN DAMPING COEFFICIENT IS 1 INVERSE PS", r"TARGET PRESSURE IS 1 BAR",
            r"Info: RIGID BONDS TO HYDROGEN : ALL", r"ERROR TOLERANCE : 1e-06", r"Info: RANDOM NUMBER SEED\s+20260925$"])
        checkpoints = [folder / (base + suffix) for base in ("origin", "dense") for suffix in (".coor", ".vel", ".xsc")]
    else:
        if command["native_restart_step"] != origin + STEPS or (data / "dense-progress.txt").read_text().strip() != str(origin + STEPS):
            raise ValueError("LAMMPS native restart/progress is incomplete")
        if re.findall(r"Loop time of \S+ on .*? for (\d+) steps", text) != [str(STEPS)]:
            raise ValueError("LAMMPS native loop is not exactly 10,000 steps")
        require_patterns(text, [r"KOKKOS mode", r"restoring atom style full/kk from restart", r"  6598 atoms$",
            r"fix style: nph/kk, fix ID: integrate", r"All restart file global fix info was re-assigned",
            r"grid = 64 64 64$", r"stencil order = 4$", r"Time step\s+: 2$", rf"Current step\s+: {origin}$",
            r"pair lj/cut/coul/long/kk", r"kokkos_device", r"update: every = 1 steps, delay = 0 steps, check = yes"])
        script = (data / "dense.in").read_text()
        require_patterns(script, [r"^read_restart origin.restart$", r"^fix thermal all langevin 300.0 300.0 1000.0 20260925 zero yes$", r"^run 610000 upto$"])
        require_patterns((data / "nonbonded.inc").read_text(), [r"^pair_style lj/cut/coul/long 10.0 10.0$", r"^kspace_style pppm 1e-5$", r"^kspace_modify mesh 64 64 64 order 4$", r"^timestep 2.0$"])
        checkpoints = [data / "origin.restart", data / "dense.restart"]
    return log, checkpoints, [line for line in text.splitlines() if line.lower().startswith("warning")]


def validate(args):
    """CPU-only semantic gate over already downloaded native customer artifacts."""
    import numpy as np
    md = args.md_source.resolve()
    # The released image deliberately isolates API and analysis environments.
    # Only schema packages are needed from its existing API environment; no install.
    for path in Path("/opt/scientific-client/lib").glob("python*/site-packages"):
        sys.path.append(str(path))
    for relative in ("gromacs/runtime", "namd/runtime", "lammps/runtime", "comparison/analysis", "comparison/lammps"):
        sys.path.insert(0, str(md / relative))
    from native import frames
    from geometry import minimum_image
    from thermo import native_thermo, native_performance
    fixture, workspace = args.fixture.resolve(), args.workspace.resolve()
    provenance = read(fixture / "fixture.json")
    engine, origin = provenance["engine"], provenance["start_step"]
    if origin != START[engine] or provenance["worker_image"] != WORKERS[engine]:
        raise ValueError("dense origin/worker identity differs")
    result, audit = inventory_audit(engine, fixture, workspace)
    command = result["commands"][0]
    data = workspace / "data"
    native_data = data / "alanine" if engine == "namd" else data
    topology, masses, pairs, distances = constraint_model(engine, native_data, args.master)
    log, checkpoints, warnings = validate_native_settings(engine, data, command, origin)
    trajectory = native_data / ("dense.part000001.dcd" if engine == "namd" else "dense.1.lammpstrj")
    steps, times, max_error, first, final = [], [], 0., None, None
    previous_positions, identical_consecutive_frames = None, 0
    for frame in frames(trajectory, engine, .002):
        if frame.positions.shape != (ATOMS, 3) or not np.isfinite(frame.positions).all() or not np.isfinite(frame.cell).all() or np.linalg.det(frame.cell) <= 0:
            raise ValueError("invalid native frame dimensions/coordinates/cell")
        first = frame if first is None else first
        final = frame
        steps.append(frame.step)
        times.append(frame.time_ps)
        vector = minimum_image(frame.positions[pairs[:, 0]] - frame.positions[pairs[:, 1]], frame.cell)
        max_error = max(max_error, float(np.abs(np.linalg.norm(vector, axis=1) - distances).max()))
        if previous_positions is not None and np.array_equal(previous_positions, frame.positions):
            identical_consecutive_frames += 1
        previous_positions = frame.positions.copy()
    timeline = frame_schedule(steps, times, origin, initial=engine == "lammps")
    if max_error > 1e-4 or identical_consecutive_frames:
        raise ValueError("constraint tolerance exceeded or consecutive trajectory frames duplicated")
    boundary = None
    if engine == "lammps":
        from shake_boundary import KIND, SOURCE_REVISION, verify_projection
        old = list(frames(native_data / "origin-closed.lammpstrj", engine, .002))
        if len(old) != 1:
            raise ValueError("preceding closed checkpoint record is ambiguous")
        evidence = {"kind": KIND, "source_revision": SOURCE_REVISION, "worker_image": WORKERS[engine],
                    "step": origin, "topology_sha256": sha(topology), "previous_record_sha256": sha(native_data / "origin-closed.lammpstrj"),
                    "next_trajectory_sha256": sha(trajectory), "model": {"masses_Da": masses.tolist(), "pairs_zero_based": pairs.tolist(), "distances_A": distances.tolist()}}
        evidence_hash = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        boundary = verify_projection(old[0], first, {**evidence, "evidence_sha256": evidence_hash})
        boundary["new_boundary_evidence"] = {k: v for k, v in evidence.items() if k != "model"}
        boundary["model_reconstructed_from_verified_topology"] = True
    else:
        import struct
        raw = (native_data / "dense.coor").read_bytes()
        endian = next(e for e in ("<", ">") if struct.unpack(e + "i", raw[:4])[0] == ATOMS)
        final_positions = np.frombuffer(raw[4:], dtype=endian + "f8").reshape(ATOMS, 3)
        difference = float(np.abs(minimum_image(final.positions - final_positions, final.cell)).max())
        if difference > 1e-4:
            raise ValueError("last DCD frame differs from final binary checkpoint")
        boundary = {"final_DCD_checkpoint_max_component_difference_A": difference,
                    "native_COM_velocity_removal_log": [line for line in log.read_text().splitlines() if "REMOVING COM VELOCITY" in line],
                    "note": "Native restart coordinates/velocities/XSC restored; native COM-velocity setup is retained, not a bitwise RNG continuation claim."}
    rows = native_thermo(log, engine + "_log", .002, float(masses.sum()))
    if [r["step"] for r in rows] != list(range(origin, origin + STEPS + 1, CADENCE)):
        raise ValueError("native thermodynamic cadence/endpoints differ")
    customer, status = read(args.receipt / "receipt.json"), read(args.receipt / "status.json")
    attempts = [a for s in status["batch"]["stages"] for a in s["attempts"]]
    if (customer["state"] != "verified" or customer["operation_id"] != result["operation_id"]
            or status["operation"]["id"] != result["operation_id"] or status["operation"]["status"] != "succeeded"
            or len(attempts) != 1 or attempts[0]["outcome"] != "succeeded" or not attempts[0]["resource_released"]):
        raise ValueError("customer operation/attempt not verified, successful and released")
    pod = read(args.pod_evidence)
    if pod["operation_id"] != result["operation_id"]:
        raise ValueError("pod evidence belongs to another operation")
    captures = []
    for entry in pod["pods"]:
        runner = [c for c in entry["containers"] if c["resources"].get("limits", {}).get("nvidia.com/gpu")]
        if len(runner) != 1 or runner[0]["image"] != WORKERS[engine]:
            raise ValueError("captured worker image differs")
        captures.append({"pod": entry["name"], "uid": entry["uid"], "node": entry["node"], "worker_image": runner[0]["image"], "gpu": entry["gpu_observation"]})
    def file_record(path):
        return {"path": str(path), "sha256": sha(path), "bytes": path.stat().st_size}
    report = {"schema": "fs2-dense-native-validation/v1", "status": "passed", "recorded_at": datetime.now(timezone.utc).isoformat(),
        "engine": engine, "operation_id": result["operation_id"], "worker_image": WORKERS[engine], "engine_id": result["engine_id"],
        "client_image": CLIENT, "native_command": command, "inventory": audit,
        "input_sha256": provenance["input_sha256"], "request_sha256": provenance["request_sha256"], "result": file_record(workspace / "result.json"),
        "trajectory": file_record(trajectory), "topology": file_record(topology), "native_log": file_record(log),
        "restart_files": [file_record(p) for p in checkpoints], "master_topology_sha256": sha(args.master / "system.prmtop"),
        "timeline": timeline, "atoms": ATOMS, "constraints": len(pairs), "max_constraint_distance_error_A": max_error,
        "constraint_tolerance_A": 1e-4, "finite_coordinates_cells_and_thermodynamics": True,
        "identical_consecutive_frames": identical_consecutive_frames, "thermodynamic_samples": len(rows), "boundary": boundary,
        "native_performance": native_performance(log, engine, STEPS, .002), "restart_semantics": provenance["restart_semantics"],
        "warnings": warnings, "customer_artifact_count": len(customer["verified_artifacts"]), "customer_receipt": file_record(args.receipt / "receipt.json"),
        "released_attempt": attempts[0], "pod_evidence": file_record(args.pod_evidence), "pod_captures": captures,
        "identity_limitations": [] if captures else ["Native Pod deleted before live observation; no per-Pod imageID/GPU UUID/driver capture. Exact caller-visible runtime digest and retained admission/native dispatch evidence are preserved; no inferred UUID or driver."],
        "analysis_source": {str(p.relative_to(md)): sha(p) for p in [md / "comparison/analysis" / name for name in ("native.py", "geometry.py", "thermo.py", "shake_boundary.py")]},
        "validator_source_sha256": sha(Path(__file__)), "scientific_convergence_claimed": False, "interpolation_or_coordinate_averaging": False}
    save(args.output, report)
    renderer = {"engine": engine, "trajectory": str(trajectory), "trajectory_format": "DCD" if engine == "namd" else "LAMMPSDUMP",
                "origin_time_ps": origin * .002, "origin_step": origin, "canonical_to_native": "identity", "validation_receipt": str(args.output.resolve())}
    save(args.output.with_name(args.output.stem + "-render-spec.json"), renderer)
    print(json.dumps({"status": "passed", "engine": engine, "operation_id": result["operation_id"], "positive_frames": 1000, "max_constraint_distance_error_A": max_error, "receipt": str(args.output)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--delivery", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--engine", choices=WORKERS, required=True)
    p = sub.add_parser("observe")
    p.add_argument("--receipt", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--kubeconfig", type=Path, required=True)
    p.add_argument("--context", required=True)
    p = sub.add_parser("validate")
    for name in ("fixture", "workspace", "md-source", "master", "receipt", "pod-evidence", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    for kind in ("discover", "submit", "_discover"):
        p = sub.add_parser(kind)
        p.add_argument("--fixture", type=Path, required=True)
        p.add_argument("--output", type=Path, required=True)
        if kind != "_discover":
            p.add_argument("--key-file", type=Path, required=True)
            p.add_argument("--previous-receipt", type=Path, required=True)
            p.add_argument("--mcp-url", default="https://89.169.99.188/mcp")
        if kind == "submit":
            p.add_argument("--preflight", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        f = prepare(args.delivery, args.output, args.engine)
        print(json.dumps({k: f[k] for k in ("engine", "start_step", "final_step", "input_sha256", "request_sha256")}))
    elif args.command == "_discover":
        asyncio.run(discover_inside(args.fixture, args.output))
    elif args.command == "observe":
        observe(args)
    elif args.command == "validate":
        if args.output.exists():
            raise ValueError("use a new validation receipt path")
        try:
            validate(args)
        except Exception as exc:
            if not args.output.exists():
                save(args.output, {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "validator_source_sha256": sha(Path(__file__))})
            raise
    else:
        raise SystemExit(customer(args))


if __name__ == "__main__":
    main()
