"""Read-only sampler of this task's explicitly labelled native probes only."""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gromacs-mpinat-20261003"))
from observe import now, read_observation

TASKS = ("lynx-performance-20261005", "lynx-rdma-20261005")


def task_selector(task):
    if task not in TASKS:
        raise ValueError("Only this task's owned native probe labels may be sampled")
    return "scientific-ai.nebius.com/task=" + task


async def run(args):
    selector = task_selector(getattr(args, "task_label", TASKS[0]))
    args.output.mkdir(parents=True, exist_ok=False)
    kube = ["kubectl", "--context", args.context, "--request-timeout=15s", "-n", "fs2-models"]
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline and not (args.output / "STOP").exists():
        query = await read_observation([*kube, "get", "pods", "-l",
                                       selector, "-o", "json"])
        values = []
        if query.get("returncode") == 0:
            for pod in json.loads(query["stdout"])["items"]:
                if pod["status"].get("phase") != "Running":
                    continue
                base = [*kube, "exec", pod["metadata"]["name"], "-c", "runtime", "--"]
                gpu, cpu = await asyncio.gather(
                    read_observation([*base, "nvidia-smi", "--query-gpu=uuid,name,utilization.gpu,utilization.memory,memory.used,power.draw,clocks.sm", "--format=csv,noheader,nounits"]),
                    read_observation([*base, "cat", "/sys/fs/cgroup/cpu.stat", "/sys/fs/cgroup/cpu.max", "/sys/fs/cgroup/memory.current"]))
                values.append({"pod": pod["metadata"]["name"], "uid": pod["metadata"]["uid"],
                               "node": pod["spec"].get("nodeName"), "gpu": gpu, "cpu": cpu})
        with (args.output / "samples.jsonl").open("a") as stream:
            stream.write(json.dumps({"at": now(), "samples": values,
                                    "query_failure": query if query.get("returncode") != 0 else None}) + "\n")
        await asyncio.sleep(args.interval)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--context", default="nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a")
    parser.add_argument("--seconds", type=int, default=14400)
    parser.add_argument("--interval", type=int, default=10)
    parser.add_argument("--task-label", choices=TASKS, default=TASKS[0])
    return parser.parse_args(argv)


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
