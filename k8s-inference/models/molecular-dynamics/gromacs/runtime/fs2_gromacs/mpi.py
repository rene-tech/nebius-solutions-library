"""JobSet-local Open MPI launch and rank lifecycle; no new job scheduler.

The controller supplies a fresh attempt-scoped SSH seed and fixed JobSet DNS
names for multi-node runs. Single-node runs do not start SSH. Each Pod owns one
node's GPU allocation and starts one MPI process per GPU. MPI processes have no
platform/bucket credential. Only rank zero publishes native checkpoints and
results. SSH is internal to the gang, never a customer endpoint.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path

from .contracts import normalize
from .files import atomic_json, extract_inputs

# Bounds initial allocation for the 16-rank/two-node profile. UCX 1.19 otherwise
# grows RX by at least 1024 elements even when
# RX_QUEUE_LEN is smaller (uct/ib/base/ib_iface.c). Explicit growth avoids that
# initial 10-38 MiB pool. Larger real host collectives still require the
# RDMA-only IPC_LOCK executables below; no host limits are changed. These are
# growth increments, not hard MAX_BUFS caps that could deadlock RC progress.
RDMA_UCX_SETTINGS = {
    "UCX_RC_RX_QUEUE_LEN": "256",
    "UCX_RC_RX_BUFS_GROW": "64",
    "UCX_RC_TX_BUFS_GROW": "64",
    "UCX_UD_RX_QUEUE_LEN": "256",
    "UCX_UD_RX_QUEUE_LEN_INIT": "64",
    "UCX_UD_RX_BUFS_GROW": "64",
    "UCX_UD_TX_BUFS_GROW": "64",
}
RDMA_BINARY = "/opt/fs2-rdma/bin/gmx_mpi"


def check_rdma_memlock() -> dict:
    result = subprocess.run(
        ["/opt/fs2-rdma/bin/memlock-probe"], check=True, capture_output=True,
        text=True, timeout=15,
    )
    proof = json.loads(result.stdout)
    if (proof.get("status") != "passed" or proof.get("uid") != 10001
            or proof.get("euid") != 10001 or proof.get("cap_effective") != "4000"
            or proof.get("cap_permitted") != "4000" or proof.get("cap_bounding") != "4000"
            or proof.get("no_new_privs") != 0 or proof.get("locked_bytes") != 64 * 1024 * 1024):
        raise ValueError("RDMA requires the qualified non-root IPC_LOCK capability and real memlock proof")
    return proof


def workflow_binary() -> str | None:
    # Keep the original unprivileged path for every older local/TCP shape.
    return RDMA_BINARY if os.environ.get("FS2_GROMACS_MPI_TRANSPORT") == "ucx-rdma" else None


def rank() -> int:
    """Controller Pod/node index, not the MPI process rank within a node."""
    return int(os.environ["FS2_MPI_RANK"])


def peers() -> list[str]:
    values = os.environ["FS2_MPI_HOSTS"].split(",")
    if not 1 <= len(values) <= 8 or len(set(values)) != len(values):
        raise ValueError("MPI requires one to eight distinct fixed peer names")
    import re

    if any(not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", value) for value in values):
        raise ValueError("MPI peer names must be controller-provided DNS names")
    if not 0 <= rank() < len(values):
        raise ValueError("MPI rank is outside its frozen gang")
    return values


def prepare_keys(workspace: Path, *, authorized_directory: Path | None = None) -> Path:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    hosts = peers()
    seed = bytes.fromhex(os.environ.pop("FS2_MPI_SSH_SEED"))
    if len(seed) != 32:
        raise ValueError("Invalid attempt SSH seed")
    directory = workspace / ".fs2" / "mpi"
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory.chmod(0o700)

    def key(label: str):
        return Ed25519PrivateKey.from_private_bytes(
            hashlib.sha256(seed + label.encode()).digest()
        )

    def private(name: str, value):
        path = directory / name
        with path.open("wb") as handle:
            path.chmod(0o600)
            handle.write(
                value.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.OpenSSH,
                    serialization.NoEncryption(),
                )
            )

    def public(value) -> str:
        return (
            value.public_key()
            .public_bytes(
                serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
            )
            .decode()
        )

    private("host", key(f"host:{rank()}"))
    if rank() == 0:
        private("client", key("client"))
    # Kubernetes fsGroup makes the workspace volume's ancestor writable by its
    # group. OpenSSH StrictModes rightly rejects an authorized_keys file under
    # that ancestor. Keep the public authorization file in the image user's
    # private home, without changing workspace permissions or SSH checking.
    authorized_directory = authorized_directory or Path.home() / ".ssh"
    authorized_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    authorized_directory.chmod(0o700)
    authorized_keys = authorized_directory / "authorized_keys"
    authorized_keys.write_text(public(key("client")) + "\n")
    authorized_keys.chmod(0o600)
    (directory / "known_hosts").write_text(
        "".join(
            f"[{host}]:2222 {public(key(f'host:{index}'))}\n"
            for index, host in enumerate(hosts)
        )
    )
    (directory / "sshd_config").write_text(
        f"Port 2222\nHostKey {directory}/host\nPidFile {directory}/sshd.pid\n"
        f"AuthorizedKeysFile {authorized_keys}\n"
        "PasswordAuthentication no\nKbdInteractiveAuthentication no\nUsePAM no\n"
        "StrictModes yes\nAllowUsers fs2\nAllowTcpForwarding no\nX11Forwarding no\n"
        "PermitTTY no\nLogLevel ERROR\n"
    )
    return directory


def ssh_options(directory: Path) -> list[str]:
    return [
        "ssh",
        "-p",
        "2222",
        "-i",
        str(directory / "client"),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={directory}/known_hosts",
        "-o",
        "LogLevel=ERROR",
    ]


def validate_shape(request: dict) -> None:
    """Do not launch a different process shape than the controller reserved."""
    nodes, gpus = request["nodes"], request.get("gpus_per_node", 1)
    expected = {
        "FS2_GROMACS_MPI_NODES": nodes,
        "FS2_GROMACS_MPI_GPUS_PER_NODE": gpus,
        "FS2_GROMACS_MPI_RANKS_PER_NODE": gpus,
        "FS2_GROMACS_MPI_TOTAL_RANKS": nodes * gpus,
    }
    for name, count in expected.items():
        if name in os.environ and os.environ[name] != str(count):
            raise ValueError(f"{name} differs from the frozen request shape")
    if nodes != len(peers()):
        raise ValueError("Requested nodes differ from the admitted peer set")


def rdma_devices(
    sysfs: Path = Path("/sys/class/infiniband"),
    devfs: Path = Path("/dev/infiniband"),
) -> list[str]:
    """Fail closed on the qualified full-node bundle, not host sysfs alone."""
    devices = sorted(sysfs.glob("mlx5_*"))
    verbs = sorted(devfs.glob("uverbs*"))
    if len(devices) != 8 or len(verbs) != 8 or any(
        not os.access(path, os.R_OK | os.W_OK) for path in verbs
    ):
        raise ValueError("RDMA requires eight accessible HCA character devices from its reserved bundle")
    names = []
    for device in devices:
        port = device / "ports" / "1"
        if (port / "link_layer").read_text().strip() != "InfiniBand" or not (
            port / "state"
        ).read_text().strip().startswith("4:"):
            raise ValueError("RDMA requires eight active InfiniBand ports")
        names.append(f"{device.name}:1")
    return names


def configure_transport(nodes: int) -> dict:
    """Bounded operator policy; never label a TCP job as RDMA or CUDA-aware."""
    transport = os.environ.get(
        "FS2_GROMACS_MPI_TRANSPORT", "ucx-local" if nodes == 1 else "tcp-host-staged"
    )
    memlock_proof = None
    for name in ("UCX_NET_DEVICES", "UCX_IB_GPU_DIRECT_RDMA", "UCX_PROTO_INFO", *RDMA_UCX_SETTINGS):
        os.environ.pop(name, None)
    if transport == "ucx-local":
        if nodes != 1:
            raise ValueError("ucx-local is only supported within one admitted node")
        os.environ["OMPI_MCA_pml"] = "ucx"
        # UCX normally prefers RDMA devices when selected by Open MPI. Explicit
        # any permits the CUDA IPC/shared-memory path on nodes without RDMA.
        os.environ["OMPI_MCA_pml_ucx_tls"] = "any"
        os.environ["OMPI_MCA_pml_ucx_devices"] = "any"
        os.environ["UCX_TLS"] = "self,sm,cuda_copy,cuda_ipc"
        os.environ.pop("OMPI_MCA_btl", None)
        os.environ.pop("GMX_DISABLE_DIRECT_GPU_COMM", None)
    elif transport == "ucx-rdma":
        if nodes != 2:
            raise ValueError("ucx-rdma currently requires the qualified two-node gang")
        devices = rdma_devices()
        memlock_proof = check_rdma_memlock()
        os.environ["OMPI_MCA_pml"] = "ucx"
        os.environ["OMPI_MCA_pml_ucx_tls"] = "rc_mlx5"
        os.environ["OMPI_MCA_pml_ucx_devices"] = "mlx5_*"
        # rc_x includes the UD bootstrap required by RC, but no TCP fallback.
        # CUDA transports are mandatory for correct device-pointer detection.
        os.environ["UCX_TLS"] = "rc_x,self,sm,cuda_copy,cuda_ipc"
        os.environ["UCX_NET_DEVICES"] = ",".join(devices)
        os.environ["UCX_IB_GPU_DIRECT_RDMA"] = "yes"
        os.environ["UCX_LOG_LEVEL"] = "info"
        os.environ["UCX_PROTO_INFO"] = "y"
        os.environ.update(RDMA_UCX_SETTINGS)
        os.environ.pop("OMPI_MCA_btl", None)
        os.environ.pop("GMX_DISABLE_DIRECT_GPU_COMM", None)
    elif transport == "tcp-host-staged":
        os.environ["OMPI_MCA_pml"] = "ob1"
        os.environ["OMPI_MCA_btl"] = "self,sm,tcp"
        os.environ["GMX_DISABLE_DIRECT_GPU_COMM"] = "1"
        for name in ("OMPI_MCA_pml_ucx_tls", "OMPI_MCA_pml_ucx_devices", "UCX_TLS"):
            os.environ.pop(name, None)
    else:
        raise ValueError("Unsupported operator MPI transport profile")
    os.environ["FS2_GROMACS_MPI_TRANSPORT"] = transport
    return {
        "transport": transport,
        "transport_observed": None,
        "rdma": None if transport == "ucx-rdma" else False,
        "rdma_requested": transport == "ucx-rdma",
        "direct_gpu_communication": "autodetect"
        if transport in {"ucx-local", "ucx-rdma"}
        else "disabled",
        "ucx_tls": os.environ.get("UCX_TLS"),
        "ucx_net_devices": os.environ.get("UCX_NET_DEVICES"),
        "ucx_queue_settings": RDMA_UCX_SETTINGS if transport == "ucx-rdma" else None,
        "memlock_proof": memlock_proof,
    }


def configure_launcher(
    workspace: Path, directory: Path, threads: int, gpus_per_node: int = 1
) -> None:
    hosts = peers()
    directory.mkdir(parents=True, exist_ok=True)
    hostfile = directory / "hosts"
    hostfile.write_text("".join(f"{host} slots={gpus_per_node}\n" for host in hosts))
    # Open MPI parses its agent into argv; the path is platform-generated and
    # contains no shell interpolation. Workload inputs never choose the agent.
    if len(hosts) > 1:
        os.environ["PRTE_MCA_plm_ssh_agent"] = " ".join(ssh_options(directory))
    # Only the coordinator has a client private key. Do not ask remote peers
    # to fan out SSH launches when gangs grow beyond two ranks.
    os.environ["PRTE_MCA_plm_ssh_no_tree_spawn"] = "1"
    os.environ["FS2_GROMACS_MPI_HOSTFILE"] = str(hostfile)
    os.environ["FS2_GROMACS_MPI_RANKS"] = str(len(hosts) * gpus_per_node)
    transport = configure_transport(len(hosts))
    os.environ["OMP_NUM_THREADS"] = str(threads)
    deadline = time.monotonic() + 300
    for host in hosts if len(hosts) > 1 else []:
        while True:
            result = subprocess.run(
                [
                    *ssh_options(directory),
                    f"fs2@{host}",
                    "test",
                    "-f",
                    str(directory / "ready"),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            if result.returncode == 0:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "MPI peer startup timed out; no scientific result was published"
                )
            time.sleep(1)
    atomic_json(
        workspace / ".fs2" / "mpi-topology.json",
        {
            "ranks": len(hosts) * gpus_per_node,
            "nodes": len(hosts),
            "hosts": hosts,
            "rank_per_node": gpus_per_node,
            "gpus_per_node": gpus_per_node,
            "gpu_count": len(hosts) * gpus_per_node,
            "gpu_binding": "one-GMX_GPU_ID-per-local-rank",
            "cpu_binding": "unbound-within-admitted-pod-cpuset",
            **transport,
            "threads_per_rank": threads,
        },
    )


def launch_command(request: dict, command: list[str]) -> list[str]:
    gpus = request.get("gpus_per_node", 1)
    arguments = [
        "mpirun",
        "--prefix",
        "/opt/ompi",
        "--hostfile",
        os.environ["FS2_GROMACS_MPI_HOSTFILE"],
        "-np",
        str(request["nodes"] * gpus),
        "--map-by",
        f"ppr:{gpus}:node",
        "--bind-to",
        "none",
    ]
    for name in (
        "OMP_NUM_THREADS",
        "LD_LIBRARY_PATH",
        "PATH",
        "PYTHONPATH",
        "OMPI_MCA_pml",
        "OMPI_MCA_btl",
        "OMPI_MCA_pml_ucx_tls",
        "OMPI_MCA_pml_ucx_devices",
        "UCX_TLS",
        "UCX_NET_DEVICES",
        "UCX_IB_GPU_DIRECT_RDMA",
        "UCX_LOG_LEVEL",
        "UCX_PROTO_INFO",
        *RDMA_UCX_SETTINGS,
        "FS2_GROMACS_MPI_TRANSPORT",
        "GMX_DISABLE_DIRECT_GPU_COMM",
    ):
        if name in os.environ:
            arguments.extend(("-x", name))
    # Device visibility is deliberately NOT forwarded from rank zero. Each
    # Pod may own entirely different UUIDs/ordinals; bind on its own host.
    arguments.extend(
        [
            "python3",
            "-m",
            "fs2_gromacs.mpi_rank",
            "--nodes",
            str(request["nodes"]),
            "--gpus-per-node",
            str(gpus),
            "--",
            *command,
        ]
    )
    return arguments


def peer_complete(workspace: Path, status: str) -> None:
    if status not in {"succeeded", "failed", "interrupted"}:
        raise ValueError("Unknown MPI terminal status")
    atomic_json(workspace / ".fs2" / "mpi-peer-complete.json", {"status": status})


def wait_peer(workspace: Path, deadline_seconds: int) -> int:
    path = workspace / ".fs2" / "mpi-peer-complete.json"
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if path.is_file():
            status = json.loads(path.read_text())["status"]
            return 0 if status == "succeeded" else 143 if status == "interrupted" else 1
        time.sleep(0.5)
    raise TimeoutError("MPI coordinator did not finish within the attempt budget")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--job-id", default="gang")
    parser.add_argument("--operation-id")
    parser.add_argument(
        "--checkpoint-mode", choices=("local", "companion"), default="companion"
    )
    parser.add_argument("--peer-collector", action="store_true")
    parser.add_argument("--collector-command", nargs=argparse.REMAINDER)
    parser.add_argument(
        "--mark-complete", choices=("succeeded", "failed", "interrupted")
    )
    args = parser.parse_args()
    if args.mark_complete:
        peer_complete(args.workspace, args.mark_complete)
        return
    if args.peer_collector:
        if rank() == 0:
            if not args.collector_command:
                parser.error("Rank zero requires the original collector command")
            os.execvp(args.collector_command[0], args.collector_command)
        raise SystemExit(wait_peer(args.workspace, 259800))
    if args.request is None or args.operation_id is None:
        parser.error("MPI workflows require request and operation ID")
    request = normalize(json.loads(args.request.read_text()), mpi=True)
    if request["nodes"] == 1:
        os.environ.setdefault("FS2_MPI_RANK", "0")
        os.environ.setdefault("FS2_MPI_HOSTS", "localhost")
    validate_shape(request)
    if request["nodes"] == 1:
        # No SSH identity/server is needed for a one-Pod MPI application.
        configure_launcher(
            args.workspace,
            args.workspace / ".fs2" / "mpi",
            request["threads"],
            request.get("gpus_per_node", 1),
        )
        result = run_coordinator(args, request)
        print(
            json.dumps(
                {
                    key: result[key]
                    for key in ("operation_id", "job_id", "status", "error")
                }
            )
        )
        raise SystemExit(result_exit_code(result))
    directory = prepare_keys(args.workspace)
    ssh_log = (directory / "sshd.log").open("wb")
    server = subprocess.Popen(
        ["/usr/sbin/sshd", "-D", "-e", "-f", str(directory / "sshd_config")],
        stdout=ssh_log,
        stderr=subprocess.STDOUT,
    )
    try:
        if rank() != 0:
            data = args.workspace / "data"
            if not data.exists():
                extract_inputs(
                    args.workspace / "input.tar.gz",
                    data,
                    max_bytes=request["max_output_bytes"],
                )
            for job in request["jobs"]:
                for step in job["steps"]:
                    (data / step["directory"]).mkdir(parents=True, exist_ok=True)
            (directory / "ready").touch()
            code = wait_peer(args.workspace, request["max_wall_seconds"] + 600)
        else:
            (directory / "ready").touch()
            configure_launcher(
                args.workspace,
                directory,
                request["threads"],
                request.get("gpus_per_node", 1),
            )
            result = run_coordinator(args, request)
            for host in peers()[1:]:
                subprocess.run(
                    [
                        *ssh_options(directory),
                        f"fs2@{host}",
                        "env",
                        "PYTHONPATH=/opt/fs2/gromacs",
                        "python3",
                        "-m",
                        "fs2_gromacs.mpi",
                        "--workspace",
                        str(args.workspace),
                        "--mark-complete",
                        result["status"],
                    ],
                    check=True,
                    timeout=15,
                )
            code = result_exit_code(result)
            print(
                json.dumps(
                    {
                        key: result[key]
                        for key in ("operation_id", "job_id", "status", "error")
                    }
                )
            )
    finally:
        server.terminate()
        server.wait(timeout=10)
        ssh_log.close()
    raise SystemExit(code)


def run_coordinator(args, request):
    from .worker import Workflow, report_time_limit

    worker = Workflow(
        request,
        job_id=args.job_id,
        operation_id=args.operation_id,
        workspace=args.workspace,
        checkpoint_mode=args.checkpoint_mode,
        mpi=True,
        gmx=workflow_binary(),
    )
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    result = worker.run()
    report_time_limit(result)
    return result


def result_exit_code(result):
    return (
        0
        if result["status"] == "succeeded"
        else 143
        if result["status"] == "interrupted"
        else 1
    )


if __name__ == "__main__":
    main()
