"""Bounded direct-Pod candidate regression; never customer-path acceptance.

Only creates/deletes its uniquely named task Pod, after checking free requested
GPU capacity across namespaces. No service/catalog, credentials, or node changes.
Uses the existing public MPINAT fixture; bundle bytes and physics stay unchanged.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

RUNTIME = Path(__file__).resolve().parents[2] / "models/molecular-dynamics/gromacs/runtime"
sys.path.insert(0, str(RUNTIME))
from fs2_gromacs.contracts import normalize

KUBE = ["kubectl", "--kubeconfig", "/home/tux/secure-handoff/cosmos-stockholm-sandbox2-20260917.kubeconfig",
        "--context", "fs2-remediation-sandbox2"]
TASK = "gromacs-candidate-20261003"
NS = "fs2-models"


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, value):
    with Path(path).open("x") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


def call(args, **kwargs):
    return subprocess.run(KUBE + args, check=True, capture_output=True, timeout=60, **kwargs).stdout


def free_capacity(node, nodes, pods, requested):
    target = next(n for n in nodes if n["metadata"]["name"] == node)
    if not any(c["type"] == "Ready" and c["status"] == "True" for c in target["status"]["conditions"]):
        raise ValueError("target node is not Ready")
    if target["spec"].get("unschedulable"):
        raise ValueError("target node is unschedulable")
    allowed = {("dedicated", "fs2-inference", "NoSchedule")}
    if any((t.get("key"), t.get("value"), t.get("effect")) not in allowed for t in target["spec"].get("taints", [])):
        raise ValueError("target has an unapproved taint")
    owned = []
    for p in pods:
        if p["spec"].get("nodeName") != node or p["status"].get("phase") in ("Succeeded", "Failed"):
            continue
        containers = p["spec"]["containers"]
        # Native sidecar init containers are additive; sequential init peaks
        # also consume reservations. Conservative maximum avoids overbooking.
        normal = sum(int(c.get("resources", {}).get("requests", {}).get("nvidia.com/gpu", 0)) for c in containers)
        init = p["spec"].get("initContainers", [])
        native_sidecars = sum(int(c.get("resources", {}).get("requests", {}).get("nvidia.com/gpu", 0)) for c in init if c.get("restartPolicy") == "Always")
        init_peak = max([int(c.get("resources", {}).get("requests", {}).get("nvidia.com/gpu", 0)) for c in init] or [0])
        count = max(normal + native_sidecars, init_peak + native_sidecars)
        if count:
            owned.append({"namespace": p["metadata"]["namespace"], "pod": p["metadata"]["name"], "gpus": count})
    total = int(target["status"].get("allocatable", {}).get("nvidia.com/gpu", 0))
    free = total - sum(p["gpus"] for p in owned)
    if free < requested:
        raise ValueError(f"capacity unavailable: {free} free GPUs, {requested} requested; no eviction")
    return {"node": node, "allocatable_gpus": total, "free_by_requests": free,
            "existing_gpu_pods": owned, "labels": {k: v for k, v in target["metadata"].get("labels", {}).items()
            if k in ("nebius.com/resource-preset", "node.kubernetes.io/instance-type", "topology.kubernetes.io/region", "nebius.com/nvidia_driver_version", "accelerator.fs2.nebius/pool-id")}}


def make_fixture(source, output, mpi_gpus=0, diagnostic=False, parameters=None, mpi_nodes=1):
    parameters = parameters or source / "parameters.json"
    request = json.loads(parameters.read_text())
    request = request.get("parameters", request)
    if any("-resethway" in s.get("args", []) for j in request["jobs"] for s in j["steps"]):
        raise ValueError("use the corrected saved campaign parameters; forced reset is not a valid 10k-step control")
    request["max_wall_seconds"] = 1800
    request["output_destination"] = "platform-artifacts"
    request["output_prefix"] = "qualification/gromacs-candidate-20261003"
    if mpi_gpus:
        request.update(schema="fs2-serve.nebius.ai/gromacs-mpi-workflow-request/v1", nodes=mpi_nodes, gpus_per_node=mpi_gpus)
        request["jobs"][0]["id"] = "gang"
        for step in request["jobs"][0]["steps"]:
            if step["command"] == "mdrun":
                # Explicit matched rank-layout control. Original all-bond
                # constraints still require CPU update; force PME placement
                # identically for 1/2/4 ranks, not a hidden physics change.
                step["args"] += ["-pme", "cpu", "-npme", "0"]
    if diagnostic:
        request["max_output_bytes"] = 5 * 1024**2
    request = normalize(request, mpi=bool(mpi_gpus))
    output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(source / "input.tar.gz", output / "input.tar.gz")
    save(output / "request.json", request)
    save(output / "fixture.json", {"source_directory": str(source), "source_provenance_sha256": sha(source / "provenance.json"),
        "source_parameters_path": str(parameters), "source_parameters_sha256": sha(parameters), "input_sha256": sha(output / "input.tar.gz"),
        "request_sha256": sha(output / "request.json"), "mpi_gpus": mpi_gpus, "mpi_nodes": mpi_nodes, "diagnostic_budget_test": diagnostic,
        "science": "unchanged original TPR, native finite 10000 steps, three distinct timing repeats, no forced counter reset"})


def validate_gro(path, expected_atoms):
    lines = path.read_text().splitlines()
    atoms = int(lines[1])
    if atoms != expected_atoms or len(lines) != atoms + 3:
        raise ValueError("final GRO atom count/layout mismatch")
    for line in lines[2:-1]:
        coordinates = [float(line[i:i + 8]) for i in (20, 28, 36)]
        if not all(math.isfinite(x) for x in coordinates):
            raise ValueError("nonfinite final coordinate")
    cell = [float(x) for x in lines[-1].split()]
    if len(cell) not in (3, 9) or not all(math.isfinite(x) for x in cell) or not all(x > 0 for x in cell[:3]):
        raise ValueError("invalid final periodic cell")
    return {"atoms": atoms, "cell_nm": cell, "sha256": sha(path), "finite": True}


def validate(workspace, request, gpu_count, mpi, expected_failure, worker_exit):
    result = json.loads((workspace / "result.json").read_text())
    checks = {}
    errors = []
    for item in result["files"]:
        p = workspace / "data" / item["path"]
        if not p.resolve().is_relative_to((workspace / "data").resolve()) or p.stat().st_size != item["size_bytes"] or sha(p) != item["sha256"]:
            errors.append("native inventory mismatch: " + item["path"])
    checks["inventory_rehashed_files"] = len(result["files"])
    if expected_failure:
        marker_path = workspace / ".fs2/checkpoint-ready.json"
        durable_generation = json.loads(marker_path.read_text())["state"]["generation"] if marker_path.exists() else 0
        checks.update(failed_status=result["status"] == "failed", nonzero_exit=worker_exit != 0,
                      inventory_incomplete=result["inventory_complete"] is False,
                      diagnostics_only=result["inventory_scope"] == "bounded-failure-logs-only",
                      output_budget_error="workspace file/byte budget" in (result.get("error") or ""),
                      diagnostic_files_present=bool(result["files"]),
                      uncommitted_generation_not_claimed=result["checkpoint_commit_scope"] == "local-only" and
                      result["committed_checkpoint_generation"] == durable_generation and
                      result["native_checkpoint_generation"] >= durable_generation)
    else:
        checks.update(succeeded=result["status"] == "succeeded", zero_exit=worker_exit == 0,
                      all_requested_steps=result["completed_steps"] == [s["id"] for s in request["jobs"][0]["steps"]],
                      inventory_complete=result["inventory_complete"] is True)
        runs = [c for c in result["commands"] if "mdrun" in c["command"]]
        checks["three_native_repeats"] = len(runs) == 3
        checks["all_checkpoint_steps_10000"] = all(c.get("checkpoint_step") == 10000 for c in runs)
        checks["all_native_rates_positive"] = all((c.get("performance_ns_per_day") or 0) > 0 for c in runs)
        if mpi:
            checks["all_rank_bindings_complete"] = all(c.get("rank_binding_evidence_complete") is True and len(c.get("rank_bindings", [])) == gpu_count for c in runs)
        energies = []
        coordinates = []
        for i in (1, 2, 3):
            path = workspace / "data" / f"energy{i}.xvg"
            if not path.exists():
                errors.append(f"repeat {i} energy file absent")
                continue
            rows = [[float(t) for t in line.split()] for line in path.read_text().splitlines() if line and not line.startswith(("@", "#"))]
            good = bool(rows) and all(len(row) >= 3 and all(math.isfinite(v) for v in row) and row[2] > 0 for row in rows)
            checks[f"repeat_{i}_finite_energies"] = good
            checks[f"repeat_{i}_reaches_20ps"] = bool(rows) and abs(rows[-1][0] - 20) < 1e-5
            energies.append({"repeat": i, "samples": len(rows), "first": rows[0] if rows else None, "last": rows[-1] if rows else None,
                             "sha256": sha(path)})
            gro = workspace / "data" / f"repeat{i}.gro"
            logs = sorted((workspace / "data").glob(f"repeat{i}.part*.log"))
            native = "\n".join(p.read_text(errors="replace") for p in logs)
            atoms = re.search(r"There are:\s+(\d+)\s+Atoms", native)
            if not atoms or not gro.exists():
                errors.append(f"repeat {i} native atom count/final coordinates absent")
            else:
                try:
                    coordinates.append({"repeat": i, **validate_gro(gro, int(atoms[1]))})
                except ValueError as exc:
                    errors.append(f"repeat {i}: {exc}")
        checks["energy_summaries"] = energies
        checks["final_coordinate_summaries"] = coordinates
    errors += [k for k, v in checks.items() if v is False]
    return {"status": "passed" if not errors else "failed", "errors": errors, "checks": checks,
            "validator_source_sha256": sha(__file__),
            "native_result_sha256": sha(workspace / "result.json"), "expected_failure_case": expected_failure,
            "scope": "bounded native execution/inventory/final-state regression; original benchmark records only final energy, not an ensemble-convergence study",
            "customer_path_tested": False, "gpu_snapshot_used": False}


def validate_device_probe(text, gpu_text, gpu_count, exit_code):
    """Actual device communication must pass independently of the build flag."""
    rows = [json.loads(line) for line in text.splitlines() if line.startswith('{"kind":')]
    ranks = [row for row in rows if row.get("kind") == "rank"]
    summaries = [row for row in rows if row.get("kind") == "summary"]
    allocated = {line.split(",", 1)[0].strip().removeprefix("GPU-").replace("-", "").lower()
                 for line in gpu_text.splitlines()[1:] if line.strip()}
    observed = {row.get("gpu_uuid_hex") for row in ranks}
    checks = {
        "zero_exit": exit_code == 0,
        "every_rank": sorted(row.get("rank", -1) for row in ranks) == list(range(gpu_count)),
        "initialized_cuda_support": len(ranks) == gpu_count and all(row.get("MPIX_Query_cuda_support") == 1 for row in ranks),
        "unique_allocated_gpu_per_rank": len(observed) == gpu_count and observed == allocated,
        "complete_native_summary": len(summaries) == 1 and summaries[0].get("status") == "passed"
        and summaries[0].get("ranks") == gpu_count
        and summaries[0].get("message_bytes") == [4, 4096, 1048576]
        and summaries[0].get("point_to_point_repeats") == 3
        and summaries[0].get("checks") == ["Sendrecv", "Isend/Irecv/Waitall", "Allreduce"],
    }
    errors = [key for key, value in checks.items() if not value]
    return {"status": "failed" if errors else "passed", "errors": errors, "checks": checks,
            "ranks": ranks, "native_summaries": summaries,
            "scope": "initialized CUDA query and actual device-buffer correctness; not performance/RDMA/MD qualification"}


def run(args):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", args.name) or "@sha256:" not in args.image:
        raise ValueError("require bounded Pod name and immutable image")
    request = json.loads((args.input / "request.json").read_text())
    mpi = "mpi-workflow" in request["schema"]
    probe_only = getattr(args, "device_probe_only", False)
    if probe_only and not mpi:
        raise ValueError("device-buffer probe requires the external-MPI candidate")
    normalize(request, mpi=mpi)
    if request.get("nodes", 1) != 1 or request.get("gpus_per_node", 1) != args.gpus:
        raise ValueError("this helper only qualifies one node matching its requested GPU shape")
    args.output.mkdir(parents=True, exist_ok=False)
    index_bytes = subprocess.check_output(["crane", "manifest", args.image], timeout=60)
    index = json.loads(index_bytes)
    (args.output / "registry-index.json").write_bytes(index_bytes)
    manifest_digests = [m["digest"] for m in index.get("manifests", []) if m.get("platform") == {"architecture": "amd64", "os": "linux"}]
    allowed_digests = [args.image.split("@", 1)[1], *manifest_digests]
    capacity = free_capacity(args.node, json.loads(call(["get", "nodes", "-o", "json"]))["items"],
                             json.loads(call(["get", "pods", "-A", "-o", "json"]))["items"], args.gpus)
    save(args.output / "capacity-before.json", capacity)
    env = [{"name": "FS2_GROMACS_MPI_" + key, "value": str(value)} for key, value in
           (("NODES", 1), ("GPUS_PER_NODE", args.gpus), ("RANKS_PER_NODE", args.gpus), ("TOTAL_RANKS", args.gpus))] if mpi else []
    resources = {"cpu": str(8 * args.gpus), "memory": f"{16 * args.gpus}Gi", "nvidia.com/gpu": str(args.gpus), "ephemeral-storage": "16Gi"}
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": args.name, "namespace": NS, "labels": {"scientific-ai.nebius.com/task": TASK}},
           "spec": {"automountServiceAccountToken": False, "restartPolicy": "Never", "activeDeadlineSeconds": 2400,
                    "nodeSelector": {"kubernetes.io/hostname": args.node},
                    "tolerations": [{"key": "dedicated", "operator": "Equal", "value": "fs2-inference", "effect": "NoSchedule"}],
                    "securityContext": {"runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001},
                    "containers": [{"name": "runtime", "image": args.image, "command": ["sleep", "2300"], "env": env,
                                    "resources": {"requests": resources, "limits": resources},
                                    "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                                    "volumeMounts": [{"name": "workspace", "mountPath": "/mnt/fs2-scientific"}, {"name": "shm", "mountPath": "/dev/shm"}]}],
                    "volumes": [{"name": "workspace", "emptyDir": {"sizeLimit": "16Gi"}}, {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "2Gi"}}]}}
    save(args.output / "pod-request.json", pod)
    remote = "/mnt/fs2-scientific/probe"
    uid = None
    started = time.monotonic()
    record = {"image": args.image, "source_revision": args.source_revision, "node": args.node, "pod": args.name,
              "gpus": args.gpus, "customer_path_tested": False,
              "checkpoint_mode": "not exercised" if probe_only else "local-only",
              "input_sha256": sha(args.input / "input.tar.gz"), "request_sha256": sha(args.input / "request.json"),
              "started_at": datetime.now(timezone.utc).isoformat()}
    try:
        created = json.loads(call(["-n", NS, "create", "-f", "-", "-o", "json"], input=json.dumps(pod).encode()))
        uid = created["metadata"]["uid"]
        record["pod_uid"] = uid
        for _ in range(120):
            current = json.loads(call(["-n", NS, "get", "pod", args.name, "-o", "json"]))
            if any(c["type"] == "Ready" and c["status"] == "True" for c in current["status"].get("conditions", [])):
                break
            if current["status"].get("phase") in ("Failed", "Succeeded"):
                raise RuntimeError("qualification Pod terminated before startup")
            time.sleep(2)
        else:
            raise TimeoutError("candidate Pod not Ready in bounded startup window")
        record["ready_seconds"] = time.monotonic() - started
        record["image_id"] = current["status"]["containerStatuses"][0].get("imageID")
        record["allowed_image_digests"] = allowed_digests
        if not any(d in (record["image_id"] or "") for d in allowed_digests):
            raise ValueError("running image ID differs from pinned candidate")
        for label, command in (("gpu", ["nvidia-smi", "--query-gpu=uuid,name,driver_version,compute_cap,memory.total", "--format=csv"]),
                               ("topology", ["nvidia-smi", "topo", "-m"]), ("cpu", ["lscpu"])):
            (args.output / (label + ".txt")).write_bytes(call(["-n", NS, "exec", args.name, "--", *command]))
        executable = "/opt/gromacs-mpi/bin/gmx_mpi" if mpi else "gmx"
        help_result = subprocess.run(KUBE + ["-n", NS, "exec", args.name, "--", executable, "mdrun", "-h"],
                                     capture_output=True, check=True, timeout=60)
        (args.output / "native-mdrun-help.txt").write_bytes(help_result.stdout + help_result.stderr)
        if mpi:
            query_code = ("import ctypes,json; m=ctypes.CDLL('/opt/ompi/lib/libmpi.so'); "
                          "m.MPI_Init.argtypes=[ctypes.c_void_p,ctypes.c_void_p]; "
                          "r=m.MPI_Init(None,None); q=m.MPIX_Query_cuda_support(); "
                          "print(json.dumps({'MPI_Init_return':r,'MPIX_Query_cuda_support':q})); m.MPI_Finalize()")
            for label, command_ in (("ompi-info", ["/opt/ompi/bin/ompi_info", "--all", "--parsable"]),
                                    ("ucx-info", ["/opt/ucx/bin/ucx_info", "-v", "-d"]),
                                    ("gmx-ldd", ["ldd", executable]),
                                    ("mpi-cuda-query", ["/opt/ompi/bin/mpirun", "-np", "1", "--mca", "pml", "ucx",
                                                        "--mca", "pml_ucx_tls", "any", "--mca", "pml_ucx_devices", "any",
                                                        "-x", "UCX_TLS=self,sm,cuda_copy,cuda_ipc", "python3", "-c", query_code])):
                observed = subprocess.run(KUBE + ["-n", NS, "exec", args.name, "--", *command_],
                                          capture_output=True, timeout=60)
                (args.output / (label + ".txt")).write_bytes(observed.stdout + observed.stderr)
                record.setdefault("metadata_commands", []).append({"command": command_, "exit_code": observed.returncode,
                                                                    "sha256": sha(args.output / (label + ".txt"))})
        if probe_only:
            command = ["timeout", "90", "/opt/ompi/bin/mpirun", "-np", str(args.gpus),
                       "--mca", "pml", "ucx", "--mca", "pml_ucx_tls", "any",
                       "--mca", "pml_ucx_devices", "any", "-x", "UCX_TLS=self,sm,cuda_copy,cuda_ipc",
                       "/opt/fs2-cuda-aware/mpi-device-probe"]
            call(["-n", NS, "cp", args.name + ":/opt/fs2-cuda-aware/provenance", str(args.output / "build-provenance")])
            record["simulation_submitted"] = False
            record["referenced_fixture_not_executed"] = True
        else:
            call(["-n", NS, "cp", "--no-preserve", str(args.input), args.name + ":" + remote])
            command = ["python3", "-m", "fs2_gromacs.mpi" if mpi else "fs2_gromacs.worker", "--workspace", remote,
                       "--request", remote + "/request.json", "--job-id", request["jobs"][0]["id"],
                       "--operation-id", str(uuid.uuid4()), "--checkpoint-mode", "local"]
        record["worker_command"] = command
        before = time.monotonic()
        with (args.output / "worker.log").open("wb") as log:
            process = subprocess.run(KUBE + ["-n", NS, "exec", args.name, "--", *command], stdout=log, stderr=subprocess.STDOUT, timeout=1900)
        record["runtime_wall_seconds"] = time.monotonic() - before
        record["worker_exit_code"] = process.returncode
        if probe_only:
            record["validation"] = validate_device_probe((args.output / "worker.log").read_text(errors="replace"),
                                                         (args.output / "gpu.txt").read_text(), args.gpus, process.returncode)
        else:
            call(["-n", NS, "cp", args.name + ":" + remote, str(args.output / "workspace")])
            record["validation"] = validate(args.output / "workspace", request, args.gpus, mpi, args.expect_failure, process.returncode)
    except Exception as exc:
        record["qualification_error"] = str(exc)
        record.setdefault("validation", {"status": "failed", "scope": "qualification did not complete"})
    finally:
        if uid:
            current = json.loads(call(["-n", NS, "get", "pod", args.name, "-o", "json"]))
            save(args.output / "pod-final.json", current)
            if current["metadata"]["uid"] != uid or current["metadata"]["labels"].get("scientific-ai.nebius.com/task") != TASK:
                raise ValueError("refuse cleanup of changed/non-owned Pod")
            call(["-n", NS, "delete", "pod", args.name, "--wait=false"])
            for _ in range(30):
                exists = call(["-n", NS, "get", "pod", args.name, "--ignore-not-found", "-o", "name"])
                if not exists.strip():
                    record["cleanup"] = "owned Pod deleted and absence observed"
                    break
                time.sleep(2)
            else:
                record["cleanup"] = "deletion requested; disappearance unverified"
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        save(args.output / "receipt.json", record)
    print(json.dumps({"output": str(args.output), "image": args.image, "validation": record["validation"], "cleanup": record.get("cleanup"), "qualification_error": record.get("qualification_error")}))
    return 0 if record["validation"]["status"] == "passed" and "observed" in record.get("cleanup", "") else 1


def finalize_legacy(args):
    """Capture only owned JobSet identities; never persist its SSH seed/keys."""
    record = json.loads((args.output / "qualification.json").read_text())
    name = record["jobset"]
    if not name.startswith("fs2-gromacs-mpi-probe-20261003-"):
        raise ValueError("not this task's legacy qualification JobSet")
    owner = json.loads(call(["-n", NS, "get", "jobset", name, "-o", "json"]))
    if owner["metadata"]["labels"].get("scientific-ai.nebius.com/task") != "gromacs-r20260923":
        raise ValueError("legacy qualification owner label mismatch")
    pods = json.loads(call(["-n", NS, "get", "pods", "-l", "jobset.sigs.k8s.io/jobset-name=" + name, "-o", "json"]))["items"]
    expected = {r["pod"] for r in record["ranks"]}
    if {p["metadata"]["name"] for p in pods} != expected:
        raise ValueError("unexpected legacy Pod identity")
    identities = []
    for p in pods:
        pod_name = p["metadata"]["name"]
        environment = call(["-n", NS, "exec", pod_name, "--", "nvidia-smi", "--query-gpu=uuid,name,driver_version,compute_cap,memory.total", "--format=csv"])
        (args.output / (pod_name + "-gpu.txt")).write_bytes(environment)
        identities.append({"pod": pod_name, "uid": p["metadata"]["uid"], "node": p["spec"]["nodeName"],
                           "images": [{"name": c["name"], "image_id": c.get("imageID")} for c in p["status"].get("containerStatuses", [])]})
    request = json.loads((args.input / "request.json").read_text())
    validation = validate(args.output / "workspace", request, 2, True, False, record["worker_exit_code"])
    # Independent native checkpoint readback on the same exact candidate,
    # after simulation has finished. No GPU execution is needed for gmx dump.
    coordinator = next(r["pod"] for r in record["ranks"] if r["rank"] == 0)
    checkpoints = []
    for i in (1, 2, 3):
        relative = f"fs2-repeat-{i}.cpt"
        path = args.output / f"checkpoint-{i}-native-dump.txt"
        command = KUBE + ["-n", NS, "exec", coordinator, "--", "/opt/gromacs-mpi/bin/gmx_mpi", "dump", "-cp", "/mnt/fs2-scientific/probe/data/" + relative]
        with path.open("xb") as stream:
            result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, timeout=60)
        text = path.read_text()
        step = re.search(r"^\s*step\s*=\s*(\d+)", text, re.M)
        checkpoints.append({"path": relative, "checkpoint_sha256": sha(args.output / "workspace/data" / relative),
                            "dump_sha256": sha(path), "step": int(step[1]) if step else None, "exit_code": result.returncode})
    validation["independent_checkpoint_reads"] = checkpoints
    if any(c["step"] != 10000 or c["exit_code"] != 0 for c in checkpoints):
        validation["status"] = "failed"
        validation["errors"].append("independent checkpoint read failed")
    save(args.output / "final-state-validation.json", validation)
    # The helper's complete native workspaces remain local before release.
    call(["-n", NS, "delete", "jobset", name, "--wait=false"])
    cleanup = "deletion requested; disappearance unverified"
    for _ in range(30):
        remaining = json.loads(call(["-n", NS, "get", "pods", "-l", "jobset.sigs.k8s.io/jobset-name=" + name, "-o", "json"]))["items"]
        if not remaining:
            cleanup = "owned JobSet and Pods deleted; Pod absence observed"
            break
        time.sleep(2)
    supplement = {"source_revision": args.source_revision, "image": record["image"], "jobset_uid": owner["metadata"]["uid"],
                  "pods": identities, "cleanup": cleanup, "validation_sha256": sha(args.output / "final-state-validation.json"),
                  "input_sha256": sha(args.input / "input.tar.gz"), "request_sha256": sha(args.input / "request.json"),
                  "customer_path_tested": False}
    save(args.output / "identity-cleanup.json", supplement)
    print(json.dumps({"output": str(args.output), "validation_status": validation["status"], "cleanup": cleanup}))
    return 0 if validation["status"] == "passed" and "observed" in cleanup else 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--mpi-gpus", type=int, choices=(0, 1, 2, 4, 8), default=0)
    p.add_argument("--mpi-nodes", type=int, choices=(1, 2), default=1)
    p.add_argument("--diagnostic", action="store_true")
    p.add_argument("--parameters", type=Path, help="exact corrected saved campaign request/parameters; source bundle unchanged")
    p = sub.add_parser("finalize-legacy")
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--source-revision", required=True)
    p = sub.add_parser("run")
    for key in ("node", "name", "image", "source-revision"):
        p.add_argument("--" + key, required=True)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--gpus", type=int, choices=(1, 2, 4, 8), required=True)
    p.add_argument("--expect-failure", action="store_true")
    p.add_argument("--device-probe-only", action="store_true", help="test CUDA MPI buffers only; do not execute the referenced scientific fixture")
    p = sub.add_parser("validate")
    p.add_argument("--workspace", type=Path, required=True)
    p.add_argument("--request", type=Path, required=True)
    p.add_argument("--gpus", type=int, required=True)
    p.add_argument("--worker-exit", type=int, required=True)
    p.add_argument("--expect-failure", action="store_true")
    p.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.action == "prepare":
        make_fixture(args.source, args.output, args.mpi_gpus, args.diagnostic, args.parameters, args.mpi_nodes)
        return 0
    if args.action == "validate":
        request = json.loads(args.request.read_text())
        record = validate(args.workspace, request, args.gpus, "mpi-workflow" in request["schema"], args.expect_failure, args.worker_exit)
        save(args.output, record)
        print(json.dumps({"output": str(args.output), "status": record["status"], "errors": record["errors"]}))
        return 0 if record["status"] == "passed" else 1
    if args.action == "finalize-legacy":
        return finalize_legacy(args)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
