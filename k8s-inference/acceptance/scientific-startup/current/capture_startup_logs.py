#!/usr/bin/env python3
"""Capture container logs while scientific benchmark pods still exist.

Consumes private ``kubectl get pods --watch --output-watch-events -o json``
files. PodReady is recorded as a Kubernetes condition, never interpreted as
model readiness. Model-specific probes must supply the latter boundary.
Raw logs can contain workload data: keep this output private, not in Git.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from datetime import UTC, datetime
from typing import Any


def consume_json(buffer: str) -> tuple[list[dict[str, Any]], str]:
    """Decode complete concatenated JSON objects; retain an incomplete suffix."""
    decoder = json.JSONDecoder()
    documents: list[dict[str, Any]] = []
    while buffer.strip():
        buffer = buffer.lstrip()
        try:
            value, end = decoder.raw_decode(buffer)
        except json.JSONDecodeError:
            return documents, buffer
        if not isinstance(value, dict):
            raise ValueError("watch record is not an object")
        documents.append(value)
        buffer = buffer[end:]
    return documents, ""


def pod_projection(pod: dict[str, Any]) -> dict[str, Any]:
    """Whitelist lifecycle metadata; omit env, command arguments and handles."""
    metadata = pod.get("metadata", {})
    spec = pod.get("spec", {})
    status = pod.get("status", {})
    return {
        "observed_at": datetime.now(UTC).isoformat(),
        "namespace": metadata.get("namespace"),
        "name": metadata.get("name"),
        "uid": metadata.get("uid"),
        "created_at": metadata.get("creationTimestamp"),
        "labels": metadata.get("labels", {}),
        "node": spec.get("nodeName"),
        "phase": status.get("phase"),
        "conditions": status.get("conditions", []),
        "init_containers": [
            {key: item.get(key) for key in ("name", "image", "resources")}
            for item in spec.get("initContainers", [])
        ],
        "containers": [
            {key: item.get(key) for key in ("name", "image", "resources")}
            for item in spec.get("containers", [])
        ],
        "init_statuses": status.get("initContainerStatuses", []),
        "statuses": status.get("containerStatuses", []),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--duration-seconds", type=float, default=10800)
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True)
    stopped = threading.Event()
    for name in (signal.SIGINT, signal.SIGTERM):
        signal.signal(name, lambda *_: stopped.set())
    streams = [path.open() for path in args.watch]
    buffers = [""] * len(streams)
    processes: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    command = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context]
    deadline = time.monotonic() + args.duration_seconds
    with (args.output / "lifecycle.jsonl").open("a") as evidence:
        try:
            while not stopped.is_set() and time.monotonic() < deadline:
                for index, stream in enumerate(streams):
                    buffers[index] += stream.read(1024 * 1024)
                    records, buffers[index] = consume_json(buffers[index])
                    for record in records:
                        pod = record.get("object", record)
                        if pod.get("kind") != "Pod":
                            continue
                        view = pod_projection(pod)
                        evidence.write(json.dumps(view, sort_keys=True) + "\n")
                        evidence.flush()
                        # General-serving worker owns its isolated logs. Avoid
                        # attaching a follower to the retained production model.
                        if not view["labels"].get("fs2.nebius.ai/operation-id"):
                            continue
                        for status in view["init_statuses"] + view["statuses"]:
                            state = status.get("state", {})
                            if not ("running" in state or "terminated" in state):
                                continue
                            key = (view["namespace"], view["uid"], status["name"], status.get("restartCount", 0))
                            previous = processes.get(key)
                            if previous is not None:
                                if previous["process"].poll() in (None, 0):
                                    continue
                                if previous["tries"] >= 3 or time.monotonic() - previous["at"] < 3:
                                    continue
                            tries = 1 if previous is None else previous["tries"] + 1
                            directory = args.output / view["namespace"] / view["uid"]
                            directory.mkdir(parents=True, exist_ok=True)
                            log = directory / f"{status['name']}.r{key[3]}.capture{tries}.log"
                            with log.open("wb") as output:
                                process = subprocess.Popen(
                                    [*command, "-n", view["namespace"], "logs", view["name"], "-c", status["name"], "--timestamps", "--follow", "--pod-running-timeout=20s"],
                                    stdout=output, stderr=subprocess.STDOUT,
                                )
                            processes[key] = {"process": process, "tries": tries, "at": time.monotonic()}
                stopped.wait(0.25)
        finally:
            for stream in streams:
                stream.close()
            for item in processes.values():
                if item["process"].poll() is None:
                    item["process"].terminate()
            for item in processes.values():
                try:
                    item["process"].wait(timeout=5)
                except subprocess.TimeoutExpired:
                    item["process"].kill()
                    item["process"].wait()
    print(json.dumps({"capture_complete": True, "containers": len(processes)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
