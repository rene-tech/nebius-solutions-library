"""Capture non-secret task-owned runtime provenance and cold-pull events."""
import argparse
import json
from pathlib import Path
import subprocess

from registry_probe import KUBE


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pod", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not args.pod.startswith("fs2-namd-r20260923-"):
        raise ValueError("capture only task-owned resources")
    args.output.mkdir(parents=True, exist_ok=True)
    pod = subprocess.check_output(KUBE + ["get", "pod", args.pod, "-o", "json"], text=True)
    (args.output / "pod.json").write_text(pod)
    pod_uid = json.loads(pod)["metadata"]["uid"]
    commands = {
        "events.json": ["get", "events", "--field-selector", f"involvedObject.uid={pod_uid}", "-o", "json"],
        "startup.log": ["logs", args.pod],
        "native-identity.txt": ["exec", args.pod, "--", "bash", "-lc",
            "nvidia-smi --query-gpu=name,uuid,driver_version,compute_cap,memory.total --format=csv; "
            "sha256sum /usr/local/namd/bin/namd3 /usr/local/namd/bin/psfgen; "
            "dpkg-query -W python3 python3-jsonschema libstdc++6 libc6; "
            "ls /usr/local/namd/bin; "
            "grep -A16 '# NAMD_3.0.2' /usr/src/Dockerfile"],
    }
    for filename, command in commands.items():
        result = subprocess.run(KUBE + command, text=True, capture_output=True)
        (args.output / filename).write_text(result.stdout)
        if result.returncode:
            (args.output / (filename + ".error")).write_text(result.stderr)
    print(json.dumps({"pod": args.pod, "evidence": str(args.output)}))


if __name__ == "__main__":
    main()
