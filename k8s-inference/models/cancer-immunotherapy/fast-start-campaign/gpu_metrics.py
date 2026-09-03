#!/usr/bin/env python3
"""Attach DCGM GPU telemetry to each trial receipt.

The cluster's DCGM exporter publishes per-device series keyed by node hostname
and GPU index, but it does not carry a Pod label, so a series cannot be tied to
one Pod when several trials share a node.  Every value written here is therefore
labelled node-scoped, and the receipt records how many campaign trials were
resident on that node during the window so the number is never read as if it
were exclusive to one trial.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import faststart

HERE = Path(__file__).resolve().parent
RECEIPTS = HERE / "receipts"
SERVICE = "svc/fs2-r927c465c6d-monitoring-prometheus"
NAMESPACE = "fs2-observability"

QUERIES = {
    "gpu_utilization_percent": "DCGM_FI_DEV_GPU_UTIL",
    "framebuffer_used_mib": "DCGM_FI_DEV_FB_USED",
    "sm_clock_mhz": "DCGM_FI_DEV_SM_CLOCK",
    "power_usage_watts": "DCGM_FI_DEV_POWER_USAGE",
}


@contextlib.contextmanager
def prometheus(port: int = 19090):
    process = subprocess.Popen(
        ["kubectl", "port-forward", "-n", NAMESPACE, SERVICE, f"{port}:9090"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(30):
            try:
                urllib.request.urlopen(f"{base}/-/ready", timeout=2).read()
                break
            except Exception:
                time.sleep(1)
        else:
            raise SystemExit("prometheus port-forward did not become ready")
        yield base
    finally:
        process.terminate()
        process.wait(timeout=10)


def query_range(base: str, expression: str, start: str, end: str, step: int = 5) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode({"query": expression, "start": start, "end": end, "step": step})
    with urllib.request.urlopen(f"{base}/api/v1/query_range?{params}", timeout=30) as response:
        payload = json.load(response)
    if payload.get("status") != "success":
        raise SystemExit(f"prometheus rejected {expression}: {payload}")
    return payload["data"]["result"]


def node_hostname(namespace: str, pod_name: str) -> str | None:
    """Resolve the node a trial's Pod ran on, or None once it has been cleaned up.

    Telemetry can only be gathered while the Pod object still exists. A trial
    whose Pod is gone keeps the telemetry captured earlier rather than losing it
    or having it silently recomputed against the wrong window.
    """
    raw = faststart.kubectl(
        "get", "pod", pod_name, "-n", namespace, "-o", "jsonpath={.spec.nodeName}", check=False
    ).strip()
    return raw or None


def enrich(path: Path, base: str, windows: dict[str, list[str]]) -> dict[str, Any] | None:
    receipt = json.loads(path.read_text(encoding="utf-8"))
    boundaries = receipt["measured"]["boundaries"]
    start, end = boundaries.get("container_started_at"), boundaries.get("container_finished_at")
    if not (start and end and receipt.get("pod")):
        return None
    hostname = node_hostname(receipt["cluster"]["namespace"], receipt["pod"])
    if hostname is None:
        return receipt.get("gpu_telemetry")

    devices: dict[str, dict[str, Any]] = {}
    for label, metric in QUERIES.items():
        expression = f'{metric}{{hostname="{hostname}"}}'
        for series in query_range(base, expression, start, end):
            values = [float(value) for _, value in series["values"]]
            if not values:
                continue
            index = series["metric"].get("gpu", "?")
            device = devices.setdefault(index, {
                "gpu_index": index,
                "uuid": series["metric"].get("UUID"),
                "model_name": series["metric"].get("modelName"),
                "pci_bus_id": series["metric"].get("pci_bus_id"),
            })
            device[label] = {
                "samples": len(values),
                "min": round(min(values), 3),
                "mean": round(sum(values) / len(values), 3),
                "max": round(max(values), 3),
            }

    busy = [
        device for device in devices.values()
        if (device.get("gpu_utilization_percent") or {}).get("max", 0) > 0
    ]
    concurrent = [
        other for other, span in windows.items()
        if other != receipt["trial_id"] and span[2] == hostname
        and not (span[1] <= start or span[0] >= end)
    ]

    receipt["gpu_telemetry"] = {
        "source": "DCGM exporter scraped by the cluster Prometheus",
        "scope": "node-scoped",
        "scope_note": (
            "the exporter publishes no Pod label, so these series describe every GPU on the node "
            "during the trial window rather than the single device this trial held"
        ),
        "node_hostname_sha256": receipt["node"]["node_name_sha256"],
        "window": {"from": start, "to": end},
        "devices_on_node": len(devices),
        "devices_with_nonzero_utilization": len(busy),
        "concurrent_campaign_trials_on_node": sorted(concurrent),
        "devices": [devices[key] for key in sorted(devices)],
    }
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt["gpu_telemetry"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=19090)
    arguments = parser.parse_args()

    paths = sorted(RECEIPTS.glob("*.json"))
    windows: dict[str, list[str]] = {}
    for path in paths:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        boundaries = receipt["measured"]["boundaries"]
        if boundaries.get("container_started_at") and receipt.get("pod"):
            host = node_hostname(receipt["cluster"]["namespace"], receipt["pod"])
            windows[receipt["trial_id"]] = [
                boundaries["container_started_at"],
                boundaries["container_finished_at"],
                host,
            ]

    with prometheus(arguments.port) as base:
        for path in paths:
            before = json.loads(path.read_text(encoding="utf-8")).get("gpu_telemetry")
            telemetry = enrich(path, base, windows)
            if telemetry is None:
                print(f"{path.stem}: no window, skipped")
                continue
            if telemetry is before:
                print(f"{path.stem}: Pod already cleaned up, retained earlier telemetry")
                continue
            busy = telemetry["devices_with_nonzero_utilization"]
            peak = max(
                (device.get("gpu_utilization_percent", {}).get("max", 0) for device in telemetry["devices"]),
                default=0,
            )
            print(
                f"{path.stem}: {busy}/{telemetry['devices_on_node']} devices busy, "
                f"node peak utilisation {peak}%, "
                f"{len(telemetry['concurrent_campaign_trials_on_node'])} concurrent campaign trials"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
