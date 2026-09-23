"""Run all independent repetitions sequentially on one allocated GPU.

Use inside the task-owned worker. Retains every result and 1 s GPU telemetry.
This is single-trajectory throughput; MPS is never started or inferred.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import time

from fs2_namd.worker import Workflow


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--job-id", action="append", help="Select existing job IDs without changing the normalized request or recipe")
    parser.add_argument("--append", action="store_true", help="Add new job IDs to an existing campaign; existing workspaces are never overwritten")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=args.append)
    request = json.loads((args.fixture / "request.json").read_text())
    output = json.loads((args.output / "campaign.json").read_text()) if args.append and (args.output / "campaign.json").exists() else []
    if args.job_id and set(args.job_id) - {job["id"] for job in request["jobs"]}:
        raise ValueError("requested qualification job ID is not in the fixture")
    for job in request["jobs"]:
        if args.job_id and job["id"] not in args.job_id:
            continue
        work = args.output / job["id"]
        work.mkdir()
        shutil.copyfile(args.fixture / "input.tar.gz", work / "input.tar.gz")
        gpu_file = args.output / (job["id"] + "-gpu.csv")
        with gpu_file.open("w") as gpu_log:
            sampler = subprocess.Popen(["nvidia-smi", "--query-gpu=timestamp,name,uuid,driver_version,utilization.gpu,utilization.memory,memory.used,power.draw,clocks.sm,clocks.mem", "--format=csv", "-l", "1"], stdout=gpu_log, stderr=subprocess.STDOUT)
            started = time.monotonic()
            try:
                result = Workflow(request, job_id=job["id"], operation_id=args.operation,
                                  workspace=work, checkpoint_mode="local").run()
            finally:
                sampler.terminate()
                sampler.wait(timeout=10)
        receipt = {"job": job["id"], "status": result["status"], "error": result["error"],
                   "wall_seconds": time.monotonic() - started,
                   "commands": result["commands"], "result": str(work / "result.json")}
        output.append(receipt)
        print(json.dumps(receipt), flush=True)
        (args.output / "campaign.json").write_text(json.dumps(output, indent=2) + "\n")
    raise SystemExit(0 if all(r["status"] == "succeeded" for r in output) else 1)


if __name__ == "__main__":
    main()
