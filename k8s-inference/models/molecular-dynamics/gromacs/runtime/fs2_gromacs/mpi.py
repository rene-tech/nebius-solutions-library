"""JobSet-local Open MPI launch and rank lifecycle; no new job scheduler.

The controller supplies a fresh attempt-scoped SSH seed and fixed JobSet DNS
names. MPI processes have no platform/bucket credential. Only rank zero publishes native
checkpoints and results. SSH is internal to the gang, never a customer endpoint.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from .contracts import normalize
from .files import atomic_json, extract_inputs


def rank() -> int:
    return int(os.environ["FS2_MPI_RANK"])


def peers() -> list[str]:
    values = os.environ["FS2_MPI_HOSTS"].split(",")
    if not 2 <= len(values) <= 8 or len(set(values)) != len(values):
        raise ValueError("MPI requires two to eight distinct fixed JobSet peer names")
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


def configure_launcher(workspace: Path, directory: Path, threads: int) -> None:
    hosts = peers()
    hostfile = directory / "hosts"
    hostfile.write_text("".join(f"{host} slots=1\n" for host in hosts))
    # Open MPI parses its agent into argv; the path is platform-generated and
    # contains no shell interpolation. Workload inputs never choose the agent.
    os.environ["PRTE_MCA_plm_ssh_agent"] = " ".join(ssh_options(directory))
    # Only the coordinator has a client private key. Do not ask remote peers
    # to fan out SSH launches when gangs grow beyond two ranks.
    os.environ["PRTE_MCA_plm_ssh_no_tree_spawn"] = "1"
    os.environ["FS2_GROMACS_MPI_HOSTFILE"] = str(hostfile)
    os.environ["FS2_GROMACS_MPI_RANKS"] = str(len(hosts))
    # Staged TCP is the portable correctness baseline. RDMA/CUDA-aware direct
    # communication must be separately qualified for the selected node pool.
    os.environ.setdefault("OMPI_MCA_pml", "ob1")
    os.environ.setdefault("OMPI_MCA_btl", "self,tcp")
    os.environ.setdefault("GMX_DISABLE_DIRECT_GPU_COMM", "1")
    os.environ["OMP_NUM_THREADS"] = str(threads)
    deadline = time.monotonic() + 300
    for host in hosts:
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
            "ranks": len(hosts),
            "hosts": hosts,
            "rank_per_node": 1,
            "transport": "tcp-host-staged",
            "threads_per_rank": threads,
        },
    )


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
    if request["nodes"] != len(peers()):
        raise ValueError("Requested nodes differ from the admitted JobSet")
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
            from .worker import Workflow

            (directory / "ready").touch()
            configure_launcher(args.workspace, directory, request["threads"])
            worker = Workflow(
                request,
                job_id=args.job_id,
                operation_id=args.operation_id,
                workspace=args.workspace,
                checkpoint_mode=args.checkpoint_mode,
                mpi=True,
            )
            signal.signal(signal.SIGTERM, worker.stop)
            signal.signal(signal.SIGINT, worker.stop)
            result = worker.run()
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
            code = (
                0
                if result["status"] == "succeeded"
                else 143
                if result["status"] == "interrupted"
                else 1
            )
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


if __name__ == "__main__":
    main()
