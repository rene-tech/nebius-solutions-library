"""Compare two independent processes with/without MPS in a task-owned GPU Pod.

Identical TPR copies are performance fixtures, NOT independent scientific
replicas. Never use this script on a GPU shared with customer processes.
No GPU compute mode, MIG partition, driver or cluster resource is changed.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

GMX = "/usr/local/gromacs/avx2_256/bin/gmx"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tpr", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    mps = shutil.which("nvidia-cuda-mps-control")
    if not mps:
        raise RuntimeError("this image does not contain the MPS control binary")
    env = {
        **os.environ,
        "OMP_NUM_THREADS": "4",
        "CUDA_MPS_PIPE_DIRECTORY": str(args.output / "mps-pipes"),
        "CUDA_MPS_LOG_DIRECTORY": str(args.output / "mps-logs"),
    }
    for name in ("mps-pipes", "mps-logs"):
        (args.output / name).mkdir()
    records = []
    for mode in ("no-mps", "mps"):
        if mode == "mps":
            subprocess.run([mps, "-d"], env=env, check=True)
        try:
            for repeat in range(3):
                start, children, handles = time.monotonic(), [], []
                for index in range(2):
                    directory = args.output / f"{mode}-{repeat + 1}-{index + 1}"
                    directory.mkdir()
                    log = (directory / "command.log").open("wb")
                    handles.append(log)
                    children.append(
                        (
                            directory,
                            subprocess.Popen(
                                [
                                    GMX,
                                    "mdrun",
                                    "-s",
                                    str(args.tpr.resolve()),
                                    "-deffnm",
                                    "md",
                                    "-ntmpi",
                                    "1",
                                    "-ntomp",
                                    "4",
                                    "-pin",
                                    "off",
                                ],
                                env=env,
                                cwd=directory,
                                stdout=log,
                                stderr=subprocess.STDOUT,
                            ),
                        )
                    )
                outcomes = []
                try:
                    for directory, child in children:
                        code = child.wait(timeout=300)
                        performance = re.findall(
                            r"Performance:\s+([0-9.eE+-]+)",
                            (directory / "command.log").read_text(errors="replace"),
                        )
                        outcomes.append(
                            {
                                "exit_code": code,
                                "ns_per_day": float(performance[-1])
                                if performance
                                else None,
                            }
                        )
                finally:
                    for _, child in children:
                        if child.poll() is None:
                            child.terminate()
                            child.wait(timeout=30)
                    for handle in handles:
                        handle.close()
                wall = time.monotonic() - start
                record = {
                    "mode": mode,
                    "repetition": repeat + 1,
                    "processes": 2,
                    "threads_each": 4,
                    "wall_seconds": wall,
                    "simulation_ns_total": 0.8,
                    "end_to_end_aggregate_ns_per_day": 0.8 * 86400 / wall,
                    "outcomes": outcomes,
                }
                records.append(record)
                (args.output / "measurements.json").write_text(
                    json.dumps(records, indent=2) + "\n"
                )
                print(json.dumps(record), flush=True)
        finally:
            if mode == "mps":
                subprocess.run([mps], input=b"quit\n", env=env, check=True, timeout=30)


if __name__ == "__main__":
    main()
