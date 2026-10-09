"""Run one private fixture in the candidate Pod with measured native telemetry."""

import argparse
import json
import resource
import subprocess
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--job", required=True)
    args = parser.parse_args()
    root = args.workspace
    gpu = subprocess.check_output(["nvidia-smi", "--query-gpu=name,uuid,driver_version,compute_cap,memory.total,pci.bus_id", "--format=csv"], text=True)
    (root / "environment.txt").write_text(gpu)
    command = ["python3", "-m", "fs2_amber.worker", "--request", str(root / "request.json"), "--workspace", str(root), "--job-id", args.job, "--operation-id", "a0620000-0923-4026-8000-000000000001", "--checkpoint-mode", "local"]
    started = time.monotonic()
    with (root / "gpu.csv").open("w") as gpu_log, (root / "worker.log").open("w") as output:
        monitor = subprocess.Popen(["nvidia-smi", "--query-gpu=timestamp,name,utilization.gpu,utilization.memory,memory.used,power.draw,clocks.sm", "--format=csv,noheader,nounits", "-lms", "1000"], stdout=gpu_log)
        try:
            result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT)
        finally:
            monitor.terminate()
            monitor.wait(timeout=5)
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    receipt = {"command": command, "worker_wall_seconds": time.monotonic() - started, "exit_code": result.returncode, "cpu_user_seconds": usage.ru_utime, "cpu_system_seconds": usage.ru_stime, "max_rss_kib": usage.ru_maxrss, "gpu_monitor_included_in_child_cpu_usage": True}
    (root / "execution.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt), flush=True)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
