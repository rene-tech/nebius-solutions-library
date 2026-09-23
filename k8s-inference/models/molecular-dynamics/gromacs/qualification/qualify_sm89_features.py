"""Run retained extension fixtures through the ordinary native worker.

Use inside a task-owned candidate Pod. This validates local segmented native
recovery and output integrity, not hosted admission or customer-bucket export.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def numeric_rows(path):
    rows = [
        [float(value) for value in line.split()]
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "@", "&"))
    ]
    assert rows and all(math.isfinite(value) for row in rows for value in row), path
    assert all(a[0] <= b[0] for a, b in zip(rows, rows[1:])), path
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fixture = json.loads((args.fixture / "fixture.json").read_text())
    assert digest(args.fixture / "input.tar.gz") == fixture["input_sha256"]
    shutil.copytree(args.fixture, args.output)
    request = json.loads((args.output / "request.json").read_text())
    job = request["jobs"][0]
    assert len(request["jobs"]) == 1
    started = time.monotonic()
    with (args.output / "worker.log").open("wb") as log:
        process = subprocess.run(
            ["python3", "-m", "fs2_gromacs.worker", "--workspace", str(args.output.resolve()),
             "--request", str((args.output / "request.json").resolve()), "--job-id", job["id"],
             "--operation-id", str(uuid.uuid4()), "--checkpoint-mode", "local"],
            stdout=log, stderr=subprocess.STDOUT, timeout=600,
        )
    elapsed = time.monotonic() - started
    result = json.loads((args.output / "result.json").read_text())
    assert process.returncode == 0 and result["status"] == "succeeded", result.get("error")
    assert result["completed_steps"] == [step["id"] for step in job["steps"]]
    assert result["native_checkpoint_generation"] >= 3
    assert all(command["exit_code"] == 0 for command in result["commands"])
    for entry in result["files"]:
        path = args.output / "data" / entry["path"]
        assert path.stat().st_size == entry["size_bytes"] and digest(path) == entry["sha256"], path
    gyration = numeric_rows(args.output / "data/gyration.xvg")
    assert len(gyration) == 101 and math.isclose(gyration[-1][0], 200)
    checks = [command for command in result["commands"] if command["command"][1] == "check"]
    assert checks
    for command in checks:
        log = (args.output / "data" / command["log"]).read_text()
        frames = re.findall(r"Last frame\s+(\d+)\s+time\s+([\d.]+)", log)
        assert frames and int(frames[-1][0]) == 100 and float(frames[-1][1]) == 200
    biases = ([args.output / "data/HILLS", args.output / "data/COLVAR"]
              if fixture["extension"] == "plumed"
              else sorted((args.output / "data").glob("*.colvars.traj")))
    assert biases
    bias_summary = []
    for path in biases:
        rows = numeric_rows(path)
        bias_summary.append({"file": path.name, "rows": len(rows), "first": rows[0][0], "last": rows[-1][0]})
        if fixture["extension"] == "plumed":
            assert rows[0][0] <= 2.0001 and math.isclose(rows[-1][0], 200, abs_tol=0.001)
            assert len(rows) >= 100
    receipt = {
        "extension": fixture["extension"], "input_sha256": fixture["input_sha256"],
        "status": "passed", "wrapper_wall_seconds": elapsed,
        "native_command_wall_seconds": sum(command["wall_seconds"] for command in result["commands"]),
        "engine_id": result["engine_id"], "verified_files": len(result["files"]),
        "checkpoint_generations": result["native_checkpoint_generation"],
        "trajectory_frames": 101, "final_time_ps": 200, "finite_analysis_rows": len(gyration),
        "bias": bias_summary, "customer_path_tested": False,
    }
    (args.output / "qualification.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
