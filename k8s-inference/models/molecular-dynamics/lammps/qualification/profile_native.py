"""Unprivileged CUDA/NVTX trace, separate from every performance repetition.

Copies an existing Nsight target package into task-owned scratch only. Does not
request CPU/GPU hardware counters or change perf, driver or container policy.
"""

import argparse
import json
import subprocess
from pathlib import Path

from cluster import owned
from mirror_runtime import KUBE
from native_receipt import digest
from validate_case import validate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pod", required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--case", default="rhodo")
    parser.add_argument("--nsys-target", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pod = owned(args.pod)
    processes = subprocess.check_output(KUBE + ["exec", args.pod, "--", "ps", "-eo", "args"], text=True)
    if "-m fs2_lammps.worker" in processes or "/bin/lmp -k" in processes:
        raise ValueError("do not profile concurrently with a performance run")
    if not (args.nsys_target / "nsys").is_file():
        raise ValueError("complete existing Nsight target package is required")
    args.output.mkdir(parents=True, exist_ok=False)
    remote = "/mnt/fs2-scientific/" + args.output.name
    tool = "/mnt/fs2-scientific/nsys-target/nsys"
    subprocess.run(KUBE + ["cp", "--no-preserve", str(args.nsys_target), args.pod + ":/mnt/fs2-scientific/nsys-target"], check=True)
    subprocess.run(KUBE + ["cp", "--no-preserve", str(args.fixture), args.pod + ":" + remote], check=True)
    for name, command in (("profiler-version.txt", [tool, "--version"]), ("profiler-environment.txt", [tool, "status", "--environment"]), ("gpu-context.txt", ["nvidia-smi", "-q"])):
        probe = subprocess.run(KUBE + ["exec", args.pod, "--"] + command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        (args.output / name).write_bytes(probe.stdout)
    command = [tool, "profile", "--trace=cuda,nvtx", "--sample=none", "--cpuctxsw=none", "--cuda-memory-usage=true", "--trace-fork-before-exec=true", "--export=sqlite", "--force-overwrite=false", "--output=" + remote + "/native-trace", "python3", "-m", "fs2_lammps.worker", "--request", remote + "/request.json", "--workspace", remote, "--job-id", args.case, "--operation-id", "80309fd0-1a46-4335-905f-2da9fae09923", "--checkpoint-mode", "local"]
    with (args.output / "profiler-client.log").open("wb") as output:
        process = subprocess.run(KUBE + ["exec", args.pod, "--"] + command, stdout=output, stderr=subprocess.STDOUT)
    workspace = args.output / "workspace"
    subprocess.run(KUBE + ["cp", "--retries=3", args.pod + ":" + remote, str(workspace)], check=True)
    result_path = workspace / "result.json"
    if result_path.exists():
        result = json.loads(result_path.read_text())
        for entry in result["files"]:
            path = workspace / "data" / entry["path"]
            if path.stat().st_size != entry["size_bytes"] or digest(path) != entry["sha256"]:
                raise ValueError("profiled output copy differs from native result inventory")
    try:
        validation = validate(workspace)
    except Exception as exc:
        validation = {"status": "failed", "error": str(exc)}
    (args.output / "validation.json").write_text(json.dumps(validation, indent=2) + "\n")
    trace = workspace / "native-trace.nsys-rep"
    if trace.exists():
        for report in ("cuda_gpu_kern_sum", "cuda_gpu_mem_time_sum", "cuda_api_sum"):
            with (args.output / (report + ".csv")).open("wb") as output:
                subprocess.run(["nsys", "stats", "--report", report, "--format", "csv", str(trace)], stdout=output, stderr=subprocess.STDOUT, check=True)
    receipt = {"status": "captured" if process.returncode == 0 and trace.exists() and validation["status"] == "passed" else "failed", "runtime_image": pod["spec"]["containers"][0]["image"], "pod_uid": pod["metadata"]["uid"], "node": pod["spec"]["nodeName"], "input_sha256": digest(args.fixture / "input.tar.gz"), "trace_sha256": digest(trace) if trace.exists() else None, "result_sha256": digest(result_path) if result_path.exists() else None, "profiler_exit": process.returncode, "command": command, "performance_measurement": False, "hardware_counters_requested": False, "policy_changed": False, "customer_ready": False, "raw_evidence": str(args.output)}
    (args.output / "profile-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))
    raise SystemExit(0 if receipt["status"] == "captured" else 1)


if __name__ == "__main__":
    main()
