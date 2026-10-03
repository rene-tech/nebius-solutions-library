"""Bind one externally launched MPI rank to one GPU in its admitted Pod.

CUDA driver enumeration respects the Pod's device visibility. A host-global
nvidia-smi index, or the coordinator's GPU UUID, must never be forwarded to a
different Pod. Resolve the local visible ordinal and UUID here, then use the
documented per-rank GMX_GPU_ID selection. Keep other admitted GPUs visible to
UCX/CUDA IPC. This is execution placement, not a protocol change.
"""

from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import json
import os
import socket
import uuid

RECEIPT_PREFIX = "FS2_MPI_RANK_BINDING "


def visible_gpu_uuids() -> list[str]:
    cuda = ctypes.CDLL("libcuda.so.1")

    def checked(name, *arguments):
        code = getattr(cuda, name)(*arguments)
        if code != 0:
            raise RuntimeError(
                f"CUDA allocation inspection {name} failed with code {code}"
            )

    checked("cuInit", ctypes.c_uint(0))
    count = ctypes.c_int()
    checked("cuDeviceGetCount", ctypes.byref(count))
    if not 1 <= count.value <= 8:
        raise ValueError("MPI Pod must expose one to eight CUDA devices")
    values = []
    function = (
        "cuDeviceGetUuid_v2"
        if hasattr(cuda, "cuDeviceGetUuid_v2")
        else "cuDeviceGetUuid"
    )
    for index in range(count.value):
        device = ctypes.c_int()
        checked("cuDeviceGet", ctypes.byref(device), ctypes.c_int(index))
        identifier = (ctypes.c_ubyte * 16)()
        checked(function, ctypes.byref(identifier), device)
        values.append("GPU-" + str(uuid.UUID(bytes=bytes(identifier))))
    if len(set(values)) != len(values):
        raise ValueError("CUDA allocation contains duplicate GPU identities")
    return values


def bind_rank(*, nodes: int, gpus_per_node: int) -> dict:
    local_rank = int(os.environ["OMPI_COMM_WORLD_LOCAL_RANK"])
    local_size = int(os.environ["OMPI_COMM_WORLD_LOCAL_SIZE"])
    world_rank = int(os.environ["OMPI_COMM_WORLD_RANK"])
    world_size = int(os.environ["OMPI_COMM_WORLD_SIZE"])
    if (
        local_size != gpus_per_node
        or world_size != nodes * gpus_per_node
        or not 0 <= local_rank < local_size
        or not 0 <= world_rank < world_size
    ):
        raise ValueError("MPI process topology differs from the admitted GPU shape")
    visible = visible_gpu_uuids()
    if len(visible) != gpus_per_node:
        raise ValueError(
            "Visible CUDA device count differs from the admitted GPUs per node"
        )
    selected = visible[local_rank]
    # GROMACS explicitly supports different GMX_GPU_ID values on MPI ranks.
    # Do not hide peer GPUs from UCX/CUDA IPC by narrowing CUDA_VISIBLE_DEVICES.
    os.environ["GMX_GPU_ID"] = str(local_rank)
    os.environ.pop("GMX_GPUTASKS", None)
    return {
        "schema": "fs2-serve.nebius.ai/gromacs-mpi-rank-binding/v1",
        "world_rank": world_rank,
        "world_size": world_size,
        "local_rank": local_rank,
        "local_size": local_size,
        "host": socket.gethostname(),
        "gpu_uuid": selected,
        "visible_gpu_count_before_binding": len(visible),
        "gromacs_gpu_id": local_rank,
        "gpu_binding": "GMX_GPU_ID",
        "cpu_affinity": sorted(os.sched_getaffinity(0)),
        "threads_per_rank": int(os.environ["OMP_NUM_THREADS"]),
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def read_rank_bindings(log, *, expected_ranks: int) -> dict:
    # Each rank emits this before exec; startup receipts precede engine output.
    # Keep the full log as evidence, but never materialize long native logs.
    with log.open("rb") as handle:
        text = handle.read(512 * 1024).decode(errors="replace")
    bindings = []
    for line in text.splitlines():
        if not line.startswith(RECEIPT_PREFIX):
            continue
        try:
            value = json.loads(line[len(RECEIPT_PREFIX) :])
            if (
                isinstance(value, dict)
                and value.get("schema")
                == "fs2-serve.nebius.ai/gromacs-mpi-rank-binding/v1"
                and type(value.get("world_rank")) is int
                and value.get("world_size") == expected_ranks
                and isinstance(value.get("gpu_uuid"), str)
                and isinstance(value.get("host"), str)
            ):
                bindings.append(value)
        except (ValueError, TypeError):
            continue
        if len(bindings) >= 16:
            break
    ranks = {item.get("world_rank") for item in bindings}
    complete = len(bindings) == expected_ranks and ranks == set(range(expected_ranks))
    if complete:
        complete = len({item.get("gpu_uuid") for item in bindings}) == expected_ranks
    return {"rank_bindings": bindings, "rank_binding_evidence_complete": complete}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", required=True, type=int, choices=range(1, 9))
    parser.add_argument(
        "--gpus-per-node", required=True, type=int, choices=(1, 2, 4, 8)
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or args.nodes * args.gpus_per_node > 16:
        parser.error(
            "A native command and an admitted shape of at most sixteen GPUs are required"
        )
    receipt = bind_rank(nodes=args.nodes, gpus_per_node=args.gpus_per_node)
    # One bounded write keeps per-rank startup evidence readable in mpirun logs.
    os.write(
        1, (RECEIPT_PREFIX + json.dumps(receipt, separators=(",", ":")) + "\n").encode()
    )
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    main()
