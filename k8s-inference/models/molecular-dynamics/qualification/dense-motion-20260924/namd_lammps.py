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
    else:
        raise SystemExit(customer(args))


if __name__ == "__main__":
    main()
