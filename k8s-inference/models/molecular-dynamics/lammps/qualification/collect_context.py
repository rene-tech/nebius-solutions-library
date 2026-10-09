"""Read-only Pod, resource, driver and startup context for a native cohort."""

import argparse
import json
import subprocess
from pathlib import Path

from cluster import owned
from mirror_runtime import KUBE
from native_receipt import digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pod", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pod = owned(args.pod)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "pod.json").write_text(json.dumps(pod, indent=2) + "\n")
    for name, command in (("gpu.txt", ["nvidia-smi", "-q"]), ("cpu.txt", ["lscpu"]), ("kernel.txt", ["uname", "-a"]), ("cpu-limit.txt", ["cat", "/sys/fs/cgroup/cpu.max"]), ("memory-limit.txt", ["cat", "/sys/fs/cgroup/memory.max"])):
        probe = subprocess.run(KUBE + ["exec", args.pod, "--"] + command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        (args.output / name).write_bytes(probe.stdout)
    events = subprocess.check_output(KUBE + ["get", "events", "--field-selector", "involvedObject.uid=" + pod["metadata"]["uid"], "-o", "json"])
    (args.output / "events.json").write_bytes(events)
    receipt = {"runtime_image": pod["spec"]["containers"][0]["image"], "pod_uid": pod["metadata"]["uid"], "node": pod["spec"]["nodeName"], "created_at": pod["metadata"]["creationTimestamp"], "started_at": pod["status"]["containerStatuses"][0]["state"]["running"]["startedAt"], "startup_boundary": "qualification sleep container start, not a hosted worker or image-pull-cold guarantee; inspect events for cache reuse", "files": {p.name: digest(p) for p in sorted(args.output.iterdir()) if p.is_file()}}
    (args.output / "context.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
