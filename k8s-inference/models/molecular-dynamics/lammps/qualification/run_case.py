"""Run inside the qualification Pod, retaining GPU/CPU and native raw outputs."""

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
    environment = subprocess.check_output(["nvidia-smi", "--query-gpu=name,uuid,driver_version,compute_cap,memory.total,pci.bus_id", "--format=csv"], text=True)
    (root / "environment.txt").write_text(environment)
    command = ["python3", "-m", "fs2_lammps.worker", "--request", str(root / "request.json"), "--workspace", str(root), "--job-id", args.job, "--operation-id", "32989a99-3824-4e2c-b4c9-0e7c46a10923", "--checkpoint-mode", "local"]
    start = time.monotonic()
    with (root / "gpu.csv").open("w") as gpu, (root / "worker.log").open("w") as output:
        monitor = subprocess.Popen(["nvidia-smi", "--query-gpu=timestamp,name,utilization.gpu,utilization.memory,memory.used,power.draw,clocks.sm", "--format=csv,noheader,nounits", "-lms", "1000"], stdout=gpu, stderr=subprocess.STDOUT)
        try:
            result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT)
        finally:
            monitor.terminate()
            monitor.wait(timeout=5)
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    (root / "execution.json").write_text(json.dumps({"command": command, "elapsed_seconds": time.monotonic() - start, "exit_code": result.returncode, "cpu_user_seconds": usage.ru_utime, "cpu_system_seconds": usage.ru_stime, "max_rss_kib": usage.ru_maxrss, "block_input_ops": usage.ru_inblock, "block_output_ops": usage.ru_oublock, "gpu_monitor_included_in_cpu_usage": True}, indent=2) + "\n")
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
