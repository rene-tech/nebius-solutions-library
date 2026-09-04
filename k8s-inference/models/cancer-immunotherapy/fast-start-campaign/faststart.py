#!/usr/bin/env python3
"""Run and measure live H100 fast-start trials for qualified scientific models.

The tool renders one task-owned Kubernetes Job per trial, submits it through the
live Kueue LocalQueue, then reconstructs a phase-resolved timeline from objects
the cluster itself recorded: the Job, the Kueue Workload, the Pod, the kubelet
events for that Pod, and the container log with its runtime timestamps.

Every boundary is read back from the cluster.  A boundary that cannot be
observed is reported as ``null`` and named in ``unavailable``; it is never
estimated, and no level is assigned from a boundary that was not measured.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
MATRIX_PATH = HERE / "campaign_matrix.json"
RAW = HERE / "raw"
RECEIPTS = HERE / "receipts"

# Directory roots this campaign creates on the shared claim. Nothing outside
# these may be purged, because the claim is owned by another task.
CAMPAIGN_DIRECTORIES = ("faststart", "faststart-cache", "faststart-jit-cache")

# Kubelet reports a cached image with this exact phrase; anything else is a pull.
IMAGE_ALREADY_PRESENT = "already present on machine"
PULL_DURATION = re.compile(r"\bin ((?:\d+h)?(?:\d+m)?[0-9.]+m?s)\b")
DURATION_PART = re.compile(r"([0-9.]+)(h|ms|m|s)")


def parse_duration(text: str) -> float | None:
    """Parse a Go-style duration such as ``1m45.457s`` or ``850ms``."""
    scale = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}
    total = None
    for value, unit in DURATION_PART.findall(text):
        total = (total or 0.0) + float(value) * scale[unit]
    return None if total is None else round(total, 3)


def matrix() -> dict[str, Any]:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def kubectl(*args: str, check: bool = True, stdin: str | None = None) -> str:
    result = subprocess.run(
        ["kubectl", *args],
        check=False,
        capture_output=True,
        text=True,
        input=stdin,
    )
    if check and result.returncode != 0:
        raise SystemExit(f"kubectl {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def kube_json(*args: str) -> dict[str, Any]:
    return json.loads(kubectl(*args, "-o", "json"))


TIMESTAMP_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def parse_time(value: str | None) -> dt.datetime | None:
    """Parse an RFC3339 stamp, or return None for anything that is not one.

    Container logs interleave tqdm progress bars whose carriage returns split
    into fragments that carry no stamp, so this has to reject rather than raise.
    """
    if not value or not TIMESTAMP_PREFIX.match(value):
        return None
    text = value.replace("Z", "+00:00")
    # Kubernetes event microsecond precision varies; normalise to <=6 digits.
    if "." in text:
        head, _, tail = text.partition(".")
        digits = "".join(ch for ch in tail if ch.isdigit())[:6]
        offset = tail[len(digits):] if not tail[len(digits):].isdigit() else ""
        text = f"{head}.{digits.ljust(6, '0')}{offset or '+00:00'}"
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        return None


def seconds_between(start: dt.datetime | None, end: dt.datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds(), 3)


def iso(value: dt.datetime | None) -> str | None:
    return None if value is None else value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def trial_token(campaign: str, model: str, variant: str, trial: int) -> str:
    digest = hashlib.sha256(f"{campaign}/{model}/{variant}/{trial}".encode()).hexdigest()
    return digest[:12]


def trial_id(model: str, variant: str, trial: int) -> str:
    return f"{model}-{variant}-t{trial:02d}"


def job_name(spec_matrix: dict[str, Any], model: str, variant: str, trial: int) -> str:
    token = trial_token(spec_matrix["campaign_id"], model, variant, trial)
    return f"fsc-{model}-{variant}-t{trial:02d}-{token}"[:63]


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def namespace_of(root: dict[str, Any], spec: dict[str, Any]) -> str:
    """Models bind to the namespace that actually holds their artifact claim."""
    return spec.get("namespace", root["cluster"]["namespace"])


def render(model: str, variant: str, trial: int) -> tuple[dict[str, Any], dict[str, Any]]:
    root = matrix()
    spec = root["models"][model]
    if not spec.get("runnable"):
        raise SystemExit(f"model {model} is not marked runnable in the campaign matrix")
    if variant not in spec["variants"]:
        raise SystemExit(f"model {model} has no variant {variant}")
    variant_spec = spec["variants"][variant]

    name = job_name(root, model, variant, trial)
    tid = trial_id(model, variant, trial)
    cluster = root["cluster"]
    namespace = namespace_of(root, spec)

    data = {}
    for filename, source in spec["request_files"].items():
        data[filename] = (HERE / source).read_text(encoding="utf-8")

    labels = dict(root["labels"])
    labels.update({
        "fs2.nebius.ai/model-id": model,
        "fs2.nebius.ai/campaign-variant": variant,
        "fs2.nebius.ai/trial": f"{trial:02d}",
        "fs2.nebius.ai/trial-id": tid,
    })

    config_map = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": f"{name}-request", "namespace": namespace, "labels": labels},
        "data": data,
    } if data else None

    substitutions = {"seed": spec["seed"], "trial_id": tid, "variant": variant}
    command = [part.format(**substitutions) for part in spec["command_template"]]
    if spec.get("shell_wrapper"):
        # Runtimes that print no machine-readable completion line are wrapped so
        # the container log carries an observable end-of-result timestamp. The
        # inner argv stays byte-identical to the qualified command.
        script = "\n".join([
            "set -eu",
            *spec.get("shell_prelude", []),
            " ".join(f"'{part}'" for part in command),
            'echo "FS2_SEMANTIC_RESULT_EMITTED"',
            *[line.format(**substitutions) for line in spec.get("shell_postlude", [])],
        ])
        command = ["/bin/sh", "-c", script]

    env = dict(spec.get("env", {}))
    env.update(variant_spec.get("env", {}))
    env = {key: value.format(**substitutions) for key, value in env.items()}

    volumes: list[dict[str, Any]] = [{"name": "tmp", "emptyDir": {"sizeLimit": spec["tmp_size"]}}]
    mounts: list[dict[str, Any]] = [{"name": "tmp", "mountPath": "/tmp"}]
    if config_map is not None:
        volumes.insert(0, {"name": "request", "configMap": {"name": f"{name}-request", "defaultMode": 420}})
        mounts.insert(0, {"name": "request", "mountPath": "/var/run/fs2", "readOnly": True})
    for volume_name, volume in spec["volumes"].items():
        if volume.get("emptyDir"):
            volumes.append({"name": volume_name, "emptyDir": {"sizeLimit": volume["emptyDir"]}})
        else:
            # readOnly belongs on the mount, never on the claim: a read-only
            # claim marks the whole CSI attachment read-only and any sibling
            # writable mount of the same claim loses write access with it.
            volumes.append({
                "name": volume_name,
                "persistentVolumeClaim": {"claimName": volume["claim"]},
            })
        mount = {"name": volume_name, "mountPath": volume["mount"]}
        if volume.get("subPath"):
            mount["subPath"] = volume["subPath"].format(**substitutions)
        if volume.get("readOnly"):
            mount["readOnly"] = True
        mounts.append(mount)

    pod_labels = dict(labels)
    uses_kueue = spec.get("use_kueue", True)
    if uses_kueue:
        pod_labels["kueue.x-k8s.io/queue-name"] = cluster["local_queue"]

    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": dict(pod_labels, **(
                {"fs2.nebius.ai/local-queue": cluster["local_queue"]} if uses_kueue else {}
            )),
            "annotations": {
                "fs2.nebius.ai/campaign-id": root["campaign_id"],
                "fs2.nebius.ai/image-digest": spec["image_digest"],
                "fs2.nebius.ai/variant": variant,
            },
        },
        "spec": {
            "backoffLimit": 0,
            "completions": 1,
            "parallelism": 1,
            "ttlSecondsAfterFinished": 86400,
            "template": {
                "metadata": {"labels": pod_labels},
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    "nodeSelector": root["placement"]["node_selector"],
                    "tolerations": root["placement"]["tolerations"],
                    "securityContext": dict(
                        spec.get("pod_security", root["pod_security"]),
                        **variant_spec.get("pod_security_patch", {}),
                    ),
                    "volumes": volumes,
                    "containers": [
                        {
                            "name": "batch",
                            "image": spec["image"],
                            "command": command,
                            "env": [{"name": key, "value": value} for key, value in sorted(env.items())],
                            "resources": spec["resources"],
                            "securityContext": spec.get("container_security", root["container_security"]),
                            "volumeMounts": mounts,
                        }
                    ],
                },
            },
        },
    }
    return config_map, job


# --------------------------------------------------------------------------- #
# submission and observation
# --------------------------------------------------------------------------- #


def submit(model: str, variant: str, trial: int) -> str:
    config_map, job = render(model, variant, trial)
    namespace = job["metadata"]["namespace"]
    if config_map is not None:
        kubectl("apply", "-n", namespace, "-f", "-", stdin=json.dumps(config_map))
    kubectl("apply", "-n", namespace, "-f", "-", stdin=json.dumps(job))
    return job["metadata"]["name"]


def gpu_nodes() -> list[str]:
    selector = ",".join(f"{key}={value}" for key, value in matrix()["placement"]["node_selector"].items())
    listing = kube_json("get", "nodes", "-l", selector)
    return sorted(item["metadata"]["name"] for item in listing["items"])


def prewarm(model: str) -> dict[str, Any]:
    """Pull the runtime image onto every accelerator node before timing anything.

    The prewarm Pod runs the image's own no-GPU smoke command, so the only work
    it does is image setup.  That separates the cold image-layer cost from the
    prepared-node trials, which is the boundary the fast-start contract needs.
    """
    root = matrix()
    spec = root["models"][model]
    namespace = namespace_of(root, spec)
    labels = dict(root["labels"])
    labels["fs2.nebius.ai/model-id"] = model
    labels["fs2.nebius.ai/phase"] = "prewarm"

    created = []
    for index, node in enumerate(gpu_nodes()):
        name = f"fsc-prewarm-{model}-{index}"
        pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "namespace": namespace, "labels": labels},
            "spec": {
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "nodeName": node,
                "tolerations": root["placement"]["tolerations"],
                "securityContext": spec.get("pod_security", root["pod_security"]),
                "containers": [
                    {
                        "name": "prewarm",
                        "image": spec["image"],
                        "command": spec["prewarm_command"],
                        "resources": {
                            "requests": {"cpu": "500m", "memory": "1Gi"},
                            "limits": {"cpu": "2", "memory": "4Gi"},
                        },
                        "securityContext": spec.get("container_security", root["container_security"]),
                        "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
                    }
                ],
                "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}}],
            },
        }
        kubectl("apply", "-n", namespace, "-f", "-", stdin=json.dumps(pod))
        created.append({"pod": name, "node_name_sha256": hashlib.sha256(node.encode()).hexdigest()})

    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        pods = kube_json("get", "pods", "-n", namespace, "-l", "fs2.nebius.ai/phase=prewarm")
        phases = {item["metadata"]["name"]: item["status"]["phase"] for item in pods["items"]}
        if all(phase in {"Succeeded", "Failed"} for phase in phases.values()) and phases:
            break
        time.sleep(5)

    for entry in created:
        events = kube_json(
            "get", "events", "-n", namespace,
            "--field-selector", f"involvedObject.name={entry['pod']}",
        )
        entry["image"] = image_evidence(events)
    RAW.mkdir(parents=True, exist_ok=True)
    (RAW / f"prewarm-{model}.json").write_text(
        json.dumps(created, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {"model": model, "prewarmed": created}


def job_state(namespace: str, name: str) -> tuple[str, dict[str, Any]]:
    job = kube_json("get", "job", name, "-n", namespace)
    status = job.get("status", {})
    for condition in status.get("conditions", []):
        if condition.get("status") == "True" and condition.get("type") in {"Complete", "Failed"}:
            return condition["type"].lower(), job
    return "running", job


def wait(namespace: str, names: list[str], timeout: int) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    outcome: dict[str, str] = {}
    while time.monotonic() < deadline:
        for name in names:
            if name in outcome:
                continue
            state, _ = job_state(namespace, name)
            if state != "running":
                outcome[name] = state
        if len(outcome) == len(names):
            return outcome
        time.sleep(10)
    for name in names:
        outcome.setdefault(name, "timeout")
    return outcome


def capture(namespace: str, name: str) -> dict[str, Any]:
    """Persist every raw cluster object the timeline is derived from."""
    job = kube_json("get", "job", name, "-n", namespace)
    uid = job["metadata"]["uid"]
    pods = kube_json("get", "pods", "-n", namespace, "-l", f"batch.kubernetes.io/job-name={name}")
    pod = pods["items"][0] if pods["items"] else None
    workloads = kube_json("get", "workloads.kueue.x-k8s.io", "-n", namespace)
    workload = next(
        (
            item
            for item in workloads["items"]
            if any(ref.get("uid") == uid for ref in item["metadata"].get("ownerReferences", []))
        ),
        None,
    )
    events: dict[str, Any] = {"items": []}
    logs = ""
    if pod is not None:
        pod_name = pod["metadata"]["name"]
        events = kube_json(
            "get", "events", "-n", namespace,
            "--field-selector", f"involvedObject.name={pod_name}",
        )
        logs = kubectl("logs", f"pod/{pod_name}", "-n", namespace, "--timestamps", check=False)
    return {"job": job, "pod": pod, "workload": workload, "events": events, "logs": logs}


# --------------------------------------------------------------------------- #
# timeline reconstruction
# --------------------------------------------------------------------------- #


def condition_time(obj: dict[str, Any] | None, kind: str) -> dt.datetime | None:
    if obj is None:
        return None
    for condition in obj.get("status", {}).get("conditions", []):
        if condition.get("type") == kind and condition.get("status") == "True":
            return parse_time(condition.get("lastTransitionTime"))
    return None


def event_time(events: dict[str, Any], reason: str) -> dt.datetime | None:
    for item in events.get("items", []):
        if item.get("reason") == reason:
            return parse_time(item.get("firstTimestamp") or item.get("eventTime"))
    return None


def image_evidence(events: dict[str, Any]) -> dict[str, Any]:
    pulled = next((item for item in events.get("items", []) if item.get("reason") == "Pulled"), None)
    pulling = next((item for item in events.get("items", []) if item.get("reason") == "Pulling"), None)
    if pulled is None:
        return {"state": "unobserved", "cached_on_node": None, "pull_seconds": None, "message": None}
    message = pulled.get("message", "")
    cached = IMAGE_ALREADY_PRESENT in message
    pull_seconds = None
    image_bytes = None
    if not cached:
        found = PULL_DURATION.search(message)
        if found:
            pull_seconds = parse_duration(found.group(1))
        size = re.search(r"Image size: (\d+) bytes", message)
        if size:
            image_bytes = int(size.group(1))
    return {
        "state": "node-cached" if cached else "pulled",
        "cached_on_node": cached,
        "pull_seconds": pull_seconds,
        "image_bytes": image_bytes,
        "pull_bytes_per_second": (
            round(image_bytes / pull_seconds) if image_bytes and pull_seconds else None
        ),
        "pulling_started_at": iso(parse_time(pulling.get("firstTimestamp") or pulling.get("eventTime"))) if pulling else None,
        "pulled_at": iso(parse_time(pulled.get("firstTimestamp") or pulled.get("eventTime"))),
        "message": message,
    }


def parse_logs(logs: str, markers: dict[str, str]) -> dict[str, Any]:
    """Split the timestamped container log into marker hits.

    ``kubectl logs --timestamps`` prefixes every line with an RFC3339 stamp that
    the container runtime recorded, so these are real observations rather than
    client-side guesses.
    """
    compiled = {name: re.compile(pattern) for name, pattern in markers.items()}
    hits: dict[str, list[tuple[dt.datetime, str]]] = {name: [] for name in markers}
    first: dt.datetime | None = None
    last: dt.datetime | None = None
    for line in logs.splitlines():
        stamp, _, body = line.partition(" ")
        when = parse_time(stamp)
        if when is None:
            continue
        first = first or when
        last = when
        for name, pattern in compiled.items():
            if pattern.search(body):
                hits[name].append((when, body))
    return {"first": first, "last": last, "hits": hits}


def level_for(seconds: float | None, thresholds: dict[str, int]) -> str:
    if seconds is None:
        return "unavailable"
    for level in ("L4", "L3", "L2", "L1"):
        if seconds <= thresholds[level]:
            return level
    return "Off"


def timeline(raw: dict[str, Any], spec: dict[str, Any], thresholds: dict[str, int]) -> dict[str, Any]:
    job, pod, workload, events = raw["job"], raw["pod"], raw["workload"], raw["events"]
    unavailable: list[str] = []

    submitted = parse_time(job["metadata"]["creationTimestamp"])
    quota_reserved = condition_time(workload, "QuotaReserved")
    admitted = condition_time(workload, "Admitted") or quota_reserved
    scheduled = condition_time(pod, "PodScheduled")
    image = image_evidence(events)

    container: dict[str, Any] = {}
    started = finished = None
    exit_code = None
    if pod is not None and pod.get("status", {}).get("containerStatuses"):
        container = pod["status"]["containerStatuses"][0]
        state = container.get("state", {})
        terminated = state.get("terminated") or container.get("lastState", {}).get("terminated") or {}
        started = parse_time(terminated.get("startedAt") or (state.get("running") or {}).get("startedAt"))
        finished = parse_time(terminated.get("finishedAt"))
        exit_code = terminated.get("exitCode")

    log = parse_logs(raw["logs"], spec.get("log_markers", {}))
    result_hits = log["hits"].get("runtime_result", [])
    result_at = result_hits[0][0] if result_hits else None
    if result_at is None:
        unavailable.append("first_semantic_result")

    progress = log["hits"].get("optimizer_progress", [])
    warmup_at = progress[0][0] if progress else None
    steady_end = progress[-1][0] if progress else None

    precision_notes: list[str] = []

    for label, value in (
        ("kueue_admission", admitted),
        ("pod_scheduled", scheduled),
        ("container_started", started),
        ("container_finished", finished),
    ):
        if value is None:
            unavailable.append(label)

    # FAST_START_LEVELS.md puts queue admission and placement in capacity wait,
    # and puts image setup, artifact restore, runtime initialisation, weight
    # transfer and compilation in model start.  Model start is therefore
    # measured from the capacity-available point, which for a batch stage is the
    # instant the Pod is scheduled onto a Ready accelerator node.
    model_start = seconds_between(scheduled, result_at)
    phases = {
        "capacity_wait_seconds": seconds_between(submitted, scheduled),
        "queue_admission_seconds": seconds_between(submitted, admitted),
        "placement_seconds": seconds_between(admitted, scheduled),
        "sandbox_and_volume_setup_seconds": seconds_between(scheduled, started),
        "runtime_init_and_artifact_load_seconds": seconds_between(started, warmup_at),
        "compute_to_first_result_seconds": seconds_between(warmup_at, result_at),
        "teardown_seconds": seconds_between(result_at, finished),
        "model_start_seconds": model_start,
        # The level is evaluated on first semantic result. This second value
        # closes the whole admitted stage, including writing the result out.
        "schedule_to_semantic_complete_seconds": seconds_between(scheduled, finished),
        "time_to_first_semantic_result_seconds": seconds_between(started, result_at),
        "container_seconds": seconds_between(started, finished),
        "end_to_end_seconds": seconds_between(submitted, finished),
    }

    for label, value in list(phases.items()):
        # Job and Pod timestamps carry one-second granularity while container
        # log timestamps carry nanoseconds, so a phase shorter than a second can
        # come out slightly negative.  Clamp it and say so rather than publish it.
        if value is not None and -1.0 < value < 0.0:
            phases[label] = 0.0
            precision_notes.append(
                f"{label} measured {value}s and was clamped to 0.0; the interval is shorter than the "
                "one-second granularity of the Kubernetes object timestamp that bounds it"
            )

    throughput = None
    if len(progress) >= 2:
        span = seconds_between(progress[0][0], progress[-1][0])
        if span and span > 0:
            throughput = {
                "observed_progress_lines": len(progress),
                "span_seconds": span,
                "progress_lines_per_second": round(len(progress) / span, 4),
            }
    step_pattern = spec.get("log_markers", {}).get("step_time")
    if step_pattern:
        values = []
        for _, body in log["hits"].get("step_time", []):
            found = re.search(step_pattern, body)
            if found:
                values.append(float(found.group(1)))
        if values:
            throughput = dict(throughput or {}, **{
                "runtime_reported_step_seconds_count": len(values),
                "runtime_reported_step_seconds_first": values[0],
                "runtime_reported_step_seconds_mean": round(sum(values) / len(values), 4),
                "runtime_reported_step_seconds_max": max(values),
                "runtime_reported_step_seconds_min": min(values),
            })

    reported: dict[str, Any] = {}
    for name, pattern in spec.get("runtime_reported_metrics", {}).items():
        compiled = re.compile(pattern)
        for line in raw["logs"].splitlines():
            found = compiled.search(line)
            if found:
                text = found.group(1)
                try:
                    reported[name] = float(text) if "." in text else int(text)
                except ValueError:
                    reported[name] = text
                break

    gpu_kind = None
    for _, body in log["hits"].get("gpu_device_kind", []):
        found = re.search(spec["log_markers"]["gpu_device_kind"], body)
        if found:
            gpu_kind = found.group(1)
            break

    ownership = [
        item for item in events.get("items", [])
        if item.get("reason") == "VolumePermissionChangeInProgress"
    ]
    volume_ownership = {
        "recursive_fsgroup_pass_observed": bool(ownership),
        "event_count": len(ownership),
        "first_reported_at": iso(parse_time(
            (ownership[0].get("firstTimestamp") or ownership[0].get("eventTime")) if ownership else None
        )),
        "last_reported_at": iso(parse_time(
            (ownership[-1].get("firstTimestamp") or ownership[-1].get("eventTime")) if ownership else None
        )),
        "sample_message": ownership[0].get("message") if ownership else None,
    }

    return {
        "volume_ownership": volume_ownership,
        "boundaries": {
            "submitted_at": iso(submitted),
            "quota_reserved_at": iso(quota_reserved),
            "admitted_at": iso(admitted),
            "pod_scheduled_at": iso(scheduled),
            "container_started_at": iso(started),
            "first_log_at": iso(log["first"]),
            "first_progress_at": iso(warmup_at),
            "last_progress_at": iso(steady_end),
            "first_semantic_result_at": iso(result_at),
            "container_finished_at": iso(finished),
        },
        "phases_seconds": phases,
        "image": image,
        "throughput": throughput,
        "exit_code": exit_code,
        "container_id": container.get("containerID"),
        "image_id_on_node": container.get("imageID"),
        "gpu_device_kind_reported_by_runtime": gpu_kind,
        "runtime_reported": reported,
        "assigned_level": level_for(model_start, thresholds),
        "unavailable": sorted(set(unavailable)),
        "precision_notes": precision_notes,
    }


def node_identity(namespace: str, pod: dict[str, Any] | None) -> dict[str, Any]:
    if pod is None:
        return {"state": "unobserved"}
    name = pod["spec"].get("nodeName")
    if not name:
        return {"state": "unscheduled"}
    node = kube_json("get", "node", name)
    labels = node["metadata"]["labels"]
    return {
        "node_name_sha256": hashlib.sha256(name.encode()).hexdigest(),
        "node_uid": node["metadata"]["uid"],
        "instance_type": labels.get("node.kubernetes.io/instance-type"),
        "accelerator_class": labels.get("accelerator.fs2.nebius/class"),
        "pool_id": labels.get("accelerator.fs2.nebius/pool-id"),
        "capacity_source": labels.get("capacity.fs2.nebius/source"),
        "capacity_type": labels.get("capacity.fs2.nebius/type"),
        "nvidia_driver_version": labels.get("nebius.com/nvidia_driver_version"),
        "cuda_version": labels.get("nebius.com/cuda_version"),
        "gpu_name": labels.get("nebius.com/gpu-name"),
        "local_nvme_eligible": labels.get("local-nvme.fs2.nebius/eligible"),
        "snapshot_eligible": labels.get("snapshot.fs2.nebius/eligible"),
        "kubelet_version": node["status"]["nodeInfo"]["kubeletVersion"],
        "container_runtime": node["status"]["nodeInfo"]["containerRuntimeVersion"],
        "kernel": node["status"]["nodeInfo"]["kernelVersion"],
    }


def collect(model: str, variant: str, trial: int, from_raw: bool = False) -> dict[str, Any]:
    root = matrix()
    spec = root["models"][model]
    namespace = namespace_of(root, spec)
    name = job_name(root, model, variant, trial)
    tid = trial_id(model, variant, trial)
    previous_path = RECEIPTS / f"{tid}.json"
    previous = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.exists() else {}

    if from_raw:
        # Rebuild the timeline from the evidence already stored for this trial.
        # This is why raw/ is committed: a receipt stays reproducible after the
        # cluster objects it came from have been cleaned up.
        stored = json.loads((RAW / f"{tid}.objects.json").read_text(encoding="utf-8"))
        raw = dict(stored, logs=(RAW / f"{tid}.log").read_text(encoding="utf-8"))
        state = previous.get("job_state", "complete")
    else:
        raw = capture(namespace, name)
        RAW.mkdir(parents=True, exist_ok=True)
        (RAW / f"{tid}.log").write_text(raw["logs"], encoding="utf-8")
        (RAW / f"{tid}.objects.json").write_text(
            json.dumps({k: v for k, v in raw.items() if k != "logs"}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        state, _ = job_state(namespace, name)

    thresholds = root["level_contract"]["thresholds_seconds"]
    measured = timeline(raw, spec, thresholds)

    receipt = {
        "schema": "fs2.nebius.ai/fast-start-live-trial-receipt/v1",
        "campaign_id": root["campaign_id"],
        "owner_task": root["owner_task"],
        "trial_id": tid,
        "model_id": model,
        "variant": variant,
        "variant_label": spec["variants"][variant]["label"],
        "trial_index": trial,
        "job": name,
        "job_uid": raw["job"]["metadata"]["uid"],
        "pod": raw["pod"]["metadata"]["name"] if raw["pod"] else None,
        "pod_uid": raw["pod"]["metadata"]["uid"] if raw["pod"] else None,
        "kueue_workload": raw["workload"]["metadata"]["name"] if raw["workload"] else None,
        "job_state": state,
        "cluster": dict(root["cluster"], namespace=namespace, kueue_admitted=spec.get("use_kueue", True)),
        "placement": {
            "node_selector": root["placement"]["node_selector"],
            "capacity_source": root["placement"]["capacity_source"],
            "gpus_requested": spec["resources"]["requests"].get("nvidia.com/gpu"),
        },
        "node": previous["node"] if from_raw and previous.get("node") else node_identity(namespace, raw["pod"]),
        "image": {
            "reference": spec["image"],
            "digest": spec["image_digest"],
            "tag": spec["image_tag"],
            "source_revision": spec["source_revision"],
        },
        "external_artifacts": spec["external_artifacts"],
        "artifact_bytes_read_floor": spec["artifact_bytes_read_floor"],
        "measured": measured,
        "mechanism_evidence": root["mechanism_evidence"],
        "jit_cache": spec["variants"][variant]["jit_cache"],
    }
    # A re-parse of the timeline says nothing about the science, so an existing
    # semantic verdict and GPU telemetry survive it rather than being dropped.
    for carried in ("semantic_validation", "gpu_telemetry"):
        if carried in previous:
            receipt[carried] = previous[carried]
    RECEIPTS.mkdir(parents=True, exist_ok=True)
    previous_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


# --------------------------------------------------------------------------- #
# cleanup
# --------------------------------------------------------------------------- #


def cleanup_claim_data() -> dict[str, Any]:
    """Remove the run trees this campaign wrote onto the shared claim.

    The claim belongs to another task, so the campaign leaves it exactly as it
    found it: only directories this campaign created are removed, by name.
    """
    root = matrix()
    namespace = root["cluster"]["namespace"]
    claim = root["models"]["mosaic"]["volumes"]["workspace"]["claim"]
    name = "fsc-campaign-data-cleanup"
    targets = " ".join(f"/data/{directory}" for directory in CAMPAIGN_DIRECTORIES)
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace, "labels": dict(root["labels"])},
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "nodeSelector": {"workload.fs2.nebius/system": "true"},
            "securityContext": {"runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001, "runAsNonRoot": True},
            "containers": [
                {
                    "name": "cleanup",
                    "image": "busybox:1.36",
                    "command": ["sh", "-c", f"for d in {targets}; do rm -rf \"$d\"; done; ls -A /data"],
                    "resources": {
                        "requests": {"cpu": "200m", "memory": "256Mi"},
                        "limits": {"cpu": "1", "memory": "1Gi"},
                    },
                    "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                }
            ],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": claim}}],
        },
    }
    kubectl("delete", "pod", name, "-n", namespace, "--ignore-not-found", check=False)
    kubectl("apply", "-n", namespace, "-f", "-", stdin=json.dumps(pod))
    for _ in range(90):
        state = kube_json("get", "pod", name, "-n", namespace)
        if state["status"]["phase"] in {"Succeeded", "Failed"}:
            break
        time.sleep(5)
    remaining = kubectl("logs", name, "-n", namespace, check=False).split()
    kubectl("delete", "pod", name, "-n", namespace, "--wait=false", check=False)
    return {"claim": claim, "removed": list(CAMPAIGN_DIRECTORIES), "claim_root_after": sorted(remaining)}


def purge(relatives: list[str]) -> dict[str, Any]:
    """Remove named paths from the shared claim.

    Needed because the qualified Mosaic runtime creates its output directory
    with ``exist_ok=False`` and refuses to overwrite a previous result.  That is
    correct behaviour for a scientific runtime, so re-running a trial resets the
    trial's own output tree rather than weakening the runtime's guarantee.  Only
    explicitly named paths are removed; nothing is globbed.
    """
    root = matrix()
    namespace = root["cluster"]["namespace"]
    claim = root["models"]["mosaic"]["volumes"]["workspace"]["claim"]
    name = "fsc-purge"
    safe = []
    for relative in relatives:
        cleaned = relative.strip("/")
        if not cleaned or ".." in cleaned.split("/"):
            raise SystemExit(f"refusing to purge unsafe path {relative!r}")
        if cleaned.split("/")[0] not in CAMPAIGN_DIRECTORIES:
            raise SystemExit(
                f"refusing to purge {relative!r}: only campaign-owned roots "
                f"{CAMPAIGN_DIRECTORIES} may be removed"
            )
        safe.append(f"/data/{cleaned}")
    script = "; ".join(f'rm -rf "{path}"' for path in safe) + "; echo PURGED"
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace, "labels": dict(root["labels"])},
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "nodeSelector": root["placement"]["node_selector"],
            "tolerations": root["placement"]["tolerations"],
            "securityContext": {
                "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001,
                "runAsNonRoot": True, "fsGroupChangePolicy": "OnRootMismatch",
            },
            "containers": [
                {
                    "name": "purge",
                    "image": "busybox:1.36",
                    "command": ["sh", "-c", script],
                    "resources": {
                        "requests": {"cpu": "200m", "memory": "256Mi"},
                        "limits": {"cpu": "1", "memory": "1Gi"},
                    },
                    "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                }
            ],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": claim}}],
        },
    }
    kubectl("delete", "pod", name, "-n", namespace, "--ignore-not-found", check=False)
    kubectl("apply", "-n", namespace, "-f", "-", stdin=json.dumps(pod))
    for _ in range(60):
        state = kube_json("get", "pod", name, "-n", namespace)
        if state["status"]["phase"] in {"Succeeded", "Failed"}:
            break
        time.sleep(5)
    phase = kube_json("get", "pod", name, "-n", namespace)["status"]["phase"]
    kubectl("delete", "pod", name, "-n", namespace, "--wait=false", check=False)
    if phase != "Succeeded":
        raise SystemExit(f"purge pod ended {phase}")
    return {"claim": claim, "purged": safe}


def probe_cache(relative: str) -> dict[str, Any]:
    """Report entry count and byte size of a directory on the shared claim.

    Used as corroboration that a persistent compilation cache really was
    populated between trials, rather than inferring reuse from a timing alone.
    """
    root = matrix()
    namespace = root["cluster"]["namespace"]
    claim = root["models"]["mosaic"]["volumes"]["workspace"]["claim"]
    name = "fsc-cache-probe"
    target = f"/data/{relative.lstrip('/')}"
    script = (
        f"if [ -d '{target}' ]; then "
        f"echo COUNT $(find '{target}' -type f | wc -l); "
        f"echo BYTES $(du -sb '{target}' | cut -f1); "
        f"else echo COUNT 0; echo BYTES 0; fi"
    )
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace, "labels": dict(root["labels"])},
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "nodeSelector": {"workload.fs2.nebius/system": "true"},
            "securityContext": {"runAsUser": 10001, "runAsGroup": 10001, "runAsNonRoot": True},
            "containers": [
                {
                    "name": "probe",
                    "image": "busybox:1.36",
                    "command": ["sh", "-c", script],
                    "resources": {
                        "requests": {"cpu": "200m", "memory": "256Mi"},
                        "limits": {"cpu": "1", "memory": "1Gi"},
                    },
                    "volumeMounts": [{"name": "data", "mountPath": "/data", "readOnly": True}],
                }
            ],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": claim}}],
        },
    }
    kubectl("delete", "pod", name, "-n", namespace, "--ignore-not-found", check=False)
    kubectl("apply", "-n", namespace, "-f", "-", stdin=json.dumps(pod))
    for _ in range(60):
        state = kube_json("get", "pod", name, "-n", namespace)
        if state["status"]["phase"] in {"Succeeded", "Failed"}:
            break
        time.sleep(5)
    output = kubectl("logs", name, "-n", namespace, check=False).split()
    kubectl("delete", "pod", name, "-n", namespace, "--wait=false", check=False)
    values = dict(zip(output[::2], output[1::2]))
    return {
        "claim": claim,
        "path": relative,
        "entries": int(values.get("COUNT", 0)),
        "bytes": int(values.get("BYTES", 0)),
    }


def cleanup() -> list[str]:
    root = matrix()
    selector = f"fs2.nebius.ai/task={root['owner_task']}"
    namespaces = {root["cluster"]["namespace"]}
    namespaces.update(spec.get("namespace", root["cluster"]["namespace"]) for spec in root["models"].values())
    removed = []
    for namespace in sorted(namespaces):
        for kind in ("job", "configmap", "pod"):
            listing = kube_json("get", kind, "-n", namespace, "-l", selector)
            for item in listing.get("items", []):
                name = item["metadata"]["name"]
                kubectl("delete", kind, name, "-n", namespace, "--wait=false", check=False)
                removed.append(f"{namespace}/{kind}/{name}")
    return removed


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    for command in ("render", "submit", "collect"):
        node = sub.add_parser(command)
        node.add_argument("--model", required=True)
        node.add_argument("--variant", default="baseline")
        node.add_argument("--trial", type=int, required=True)
        if command == "collect":
            node.add_argument("--from-raw", action="store_true",
                              help="rebuild the timeline from stored raw evidence instead of the cluster")

    node = sub.add_parser("prewarm")
    node.add_argument("--model", required=True)

    node = sub.add_parser("wait")
    node.add_argument("--job", action="append", required=True)
    node.add_argument("--namespace")
    node.add_argument("--timeout", type=int, default=1800)

    node = sub.add_parser("probe-cache")
    node.add_argument("--path", required=True, help="path on the shared claim, relative to its root")

    node = sub.add_parser("purge")
    node.add_argument("--path", action="append", required=True,
                      help="campaign-owned path on the shared claim to remove; repeatable")

    node = sub.add_parser("cleanup")
    node.add_argument("--claim-data", action="store_true", help="also remove the run trees written onto the shared claim")

    arguments = parser.parse_args()
    root = matrix()

    if arguments.command == "render":
        config_map, job = render(arguments.model, arguments.variant, arguments.trial)
        print(json.dumps({"configMap": config_map, "job": job}, indent=2, sort_keys=True))
    elif arguments.command == "submit":
        name = submit(arguments.model, arguments.variant, arguments.trial)
        print(json.dumps({"submitted": name}, sort_keys=True))
    elif arguments.command == "prewarm":
        print(json.dumps(prewarm(arguments.model), indent=2, sort_keys=True))
    elif arguments.command == "wait":
        outcome = wait(arguments.namespace or root["cluster"]["namespace"], arguments.job, arguments.timeout)
        print(json.dumps(outcome, indent=2, sort_keys=True))
        return 0 if all(value == "complete" for value in outcome.values()) else 1
    elif arguments.command == "collect":
        receipt = collect(arguments.model, arguments.variant, arguments.trial, arguments.from_raw)
        print(json.dumps({
            "trial_id": receipt["trial_id"],
            "job_state": receipt["job_state"],
            "phases_seconds": receipt["measured"]["phases_seconds"],
            "assigned_level": receipt["measured"]["assigned_level"],
            "image": receipt["measured"]["image"]["state"],
            "unavailable": receipt["measured"]["unavailable"],
        }, indent=2, sort_keys=True))
    elif arguments.command == "purge":
        print(json.dumps(purge(arguments.path), indent=2, sort_keys=True))
    elif arguments.command == "probe-cache":
        print(json.dumps(probe_cache(arguments.path), indent=2, sort_keys=True))
    elif arguments.command == "cleanup":
        data = cleanup_claim_data() if arguments.claim_data else None
        print(json.dumps({"deleted": cleanup(), "claim_data": data}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
