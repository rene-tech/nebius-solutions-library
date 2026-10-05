"""Read-only sampler of this task's explicitly labelled native probes only."""

import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gromacs-mpinat-20261003"))
from observe import now, read_observation


async def run(args):
    args.output.mkdir(parents=True, exist_ok=False)
    kube = ["kubectl", "--context", args.context, "--request-timeout=15s", "-n", "fs2-models"]
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline and not (args.output / "STOP").exists():
        query = await read_observation([*kube, "get", "pods", "-l",
                                       "scientific-ai.nebius.com/task=lynx-performance-20261005", "-o", "json"])
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--context", default="nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a")
    parser.add_argument("--seconds", type=int, default=14400)
    parser.add_argument("--interval", type=int, default=10)
    asyncio.run(run(parser.parse_args()))
