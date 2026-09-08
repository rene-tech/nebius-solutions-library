"""Export allowlisted image qualification facts from private captured receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path


def documents(text):
    decoder = json.JSONDecoder()
    while text.strip():
        text = text.lstrip()
        value, end = decoder.raw_decode(text)
        yield value
        text = text[end:]


def export(root):
    receipts = {}
    hashes = {}
    for path in sorted(root.glob("*.json")):
        raw = path.read_bytes()
        hashes[path.name] = hashlib.sha256(raw).hexdigest()
        receipt = json.loads(raw)
        receipts[path.name.split("-", 1)[1].removesuffix(".json")] = receipt
    qualification = list(documents(receipts["qualification"]["stdout"]))[-1]
    assert qualification["outcome"] == "passed"
    created = json.loads(receipts["created"]["stdout"])
    pod = json.loads(receipts["pre-cleanup"]["stdout"])
    cleanup = json.loads(receipts["cleanup"]["stdout"])
    assert cleanup["items"] == []
    assert created["metadata"]["uid"] == pod["metadata"]["uid"]
    conditions = {
        item["type"]: item["lastTransitionTime"]
        for item in pod["status"]["conditions"] if item["status"] == "True"
    }
    container = pod["status"]["containerStatuses"][0]
    events = json.loads(receipts["events"]["stdout"])["items"]
    benchmarks = [item["result"] for item in qualification["benchmarks"]]
    source = benchmarks[0]["source_sha256"]
    assert all(item["source_sha256"] == source for item in benchmarks)
    environment = json.loads(receipts["environment"]["stdout"])
    return {
        "schema": "fs2-aging-runtime-image-qualification/v1",
        "outcome": "passed",
        "source_revision": pod["metadata"]["annotations"]["acceptance.fs2.nebius/source"],
        "model": qualification["readiness"]["body"],
        "image": container["imageID"],
        "source_sha256": source,
        "cluster": {"context": "k8s-inference-h100", "project": "project-e00rene", "region": "eu-north1"},
        "pod": {
            "name": pod["metadata"]["name"], "namespace": pod["metadata"]["namespace"],
            "uid": pod["metadata"]["uid"], "node": pod["spec"]["nodeName"],
            "resources": pod["spec"]["containers"][0]["resources"],
            "restart_count": container["restartCount"],
        },
        "startup": {
            "creation": pod["metadata"]["creationTimestamp"], "conditions": conditions,
            "container_started": container["state"]["running"]["startedAt"],
            "created_to_ready_seconds_coarse": (
                datetime.fromisoformat(conditions["Ready"].replace("Z", "+00:00"))
                - datetime.fromisoformat(pod["metadata"]["creationTimestamp"].replace("Z", "+00:00"))
            ).total_seconds(),
            "events": [{"reason": event["reason"], "at": event["firstTimestamp"], "message": event["message"]} for event in events],
            "runtime_log": receipts["startup-logs"]["stdout"],
        },
        "native_http_predictions": qualification["predictions"],
        "cpu_cuda_parity": qualification.get("parity"),
        "gpu": qualification.get("gpu"),
        "environment": {
            "host": environment["host"],
            "python_stack": environment["python_stack"],
            "nvidia_gpu_query": environment["commands"]["nvidia_gpu_query"],
        },
        "module_benchmarks": benchmarks,
        "cleanup": {"confirmed_absent_at": receipts["cleanup"]["completed_at"], "uid_verified": True},
        "raw_receipt_sha256": hashes,
        "limitations": [
            "Isolated temporary native worker; not public App, MCP, durable queue or scale-to-zero qualification.",
            "No new node provisioning; coarse Kubernetes timestamps are not high-resolution request timings.",
            "Image pull duration is the kubelet observation; reported image size is not wire bytes.",
            "Module imports/constructor timing excludes image pull, scheduling, public admission and network.",
            "Synthetic fixtures demonstrate computational correctness, not clinical prediction accuracy.",
            "No GPU snapshot restore was performed or qualified.",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with args.output.open("x") as stream:
        json.dump(export(args.private), stream, indent=2, sort_keys=True)
        stream.write("\n")
