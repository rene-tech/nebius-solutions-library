"""Check downloaded native results, not only the outer succeeded operation."""

import argparse
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--parameters", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-frames", type=int)
    parser.add_argument("--expected-last-time-ps", type=float)
    args = parser.parse_args()
    status = json.loads((args.receipt / "status.json").read_text())
    assert (
        status["operation"]["status"] == "succeeded"
        and status["batch"]["result_published"]
    )
    receipt = json.loads((args.receipt / "receipt.json").read_text())
    assert receipt["state"] == "verified"
    manifest = json.loads((args.receipt / "output-manifest.json").read_text())
    by_digest, results = {}, []
    for index, item in enumerate(manifest["entries"]):
        source = args.receipt / f"output-{index:02d}.artifact"
        with source.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        assert (
            digest == item["artifact"]["sha256"]
            and source.stat().st_size == item["artifact"]["size_bytes"]
        )
        by_digest[digest] = source
        if item["semantic_type"] == "gromacs-workflow-result/v1":
            results.append(json.loads(source.read_text()))
    requested = {
        job["id"]: job for job in json.loads(args.parameters.read_text())["jobs"]
    }
    assert {result["job_id"] for result in results} == set(requested) and len(
        results
    ) == len(requested)
    verified = []
    for result in results:
        assert (
            result["operation_id"] == status["operation"]["id"]
            and result["status"] == "succeeded"
        )
        assert result["completed_steps"] == [
            step["id"] for step in requested[result["job_id"]]["steps"]
        ]
        assert result["gpu_snapshot_used"] is False
        assert all(command["exit_code"] == 0 for command in result["commands"])
        paths = {}
        for item in result["files"]:
            source = by_digest[item["sha256"]]
            assert (
                source.stat().st_size == item["size_bytes"]
                and item["path"] not in paths
            )
            paths[item["path"]] = source
        numeric_rows = 0
        for name, source in paths.items():
            if name.endswith(".xvg"):
                for line in source.read_text().splitlines():
                    if not line.strip() or line.lstrip().startswith(("#", "@", "&")):
                        continue
                    values = [float(value) for value in line.split()]
                    assert all(math.isfinite(value) for value in values), name
                    numeric_rows += 1
        trajectory_checks = [
            command
            for command in result["commands"]
            if command["command"][1] == "check"
        ]
        checked_trajectories = []
        for command in trajectory_checks:
            log = paths[command["log"]].read_text(errors="replace")
            if '-e' in command['command']:
                assert 'Checking energy file' in log and 'Found ' in log
                frames = re.findall(r"Last energy frame read\s+(\d+)\s+time\s+([\d.]+)", log)
            else:
                assert "Last frame" in log and "Item" in log and "Time" in log
                frames = re.findall(r"Last frame\s+(\d+)\s+time\s+([\d.]+)", log)
            assert frames, command["log"]
            count, last_time = int(frames[-1][0]) + 1, float(frames[-1][1])
            if args.expected_frames is not None:
                assert count == args.expected_frames, command["log"]
            if args.expected_last_time_ps is not None:
                assert math.isclose(last_time, args.expected_last_time_ps), command[
                    "log"
                ]
            checked_trajectories.append(
                {"log": command["log"], "frames": count, "last_time_ps": last_time}
            )
        verified.append(
            {
                "job_id": result["job_id"],
                "completed_steps": len(result["completed_steps"]),
                "native_commands": len(result["commands"]),
                "files_verified": len(paths),
                "max_file_bytes": max(item["size_bytes"] for item in result["files"]),
                "native_command_wall_seconds": sum(
                    item["wall_seconds"] for item in result["commands"]
                ),
                "checkpoint_generations": result["native_checkpoint_generation"],
                "numeric_analysis_rows": numeric_rows,
                "trajectory_checks": len(trajectory_checks),
                "checked_trajectories": checked_trajectories,
            }
        )
    operation = status["operation"]
    elapsed = (
        datetime.fromisoformat(operation["completed_at"])
        - datetime.fromisoformat(operation["accepted_at"])
    ).total_seconds()
    evidence = {
        "operation_id": operation["id"],
        "accepted_to_completed_seconds": elapsed,
        "jobs": verified,
        "convergence_claimed": False,
        "gpu_snapshot_used": False,
    }
    with os.fdopen(
        os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
    ) as output:
        json.dump(evidence, output, indent=2)
    print(json.dumps(evidence))


if __name__ == "__main__":
    main()
