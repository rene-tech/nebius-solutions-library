"""Read-only hardware samples for the two recorded demo-owned operations only."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gromacs-mpinat-20261003"))
from observe import now, read_observation  # noqa: E402


def owned_pods(document, operation):
    """An operation ID does not grant permission to inspect another owner."""
    result = []
    for pod in document["items"]:
        labels = pod["metadata"].get("labels", {})
        if (labels.get("fs2.nebius.ai/operation-id"), labels.get("fs2.nebius.ai/tenant-id"),
            labels.get("fs2.nebius.ai/model-id")) != (operation, "demo-user", "gromacs"):
            raise ValueError("Observer received a Pod outside this exact demo operation")
        result.append(pod)
    return result


async def run(args):
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True)
    base = ["kubectl", "--context", args.context, "--request-timeout=15s", "-n", "fs2-models"]
    seen, previous = set(), set()
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline and not (args.state.parent / "receipt.json").exists():
        started, active = time.monotonic(), set()
        state = json.loads(args.state.read_text()) if args.state.exists() else {}
        for phase in ("source", "resume"):
            if not state.get(phase + "_operation"):
                continue
            operation = str(UUID(state[phase + "_operation"]))
            query = await read_observation([*base, "get", "pods", "-l",
                                           "fs2.nebius.ai/operation-id=" + operation, "-o", "json"])
            values = []
            if query.get("returncode") == 0:
                for pod in owned_pods(json.loads(query["stdout"]), operation):
                    uid = pod["metadata"]["uid"]
                    active.add(uid)
                    running = any(item["name"] == "scientific-stage" and "running" in item.get("state", {})
                                  for item in pod["status"].get("containerStatuses", []))
                    value = {"pod": pod["metadata"]["name"], "uid": uid,
                             "node": pod["spec"].get("nodeName"), "phase": pod["status"]["phase"],
                             "containers": [{key: item.get(key) for key in ("name", "imageID", "restartCount")}
                                            for item in pod["status"].get("containerStatuses", [])]}
                    if running:
                        execute = [*base, "exec", pod["metadata"]["name"], "-c", "scientific-stage", "--"]
                        commands = {
                            "gpu": ["nvidia-smi", "--query-gpu=uuid,name,driver_version,memory.total,memory.used,utilization.gpu,utilization.memory,power.draw", "--format=csv,noheader,nounits"],
                            "cpu": ["cat", "/sys/fs/cgroup/cpu.stat", "/sys/fs/cgroup/cpu.max", "/sys/fs/cgroup/memory.current"],
                        }
                        if uid not in seen:
                            commands["cpu_topology"] = ["lscpu"]
                            seen.add(uid)
                        observations = await asyncio.gather(*(read_observation([*execute, *cmd]) for cmd in commands.values()))
                        value["measurements"] = dict(zip(commands, observations))
                    values.append(value)
            with (args.output / "hardware.jsonl").open("a") as handle:
                handle.write(json.dumps({"at": now(), "operation": operation, "phase": phase, "pods": values,
                                         "query_failure": query if query.get("returncode") != 0 else None}) + "\n")
        if previous - active:
            with (args.output / "release.jsonl").open("a") as handle:
                handle.write(json.dumps({"at": now(), "absent_pod_uids": sorted(previous - active),
                                         "scope": "Sampled absence, not exact device release time"}) + "\n")
        previous = active
        await asyncio.sleep(max(1, args.interval - (time.monotonic() - started)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=43200)
    parser.add_argument("--interval", type=int, default=30)
    args = parser.parse_args()
    if not 30 <= args.seconds <= 43200 or not 10 <= args.interval <= 300:
        parser.error("Use a bounded observation window and sampling interval")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
