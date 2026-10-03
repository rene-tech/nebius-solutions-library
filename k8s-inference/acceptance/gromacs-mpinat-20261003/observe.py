"""Read-only attempt-level telemetry for internal QA GROMACS benchmarks.

Never enters customer Pods. Container CPU counters include preparation/analysis;
GPU utilization is sampled, not an integral. Pod binding/deletion observations
bound allocation intervals, not exact device-plugin release instants.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time


def now():
    return datetime.now(timezone.utc).isoformat()


async def main(args):
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True)
    seen, previous = set(), set()

    def save(name, data):
        with (args.output / name).open("a") as file:
            file.write(json.dumps({"observed_at": now(), **data}) + "\n")

    async def kubectl(*command):
        process = await asyncio.create_subprocess_exec("kubectl", "--context", args.context,
            "--request-timeout=15s", *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(process.communicate(), timeout=25)
            return {"returncode": process.returncode, "stdout": out.decode(errors="replace"),
                    "stderr": err.decode(errors="replace")}
        except TimeoutError:
            process.kill()
            await process.wait()
            return {"error": "observation_timeout"}

    async def sample(pod):
        meta, spec = pod["metadata"], pod["spec"]
        ident = {"pod_uid": meta["uid"], "pod": meta["name"], "node": spec.get("nodeName"),
                 "labels": meta.get("labels", {})}
        states = {item["name"]: item.get("state", {}) for item in pod["status"].get("containerStatuses", [])}
        for container in spec["containers"]:
            if "running" not in states.get(container["name"], {}) or not container.get("resources", {}).get("limits", {}).get("nvidia.com/gpu"):
                continue
            base = ("-n", "fs2-models", "exec", meta["name"], "-c", container["name"], "--")
            queries = {
                "gpu": ("nvidia-smi", "--query-gpu=uuid,name,driver_version,memory.total,memory.used,utilization.gpu,utilization.memory,power.draw", "--format=csv,noheader,nounits"),
                "cpu_memory_counters": ("cat", "/sys/fs/cgroup/cpu.stat", "/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.peak"),
            }
            root = container.get("workingDir")
            if root and root.startswith("/mnt/fs2-scientific/work/gromacs"):
                # Preserve native failure reasons and protocol records before
                # automatic Pod cleanup. These are only public-fixture QA runs.
                code = ("import pathlib,json,sys; p=pathlib.Path(sys.argv[1]); "
                        "paths=list((p/'.fs2').glob('*tpr.mdp'))+list((p/'data').glob('*.log')); "
                        "paths += [p/'result.json',p/'.fs2/checkpoint-ready.json',p/'.fs2/checkpoint-ack.json']; "
                        "print(json.dumps({str(f.relative_to(p)):{'bytes':f.stat().st_size,'tail':f.read_bytes()[-131072:].decode(errors='replace')} "
                        "for f in paths if f.is_file() and f.stat().st_size < 2097152}))")
                queries["native_progress"] = ("python3", "-c", code, root)
            marker = (meta["uid"], container["name"])
            if marker not in seen:
                queries["topology"] = ("nvidia-smi", "topo", "-m")
                queries["cpu_topology"] = ("lscpu",)
                seen.add(marker)
            results = await asyncio.gather(*(kubectl(*base, *cmd) for cmd in queries.values()))
            save("container-samples.jsonl", {**ident, "container": container["name"],
                "measurements": dict(zip(queries, results))})

    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline and not (args.output / "STOP").exists():
        started = time.monotonic()
        query = await kubectl("get", "pods", "-n", "fs2-models", "-l", "fs2.nebius.ai/tenant-id=system", "-o", "json")
        if query.get("returncode") != 0:
            save("errors.jsonl", query)
            await asyncio.sleep(5)
            continue
        pods = [pod for pod in json.loads(query["stdout"])["items"]
                if pod["metadata"].get("labels", {}).get("fs2.nebius.ai/model-id") in ("gromacs", "gromacs-mpi")]
        current = {p["metadata"]["uid"] for p in pods}
        for uid in previous - current:
            save("pod-disappearance.jsonl", {"pod_uid": uid, "precision": "sampled upper bound; inspect native resource-release events"})
        previous = current
        for pod in pods:
            # Retain resources/commands/runtime IDs, never credential env values.
            for kind in ("containers", "initContainers"):
                for container in pod["spec"].get(kind, []):
                    container["env"] = [{"name": row["name"], "valueFrom": row.get("valueFrom")} for row in container.get("env", [])]
            pod["metadata"].pop("managedFields", None)
            pod["metadata"].pop("annotations", None)
            save("pods.jsonl", {"pod": pod})
        if pods:
            metrics, nodes, events = await asyncio.gather(
                kubectl("get", "--raw", "/apis/metrics.k8s.io/v1beta1/namespaces/fs2-models/pods"),
                kubectl("get", "nodes", "-o", "json"),
                kubectl("get", "events", "-n", "fs2-models", "-o", "json"))
            names = {p["metadata"]["name"] for p in pods}
            node_names = {p["spec"].get("nodeName") for p in pods}
            for name, result in (("metrics", metrics), ("nodes", nodes), ("events", events)):
                if result.get("returncode"):
                    save("errors.jsonl", {"kind": name, **result})
                    continue
                items = json.loads(result.get("stdout", "{}" )).get("items", [])
                items = [item for item in items if
                    (name == "events" and item.get("involvedObject", {}).get("name") in names) or
                    (name == "metrics" and item["metadata"]["name"] in names) or
                    (name == "nodes" and item["metadata"]["name"] in node_names)]
                for item in items:
                    item["metadata"].pop("managedFields", None)
                save(name + ".jsonl", {"items": items})
            await asyncio.gather(*(sample(pod) for pod in pods))
        await asyncio.sleep(max(1, args.interval - (time.monotonic() - started)))
    save("observer-closeout.jsonl", {"state": "stopped", "live_pod_uids_at_last_sample": sorted(previous)})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--context", default="nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a")
    parser.add_argument("--seconds", type=int, default=43200)
    parser.add_argument("--interval", type=float, default=10)
    asyncio.run(main(parser.parse_args()))
