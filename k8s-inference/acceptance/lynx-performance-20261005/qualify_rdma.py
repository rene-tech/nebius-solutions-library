"""Exact-pool native RDMA proof; no API admission, driver changes or science."""

import argparse
import json
import re
import secrets
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gromacs-mpinat-20261003"))
import qualify_candidate as native
import qualify_cuda_two_node as two

POOL = "h100-reserved-8x"
CLUSTER = "computegpucluster-e00p8hjysxfyk1n58x"
NODES = {"computeinstance-e00s8g6t7z6qvz3f9p", "computeinstance-e00zgn138sxphp909c"}
TASK = "lynx-rdma-20261005"
HOST_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "models/molecular-dynamics/gromacs/runtime/rdma/host_collectives.c"
)
REQUEST = {
    "schema": "fs2-serve.nebius.ai/gromacs-mpi-workflow-request/v1",
    "nodes": 2,
    "gpus_per_node": 8,
    "threads": 1,
    "jobs": [
        {
            "id": "gang",
            "steps": [
                {"id": "probe", "command": "mdrun", "args": ["-s", "unused.tpr"]}
            ],
        }
    ],
}
# Native experiment only. Names/defaults are verified by the installed UCX
# 1.19.0 ucx_info -c and upstream rc/base + ud/base configuration tables.
# This does not alter host memlock, privilege or the published runtime recipe.
BOUNDED_UCX_QUEUES = {
    "UCX_RC_RX_QUEUE_LEN": "256",
    "UCX_RC_RX_BUFS_GROW": "64",
    "UCX_RC_TX_BUFS_GROW": "64",
    "UCX_UD_RX_QUEUE_LEN": "256",
    "UCX_UD_RX_QUEUE_LEN_INIT": "64",
    "UCX_UD_RX_BUFS_GROW": "64",
    "UCX_UD_TX_BUFS_GROW": "64",
}
PROBE_CODE = two.PROBE_CODE.replace(
    "request=normalize(json.loads(Path('/mnt/fs2-scientific/probe/request.json').read_text()),mpi=True)",
    "request=normalize(" + repr(REQUEST) + ",mpi=True)",
).replace(
    "configure_launcher(w,d,request['threads'],1)",
    "configure_launcher(w,d,request['threads'],8)",
)
INSPECT_CODE = r"""
import json,os,resource,subprocess
from pathlib import Path
from fs2_gromacs.mpi import rdma_devices
info={'uid':os.getuid(),'memlock':resource.getrlimit(resource.RLIMIT_MEMLOCK),'devices':rdma_devices(),
      'capabilities':[s for s in Path('/proc/self/status').read_text().splitlines() if s.startswith(('Cap','NoNewPrivs'))],
      'device_nodes':[str(p) for p in Path('/dev/infiniband').iterdir()],
      'counters':{str(p):p.read_text().strip() for p in Path('/sys/class/infiniband').glob('mlx5_*/ports/1/counters/port_*_data')}}
print(json.dumps(info),flush=True)
for argv in [['/opt/ucx/bin/ucx_info','-v'],['/opt/ucx/bin/ucx_info','-d'],['/opt/ucx/bin/ucx_info','-c']]:
 subprocess.run(argv,check=True)
"""


def manifest(
    name,
    image,
    nodes,
    seed,
    *,
    ipc_lock=False,
    file_ipc_lock=False,
    transport="ucx-rdma",
):
    if (
        set(nodes) != NODES
        or not name.startswith("fs2-lynx-rdma-")
        or not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", image)
        or transport not in {"ucx-rdma", "tcp-host-staged"}
        or (file_ipc_lock and transport != "ucx-rdma")
    ):
        raise ValueError("exact approved nodes, immutable image and task name required")
    spec = two.manifest(name, image, nodes, seed)
    spec["metadata"]["labels"]["scientific-ai.nebius.com/task"] = TASK
    pod = spec["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]
    pod["metadata"]["labels"]["scientific-ai.nebius.com/task"] = TASK
    pod["spec"]["nodeSelector"] = {
        "accelerator.fs2.nebius/pool-id": POOL,
        "topology.fs2.nebius/scope": "gpu_cluster",
        "topology.nebius.com/gpu-cluster-id": CLUSTER,
    }
    container = pod["spec"]["containers"][0]
    resources = {
        "cpu": "64",
        "memory": "128Gi",
        "ephemeral-storage": "16Gi",
        "nvidia.com/gpu": "8",
        "rdma.fs2.nebius/hca": "1",
    }
    container["resources"] = {"requests": resources, "limits": resources}
    if ipc_lock or file_ipc_lock:
        container["securityContext"]["capabilities"]["add"] = ["IPC_LOCK"]
    if file_ipc_lock:
        container["securityContext"]["allowPrivilegeEscalation"] = True
    container["env"].append({"name": "FS2_GROMACS_MPI_TRANSPORT", "value": transport})
    return spec


def validate_transport(log):
    # Configuration alone is not execution evidence: require UCX's endpoint
    # diagnostics plus the installed CUDA-buffer correctness probe's result.
    summaries = [
        json.loads(line)
        for line in log.splitlines()
        if line.startswith('{"kind":"summary"')
    ]
    if (
        len(summaries) != 1
        or summaries[0].get("status") != "passed"
        or summaries[0].get("ranks") != 16
    ):
        raise ValueError("sixteen-rank device-buffer proof did not pass")
    # UCX 1.19 prints protocol tables on first use, while older builds print
    # ep_cfg. Deinterleave by host/process before relating a CUDA table header
    # to its selected protocol; another rank's host-memory table is not proof.
    streams = {}
    for line in log.splitlines():
        match = re.match(r"^\[[^]]+\]\s+\[([^]]+)\]\s+(.*)$", line)
        if match:
            streams.setdefault(match[1], []).append(match[2])
    cuda_tables = []
    for lines in streams.values():
        for index, line in enumerate(lines):
            if "inter-node cfg#" in line and re.search(r"(?:from|into) cuda", line):
                table = [line]
                for following in lines[index + 1 : index + 12]:
                    if "ucp_context_" in following:
                        break
                    table.append(following)
                cuda_tables.append("\n".join(table))
    executed_rc = any("rc_mlx5/mlx5_" in table for table in cuda_tables)
    if not executed_rc and not re.search(r"ep_cfg\[\d+\].*rc_mlx5/mlx5_", log):
        raise ValueError("no executed accelerated verbs endpoint was recorded")
    if re.search(r"ep_cfg\[\d+\].*tcp/", log) or any(
        "tcp/" in table for table in cuda_tables
    ):
        raise ValueError("unexpected TCP MPI data endpoint")
    # Report GPUDirect only with a CUDA-memory protocol explicitly using
    # zero-copy verbs; a successful CUDA-aware MPI query alone is insufficient.
    direct = any("zero-copy" in table and "rc_mlx5/" in table for table in cuda_tables)
    return {
        "device_buffer_correctness": summaries[0],
        "transport_observed": "rc_mlx5",
        "tcp_mpi_data_fallback": False,
        "gpudirect_zero_copy_observed": direct,
        "cuda_inter_node_protocol_tables": len(cuda_tables),
        "proof_semantics": "passed actual cudaMalloc-buffer communication plus UCX first-use protocol diagnostics; not hardware traffic counters",
    }


def validate_rank_layout(log, *, rdma=True):
    bindings = [
        json.loads(line.removeprefix("FS2_MPI_RANK_BINDING "))
        for line in log.splitlines()
        if line.startswith("FS2_MPI_RANK_BINDING ")
    ]
    if len(bindings) != 16 or {b["world_rank"] for b in bindings} != set(range(16)):
        raise ValueError("missing sixteen-rank binding proof")
    hosts = {b["host"] for b in bindings}
    if len(hosts) != 2 or len({b["gpu_uuid"] for b in bindings}) != 16:
        raise ValueError("rank bindings do not cover two distinct eight-GPU hosts")
    if not rdma:
        return bindings
    for host in hosts:
        local = [b for b in bindings if b["host"] == host]
        if {b.get("ucx_net_device") for b in local} != {
            f"mlx5_{i}:1" for i in range(8)
        }:
            raise ValueError("rank bindings do not cover all eight HCAs on each host")
        if any(
            b.get("rdma_layout") != "one-topology-local-hca-per-rank" for b in local
        ):
            raise ValueError("rank bindings lack verified PCI-local topology")
    return bindings


def validate_host_collectives(log):
    records = [
        json.loads(line)
        for line in log.splitlines()
        if line.startswith('{"kind":"host-collective')
    ]
    summaries = [row for row in records if row["kind"] == "host-collectives-summary"]
    expected = {
        (name, size)
        for name in ("Bcast", "Scatterv", "Alltoall")
        for size in (9000000, 16777216, 33554432)
    }
    completed = [row for row in records if row["kind"] == "host-collective"]
    if (
        len(summaries) != 1
        or summaries[0].get("status") != "passed"
        or summaries[0].get("ranks") != 16
        or len(completed) != 9
        or {(row["collective"], row["message_bytes"]) for row in completed} != expected
        or any(row.get("status") != "passed" for row in completed)
    ):
        raise ValueError("large host-buffer collective proof is incomplete")
    return {
        "summary": summaries[0],
        "completed": completed,
        "semantics": "byte-validated host buffers; message_bytes is per destination for Scatterv/Alltoall; not a bandwidth benchmark",
    }


def validated_host_build(binary, receipt):
    proof = json.loads(receipt.read_text())
    if (
        proof.get("binary_sha256") != native.sha(binary)
        or proof.get("source_sha256") != native.sha(HOST_SOURCE)
        or set(proof.get("runtime_files", {}))
        != {"/opt/ompi/include/mpi.h", "/opt/ompi/lib/libmpi.so.40"}
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", proof.get("compiler_image", ""))
    ):
        raise ValueError(
            "host probe build does not bind this source, binary and exact MPI runtime"
        )
    return proof


def execute_lynx(pods, fixture, output):
    """Run the unchanged finite Lynx recipe only after device-buffer proof."""
    from native_probe import validate

    request = native.normalize(
        json.loads((fixture / "request.json").read_text()), mpi=True
    )
    if (request["nodes"], request.get("gpus_per_node", 1)) != (2, 8):
        raise ValueError("native RDMA science requires the exact two-by-eight fixture")
    remote = "/mnt/fs2-scientific/lynx-rdma"
    operation_id = str(uuid.uuid4())

    def execute(pod):
        name = pod["metadata"]["name"]
        rank = int(pod["metadata"]["labels"]["jobset.sigs.k8s.io/job-index"])
        native.call(
            ["-n", native.NS, "cp", "--no-preserve", str(fixture), name + ":" + remote]
        )
        with (output / f"native-{rank}.log").open("xb") as log:
            result = subprocess.run(
                native.KUBE
                + [
                    "-n",
                    native.NS,
                    "exec",
                    name,
                    "--",
                    "python3",
                    "-m",
                    "fs2_gromacs.mpi",
                    "--workspace",
                    remote,
                    "--request",
                    remote + "/request.json",
                    "--job-id",
                    "gang",
                    "--operation-id",
                    operation_id,
                    "--checkpoint-mode",
                    "local",
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=900,
                check=False,
            )
        if rank == 0:
            workspace = output / "workspace"
            workspace.mkdir()
            for relative in (
                "data",
                "result.json",
                ".fs2/mpi-topology.json",
                ".fs2/engine.json",
            ):
                native.call(
                    [
                        "-n",
                        native.NS,
                        "cp",
                        name + ":" + remote + "/" + relative,
                        str(workspace / Path(relative).name),
                    ]
                )
        return {"rank": rank, "worker_exit_code": result.returncode}

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(execute, pods))
    validation = validate(
        output / "workspace",
        request,
        16,
        True,
        False,
        max(item["worker_exit_code"] for item in outcomes),
    )
    return {
        "fixture": str(fixture),
        "request_sha256": native.sha(fixture / "request.json"),
        "input_sha256": native.sha(fixture / "input.tar.gz"),
        "native_operation_id": operation_id,
        "rank_outcomes": outcomes,
        "runtime_wall_seconds": time.monotonic() - started,
        "validation": validation,
    }


def run(args):
    transport = getattr(args, "transport", "ucx-rdma")
    host_collectives = getattr(args, "host_collectives", False)
    file_ipc_lock = getattr(args, "file_ipc_lock", False)
    if transport != "ucx-rdma" and (not host_collectives or args.input):
        raise ValueError(
            "TCP is allowed only as an explicit host-collective control, never RDMA fallback"
        )
    host_build = None
    if getattr(args, "host_binary", None):
        if not host_collectives or not args.host_build:
            raise ValueError("external host probe requires its exact build receipt")
        host_build = validated_host_build(args.host_binary, args.host_build)
    args.output.mkdir(parents=True, exist_ok=False)
    call = native.call
    nodes = sorted(NODES)
    inventory = json.loads(call(["get", "nodes", "-o", "json"]))["items"]
    pods = json.loads(call(["get", "pods", "-A", "-o", "json"]))["items"]
    capacity = [native.free_capacity(node, inventory, pods, 8) for node in nodes]
    for node in inventory:
        if (
            node["metadata"]["name"] in NODES
            and node["status"]["allocatable"].get("rdma.fs2.nebius/hca") != "1"
        ):
            raise ValueError("missing exclusive RDMA bundle")
    for pod in pods:
        if (
            pod["spec"].get("nodeName") in NODES
            and pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
            and any(
                c.get("resources", {}).get("requests", {}).get("rdma.fs2.nebius/hca")
                for c in pod["spec"]["containers"]
            )
        ):
            raise ValueError("RDMA bundle is already in use")
    native.save(args.output / "capacity-before.json", capacity)
    index = json.loads(
        subprocess.check_output(["crane", "manifest", args.image], timeout=60)
    )
    native.save(args.output / "registry-index.json", index)
    allowed = {
        args.image.split("@", 1)[1],
        *[
            m["digest"]
            for m in index.get("manifests", [])
            if m.get("platform") == {"architecture": "amd64", "os": "linux"}
        ],
    }
    spec = manifest(
        args.name,
        args.image,
        nodes,
        secrets.token_hex(32),
        ipc_lock=args.ipc_lock,
        file_ipc_lock=file_ipc_lock,
        transport=transport,
    )
    sanitized = json.loads(json.dumps(spec))
    for env in sanitized["spec"]["replicatedJobs"][0]["template"]["spec"]["template"][
        "spec"
    ]["containers"][0]["env"]:
        if env["name"] == "FS2_MPI_SSH_SEED":
            env["value"] = "REDACTED_ATTEMPT_SECRET"
    native.save(args.output / "jobset-request-redacted.json", sanitized)
    record = {
        "image": args.image,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "native_only": True,
        "source_sha256": native.sha(__file__),
        "science_executed": False,
        "added_capabilities": ["IPC_LOCK"] if args.ipc_lock or file_ipc_lock else [],
        "file_capability_executables": file_ipc_lock,
        "transport_requested": transport,
        "host_collective_control": host_collectives,
    }
    if host_build:
        record["host_build"] = host_build
    uid = None
    try:
        call(
            ["-n", native.NS, "create", "--dry-run=server", "-f", "-"],
            input=json.dumps(spec).encode(),
        )
        created = json.loads(
            call(
                ["-n", native.NS, "create", "-f", "-", "-o", "json"],
                input=json.dumps(spec).encode(),
            )
        )
        uid = created["metadata"]["uid"]
        record.update(jobset=args.name, jobset_uid=uid)
        deadline = time.monotonic() + 480
        while time.monotonic() < deadline:
            pods = json.loads(
                call(
                    [
                        "-n",
                        native.NS,
                        "get",
                        "pods",
                        "-l",
                        "jobset.sigs.k8s.io/jobset-name=" + args.name,
                        "-o",
                        "json",
                    ]
                )
            )["items"]
            if len(pods) == 2 and all(
                any(
                    c["type"] == "Ready" and c["status"] == "True"
                    for c in p["status"].get("conditions", [])
                )
                for p in pods
            ):
                break
            time.sleep(2)
        else:
            raise TimeoutError("bounded RDMA Pod startup expired")
        if {p["spec"]["nodeName"] for p in pods} != NODES:
            raise ValueError("unexpected node placement")
        if any(
            not any(
                d in p["status"]["containerStatuses"][0]["imageID"] for d in allowed
            )
            for p in pods
        ):
            raise ValueError("observed runtime differs from the immutable candidate")
        record["pods"] = [
            {
                "name": p["metadata"]["name"],
                "uid": p["metadata"]["uid"],
                "node": p["spec"]["nodeName"],
                "image_id": p["status"]["containerStatuses"][0]["imageID"],
            }
            for p in pods
        ]
        native.save(args.output / "pods.json", pods)

        probe_code = PROBE_CODE
        if file_ipc_lock:
            probe_code = probe_code.replace(
                "/opt/fs2-cuda-aware/mpi-device-probe",
                "/opt/fs2-rdma/bin/mpi-device-probe",
            )
        if args.bounded_ucx_queues:
            probe_code = probe_code.replace(
                "['/opt/fs2-cuda-aware/mpi-device-probe']",
                repr(
                    [
                        "env",
                        *[
                            f"{key}={value}"
                            for key, value in BOUNDED_UCX_QUEUES.items()
                        ],
                        "/opt/fs2-cuda-aware/mpi-device-probe",
                    ]
                ),
            )
            record["experimental_ucx_overrides"] = BOUNDED_UCX_QUEUES

        def execute(pod):
            name = pod["metadata"]["name"]
            rank = int(pod["metadata"]["labels"]["jobset.sigs.k8s.io/job-index"])
            phases = [
                ("before", INSPECT_CODE),
                ("buffer", probe_code),
            ]
            if file_ipc_lock:
                phases.insert(
                    1,
                    (
                        "memlock",
                        "import subprocess;subprocess.run(['/opt/fs2-rdma/bin/memlock-probe'],check=True)",
                    ),
                )
            if host_collectives:
                source = HOST_SOURCE
                call(
                    [
                        "-n",
                        native.NS,
                        "cp",
                        str(source),
                        name + ":/mnt/fs2-scientific/host-collectives.c",
                    ]
                )
                if file_ipc_lock:
                    observed = call(
                        [
                            "-n",
                            native.NS,
                            "exec",
                            name,
                            "--",
                            "sha256sum",
                            "/opt/fs2-rdma/source/host_collectives.c",
                            "/opt/fs2-rdma/bin/host-collectives",
                        ]
                    ).decode()
                    actual = {
                        line.split()[1]: line.split()[0]
                        for line in observed.splitlines()
                    }
                    if actual["/opt/fs2-rdma/source/host_collectives.c"] != native.sha(
                        source
                    ):
                        raise ValueError(
                            "installed host collective probe source differs"
                        )
                    native.save(args.output / f"host-build-{rank}.json", actual)
                elif host_build:
                    observed = call(
                        [
                            "-n",
                            native.NS,
                            "exec",
                            name,
                            "--",
                            "sha256sum",
                            *host_build["runtime_files"],
                        ]
                    ).decode()
                    actual = {
                        line.split()[1]: line.split()[0]
                        for line in observed.splitlines()
                    }
                    if actual != host_build["runtime_files"]:
                        raise ValueError(
                            "host collective binary was not built against this exact MPI installation"
                        )
                    call(
                        [
                            "-n",
                            native.NS,
                            "cp",
                            str(args.host_binary),
                            name + ":/mnt/fs2-scientific/host-collectives",
                        ]
                    )
                    observed = (
                        call(
                            [
                                "-n",
                                native.NS,
                                "exec",
                                name,
                                "--",
                                "sha256sum",
                                "/mnt/fs2-scientific/host-collectives",
                            ]
                        )
                        .decode()
                        .split()[0]
                    )
                    if observed != host_build["binary_sha256"]:
                        raise ValueError("staged host collective binary differs")
                    native.save(
                        args.output / f"host-build-{rank}.json",
                        {"runtime_files": actual, "binary_sha256": observed},
                    )
                else:
                    compile_host_probe(name, args.output / f"host-compile-{rank}.log")
                host_code = (
                    probe_code.replace("/buffer-probe", "/host-probe")
                    .replace(
                        "/opt/fs2-rdma/bin/mpi-device-probe"
                        if file_ipc_lock
                        else "/opt/fs2-cuda-aware/mpi-device-probe",
                        "/opt/fs2-rdma/bin/host-collectives"
                        if file_ipc_lock
                        else "/mnt/fs2-scientific/host-collectives",
                    )
                    .replace("timeout=90", "timeout=150")
                    .replace("exceeded 90", "exceeded 150")
                )
                phases.append(("host", host_code))
            phases.append(("after", INSPECT_CODE))
            for phase, code in phases:
                with (args.output / f"{phase}-{rank}.log").open("xb") as log:
                    result = subprocess.run(
                        native.KUBE
                        + ["-n", native.NS, "exec", name, "--", "python3", "-c", code],
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        timeout=240,
                        check=False,
                    )
                if result.returncode:
                    return {
                        "rank": rank,
                        "phase": phase,
                        "exit_code": result.returncode,
                    }
            return {"rank": rank, "exit_code": 0}

        def compile_host_probe(name, logfile):
            with logfile.open("xb") as log:
                subprocess.run(
                    native.KUBE
                    + [
                        "-n",
                        native.NS,
                        "exec",
                        name,
                        "--",
                        "/opt/ompi/bin/mpicc",
                        "-O2",
                        "-Wall",
                        "-Wextra",
                        "-Werror",
                        "/mnt/fs2-scientific/host-collectives.c",
                        "-o",
                        "/mnt/fs2-scientific/host-collectives",
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=60,
                    check=True,
                )

        with ThreadPoolExecutor(max_workers=2) as executor:
            record["rank_outcomes"] = list(executor.map(execute, pods))
        if any(x["exit_code"] for x in record["rank_outcomes"]):
            raise ValueError("native device/verbs probe failed; retained per-rank logs")
        buffer_log = (args.output / "buffer-0.log").read_text()
        if transport == "ucx-rdma":
            record["transport_proof"] = validate_transport(buffer_log)
        else:
            summaries = [
                json.loads(line)
                for line in buffer_log.splitlines()
                if line.startswith('{"kind":"summary"')
            ]
            if (
                len(summaries) != 1
                or summaries[0].get("status") != "passed"
                or summaries[0].get("ranks") != 16
            ):
                raise ValueError("explicit TCP control device-buffer proof failed")
            record["transport_proof"] = {
                "device_buffer_correctness": summaries[0],
                "transport_requested": transport,
                "rdma_claim": False,
            }
        record["rank_bindings"] = validate_rank_layout(
            buffer_log, rdma=transport == "ucx-rdma"
        )
        if host_collectives:
            record["host_collectives"] = validate_host_collectives(
                (args.output / "host-0.log").read_text()
            )
            record["host_collective_source_sha256"] = native.sha(HOST_SOURCE)
        if args.input:
            record["science_executed"] = True
            record["native_md"] = execute_lynx(pods, args.input, args.output)
            if record["native_md"]["validation"]["status"] != "passed":
                raise ValueError("exact-input finite native validation failed")
        record["status"] = "passed"
    except (ValueError, OSError, TimeoutError, subprocess.SubprocessError) as exc:
        record.update(status="failed", error=str(exc))
    finally:
        if uid:
            current = json.loads(
                call(["-n", native.NS, "get", "jobset", args.name, "-o", "json"])
            )
            if (
                current["metadata"]["uid"] != uid
                or current["metadata"]["labels"].get("scientific-ai.nebius.com/task")
                != TASK
            ):
                raise ValueError("refuse changed/unowned JobSet cleanup")
            call(["-n", native.NS, "delete", "jobset", args.name, "--wait=false"])
            for _ in range(30):
                remaining = json.loads(
                    call(
                        [
                            "-n",
                            native.NS,
                            "get",
                            "pods",
                            "-l",
                            "jobset.sigs.k8s.io/jobset-name=" + args.name,
                            "-o",
                            "json",
                        ]
                    )
                )["items"]
                if not remaining:
                    record["cleanup"] = "owned JobSet/Pods deleted; absence observed"
                    break
                time.sleep(2)
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        native.save(args.output / "receipt.json", record)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "status": record["status"],
                "transport_proof": record.get("transport_proof"),
                "native_md_status": record.get("native_md", {})
                .get("validation", {})
                .get("status"),
                "error": record.get("error"),
                "cleanup": record.get("cleanup"),
            }
        )
    )
    return 0 if record["status"] == "passed" and record.get("cleanup") else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ipc-lock", action="store_true")
    parser.add_argument("--file-ipc-lock", action="store_true")
    parser.add_argument("--bounded-ucx-queues", action="store_true")
    parser.add_argument("--host-collectives", action="store_true")
    parser.add_argument("--host-binary", type=Path)
    parser.add_argument("--host-build", type=Path)
    parser.add_argument(
        "--transport", choices=["ucx-rdma", "tcp-host-staged"], default="ucx-rdma"
    )
    parser.add_argument(
        "--input", type=Path, help="Optional exact two-by-eight Lynx finite fixture"
    )
    raise SystemExit(run(parser.parse_args()))
