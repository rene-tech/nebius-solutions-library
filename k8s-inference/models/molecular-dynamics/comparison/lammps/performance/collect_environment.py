"""Read-only native profiling inventory; no clock, driver or host-policy changes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from screen import native_environment


def collect(executable):
    names = "nvidia-smi nsys ncu dcgmi dcgmproftester12 nvbandwidth all_reduce_perf nvcc compute-sanitizer cuobjdump nvdisasm nvidia-ctk docker perf py-spy strace ldd readelf numactl lstopo ibv_devinfo".split()
    tools = {name: shutil.which(name) for name in names}
    commands = [["nvidia-smi"], ["nvidia-smi", "topo", "-m"],
                ["nvidia-smi", "--query-gpu=index,name,uuid,driver_version,compute_cap,pci.bus_id", "--format=csv"],
                [executable, "-h"], ["ldd", executable], ["nsys", "--version"], ["ncu", "--version"], ["dcgmi", "--version"]]
    result = {"tools": tools, "commands": [], "policy": {},
              "environment": {key: os.environ.get(key) for key in ("OMP_NUM_THREADS", "CUDA_LAUNCH_BLOCKING", "CUDA_VISIBLE_DEVICES", "KOKKOS_TOOLS_LIBS")}}
    with open(executable, "rb") as stream:
        result["executable_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    for command in commands:
        try:
            probe = subprocess.run(command, capture_output=True, text=True, timeout=30, env=native_environment(executable))
            result["commands"].append({"argv": command, "exit_code": probe.returncode, "stdout": probe.stdout, "stderr": probe.stderr})
        except (OSError, subprocess.TimeoutExpired) as exc:
            result["commands"].append({"argv": command, "error": str(exc)})
    for filename in ("/proc/sys/kernel/perf_event_paranoid", "/proc/driver/nvidia/params"):
        path = Path(filename)
        try:
            text = path.read_text()
            result["policy"][filename] = text if filename.endswith("perf_event_paranoid") else "\n".join(line for line in text.splitlines() if any(key in line for key in ("Profiling", "Restrict", "RmProfilingAdminOnly")))
        except OSError as exc:
            result["policy"][filename] = {"error": str(exc)}
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", default="/usr/local/lammps/sm90/bin/lmp")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output must be new")
    args.output.write_text(json.dumps(collect(args.executable), indent=2) + "\n")
    print(str(args.output))
