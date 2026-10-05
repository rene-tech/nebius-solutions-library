"""Finite, exact-input Lynx controls; no uploads, submissions or physics edits."""

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "models/molecular-dynamics/gromacs/runtime"))
from fs2_gromacs.contracts import normalize  # noqa: E402

TPR_SHA256 = "e2ee73571f0dd9855709d2a957e41e5ad52316b3f1d2b1808e65476f4bd8ef10"
ATOMS = 185486
DT_PS = 0.002


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


def parameters(*, steps=50000, repetitions=3, threads=8, mpi=False, nodes=1,
               gpus_per_node=1, bonded="auto", pme="auto", update="auto",
               pin="auto", nstlist=None, segment_minutes=60):
    if type(steps) is not int or not 10000 <= steps <= 2000000:
        raise ValueError("Use an explicit finite 10000..2000000-step benchmark")
    if type(repetitions) is not int or not 1 <= repetitions <= 3:
        raise ValueError("Screen one to three repeats; confirmations require three")
    if bonded not in ("auto", "cpu", "gpu") or pme not in ("auto", "cpu", "gpu"):
        raise ValueError("Unknown native task placement")
    if pin not in ("auto", "on", "off") or update not in ("auto", "cpu", "gpu"):
        raise ValueError("Unknown pin/update mode")
    if nodes * gpus_per_node > 1 and update != "cpu":
        raise ValueError("This TPR lacks consecutive update groups: distributed update must use CPU")
    if not mpi and (nodes, gpus_per_node) != (1, 1):
        raise ValueError("Multiple GPUs require the external-MPI contract")
    if nstlist not in (None, 100, 200, 300):
        raise ValueError("Only the explicitly reviewed neighbor-list screens are allowed")
    commands = [{"id": "finite-tpr", "command": "convert-tpr",
                 "args": ["-s", "original.tpr", "-o", "benchmark.tpr", "-nsteps", str(steps)],
                 "expected_outputs": ["benchmark.tpr"]}]
    for repeat in range(1, repetitions + 1):
        argv = ["-s", "benchmark.tpr", "-deffnm", f"repeat{repeat}",
                "-nb", "gpu", "-bonded", bonded, "-pme", pme, "-update", update, "-pin", pin]
        if pme != "auto":
            argv += ["-pmefft", pme, "-notunepme", "-npme",
                     "1" if pme == "gpu" and nodes * gpus_per_node > 1 else "0"]
        if nstlist is not None:
            argv += ["-nstlist", str(nstlist)]
        commands += [{"id": f"repeat-{repeat}", "command": "mdrun", "args": argv},
                     {"id": f"join-energy-{repeat}", "command": "eneconv",
                      "args": ["-f", {"files": f"repeat{repeat}.part*.edr"}, "-o", f"energy{repeat}.edr"],
                      "expected_outputs": [f"energy{repeat}.edr"]},
                     {"id": f"energy-{repeat}", "command": "energy",
                      "args": ["-f", f"energy{repeat}.edr", "-o", f"energy{repeat}.xvg"],
                      "stdin": "Potential\nKinetic-En.\nTotal-Energy\nTemperature\nPressure\n0\n",
                      "expected_outputs": [f"energy{repeat}.xvg"]}]
        if steps >= 500000:
            # The immutable source emits an XTC frame every 500000 steps.
            # Longer confirmations retain that cadence and ask the native
            # decoder to read the real first trajectory part, not a mock file.
            commands.append({"id": f"trajectory-check-{repeat}", "command": "check",
                             "args": ["-f", f"repeat{repeat}.part0001.xtc"]})
    value = {"schema": "fs2-serve.nebius.ai/gromacs-workflow-request/v1",
             "jobs": [{"id": "benchmark", "steps": commands}], "threads": threads,
             "checkpoint_minutes": 5, "segment_minutes": segment_minutes,
             "max_wall_seconds": 5400, "max_output_bytes": 4 * 1024**3,
             "output_destination": "customer-bucket",
             "output_prefix": "runs/fs2-lynx-performance-20261005"}
    if mpi:
        value.update(schema="fs2-serve.nebius.ai/gromacs-mpi-workflow-request/v1",
                     nodes=nodes, gpus_per_node=gpus_per_node)
        value["jobs"][0]["id"] = "gang"
    return normalize(value, mpi=mpi)


def prepare(tpr, output, **options):
    if sha(tpr) != TPR_SHA256:
        raise ValueError("Not the approved immutable Lynx TPR")
    request = parameters(**options)
    output.mkdir(parents=True, exist_ok=False)
    data = Path(tpr).read_bytes()
    memory = io.BytesIO()
    with tarfile.open(fileobj=memory, mode="w") as archive:
        info = tarfile.TarInfo("original.tpr")
        info.size, info.mode, info.mtime = len(data), 0o600, 0
        archive.addfile(info, io.BytesIO(data))
    (output / "input.tar.gz").write_bytes(gzip.compress(memory.getvalue(), mtime=0))
    save(output / "request.json", request)
    save(output / "fixture.json", {"schema": "lynx-exact-input-benchmark/v1", "tpr_sha256": TPR_SHA256,
         "atoms": ATOMS, "dt_ps": DT_PS, "steps": options.get("steps", 50000),
         "repetitions": options.get("repetitions", 3), "options": options,
         "input_sha256": sha(output / "input.tar.gz"), "request_sha256": sha(output / "request.json"),
         "source_tpr_path": str(Path(tpr).resolve()), "operator_copy_read_only": True,
         "warmup": "Included; no forced counter reset. Each repeat starts from the same input state.",
         "scientific_change": "Only finite nsteps in derived TPR; force field, timestep, constraints, PME accuracy, thermostat, barostat and output cadence retained.",
         "neighbor_list_note": "An explicit nstlist changes list construction only; require original positive Verlet-buffer tolerance and retain automatic buffer proof before acceptance.",
         "claim": "Performance repetitions, not independent scientific replicas or ensemble convergence."})


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tpr", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--steps", type=int, default=50000)
    p.add_argument("--repetitions", type=int, default=3)
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--mpi", action="store_true")
    p.add_argument("--nodes", type=int, default=1)
    p.add_argument("--gpus-per-node", type=int, default=1)
    for name in ("bonded", "pme", "update"):
        p.add_argument("--" + name, choices=("auto", "cpu", "gpu"), default="auto")
    p.add_argument("--pin", choices=("auto", "on", "off"), default="auto")
    p.add_argument("--nstlist", type=int)
    p.add_argument("--segment-minutes", type=float, default=60)
    args = vars(p.parse_args())
    prepare(args.pop("tpr"), args.pop("output"), **args)


if __name__ == "__main__":
    main()
