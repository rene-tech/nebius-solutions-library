"""Stage changed native MD inputs on every rank, not growing trajectories.

Preparation runs once on rank zero. GROMACS validates input filenames on all
ranks before broadcasting simulation state, even with -noappend. Each transfer
is streamed, content-checked, and atomically installed before MPI is launched.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

from .contracts import relative_path
from .files import digest_file

# File-input switches from this pinned engine's `gmx_mpi mdrun -h`.
# -multidir and -plumed are not supported by this execution shape.
INPUT_FLAGS = frozenset(
    {
        "-s",
        "-cpi",
        "-table",
        "-tablep",
        "-tableb",
        "-rerun",
        "-ei",
        "-awh",
        "-membed",
        "-mp",
        "-mn",
    }
)


def md_inputs(data: Path, cwd: Path, args: list[str]) -> list[Path]:
    names = []
    for index, token in enumerate(args):
        if token not in INPUT_FLAGS:
            continue
        following = args[index + 1 :]
        selected = []
        for value in following:
            if value.startswith("-"):
                break
            selected.append(value)
            if token != "-tableb":
                break
        if not selected:
            raise ValueError(f"MPI file input {token} requires an explicit filename")
        names.extend(selected)
    if "-s" not in args:
        names.append("topol.tpr")
    paths = []
    for name in names:
        relative_path(name)
        path = cwd / name
        if (
            path.is_symlink()
            or not path.is_file()
            or data.resolve() not in path.resolve().parents
        ):
            raise ValueError("MPI input must be a contained regular workspace file")
        if path not in paths:
            paths.append(path)
    return paths


def receive(root: Path, name: str, size: int, sha256: str, stream) -> None:
    relative_path(name)
    path = root / name
    if size < 0 or root.resolve() not in path.resolve().parents or path.is_symlink():
        raise ValueError("Invalid MPI input destination")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".mpi-transfer")
    if temporary.exists() or temporary.is_symlink():
        raise ValueError("Another MPI input transfer is already active")
    digest = hashlib.sha256()
    try:
        with temporary.open("xb") as output:
            remaining = size
            while remaining:
                block = stream.read(min(4 * 1024 * 1024, remaining))
                if not block:
                    raise ValueError("MPI input transfer was truncated")
                output.write(block)
                digest.update(block)
                remaining -= len(block)
            if stream.read(1) or digest.hexdigest() != sha256:
                raise ValueError("MPI input transfer content differs from its manifest")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class InputStager:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.known: dict[tuple[str, str], str] = {}

    def stage(self, cwd: Path, args: list[str], *, timeout: float) -> dict:
        from .mpi import peers, ssh_options

        started = time.monotonic()
        data = self.workspace / "data"
        files = [
            (path, digest_file(path), path.stat().st_size)
            for path in md_inputs(data, cwd, args)
        ]
        directory = self.workspace / ".fs2" / "mpi"

        def send(host):
            count = total = 0
            for path, digest, size in files:
                name = str(path.relative_to(data))
                if self.known.get((host, name)) == digest:
                    continue
                command = [
                    "env",
                    "PYTHONPATH=/opt/fs2/gromacs",
                    "python3",
                    "-m",
                    "fs2_gromacs.mpi_files",
                    "--root",
                    str(data),
                    "--receive",
                    name,
                    "--size",
                    str(size),
                    "--sha256",
                    digest,
                ]
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError("MPI input staging exceeded the workflow budget")
                with path.open("rb") as source:
                    subprocess.run(
                        [*ssh_options(directory), f"fs2@{host}", shlex.join(command)],
                        stdin=source,
                        check=True,
                        timeout=remaining,
                    )
                self.known[(host, name)] = digest
                count += 1
                total += size
            return {
                "host": host,
                "files_transferred": count,
                "bytes_transferred": total,
            }

        with ThreadPoolExecutor(max_workers=min(7, len(peers()) - 1)) as executor:
            transfers = list(executor.map(send, peers()[1:]))
        return {"wall_seconds": time.monotonic() - started, "peers": transfers}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--receive", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--size", type=int, required=True)
    args = parser.parse_args()
    receive(args.root, args.receive, args.size, args.sha256, sys.stdin.buffer)


if __name__ == "__main__":
    main()
