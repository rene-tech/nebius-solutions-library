"""Matched native-engine screening, run inside a task-owned GPU Pod.

Uses one identical TPR for every layout and repetition. ns/day is engine steady
throughput, not request throughput or cold-start latency. Preserve every log,
coordinate, trajectory, energy and checkpoint for subsequent validation.
"""

import argparse
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

GMX = "/usr/local/gromacs/avx2_256/bin/gmx"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tpr", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    source = args.tpr.resolve()
    tpr = args.output.resolve() / "benchmark.tpr"
    subprocess.run([GMX, "convert-tpr", "-s", str(source), "-o", str(tpr), "-nsteps", "200000"], check=True)
    fingerprint = hashlib.sha256(tpr.read_bytes()).hexdigest()
    (args.output / "version.txt").write_bytes(subprocess.check_output([GMX, "--version"], stderr=subprocess.STDOUT))
    (args.output / "gpu.csv").write_bytes(subprocess.check_output([
        "nvidia-smi", "--query-gpu=name,uuid,driver_version,memory.total", "--format=csv",
    ]))
    records = []
    for layout, options in (
        ("automatic", []),
        ("gpu-resident", ["-nb", "gpu", "-pme", "gpu", "-bonded", "gpu", "-update", "gpu"]),
    ):
        for repetition in range(args.repetitions):
            directory = args.output / f"{layout}-{repetition + 1}"
            directory.mkdir()
            argv = [GMX, "mdrun", "-s", str(tpr), "-deffnm", "md", "-ntmpi", "1", "-ntomp", "8", *options]
            with (directory / "gpu-samples.csv").open("wb") as samples, (directory / "command.log").open("wb") as log:
                monitor = subprocess.Popen(["nvidia-smi", "--query-gpu=timestamp,utilization.gpu,utilization.memory,memory.used,power.draw",
                                            "--format=csv", "--loop-ms=200"], stdout=samples, stderr=subprocess.STDOUT)
                started = time.monotonic()
                try:
                    process = subprocess.run(argv, cwd=directory, stdout=log, stderr=subprocess.STDOUT, timeout=300)
                finally:
                    monitor.terminate()
                    monitor.wait(timeout=10)
                wall = time.monotonic() - started
            text = (directory / "command.log").read_text(errors="replace")
            performance = re.findall(r"Performance:\s+([0-9.eE+-]+)", text)
            record = {"layout": layout, "repetition": repetition + 1, "argv": argv, "tpr_sha256": fingerprint,
                      "exit_code": process.returncode, "process_wall_seconds": wall,
                      "ns_per_day": float(performance[-1]) if performance else None}
            records.append(record)
            (args.output / "measurements.json").write_text(json.dumps(records, indent=2) + "\n")
            print(json.dumps(record), flush=True)


if __name__ == "__main__":
    main()
