#!/usr/bin/env python3
"""Token-safe live acceptance for one Terraform/KEDA model activation path.

The harness never places the PAT in argv, the environment, kubectl, or its
evidence. It observes only bounded Kubernetes metadata and hashes the semantic
result rather than persisting request or response content.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import shutil
import ssl
import stat
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit
from uuid import UUID, uuid4


_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?")
_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "preempted", "expired"})
_DEMAND = frozenset({"queued", "activating", "running"})
_PROTOCOL_PATHS = {
    "openai-chat": "/v1/chat/completions",
    "openai-completions": "/v1/completions",
    "openai-embeddings": "/v1/embeddings",
    "openai-images": "/v1/images/generations",
}
_MAX_TOKEN_BYTES = 4096
_MAX_REQUEST_BYTES = 16 * 1024 * 1024
_MAX_KUBECTL_BYTES = 16 * 1024 * 1024
_MAX_HTTP_BYTES = 64 * 1024 * 1024
_MAX_LOG_BYTES = 8 * 1024 * 1024
_CACHE_OUTCOMES = frozenset({"cache-hit", "cache-hit-after-wait", "localized"})
_IDENTITY_ANNOTATIONS = frozenset(
    {
        "fs2.nebius/model-content-digest",
        "fs2.nebius/runtime-image-digest",
        "fs2.nebius/compile-cache-abi",
    }
)


class AcceptanceError(RuntimeError):
    """A stable failure code that is safe to print and persist."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _identity_digest(value: str) -> str:
    return hashlib.sha256(
        b"fs2-serve-public-identity/v1\0" + value.encode("utf-8")
    ).hexdigest()


def _elapsed_seconds(start_ns: int, end_ns: int | None) -> float | None:
    if end_ns is None:
        return None
    return round((end_ns - start_ns) / 1_000_000_000, 3)


def clock_domain() -> str:
    """Bind monotonic timestamps to one Linux boot without exposing host data."""

    try:
        value = (
            Path("/proc/sys/kernel/random/boot_id")
            .read_text(encoding="ascii")
            .strip()
            .lower()
        )
    except OSError:
        raise AcceptanceError("monotonic_clock_identity_unavailable") from None
    if re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value) is None:
        raise AcceptanceError("monotonic_clock_identity_invalid")
    return value


def _read_private_file(path: Path, maximum: int, error_prefix: str) -> bytes:
    if not path.is_absolute():
        raise AcceptanceError(f"{error_prefix}_path_not_absolute")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise AcceptanceError(f"{error_prefix}_unavailable") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise AcceptanceError(f"{error_prefix}_not_regular")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise AcceptanceError(f"{error_prefix}_mode_invalid")
        if metadata.st_uid != os.geteuid():
            raise AcceptanceError(f"{error_prefix}_owner_invalid")
        if not 1 <= metadata.st_size <= maximum:
            raise AcceptanceError(f"{error_prefix}_size_invalid")
        value = os.read(descriptor, maximum + 1)
    finally:
        os.close(descriptor)
    if not value or len(value) > maximum:
        raise AcceptanceError(f"{error_prefix}_size_invalid")
    return value


def read_token_file(path: Path) -> str:
    value = _read_private_file(path, _MAX_TOKEN_BYTES, "token_file")
    if value.endswith(b"\n"):
        value = value[:-1]
    if not 32 <= len(value) <= _MAX_TOKEN_BYTES or any(
        character < 33 or character > 126 for character in value
    ):
        raise AcceptanceError("token_file_content_invalid")
    try:
        return value.decode("ascii")
    except UnicodeDecodeError:
        raise AcceptanceError("token_file_content_invalid") from None


def read_request_file(path: Path, model_id: str) -> tuple[str, str, dict[str, Any]]:
    raw = _read_private_file(path, _MAX_REQUEST_BYTES, "request_file")
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        raise AcceptanceError("request_file_json_invalid") from None
    if not isinstance(value, dict) or set(value) != {
        "protocol",
        "operation",
        "payload",
    }:
        raise AcceptanceError("request_file_shape_invalid")
    protocol = value["protocol"]
    operation = value["operation"]
    payload = value["payload"]
    if protocol not in {*_PROTOCOL_PATHS, "native"}:
        raise AcceptanceError("request_protocol_invalid")
    if not isinstance(operation, str) or _NAME.fullmatch(operation) is None:
        raise AcceptanceError("request_operation_invalid")
    if not isinstance(payload, dict) or not payload:
        raise AcceptanceError("request_payload_invalid")
    if protocol.startswith("openai-") and payload.get("model") != model_id:
        raise AcceptanceError("request_model_invalid")
    return protocol, operation, payload


def validate_origin(value: str, tls_mode: str) -> tuple[str, ssl.SSLContext | None]:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.port not in {None, 443}
    ):
        raise AcceptanceError("endpoint_identity_invalid")
    origin = f"https://{parsed.hostname}"
    if tls_mode == "verified":
        return origin, None
    if tls_mode != "disposable-staging-insecure":
        raise AcceptanceError("tls_mode_invalid")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        raise AcceptanceError("staging_endpoint_invalid") from None
    if not isinstance(address, ipaddress.IPv4Address) or not address.is_global:
        raise AcceptanceError("staging_endpoint_invalid")
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return origin, context


def _json_http(
    origin: str,
    context: ssl.SSLContext | None,
    token: str,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
) -> tuple[int, dict[str, str], dict[str, Any], bytes]:
    body = None
    request_headers = {"Authorization": f"Bearer {token}"}
    request_headers.update(headers or {})
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(  # noqa: S310 - validate_origin permits only exact HTTPS origins.
        origin + path,
        data=body,
        headers=request_headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - request URL is bound to the validated HTTPS origin.
            request, context=context, timeout=timeout
        ) as response:
            raw = response.read(_MAX_HTTP_BYTES + 1)
            status = response.status
            response_headers = {
                key.lower(): value for key, value in response.headers.items()
            }
    except urllib.error.HTTPError as error:
        raw = error.read(_MAX_HTTP_BYTES + 1)
        status = error.code
        response_headers = {
            key.lower(): value
            for key, value in (
                error.headers.items() if error.headers is not None else []
            )
        }
    except (urllib.error.URLError, TimeoutError, OSError):
        raise AcceptanceError("http_request_failed") from None
    if len(raw) > _MAX_HTTP_BYTES:
        raise AcceptanceError("http_response_too_large")
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        raise AcceptanceError("http_response_json_invalid") from None
    if not isinstance(value, dict):
        raise AcceptanceError("http_response_shape_invalid")
    return status, response_headers, value, raw


def validate_control_plane_readiness(status: int, value: dict[str, Any]) -> None:
    activation = value.get("activation")
    if (
        status != 200
        or value.get("status") != "ready"
        or not isinstance(activation, dict)
        or activation.get("required") is not False
    ):
        raise AcceptanceError("activation_handshake_not_disabled")


class Kubectl:
    def __init__(self, kubeconfig: Path, context: str) -> None:
        binary = shutil.which("kubectl")
        if binary is None:
            raise AcceptanceError("kubectl_unavailable")
        if not kubeconfig.is_absolute() or not kubeconfig.is_file():
            raise AcceptanceError("kubeconfig_invalid")
        if _NAME.fullmatch(context) is None:
            raise AcceptanceError("kube_context_invalid")
        self.prefix = [binary, "--kubeconfig", str(kubeconfig), "--context", context]

    def get(
        self,
        resource: str,
        *,
        name: str | None = None,
        namespace: str | None = None,
        selector: str | None = None,
        field_selector: str | None = None,
        allow_missing: bool = False,
    ) -> dict[str, Any] | None:
        command = [*self.prefix, "get", resource]
        if name is not None:
            command.append(name)
        if namespace is not None:
            command.extend(["--namespace", namespace])
        if selector is not None:
            command.extend(["--selector", selector])
        if field_selector is not None:
            command.extend(["--field-selector", field_selector])
        command.extend(["--output", "json"])
        result = subprocess.run(  # noqa: S603 - argv-only kubectl with bounded validated names.
            command,
            check=False,
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            if allow_missing:
                return None
            raise AcceptanceError("kubectl_get_failed")
        if len(result.stdout) > _MAX_KUBECTL_BYTES:
            raise AcceptanceError("kubectl_response_too_large")
        try:
            value = json.loads(result.stdout)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            raise AcceptanceError("kubectl_response_invalid") from None
        if not isinstance(value, dict):
            raise AcceptanceError("kubectl_response_invalid")
        return value

    def logs(self, namespace: str, pod: str, container: str) -> str:
        for value in (namespace, pod, container):
            if _NAME.fullmatch(value) is None:
                raise AcceptanceError("kubectl_logs_identity_invalid")
        result = subprocess.run(  # noqa: S603 - argv-only bounded kubectl logs.
            [
                *self.prefix,
                "logs",
                pod,
                "--namespace",
                namespace,
                "--container",
                container,
                "--timestamps=true",
                "--tail=5000",
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0 or len(result.stdout) > _MAX_LOG_BYTES:
            raise AcceptanceError("kubectl_logs_failed")
        try:
            return result.stdout.decode("utf-8")
        except UnicodeDecodeError:
            raise AcceptanceError("kubectl_logs_invalid") from None

    def delete_pod(self, namespace: str, name: str, uid: str) -> None:
        current = self.get("pod", name=name, namespace=namespace)
        if current is None or current.get("metadata", {}).get("uid") != uid:
            raise AcceptanceError("pod_uid_changed_before_delete")
        result = subprocess.run(  # noqa: S603 - argv-only kubectl with a re-read exact Pod identity.
            [
                *self.prefix,
                "delete",
                "pod",
                name,
                "--namespace",
                namespace,
                "--wait=false",
                "--output=name",
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        if result.returncode != 0:
            raise AcceptanceError("pod_delete_failed")


def _condition_true(value: dict[str, Any], kind: str) -> bool:
    return any(
        item.get("type") == kind and item.get("status") == "True"
        for item in value.get("status", {}).get("conditions", [])
        if isinstance(item, dict)
    )


@dataclass(frozen=True)
class ClusterSnapshot:
    deployment_replicas: int
    deployment_ready: int
    endpoints: int
    scaledobject_ready: bool
    scaledobject_active: bool
    hpa_desired: int
    pod_identities: tuple[tuple[str, str, str | None, str | None], ...]


def cluster_snapshot(
    kubectl: Kubectl,
    namespace: str,
    model_id: str,
    deployment_name: str,
    service_name: str,
) -> ClusterSnapshot:
    deployment = kubectl.get("deployment", name=deployment_name, namespace=namespace)
    scaledobject = kubectl.get(
        "scaledobject.keda.sh", name=f"fs2-model-{model_id}", namespace=namespace
    )
    hpa = kubectl.get(
        "horizontalpodautoscaler.autoscaling",
        name=f"keda-hpa-fs2-model-{model_id}",
        namespace=namespace,
        allow_missing=True,
    )
    endpoint_slices = kubectl.get(
        "endpointslices.discovery.k8s.io",
        namespace=namespace,
        selector=f"kubernetes.io/service-name={service_name}",
    )
    selector_labels = (
        deployment.get("spec", {}).get("selector", {}).get("matchLabels", {})
    )
    if not isinstance(selector_labels, dict) or not selector_labels:
        raise AcceptanceError("deployment_selector_invalid")
    selector = ",".join(
        f"{key}={value}" for key, value in sorted(selector_labels.items())
    )
    pods = kubectl.get("pods", namespace=namespace, selector=selector)
    endpoints = sum(
        1
        for item in endpoint_slices.get("items", [])
        for endpoint in (item.get("endpoints") or [])
        if endpoint.get("conditions", {}).get("ready") is True
    )
    pod_identities = tuple(
        sorted(
            (
                item.get("metadata", {}).get("name", ""),
                item.get("metadata", {}).get("uid", ""),
                item.get("spec", {}).get("nodeName"),
                next(
                    (
                        condition.get("status")
                        for condition in item.get("status", {}).get("conditions", [])
                        if condition.get("type") == "Ready"
                    ),
                    None,
                ),
            )
            for item in pods.get("items", [])
        )
    )
    return ClusterSnapshot(
        deployment_replicas=int(deployment.get("spec", {}).get("replicas", 0)),
        deployment_ready=int(deployment.get("status", {}).get("readyReplicas", 0)),
        endpoints=endpoints,
        scaledobject_ready=_condition_true(scaledobject, "Ready"),
        scaledobject_active=_condition_true(scaledobject, "Active"),
        hpa_desired=int((hpa or {}).get("status", {}).get("desiredReplicas", 0)),
        pod_identities=pod_identities,
    )


def _record_cluster_identities(
    kubectl: Kubectl,
    pod_identities: Iterable[tuple[str, str, str | None, str | None]],
    pod_uids: set[str],
    node_uids: set[str],
) -> bool:
    """Retain every observed identity while tolerating a preempted Node disappearing.

    Kubernetes can leave a terminating Pod with its old ``spec.nodeName`` after
    the preempted Node object is gone. The prior Node UID remains valid evidence;
    a missing lookup must not discard it or abort observation of its replacement.
    """

    node_ready = False
    for _, pod_uid, node_name, _ in pod_identities:
        if pod_uid:
            pod_uids.add(pod_uid)
        if not node_name:
            continue
        node = kubectl.get("node", name=node_name, allow_missing=True)
        node_uid = (node or {}).get("metadata", {}).get("uid")
        if isinstance(node_uid, str):
            node_uids.add(node_uid)
        if node is not None:
            node_ready |= _condition_true(node, "Ready")
    return node_ready


def _load_cold_start_framework() -> Any:
    path = (
        Path(__file__).resolve().parents[3]
        / "models/cold-start/cold_start_framework.py"
    )
    spec = importlib.util.spec_from_file_location("fs2_cold_start_framework", path)
    if spec is None or spec.loader is None:
        raise AcceptanceError("cold_start_framework_unavailable")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except BaseException:  # noqa: BLE001 - stable error code, no imported exception text.
        raise AcceptanceError("cold_start_framework_load_failed") from None
    return module


def _safe_container_statuses(items: Any) -> list[dict[str, Any]]:
    safe: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        state = item.get("state") if isinstance(item.get("state"), dict) else {}
        safe_state: dict[str, Any] = {}
        for state_name in ("running", "terminated"):
            value = state.get(state_name)
            if not isinstance(value, dict):
                continue
            safe_state[state_name] = {
                key: value[key]
                for key in ("startedAt", "finishedAt", "exitCode", "reason")
                if key in value
            }
        safe.append(
            {
                "name": item.get("name"),
                "imageID": item.get("imageID"),
                "ready": item.get("ready"),
                "restartCount": item.get("restartCount"),
                "state": safe_state,
            }
        )
    return safe


def _safe_environment_source(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    safe: dict[str, Any] = {}
    allowed_fields = {
        "configMapKeyRef": ("name", "key", "optional"),
        "secretKeyRef": ("name", "key", "optional"),
        "fieldRef": ("apiVersion", "fieldPath"),
        "resourceFieldRef": ("containerName", "resource", "divisor"),
    }
    for source_name, fields in allowed_fields.items():
        source = value.get(source_name)
        if isinstance(source, dict):
            safe[source_name] = {
                field: source.get(field) for field in fields if field in source
            }
    return safe


def _safe_environment_from(items: Any) -> list[dict[str, Any]]:
    safe: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        value: dict[str, Any] = {"prefix": item.get("prefix")}
        for source_name in ("configMapRef", "secretRef"):
            source = item.get(source_name)
            if isinstance(source, dict):
                value[source_name] = {
                    field: source.get(field)
                    for field in ("name", "optional")
                    if field in source
                }
        safe.append(value)
    return safe


def _safe_pod(value: dict[str, Any], primary_containers: set[str]) -> dict[str, Any]:
    metadata = value.get("metadata", {})
    status = value.get("status", {})
    spec = value.get("spec", {})
    raw_annotations = metadata.get("annotations", {})
    annotations = {
        key: item
        for key, item in (
            raw_annotations.items() if isinstance(raw_annotations, dict) else []
        )
        if key in _IDENTITY_ANNOTATIONS
    }
    runtime_specs = [
        container
        for container in spec.get("containers", [])
        if container.get("name") in primary_containers
    ]
    argv_identity = [
        {
            "name": item.get("name"),
            "command": item.get("command", []),
            "args": item.get("args", []),
        }
        for item in runtime_specs
    ]
    environment_identity = [
        {
            "name": item.get("name"),
            "env": [
                {
                    "name": env.get("name"),
                    "value_sha256": (
                        hashlib.sha256(str(env["value"]).encode("utf-8")).hexdigest()
                        if "value" in env
                        else None
                    ),
                    "value_from": _safe_environment_source(env.get("valueFrom")),
                }
                for env in item.get("env", [])
            ],
            "env_from": _safe_environment_from(item.get("envFrom")),
        }
        for item in runtime_specs
    ]
    return {
        "metadata": {
            "name": metadata.get("name"),
            "uid": metadata.get("uid"),
            "creationTimestamp": metadata.get("creationTimestamp"),
            "annotations": annotations,
        },
        "spec": {
            "nodeName": spec.get("nodeName"),
            "runtimeArgvDigest": hashlib.sha256(
                json.dumps(
                    argv_identity, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest(),
            "runtimeEnvironmentDigest": hashlib.sha256(
                json.dumps(
                    environment_identity, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest(),
        },
        "status": {
            "phase": status.get("phase"),
            "conditions": [
                {
                    key: condition.get(key)
                    for key in ("type", "status", "lastTransitionTime", "reason")
                }
                for condition in status.get("conditions", [])
                if isinstance(condition, dict)
            ],
            "containerStatuses": _safe_container_statuses(
                status.get("containerStatuses")
            ),
            "initContainerStatuses": _safe_container_statuses(
                status.get("initContainerStatuses")
            ),
        },
    }


def _safe_node(value: dict[str, Any]) -> dict[str, Any]:
    metadata = value.get("metadata", {})
    status = value.get("status", {})
    labels = {
        key: item
        for key, item in metadata.get("labels", {}).items()
        if key.startswith(
            (
                "nvidia.com/",
                "nebius.com/",
                "accelerator.fs2.nebius/",
                "capacity.fs2.nebius/",
                "topology.kubernetes.io/",
                "node.kubernetes.io/",
                "feature.node.kubernetes.io/",
            )
        )
    }
    return {
        "metadata": {
            "name": metadata.get("name"),
            "uid": metadata.get("uid"),
            "labels": labels,
        },
        "status": {
            "conditions": [
                {
                    key: condition.get(key)
                    for key in ("type", "status", "lastTransitionTime", "reason")
                }
                for condition in status.get("conditions", [])
                if isinstance(condition, dict)
            ],
            "nodeInfo": {
                key: status.get("nodeInfo", {}).get(key)
                for key in (
                    "architecture",
                    "operatingSystem",
                    "osImage",
                    "kernelVersion",
                    "containerRuntimeVersion",
                    "kubeletVersion",
                )
            },
            "capacity": {
                key: item
                for key, item in status.get("capacity", {}).items()
                if key.startswith("nvidia.com/")
            },
            "allocatable": {
                key: item
                for key, item in status.get("allocatable", {}).items()
                if key.startswith("nvidia.com/")
            },
        },
    }


def _safe_events(value: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "reason": item.get("reason"),
            "eventTime": item.get("eventTime"),
            "firstTimestamp": item.get("firstTimestamp"),
            "lastTimestamp": item.get("lastTimestamp"),
            "count": item.get("count"),
            "metadata": {
                "uid": item.get("metadata", {}).get("uid"),
                "creationTimestamp": item.get("metadata", {}).get("creationTimestamp"),
            },
        }
        for item in value.get("items", [])
        if isinstance(item, dict)
        and item.get("reason")
        in {"Scheduled", "Pulling", "Pulled", "Created", "Started"}
    ]


def _startup_markers(logs: str) -> dict[str, str]:
    markers: dict[str, str] = {}
    for line in logs.splitlines():
        timestamp, separator, payload = line.partition(" ")
        if not separator or not timestamp.endswith("Z"):
            continue
        try:
            value = json.loads(payload)
        except (json.JSONDecodeError, RecursionError):
            continue
        if not isinstance(value, dict) or value.get("event") != "fs2-startup-phase":
            continue
        name = value.get("name")
        if isinstance(name, str):
            if name in markers:
                raise AcceptanceError("startup_phase_marker_duplicate")
            markers[name] = timestamp
    return markers


def _json_log_values(logs: str) -> Iterable[dict[str, Any]]:
    """Yield JSON objects from timestamped or plain container log lines."""

    for line in logs.splitlines():
        _, separator, timestamped_payload = line.partition(" ")
        candidates = (timestamped_payload, line) if separator else (line,)
        for candidate in candidates:
            try:
                value = json.loads(candidate)
            except (json.JSONDecodeError, RecursionError):
                continue
            if isinstance(value, dict):
                yield value
                break


def _capture_localization_observation(
    kubectl: Kubectl,
    namespace: str,
    pod: dict[str, Any],
) -> dict[str, Any]:
    """Capture only the allowlisted result emitted by localization init containers.

    Repository names, revisions, file inventories, and error strings are never
    copied from logs. The cache result is therefore safe to include in a public
    receipt while still distinguishing a warm shared-cache hit from a download.
    """

    metadata = pod.get("metadata", {})
    pod_name = metadata.get("name")
    if not isinstance(pod_name, str) or _NAME.fullmatch(pod_name) is None:
        raise AcceptanceError("localization_pod_identity_invalid")
    observations: list[dict[str, Any]] = []
    for container in pod.get("spec", {}).get("initContainers", []):
        if not isinstance(container, dict):
            continue
        name = container.get("name")
        if not isinstance(name, str) or "localiz" not in name:
            continue
        matches = [
            value
            for value in _json_log_values(kubectl.logs(namespace, pod_name, name))
            if value.get("outcome") in _CACHE_OUTCOMES
        ]
        if len(matches) != 1:
            raise AcceptanceError("localization_outcome_not_unique")
        match = matches[0]
        elapsed = match.get("elapsed_seconds")
        if (
            not isinstance(elapsed, (int, float))
            or isinstance(elapsed, bool)
            or elapsed < 0
        ):
            raise AcceptanceError("localization_elapsed_invalid")
        source = match.get("source")
        if source is not None and source not in {
            "huggingface-download",
            "huggingface-local-cache",
            "verified-legacy-payload",
        }:
            raise AcceptanceError("localization_source_invalid")
        observations.append(
            {
                "container": name,
                "outcome": match["outcome"],
                "source": source,
                "elapsed_seconds": round(float(elapsed), 3),
            }
        )
    if not observations:
        return {
            "state": "unavailable",
            "reason": "no-localization-init-container",
            "observations": [],
        }
    return {"state": "observed", "reason": None, "observations": observations}


def _require_localization_outcome(
    observation: dict[str, Any] | None, required: str | None
) -> None:
    if required is None:
        return
    outcomes = {
        item.get("outcome")
        for item in (observation or {}).get("observations", [])
        if isinstance(item, dict)
    }
    if required == "cache-hit":
        passed = bool(outcomes & {"cache-hit", "cache-hit-after-wait"})
    elif required == "localized":
        passed = "localized" in outcomes
    else:
        passed = bool(outcomes & _CACHE_OUTCOMES)
    if not passed:
        raise AcceptanceError("required_localization_outcome_not_observed")


def _capture_runtime_identity(
    kubectl: Kubectl,
    args: argparse.Namespace,
    *,
    pod_identities: Iterable[tuple[str, str, str | None, str | None]],
) -> tuple[
    Any, dict[str, Any], dict[str, Any], str, str, dict[str, Any], dict[str, Any]
]:
    """Capture one ready Pod's allowlisted runtime identity for any floor state."""

    framework = _load_cold_start_framework()
    matrix = framework.load_json(args.optimization_matrix)
    framework.validate_matrix(matrix)
    model = framework.matrix_model(matrix, args.model_id)
    ready_pods = sorted(
        (name, uid, node_name)
        for name, uid, node_name, ready in pod_identities
        if ready == "True" and name and uid and node_name
    )
    if len(ready_pods) != 1:
        raise AcceptanceError("startup_phase_ready_pod_not_unique")
    pod_name, pod_uid, node_name = ready_pods[0]
    raw_pod = kubectl.get("pod", name=pod_name, namespace=args.namespace)
    if raw_pod.get("metadata", {}).get("uid") != pod_uid:
        raise AcceptanceError("startup_phase_pod_uid_changed")
    raw_node = kubectl.get("node", name=node_name)
    primary = set(model["primary_containers"])
    safe_pod = _safe_pod(raw_pod, primary)
    safe_node = _safe_node(raw_node)
    identity = {
        "pod": {"name": pod_name, "uid": pod_uid, "node_name": node_name},
        "deployment_annotations": safe_pod["metadata"]["annotations"],
        "container_image_ids": sorted(
            [
                {"name": item["name"], "image_id": item["imageID"]}
                for key in ("containerStatuses", "initContainerStatuses")
                for item in safe_pod["status"][key]
                if isinstance(item.get("name"), str)
                and isinstance(item.get("imageID"), str)
            ],
            key=lambda item: (item["name"], item["image_id"]),
        ),
        "pod_image_ids": sorted(
            {
                item["imageID"]
                for key in ("containerStatuses", "initContainerStatuses")
                for item in safe_pod["status"][key]
                if isinstance(item.get("imageID"), str)
            }
        ),
        "runtime_argv_digest": safe_pod["spec"]["runtimeArgvDigest"],
        "runtime_environment_digest": safe_pod["spec"]["runtimeEnvironmentDigest"],
        "node": safe_node,
        "localization": _capture_localization_observation(
            kubectl, args.namespace, raw_pod
        ),
    }
    return framework, matrix, model, pod_name, pod_uid, safe_pod, identity


def _capture_startup_observation(
    kubectl: Kubectl,
    args: argparse.Namespace,
    *,
    mechanism: str,
    pod_identities: Iterable[tuple[str, str, str | None, str | None]],
    external_events: dict[str, str | None],
) -> dict[str, Any]:
    (
        framework,
        matrix,
        model,
        pod_name,
        pod_uid,
        safe_pod,
        identity,
    ) = _capture_runtime_identity(kubectl, args, pod_identities=pod_identities)
    raw_events = kubectl.get(
        "events",
        namespace=args.namespace,
        field_selector=f"involvedObject.uid={pod_uid}",
    )
    primary = set(model["primary_containers"])
    marker_containers = sorted(primary | set(model["artifact_init_containers"]))
    markers: dict[str, str] = {}
    for container in marker_containers:
        container_markers = _startup_markers(
            kubectl.logs(args.namespace, pod_name, container)
        )
        if set(markers) & set(container_markers):
            raise AcceptanceError("startup_phase_marker_duplicate")
        markers.update(container_markers)
    safe_events = _safe_events(raw_events)
    observation = framework.build_phase_observation(
        matrix,
        model_id=args.model_id,
        mechanism=mechanism,
        pod=safe_pod,
        events=safe_events,
        node=identity["node"],
        external_events=external_events,
        runtime_markers=markers,
    )
    return {
        "phase_observation": observation,
        "identity_observation": identity,
    }


def _sleep_until(deadline: float, seconds: float = 2) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AcceptanceError("acceptance_timeout")
    time.sleep(min(seconds, remaining))


def _validated_operation_id(value: str) -> str:
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise AcceptanceError("operation_id_invalid") from None
    if str(parsed) != value.lower():
        raise AcceptanceError("operation_id_invalid")
    return str(parsed)


def _acknowledge_operation(
    origin: str,
    context: ssl.SSLContext | None,
    token: str,
    operation_id: str,
) -> None:
    status, _, value, _ = _json_http(
        origin,
        context,
        token,
        "POST",
        f"/v1/operations/{_validated_operation_id(operation_id)}:acknowledge",
    )
    if status != 200 or value.get("status") not in _TERMINAL:
        raise AcceptanceError("operation_acknowledgement_failed")


def failure_cleanup(args: argparse.Namespace, token: str) -> dict[str, Any]:
    """Cancel unfinished accepted work, purge terminal payloads, and restore floor."""

    result: dict[str, Any] = {
        "attempted": True,
        "operations": [],
        "floor_restored": False,
        "result": "PARTIAL",
    }
    try:
        origin, context = validate_origin(args.endpoint, args.tls_mode)
        kubectl = Kubectl(args.kubeconfig, args.context)
        deadline = time.monotonic() + args.cleanup_timeout_seconds
        for raw_operation_id in getattr(args, "accepted_operation_ids", []):
            operation_id = _validated_operation_id(raw_operation_id)
            item: dict[str, Any] = {
                "operation_id_sha256": _identity_digest(operation_id),
                "cancelled": False,
                "acknowledged": False,
            }
            status, _, current, _ = _json_http(
                origin, context, token, "GET", f"/v1/operations/{operation_id}"
            )
            if status != 200:
                raise AcceptanceError("cleanup_operation_status_failed")
            if current.get("status") not in _TERMINAL:
                cancel_status, _, cancelled, _ = _json_http(
                    origin,
                    context,
                    token,
                    "POST",
                    f"/v1/operations/{operation_id}:cancel",
                )
                if cancel_status != 200:
                    raise AcceptanceError("cleanup_operation_cancel_failed")
                item["cancelled"] = cancelled.get("status") == "cancelled"
            while time.monotonic() < deadline:
                poll_status, _, current, _ = _json_http(
                    origin, context, token, "GET", f"/v1/operations/{operation_id}"
                )
                if poll_status == 200 and current.get("status") in _TERMINAL:
                    break
                _sleep_until(deadline)
            if current.get("status") not in _TERMINAL:
                raise AcceptanceError("cleanup_operation_not_terminal")
            _acknowledge_operation(origin, context, token, operation_id)
            item["acknowledged"] = True
            result["operations"].append(item)
        floor = _wait_for_floor(kubectl, args, args.expected_floor, deadline)
        result["floor_restored"] = True
        result["floor"] = {
            "replicas": floor.deployment_replicas,
            "ready": floor.deployment_ready,
            "endpoints": floor.endpoints,
            "observed_at": utc_now(),
        }
        result["result"] = "PASS"
    except AcceptanceError as error:
        result["failure_code"] = str(error)
    except BaseException:  # noqa: BLE001 - cleanup never exposes value-bearing errors.
        result["failure_code"] = "cleanup_unexpected_failure"
    return result


def _wait_for_floor(
    kubectl: Kubectl,
    args: argparse.Namespace,
    expected: int,
    deadline: float,
) -> ClusterSnapshot:
    last: ClusterSnapshot | None = None
    while time.monotonic() < deadline:
        last = cluster_snapshot(
            kubectl,
            args.namespace,
            args.model_id,
            args.deployment,
            args.service,
        )
        correct_endpoints = last.endpoints == 0 if expected == 0 else last.endpoints > 0
        correct_ready = (
            last.deployment_ready == 0 if expected == 0 else last.deployment_ready >= 1
        )
        if (
            last.scaledobject_ready
            and last.deployment_replicas == expected
            and correct_ready
            and correct_endpoints
        ):
            return last
        _sleep_until(deadline)
    raise AcceptanceError("model_floor_not_observed")


def _request_payload(
    model_id: str, protocol: str, operation: str, payload: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    if protocol == "native":
        return f"/v1/models/{model_id}:invoke", {
            "operation": operation,
            "payload": payload,
        }
    return _PROTOCOL_PATHS[protocol], payload


def validate_semantic_result(protocol: str, value: dict[str, Any]) -> str:
    if not value:
        raise AcceptanceError("semantic_result_empty")
    if protocol == "native":
        return "json-object"
    choices = value.get("choices")
    if protocol == "openai-chat":
        if (
            not isinstance(choices, list)
            or not choices
            or not isinstance(choices[0], dict)
        ):
            raise AcceptanceError("semantic_result_invalid")
        message = choices[0].get("message")
        if (
            not isinstance(message, dict)
            or not isinstance(message.get("content"), str)
            or not message["content"].strip()
        ):
            raise AcceptanceError("semantic_result_invalid")
        return "openai-chat"
    if protocol == "openai-completions":
        if (
            not isinstance(choices, list)
            or not choices
            or not isinstance(choices[0], dict)
            or not isinstance(choices[0].get("text"), str)
            or not choices[0]["text"].strip()
        ):
            raise AcceptanceError("semantic_result_invalid")
        return "openai-completions"
    data = value.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise AcceptanceError("semantic_result_invalid")
    if protocol == "openai-embeddings":
        embedding = data[0].get("embedding")
        if not isinstance(embedding, list) or not embedding:
            raise AcceptanceError("semantic_result_invalid")
        return "openai-embeddings"
    if protocol == "openai-images":
        encoded = data[0].get("b64_json")
        if not isinstance(encoded, str) or not encoded:
            raise AcceptanceError("semantic_result_invalid")
        return "openai-images"
    raise AcceptanceError("semantic_result_invalid")


def _evidence_bytes(evidence: dict[str, Any]) -> bytes:
    return (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _hashed_optional(value: Any) -> str | None:
    return _identity_digest(value) if isinstance(value, str) and value else None


def _public_runtime_identity(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    pod = value.get("pod") if isinstance(value.get("pod"), dict) else {}
    node = value.get("node") if isinstance(value.get("node"), dict) else {}
    node_metadata = (
        node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    )
    labels = node_metadata.get("labels")
    hashed_labels = (
        {key: _identity_digest(str(item)) for key, item in sorted(labels.items())}
        if isinstance(labels, dict)
        else {}
    )
    public_node = {
        "metadata": {
            "name_sha256": _hashed_optional(node_metadata.get("name")),
            "uid_sha256": _hashed_optional(node_metadata.get("uid")),
            "label_value_sha256": hashed_labels,
        },
        "status": node.get("status"),
    }
    return {
        "pod": {
            "name_sha256": _hashed_optional(pod.get("name")),
            "uid_sha256": _hashed_optional(pod.get("uid")),
            "node_name_sha256": _hashed_optional(pod.get("node_name")),
        },
        "deployment_annotations": value.get("deployment_annotations"),
        "container_image_ids": value.get("container_image_ids"),
        "pod_image_ids": value.get("pod_image_ids"),
        "runtime_argv_digest": value.get("runtime_argv_digest"),
        "runtime_environment_digest": value.get("runtime_environment_digest"),
        "node": public_node,
        "localization": value.get("localization"),
    }


def public_evidence(evidence: dict[str, Any], private_sha256: str) -> dict[str, Any]:
    """Create the shareable receipt without raw cloud, node, Pod, GPU, or op IDs."""

    operation = evidence.get("operation")
    public_operation: dict[str, Any] | None = None
    if isinstance(operation, dict):
        runtime = operation.get("runtime")
        runtime = runtime if isinstance(runtime, dict) else {}
        gpu_uuids = runtime.get("gpu_uuids")
        public_operation = {
            key: value
            for key, value in operation.items()
            if key not in {"id", "runtime"}
        }
        public_operation["id_sha256"] = _hashed_optional(operation.get("id"))
        public_operation["runtime"] = {
            "pod_uid_sha256": _hashed_optional(runtime.get("pod_uid")),
            "node_uid_sha256": _hashed_optional(runtime.get("node_uid")),
            "gpu_uuid_sha256": [
                _identity_digest(item) for item in gpu_uuids if isinstance(item, str)
            ]
            if isinstance(gpu_uuids, list)
            else [],
            "gpu_count": runtime.get("gpu_count"),
            "preemptible": runtime.get("preemptible"),
        }
    semantic_calls = []
    for item in evidence.get("semantic_calls", []):
        if not isinstance(item, dict):
            continue
        semantic_calls.append(
            {
                **{key: value for key, value in item.items() if key != "operation_id"},
                "operation_id_sha256": _hashed_optional(item.get("operation_id")),
            }
        )
    identities = evidence.get("observed_runtime_identities")
    identities = identities if isinstance(identities, dict) else {}
    public = {
        "schema": "fs2-serve.nebius.ai/model-autoscaling-public-acceptance/v1",
        "source": {
            "schema": evidence.get("schema"),
            "private_receipt_sha256": private_sha256,
        },
        "model_id": evidence.get("model_id"),
        "target": evidence.get("target"),
        "tls_mode": evidence.get("tls_mode"),
        "started_at": evidence.get("started_at"),
        "completed_at": evidence.get("completed_at"),
        "duration_seconds": evidence.get("duration_seconds"),
        "result": evidence.get("result"),
        "failure_code": evidence.get("failure_code"),
        "initial": evidence.get("initial"),
        "control_plane": evidence.get("control_plane"),
        "operation": public_operation,
        "semantic_calls": semantic_calls,
        "phase_timestamps": evidence.get("phase_timestamps"),
        "phase_durations_seconds": evidence.get("phase_durations_seconds"),
        "runtime_identity_observation": _public_runtime_identity(
            evidence.get("runtime_identity_observation")
        ),
        "observed_runtime_identity_sha256": {
            "pod_uids": [
                _identity_digest(item)
                for item in identities.get("pod_uids", [])
                if isinstance(item, str)
            ],
            "node_uids": [
                _identity_digest(item)
                for item in identities.get("node_uids", [])
                if isinstance(item, str)
            ],
        },
        "transitions": evidence.get("transitions"),
        "final": evidence.get("final"),
        "cleanup": evidence.get("cleanup"),
        "clock": {
            "kind": (evidence.get("clock") or {}).get("kind"),
            "domain_sha256": _hashed_optional(
                (evidence.get("clock") or {}).get("domain")
            ),
        },
    }
    return public


def write_evidence(path: Path, evidence: dict[str, Any], token: str) -> None:
    if not path.is_absolute():
        raise AcceptanceError("evidence_path_not_absolute")
    raw = _evidence_bytes(evidence)
    if token.encode("utf-8") in raw:
        raise AcceptanceError("evidence_redaction_failed")
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise AcceptanceError("evidence_path_invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def run(args: argparse.Namespace, token: str) -> dict[str, Any]:
    origin, context = validate_origin(args.endpoint, args.tls_mode)
    kubectl = Kubectl(args.kubeconfig, args.context)
    semantic_call_count = getattr(args, "semantic_call_count", 1)
    started = time.monotonic()
    started_monotonic_ns = time.monotonic_ns()
    deadline = started + args.timeout_seconds
    evidence: dict[str, Any] = {
        "schema": "fs2-serve.nebius.ai/model-autoscaling-acceptance/v1",
        "started_at": utc_now(),
        "model_id": args.model_id,
        "target": {
            "namespace": args.namespace,
            "deployment": args.deployment,
            "service": args.service,
            "expected_floor": args.expected_floor,
        },
        "tls_mode": args.tls_mode,
        "clock": {
            "domain": clock_domain(),
            "kind": "linux-monotonic",
            "started_monotonic_ns": started_monotonic_ns,
        },
    }
    initial = _wait_for_floor(kubectl, args, args.expected_floor, deadline)
    initial_floor_observed_at = utc_now()
    initial_floor_observed_monotonic_ns = time.monotonic_ns()
    evidence["initial"] = {
        "replicas": initial.deployment_replicas,
        "ready": initial.deployment_ready,
        "endpoints": initial.endpoints,
        "observed_at": initial_floor_observed_at,
        "seconds_after_start": _elapsed_seconds(
            started_monotonic_ns, initial_floor_observed_monotonic_ns
        ),
    }
    ready_status, _, ready_value, _ = _json_http(
        origin,
        context,
        token,
        "GET",
        "/readyz",
    )
    validate_control_plane_readiness(ready_status, ready_value)
    evidence["control_plane"] = {"activation_required": False}
    if args.request_file is None:
        evidence.update({"result": "PASS", "completed_at": utc_now()})
        return evidence

    protocol, operation, payload = read_request_file(args.request_file, args.model_id)
    path, request_payload = _request_payload(
        args.model_id, protocol, operation, payload
    )
    second_request_payload = request_payload
    if getattr(args, "second_request_file", None) is not None:
        second_protocol, second_operation, second_payload = read_request_file(
            args.second_request_file, args.model_id
        )
        if (second_protocol, second_operation) != (protocol, operation):
            raise AcceptanceError("semantic_call2_contract_mismatch")
        _, second_request_payload = _request_payload(
            args.model_id, second_protocol, second_operation, second_payload
        )
        if json.dumps(
            second_request_payload, sort_keys=True, separators=(",", ":")
        ) == json.dumps(request_payload, sort_keys=True, separators=(",", ":")):
            raise AcceptanceError("semantic_call2_payload_not_distinct")
    status, headers, admitted, _ = _json_http(
        origin,
        context,
        token,
        "POST",
        path,
        payload=request_payload,
        headers={
            "Idempotency-Key": f"fs2-keda-{uuid4()}",
            "x-fs2-wait-seconds": "0",
            "x-fs2-deadline-seconds": str(args.timeout_seconds),
        },
    )
    if status != 202:
        raise AcceptanceError("operation_not_asynchronous")
    activation_accepted_at = utc_now()
    activation_accepted_monotonic_ns = time.monotonic_ns()
    call1_accepted_at = activation_accepted_at
    call1_accepted_monotonic_ns = activation_accepted_monotonic_ns
    operation_id = headers.get("x-fs2-operation-id") or admitted.get("id")
    if not isinstance(operation_id, str):
        raise AcceptanceError("operation_id_missing")
    operation_id = _validated_operation_id(operation_id)
    args.accepted_operation_ids = [operation_id]

    observed_states: set[str] = set()
    pod_uids: set[str] = set()
    node_uids: set[str] = set()
    saw_active = False
    saw_hpa_one = False
    saw_replica_one = False
    saw_ready = False
    pod_observed_at: str | None = None
    pod_observed_monotonic_ns: int | None = None
    node_assigned_at: str | None = None
    node_assigned_monotonic_ns: int | None = None
    node_ready_at: str | None = None
    node_ready_monotonic_ns: int | None = None
    readiness_observed_at: str | None = None
    readiness_observed_monotonic_ns: int | None = None
    deleted_pod_uid: str | None = None
    final_operation: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        operation_status, _, current, _ = _json_http(
            origin,
            context,
            token,
            "GET",
            f"/v1/operations/{operation_id}",
        )
        if operation_status != 200:
            raise AcceptanceError("operation_status_failed")
        state = current.get("status")
        if isinstance(state, str):
            observed_states.add(state)
        snapshot = cluster_snapshot(
            kubectl,
            args.namespace,
            args.model_id,
            args.deployment,
            args.service,
        )
        saw_active |= snapshot.scaledobject_active
        saw_hpa_one |= snapshot.hpa_desired >= 1
        saw_replica_one |= snapshot.deployment_replicas >= 1
        ready_now = snapshot.deployment_ready >= 1 and snapshot.endpoints > 0
        saw_ready |= ready_now
        if ready_now and readiness_observed_at is None:
            readiness_observed_at = utc_now()
            readiness_observed_monotonic_ns = time.monotonic_ns()
        if snapshot.pod_identities and pod_observed_at is None:
            pod_observed_at = utc_now()
            pod_observed_monotonic_ns = time.monotonic_ns()
        if (
            any(node_name for _, _, node_name, _ in snapshot.pod_identities)
            and node_assigned_at is None
        ):
            node_assigned_at = utc_now()
            node_assigned_monotonic_ns = time.monotonic_ns()
        node_ready_now = _record_cluster_identities(
            kubectl, snapshot.pod_identities, pod_uids, node_uids
        )
        if node_ready_now and node_ready_at is None:
            node_ready_at = utc_now()
            node_ready_monotonic_ns = time.monotonic_ns()
        for pod_name, pod_uid, _, _ in snapshot.pod_identities:
            if args.delete_first_pod and deleted_pod_uid is None and pod_uid:
                kubectl.delete_pod(args.namespace, pod_name, pod_uid)
                deleted_pod_uid = pod_uid
                break
        if state in _TERMINAL:
            final_operation = current
            break
        _sleep_until(deadline)
    if final_operation is None:
        raise AcceptanceError("operation_not_terminal")
    if final_operation.get("status") != "succeeded":
        raise AcceptanceError("operation_failed")
    if final_operation.get("semantic_outcome") != "protocol_valid":
        raise AcceptanceError("operation_semantic_protocol_invalid")
    if not observed_states & _DEMAND:
        raise AcceptanceError("durable_demand_not_observed")
    if not all((saw_active, saw_hpa_one, saw_replica_one, saw_ready)):
        raise AcceptanceError("scale_up_transition_incomplete")
    if args.delete_first_pod and (deleted_pod_uid is None or len(pod_uids) < 2):
        raise AcceptanceError("pod_replacement_not_observed")
    if args.require_node_replacement and len(node_uids) < 2:
        raise AcceptanceError("node_replacement_not_observed")

    result_status, _, result_value, result_raw = _json_http(
        origin,
        context,
        token,
        "GET",
        f"/v1/operations/{operation_id}/result",
    )
    if result_status != 200 or not result_raw:
        raise AcceptanceError("operation_result_failed")
    semantic_kind = validate_semantic_result(protocol, result_value)
    call1_completed_at = utc_now()
    call1_completed_monotonic_ns = time.monotonic_ns()
    semantic_calls = [
        {
            "ordinal": 1,
            "operation_id": operation_id,
            "accepted_at": activation_accepted_at,
            "completed_at": call1_completed_at,
            "completion_seconds": _elapsed_seconds(
                activation_accepted_monotonic_ns, call1_completed_monotonic_ns
            ),
            "ttft_seconds": None,
            "ttft_reason": "non-streaming-result-endpoint",
            "semantic_kind": semantic_kind,
            "result_bytes": len(result_raw),
            "result_sha256": hashlib.sha256(result_raw).hexdigest(),
        }
    ]
    call2_accepted_at: str | None = None
    call2_accepted_monotonic_ns: int | None = None
    call2_completed_at: str | None = None
    call2_completed_monotonic_ns: int | None = None
    if semantic_call_count == 2:
        second_status, second_headers, second_admitted, _ = _json_http(
            origin,
            context,
            token,
            "POST",
            path,
            payload=second_request_payload,
            headers={
                "Idempotency-Key": f"fs2-keda-call2-{uuid4()}",
                "x-fs2-wait-seconds": "0",
                "x-fs2-deadline-seconds": str(args.timeout_seconds),
            },
        )
        if second_status != 202:
            raise AcceptanceError("semantic_call2_not_asynchronous")
        second_operation_id = second_headers.get(
            "x-fs2-operation-id"
        ) or second_admitted.get("id")
        if not isinstance(second_operation_id, str):
            raise AcceptanceError("semantic_call2_operation_id_missing")
        second_operation_id = _validated_operation_id(second_operation_id)
        args.accepted_operation_ids.append(second_operation_id)
        call2_accepted_at = utc_now()
        call2_accepted_monotonic_ns = time.monotonic_ns()
        second_final: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            second_operation_status, _, second_current, _ = _json_http(
                origin,
                context,
                token,
                "GET",
                f"/v1/operations/{second_operation_id}",
            )
            if second_operation_status != 200:
                raise AcceptanceError("semantic_call2_status_failed")
            if second_current.get("status") in _TERMINAL:
                second_final = second_current
                break
            second_snapshot = cluster_snapshot(
                kubectl,
                args.namespace,
                args.model_id,
                args.deployment,
                args.service,
            )
            if second_snapshot.deployment_ready < 1 or second_snapshot.endpoints < 1:
                raise AcceptanceError("semantic_call2_lost_ready_backend")
            _record_cluster_identities(
                kubectl, second_snapshot.pod_identities, pod_uids, node_uids
            )
            _sleep_until(deadline)
        if second_final is None:
            raise AcceptanceError("semantic_call2_not_terminal")
        if second_final.get("status") != "succeeded":
            raise AcceptanceError("semantic_call2_failed")
        if second_final.get("semantic_outcome") != "protocol_valid":
            raise AcceptanceError("semantic_call2_protocol_invalid")
        second_result_status, _, second_result_value, second_result_raw = _json_http(
            origin,
            context,
            token,
            "GET",
            f"/v1/operations/{second_operation_id}/result",
        )
        if second_result_status != 200 or not second_result_raw:
            raise AcceptanceError("semantic_call2_result_failed")
        second_semantic_kind = validate_semantic_result(protocol, second_result_value)
        if second_semantic_kind != semantic_kind:
            raise AcceptanceError("semantic_call2_kind_mismatch")
        call2_completed_at = utc_now()
        call2_completed_monotonic_ns = time.monotonic_ns()
        semantic_calls.append(
            {
                "ordinal": 2,
                "operation_id": second_operation_id,
                "accepted_at": call2_accepted_at,
                "completed_at": call2_completed_at,
                "completion_seconds": _elapsed_seconds(
                    call2_accepted_monotonic_ns, call2_completed_monotonic_ns
                ),
                "ttft_seconds": None,
                "ttft_reason": "non-streaming-result-endpoint",
                "semantic_kind": second_semantic_kind,
                "result_bytes": len(second_result_raw),
                "result_sha256": hashlib.sha256(second_result_raw).hexdigest(),
            }
        )
    startup_observation: dict[str, Any] | None = None
    runtime_identity_observation: dict[str, Any] | None = None
    if getattr(args, "capture_startup_phases", False):
        phase_snapshot = cluster_snapshot(
            kubectl,
            args.namespace,
            args.model_id,
            args.deployment,
            args.service,
        )
        startup_observation = _capture_startup_observation(
            kubectl,
            args,
            mechanism=args.benchmark_mechanism,
            pod_identities=phase_snapshot.pod_identities,
            external_events={
                "activation-accepted": activation_accepted_at,
                "readiness-accepted": readiness_observed_at,
                "semantic-call1-accepted": call1_accepted_at,
                "semantic-call2-accepted": call2_accepted_at,
                "return-to-zero-accepted": None,
            },
        )
        runtime_identity_observation = startup_observation["identity_observation"]
    elif getattr(args, "capture_runtime_identity", False):
        identity_snapshot = cluster_snapshot(
            kubectl,
            args.namespace,
            args.model_id,
            args.deployment,
            args.service,
        )
        *_, runtime_identity_observation = _capture_runtime_identity(
            kubectl,
            args,
            pod_identities=identity_snapshot.pod_identities,
        )
    _require_localization_outcome(
        (runtime_identity_observation or {}).get("localization"),
        getattr(args, "require_cache_outcome", None),
    )
    acknowledged_operation_ids = list(args.accepted_operation_ids)
    for accepted_operation_id in acknowledged_operation_ids:
        _acknowledge_operation(origin, context, token, accepted_operation_id)
    scale_down_deadline = min(
        deadline,
        time.monotonic() + args.cooldown_seconds + args.scale_down_timeout_seconds,
    )
    final_floor = _wait_for_floor(
        kubectl, args, args.expected_floor, scale_down_deadline
    )
    return_to_floor_accepted_at = utc_now()
    return_to_floor_accepted_monotonic_ns = time.monotonic_ns()
    if startup_observation is not None:
        phase_receipt = startup_observation["phase_observation"]
        for event in phase_receipt["events"]:
            if event["name"] == "return-to-zero-accepted":
                event.update(
                    {"state": "observed", "timestamp": return_to_floor_accepted_at}
                )
        phase_receipt["missing_required_events"] = [
            name
            for name in phase_receipt["missing_required_events"]
            if name != "return-to-zero-accepted"
        ]
        phase_receipt["complete_for_promotion"] = not phase_receipt[
            "missing_required_events"
        ]

    if args.require_node_scale_down:
        observed_node_uids = set(node_uids)
        while observed_node_uids and time.monotonic() < deadline:
            remaining: set[str] = set()
            nodes = kubectl.get("nodes") or {}
            live_uids = {
                item.get("metadata", {}).get("uid") for item in nodes.get("items", [])
            }
            remaining = {uid for uid in observed_node_uids if uid in live_uids}
            observed_node_uids = remaining
            if remaining:
                _sleep_until(deadline, 5)
        if observed_node_uids:
            raise AcceptanceError("node_scale_down_not_observed")

    final_runtime = final_operation.get("runtime")
    if not isinstance(final_runtime, dict):
        final_runtime = {}
    evidence.update(
        {
            "operation": {
                "id": operation_id,
                "status": "succeeded",
                "states": sorted(observed_states),
                "attempt": final_operation.get("attempt"),
                "accepted_at": final_operation.get("accepted_at"),
                "available_at": final_operation.get("available_at"),
                "activation_started_at": final_operation.get("activation_started_at"),
                "ready_at": final_operation.get("ready_at"),
                "started_at": final_operation.get("started_at"),
                "completed_at": final_operation.get("completed_at"),
                "outcome": final_operation.get("outcome"),
                "semantic_outcome": final_operation.get("semantic_outcome"),
                "runtime": {
                    key: final_runtime.get(key)
                    for key in (
                        "pod_uid",
                        "node_uid",
                        "gpu_uuids",
                        "gpu_count",
                        "preemptible",
                    )
                },
                "estimated_gpu_seconds": final_operation.get("estimated_gpu_seconds"),
                "cold_start_seconds": final_operation.get("cold_start_seconds"),
                "reused": final_operation.get("reused"),
                "semantic_kind": semantic_kind,
                "result_bytes": len(result_raw),
                "result_sha256": hashlib.sha256(result_raw).hexdigest(),
            },
            "semantic_calls": semantic_calls,
            "phase_timestamps": {
                "activation_accepted_at": activation_accepted_at,
                "pod_observed_at": pod_observed_at,
                "node_assigned_at": node_assigned_at,
                "node_ready_at": node_ready_at,
                "readiness_observed_at": readiness_observed_at,
                "semantic_call1_accepted_at": call1_accepted_at,
                "semantic_call1_completed_at": call1_completed_at,
                "semantic_call2_accepted_at": call2_accepted_at,
                "semantic_call2_completed_at": call2_completed_at,
                "return_to_floor_accepted_at": return_to_floor_accepted_at,
            },
            "phase_durations_seconds": {
                "activation_to_pod_observed": _elapsed_seconds(
                    activation_accepted_monotonic_ns, pod_observed_monotonic_ns
                ),
                "activation_to_node_assigned": _elapsed_seconds(
                    activation_accepted_monotonic_ns, node_assigned_monotonic_ns
                ),
                "activation_to_node_ready": _elapsed_seconds(
                    activation_accepted_monotonic_ns, node_ready_monotonic_ns
                ),
                "activation_to_model_ready": _elapsed_seconds(
                    activation_accepted_monotonic_ns, readiness_observed_monotonic_ns
                ),
                "activation_to_semantic_completion": _elapsed_seconds(
                    activation_accepted_monotonic_ns, call1_completed_monotonic_ns
                ),
                "ready_to_semantic_completion": (
                    _elapsed_seconds(
                        readiness_observed_monotonic_ns,
                        call1_completed_monotonic_ns,
                    )
                    if readiness_observed_monotonic_ns is not None
                    else None
                ),
                "semantic_completion_to_floor": _elapsed_seconds(
                    call2_completed_monotonic_ns or call1_completed_monotonic_ns,
                    return_to_floor_accepted_monotonic_ns,
                ),
            },
            "phase_monotonic_ns": {
                "activation_accepted": activation_accepted_monotonic_ns,
                "pod_observed": pod_observed_monotonic_ns,
                "node_assigned": node_assigned_monotonic_ns,
                "node_ready": node_ready_monotonic_ns,
                "readiness_observed": readiness_observed_monotonic_ns,
                "semantic_call1_accepted": call1_accepted_monotonic_ns,
                "semantic_call2_accepted": call2_accepted_monotonic_ns,
                "return_to_floor_accepted": return_to_floor_accepted_monotonic_ns,
            },
            "startup_observation": startup_observation,
            "runtime_identity_observation": runtime_identity_observation,
            "observed_runtime_identities": {
                "pod_uids": sorted(pod_uids),
                "node_uids": sorted(node_uids),
            },
            "transitions": {
                "scaledobject_active": saw_active,
                "hpa_desired_one": saw_hpa_one,
                "deployment_one": saw_replica_one,
                "endpoint_ready": saw_ready,
                "pod_uid_count": len(pod_uids),
                "node_uid_count": len(node_uids),
                "pod_deleted": deleted_pod_uid is not None,
            },
            "final": {
                "replicas": final_floor.deployment_replicas,
                "ready": final_floor.deployment_ready,
                "endpoints": final_floor.endpoints,
                "observed_at": return_to_floor_accepted_at,
            },
            "cleanup": {
                "attempted": True,
                "terminal_operations_acknowledged": len(acknowledged_operation_ids),
                "floor_restored": True,
                "result": "PASS",
            },
            "duration_seconds": round(time.monotonic() - started, 3),
            "completed_at": utc_now(),
            "result": "PASS",
        }
    )
    return evidence


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument(
        "--tls-mode",
        choices=("verified", "disposable-staging-insecure"),
        default="verified",
    )
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--request-file", type=Path)
    parser.add_argument("--second-request-file", type=Path)
    parser.add_argument("--semantic-call-count", type=int, choices=(1, 2), default=1)
    parser.add_argument("--capture-startup-phases", action="store_true")
    parser.add_argument("--capture-runtime-identity", action="store_true")
    parser.add_argument("--optimization-matrix", type=Path)
    parser.add_argument(
        "--benchmark-mechanism",
        choices=(
            "conventional",
            "shared-cache",
            "local-nvme",
            "oci-image-volume",
            "oci-modelcar",
            "cuda-criu-snapshot",
            "dynamo-snapshot",
        ),
        default="conventional",
    )
    parser.add_argument("--evidence-file", type=Path, required=True)
    parser.add_argument("--public-evidence-file", type=Path)
    parser.add_argument("--namespace", default="fs2-models")
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--expected-floor", type=int, choices=(0, 1), default=0)
    parser.add_argument("--cooldown-seconds", type=int, default=300)
    parser.add_argument("--timeout-seconds", type=int, default=7200)
    parser.add_argument("--scale-down-timeout-seconds", type=int, default=1200)
    parser.add_argument("--cleanup-timeout-seconds", type=int, default=1200)
    parser.add_argument(
        "--require-cache-outcome",
        choices=("observed", "cache-hit", "localized"),
    )
    parser.add_argument("--delete-first-pod", action="store_true")
    parser.add_argument("--require-node-replacement", action="store_true")
    parser.add_argument("--require-node-scale-down", action="store_true")
    args = parser.parse_args()
    for name in ("context", "namespace", "model_id", "deployment", "service"):
        if _NAME.fullmatch(getattr(args, name)) is None:
            parser.error(f"--{name.replace('_', '-')} is not a Kubernetes-safe name")
    if not 5 <= args.cooldown_seconds <= 7200:
        parser.error("--cooldown-seconds must be from 5 through 7200")
    if not 30 <= args.timeout_seconds <= 14400:
        parser.error("--timeout-seconds must be from 30 through 14400")
    if not 30 <= args.scale_down_timeout_seconds <= 7200:
        parser.error("--scale-down-timeout-seconds must be from 30 through 7200")
    if not 30 <= args.cleanup_timeout_seconds <= 7200:
        parser.error("--cleanup-timeout-seconds must be from 30 through 7200")
    if not args.evidence_file.is_absolute():
        parser.error("--evidence-file must be absolute")
    if args.public_evidence_file is not None:
        if not args.public_evidence_file.is_absolute():
            parser.error("--public-evidence-file must be absolute")
        if args.public_evidence_file == args.evidence_file:
            parser.error("--public-evidence-file must differ from --evidence-file")
    if args.delete_first_pod and args.request_file is None:
        parser.error("--delete-first-pod requires --request-file")
    if args.semantic_call_count == 2 and args.request_file is None:
        parser.error("--semantic-call-count 2 requires --request-file")
    if args.second_request_file is not None and args.semantic_call_count != 2:
        parser.error("--second-request-file requires --semantic-call-count 2")
    if args.capture_startup_phases:
        if (
            args.optimization_matrix is None
            or not args.optimization_matrix.is_absolute()
        ):
            parser.error(
                "--capture-startup-phases requires an absolute --optimization-matrix"
            )
        if args.semantic_call_count != 2:
            parser.error("--capture-startup-phases requires --semantic-call-count 2")
        if args.expected_floor != 0:
            parser.error("--capture-startup-phases requires --expected-floor 0")
    if args.capture_runtime_identity:
        if (
            args.optimization_matrix is None
            or not args.optimization_matrix.is_absolute()
        ):
            parser.error(
                "--capture-runtime-identity requires an absolute --optimization-matrix"
            )
        if args.request_file is None:
            parser.error("--capture-runtime-identity requires --request-file")
    if args.require_cache_outcome is not None and not (
        args.capture_startup_phases or args.capture_runtime_identity
    ):
        parser.error(
            "--require-cache-outcome requires --capture-runtime-identity or "
            "--capture-startup-phases"
        )
    return args


def main() -> int:
    args = parse_args()
    args.accepted_operation_ids = []
    token = ""
    try:
        token = read_token_file(args.token_file)
        evidence = run(args, token)
    except AcceptanceError as error:
        cleanup = (
            failure_cleanup(args, token)
            if token and args.accepted_operation_ids
            else {"attempted": False, "result": "NOT_REQUIRED"}
        )
        evidence = {
            "schema": "fs2-serve.nebius.ai/model-autoscaling-acceptance/v1",
            "model_id": args.model_id,
            "target": {
                "namespace": args.namespace,
                "deployment": args.deployment,
                "service": args.service,
                "expected_floor": args.expected_floor,
            },
            "result": "FAIL",
            "failure_code": str(error),
            "cleanup": cleanup,
            "completed_at": utc_now(),
        }
    except BaseException:  # noqa: BLE001 - never expose a value-bearing exception.
        cleanup = (
            failure_cleanup(args, token)
            if token and args.accepted_operation_ids
            else {"attempted": False, "result": "NOT_REQUIRED"}
        )
        evidence = {
            "schema": "fs2-serve.nebius.ai/model-autoscaling-acceptance/v1",
            "model_id": args.model_id,
            "target": {
                "namespace": args.namespace,
                "deployment": args.deployment,
                "service": args.service,
                "expected_floor": args.expected_floor,
            },
            "result": "FAIL",
            "failure_code": "unexpected_failure",
            "cleanup": cleanup,
            "completed_at": utc_now(),
        }
    try:
        write_evidence(args.evidence_file, evidence, token)
        if args.public_evidence_file is not None:
            private_sha256 = hashlib.sha256(_evidence_bytes(evidence)).hexdigest()
            write_evidence(
                args.public_evidence_file,
                public_evidence(evidence, private_sha256),
                token,
            )
    except AcceptanceError:
        return 2
    return 0 if evidence["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
