"""Read-only phase timing capture for the two exact saved internal QA operations.

No environment variables, capabilities, Secrets or request bodies are recorded.
The accepted operation IDs come from verify_resume.py's private state receipt.
"""

import argparse
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--seconds", type=int, default=3600)
    parser.add_argument("--transfer-progress", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    records = []
    deadline = time.monotonic() + args.seconds
    args.output.parent.mkdir(parents=True, exist_ok=True)
    previous = {}
    while time.monotonic() < deadline:
        if args.state.exists():
            state = json.loads(args.state.read_text())
            for phase in ("source", "resume"):
                operation = state.get(phase + "_operation")
                if operation is None:
                    continue
                operation = str(UUID(operation))
                result = subprocess.run(
                    [
                        "kubectl",
                        "--context",
                        args.context,
                        "--request-timeout=15s",
                        "-n",
                        "fs2-models",
                        "get",
                        "pods",
                        "-l",
                        "fs2.nebius.ai/operation-id=" + operation,
                        "-o",
                        "json",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=25,
                    check=True,
                )
                pods = []
                for pod in json.loads(result.stdout)["items"]:
                    snapshot = {
                        "name": pod["metadata"]["name"],
                        "uid": pod["metadata"]["uid"],
                        "created_at": pod["metadata"]["creationTimestamp"],
                        "node": pod["spec"].get("nodeName"),
                        "status": pod["status"],
                        "containers": [
                            {
                                key: item.get(key)
                                for key in ("name", "image", "resources")
                            }
                            for item in [
                                *pod["spec"].get("initContainers", []),
                                *pod["spec"]["containers"],
                            ]
                        ],
                    }
                    if args.transfer_progress:
                        stage = next(
                            item
                            for item in pod["spec"]["containers"]
                            if item["name"] == "scientific-stage"
                        )
                        workdir = stage.get("workingDir", "")
                        running = any(
                            item["name"] == "artifact-collector"
                            and "running" in item.get("state", {})
                            for item in pod["status"].get("containerStatuses", [])
                        )
                        if running and workdir.startswith("/mnt/fs2-scientific/work/"):
                            progress = subprocess.run(
                                [
                                    "kubectl",
                                    "--context",
                                    args.context,
                                    "--request-timeout=10s",
                                    "-n",
                                    "fs2-models",
                                    "exec",
                                    pod["metadata"]["name"],
                                    "-c",
                                    "artifact-collector",
                                    "--",
                                    "python",
                                    "-c",
                                    "import json,pathlib,sys; p=pathlib.Path(sys.argv[1]); "
                                    "v=json.loads(p.read_text()) if p.is_file() else {}; "
                                    "print(json.dumps({k:v[k] for k in "
                                    "('action','generation','total_files','completed_files','phase','phase_seconds',"
                                    "'elapsed_seconds','cohort_phase_seconds','active_cohort_phases','max_parallel_cohorts') "
                                    "if k in v}))",
                                    workdir + "/.fs2/transfer-progress.json",
                                ],
                                capture_output=True,
                                text=True,
                                timeout=15,
                            )
                            if progress.returncode == 0:
                                snapshot["transfer_progress"] = json.loads(
                                    progress.stdout
                                )
                    pods.append(snapshot)
                if pods != previous.get(phase):
                    previous[phase] = pods
                    records.append(
                        {
                            "observed_at": datetime.now(UTC).isoformat(),
                            "phase": phase,
                            "operation_id": operation,
                            "pods": pods,
                        }
                    )
                    args.output.write_text(json.dumps(records, indent=2) + "\n")
        if (args.state.parent / "receipt.json").exists():
            break
        time.sleep(5)
    print(json.dumps({"phase_snapshots": len(records), "output": str(args.output)}))


if __name__ == "__main__":
    main()
