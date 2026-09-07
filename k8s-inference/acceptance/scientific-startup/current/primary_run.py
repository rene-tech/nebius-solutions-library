#!/usr/bin/env python3
"""Run one primary scientific acceptance trial with exact startup evidence.

The public acceptance client remains the authority for input upload, request
submission, output validation and receipt redaction.  This wrapper only starts
a Kubernetes observer after that client returns the operation identity.  The
observer stores no Pod environment or Secret data.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


MODEL_FRAGMENTS = {
    "bindcraft": "models/cancer-immunotherapy/images/bindcraft-native/activation/fragment.json",
    "boltzgen": "models/cancer-immunotherapy/runtime-images/boltzgen/activation/fragment.json",
    "mosaic": "models/cancer-immunotherapy/runtime-images/mosaic/activation/fragment.json",
    "proteina-complexa": "models/cancer-immunotherapy/runtime-images/proteina-complexa/activation/fragment.json",
    "rfdiffusion": "models/cancer-immunotherapy/runtime-images/rfdiffusion/activation/fragment.json",
}
LABEL_KEYS = (
    "fs2.nebius.ai/attempt-id",
    "fs2.nebius.ai/model-id",
    "fs2.nebius.ai/operation-id",
    "fs2.nebius.ai/shard-id",
    "fs2.nebius.ai/stage-id",
    "fs2.nebius.ai/variant-id",
    "kueue.x-k8s.io/cluster-queue-name",
    "kueue.x-k8s.io/local-queue-name",
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def private_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    body = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(body)


def append_json(path: Path, value: object) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "ab") as stream:
        stream.write(
            (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
        )


def load_public_runner(repository_root: Path) -> Any:
    path = repository_root / "acceptance/scientific-fleet/run_acceptance.py"
    spec = importlib.util.spec_from_file_location("fs2_primary_public_acceptance", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("public acceptance runner is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def state_record(status: dict[str, Any]) -> dict[str, Any]:
    state = status.get("state") if isinstance(status.get("state"), dict) else {}
    return {
        "name": status.get("name"),
        "image_id": status.get("imageID"),
        "container_id": status.get("containerID"),
        "restart_count": status.get("restartCount"),
        "state": state,
    }


def safe_pod_record(pod: dict[str, Any]) -> dict[str, Any]:
    metadata = pod.get("metadata") if isinstance(pod.get("metadata"), dict) else {}
    spec = pod.get("spec") if isinstance(pod.get("spec"), dict) else {}
    status = pod.get("status") if isinstance(pod.get("status"), dict) else {}
    labels = metadata.get("labels") if isinstance(metadata.get("labels"), dict) else {}

    def containers(key: str) -> list[dict[str, Any]]:
        values = spec.get(key) if isinstance(spec.get(key), list) else []
        return [
            {
                "name": item.get("name"),
                "image": item.get("image"),
                "resources": item.get("resources"),
            }
            for item in values
            if isinstance(item, dict)
        ]

    return {
        "observed_at": utc_now(),
        "namespace": metadata.get("namespace"),
        "name": metadata.get("name"),
        "uid": metadata.get("uid"),
        "created_at": metadata.get("creationTimestamp"),
        "labels": {key: labels.get(key) for key in LABEL_KEYS if labels.get(key)},
        "node": spec.get("nodeName"),
        "phase": status.get("phase"),
        "start_time": status.get("startTime"),
        "init_containers": containers("initContainers"),
        "containers": containers("containers"),
        "init_statuses": [
            state_record(item)
            for item in status.get("initContainerStatuses", [])
            if isinstance(item, dict)
        ],
        "statuses": [
            state_record(item)
            for item in status.get("containerStatuses", [])
            if isinstance(item, dict)
        ],
    }


class OperationObserver:
    def __init__(
        self,
        *,
        operation_id: str,
        model_id: str,
        output_root: Path,
        kubeconfig: Path,
        context: str,
    ) -> None:
        self.operation_id = operation_id
        self.model_id = model_id
        self.output_root = output_root
        self.kubeconfig = kubeconfig
        self.context = context
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.followers: dict[tuple[str, str], subprocess.Popen[bytes]] = {}
        self.handles: list[Any] = []
        self.proteina_followed: set[str] = set()

    def _capture_jobs(self) -> None:
        """Retain private source templates before the controller removes them.

        Job environment can contain an attempt-scoped workload capability, so
        these records deliberately remain below the private evidence root with
        mode 0600.  They are never part of the redacted benchmark report.
        """
        response = subprocess.run(
            self._kubectl(
                "get",
                "jobs",
                "-A",
                "-l",
                f"fs2.nebius.ai/operation-id={self.operation_id}",
                "-o",
                "json",
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if response.returncode:
            return
        try:
            jobs = json.loads(response.stdout).get("items", [])
        except (UnicodeError, json.JSONDecodeError, AttributeError):
            return
        directory = self.output_root / "jobs-private"
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        for job in jobs:
            metadata = job.get("metadata", {})
            uid = metadata.get("uid")
            if not isinstance(uid, str) or not uid:
                continue
            path = directory / f"{uid}.json"
            if not path.exists():
                private_write(path, job)

    def _kubectl(self, *values: str) -> list[str]:
        return [
            "kubectl",
            "--kubeconfig",
            str(self.kubeconfig),
            "--context",
            self.context,
            *values,
        ]

    def start(self) -> None:
        self.thread.start()

    def finish(self) -> None:
        self.stop.set()
        self.thread.join(timeout=45)

    def _start_log(self, record: dict[str, Any]) -> None:
        uid = str(record["uid"])
        namespace = str(record["namespace"])
        name = str(record["name"])
        statuses = {item.get("name"): item for item in record.get("statuses", [])}
        stage_status = statuses.get("scientific-stage")
        if not stage_status or not stage_status.get("state"):
            return
        key = (uid, "scientific-stage")
        current = self.followers.get(key)
        if (
            self.model_id == "proteina-complexa"
            and record.get("labels", {}).get("fs2.nebius.ai/stage-id") == "generate"
            and uid not in self.proteina_followed
            and "running" in stage_status.get("state", {})
        ):
            self.proteina_followed.add(uid)
            path = self.output_root / f"{uid}.proteina-upstream.log"
            upstream = path.open("wb")
            os.chmod(path, 0o600)
            script = (
                "while :; do "
                "f=$(find ./logs -maxdepth 1 -type f -name 'generate_*.log' 2>/dev/null "
                "| head -n 1); "
                "if [ -n \"$f\" ]; then exec tail -n +1 -F \"$f\"; fi; "
                "sleep 0.1; done"
            )
            probe = subprocess.Popen(
                self._kubectl(
                    "-n", namespace, "exec", name, "-c", "scientific-stage", "--", "sh", "-c", script
                ),
                stdout=upstream,
                stderr=subprocess.STDOUT,
            )
            self.followers[(uid, "proteina-upstream")] = probe
            self.handles.append(upstream)
        if current is not None and current.poll() is None:
            return
        attempt = 1 + sum(1 for item in self.followers if item == key)
        path = self.output_root / f"{uid}.scientific-stage.capture{attempt}.log"
        handle = path.open("wb")
        os.chmod(path, 0o600)
        command = self._kubectl(
            "-n",
            namespace,
            "logs",
            "--timestamps",
            "-f",
            name,
            "-c",
            "scientific-stage",
        )
        self.followers[key] = subprocess.Popen(
            command, stdout=handle, stderr=subprocess.STDOUT
        )
        self.handles.append(handle)

    def _events(self, record: dict[str, Any]) -> None:
        uid = str(record["uid"])
        path = self.output_root / f"{uid}.events.json"
        if path.exists():
            return
        completed = record.get("phase") in {"Succeeded", "Failed"}
        if not completed:
            return
        response = subprocess.run(
            self._kubectl(
                "-n",
                str(record["namespace"]),
                "get",
                "events",
                "--field-selector",
                f"involvedObject.uid={uid}",
                "-o",
                "json",
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if response.returncode:
            return
        try:
            value = json.loads(response.stdout)
        except (UnicodeError, json.JSONDecodeError):
            return
        events = []
        for item in value.get("items", []):
            metadata = item.get("metadata", {})
            events.append(
                {
                    "created_at": metadata.get("creationTimestamp"),
                    "event_time": item.get("eventTime"),
                    "first_timestamp": item.get("firstTimestamp"),
                    "last_timestamp": item.get("lastTimestamp"),
                    "type": item.get("type"),
                    "reason": item.get("reason"),
                    "message": item.get("message"),
                    "count": item.get("count"),
                }
            )
        private_write(path, events)

    def _run(self) -> None:
        lifecycle = self.output_root / "pod-lifecycle.jsonl"
        quiet_since: float | None = None
        while True:
            self._capture_jobs()
            response = subprocess.run(
                self._kubectl(
                    "get",
                    "pods",
                    "-A",
                    "-l",
                    f"fs2.nebius.ai/operation-id={self.operation_id}",
                    "-o",
                    "json",
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            records: list[dict[str, Any]] = []
            if response.returncode == 0:
                try:
                    value = json.loads(response.stdout)
                    records = [safe_pod_record(item) for item in value.get("items", [])]
                except (UnicodeError, json.JSONDecodeError, TypeError):
                    records = []
            for record in records:
                append_json(lifecycle, record)
                self._start_log(record)
                self._events(record)
            active = any(record.get("phase") not in {"Succeeded", "Failed"} for record in records)
            if self.stop.is_set() and not active:
                quiet_since = quiet_since or time.monotonic()
                if time.monotonic() - quiet_since >= 12:
                    break
            else:
                quiet_since = None
            time.sleep(0.25)
        for process in self.followers.values():
            if process.poll() is None:
                process.terminate()
        for process in self.followers.values():
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        for handle in self.handles:
            handle.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, choices=sorted(MODEL_FRAGMENTS))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--endpoint", default="https://89.169.99.188")
    parser.add_argument("--credential-bundle", type=Path, required=True)
    parser.add_argument("--credential-key", default="scientific_access_token")
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", default="k8s-inference-h100")
    parser.add_argument("--timeout-seconds", type=float, default=7200)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_root = args.output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(output_root, 0o700)
    bundle = json.loads(args.credential_bundle.read_text(encoding="utf-8"))
    token = bundle.get("credentials", {}).get(args.credential_key)
    if not isinstance(token, str) or not token:
        raise RuntimeError("scientific credential is unavailable")
    os.environ["FS2_INFERENCE_TOKEN"] = token
    public = load_public_runner(args.repository_root.resolve())
    original_submit = public._submit
    observer: OperationObserver | None = None

    def observed_submit(*values: Any, **kwargs: Any) -> tuple[str, dict[str, Any]]:
        nonlocal observer
        operation_id, submitted = original_submit(*values, **kwargs)
        private_write(
            output_root / "operation.json",
            {
                "schema": "fs2-serve.nebius.ai/primary-startup-operation/v1",
                "observed_at": utc_now(),
                "model_id": args.model,
                "operation_id": operation_id,
                "run_id": args.run_id,
            },
        )
        observer = OperationObserver(
            operation_id=operation_id,
            model_id=args.model,
            output_root=output_root,
            kubeconfig=args.kubeconfig,
            context=args.context,
        )
        observer.start()
        print(f"STARTED model={args.model} operation={operation_id}", flush=True)
        return operation_id, submitted

    public._submit = observed_submit
    receipt = output_root / "public-receipt.json"
    try:
        return int(
            public.main(
                [
                    "--endpoint",
                    args.endpoint,
                    "--repository-root",
                    str(args.repository_root),
                    "--activation-fragment",
                    MODEL_FRAGMENTS[args.model],
                    "--run-id",
                    args.run_id,
                    "--receipt",
                    str(receipt),
                    "--timeout-seconds",
                    str(args.timeout_seconds),
                    "--poll-seconds",
                    "2",
                ]
            )
        )
    finally:
        if observer is not None:
            observer.finish()
        os.environ.pop("FS2_INFERENCE_TOKEN", None)


if __name__ == "__main__":
    raise SystemExit(main())
