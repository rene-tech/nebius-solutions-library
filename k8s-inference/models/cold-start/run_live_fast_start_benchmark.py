#!/usr/bin/env python3
"""Run one redacted, phase-aware fast-start benchmark against a live model.

The public FS2 gateway deliberately does not stream model responses.  This
runner therefore reports first semantic output as the completion of the
validated response; it never relabels completion latency as TTFT.  Text TTFT
can be added as a separate direct-runtime observation by a streaming-capable
runner without changing this receipt's cold-start clock.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import ssl
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

SCHEMA = "fs2-serve.nebius.ai/fast-start-benchmark-attempt/v1"
LEVELS = ("Off", "L1", "L2", "L3", "L4")
TERMINAL_OPERATION_STATES = frozenset({"succeeded", "failed", "cancelled", "preempted", "expired"})
SAFE_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?")
SHA256 = re.compile(r"(?:sha256:)?[a-f0-9]{64}")
MAX_JSON_BYTES = 96 * 1024 * 1024
MAX_TOKEN_BYTES = 4096
SOLUTION_ROOT = Path(__file__).resolve().parents[2]
COSMOS_VALIDATOR_PATH = SOLUTION_ROOT / "catalog/runtime/validators/validate_cosmos3_nano.py"
COSMOS_CONTRACT_PATH = SOLUTION_ROOT / "catalog/runtime/validators/assets/cosmos3-nano.json"


class BenchmarkError(RuntimeError):
    """A value-suppressed benchmark error suitable for a durable receipt."""

    def __init__(self, code: str) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]{0,95}", code) is None:
            raise ValueError("unsafe benchmark error code")
        self.code = code
        super().__init__(code)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: object) -> str:
    return sha256_bytes(canonical_json(value))


def _read_regular_file(path: Path, *, maximum: int, exact_mode: int | None = None) -> bytes:
    if not path.is_absolute():
        raise BenchmarkError("input_path_not_absolute")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise BenchmarkError("input_file_unavailable") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise BenchmarkError("input_file_invalid")
        if exact_mode is not None and stat.S_IMODE(metadata.st_mode) != exact_mode:
            raise BenchmarkError("input_file_mode_invalid")
        if not 1 <= metadata.st_size <= maximum:
            raise BenchmarkError("input_file_size_invalid")
        value = os.read(descriptor, maximum + 1)
    finally:
        os.close(descriptor)
    if not 1 <= len(value) <= maximum:
        raise BenchmarkError("input_file_size_invalid")
    return value


def read_json(path: Path, *, owner_only: bool = False) -> dict[str, Any]:
    raw = _read_regular_file(
        path,
        maximum=MAX_JSON_BYTES,
        exact_mode=0o600 if owner_only else None,
    )
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        raise BenchmarkError("input_json_invalid") from None
    if not isinstance(value, dict):
        raise BenchmarkError("input_json_invalid")
    return value


def read_token(path: Path) -> str:
    raw = _read_regular_file(path, maximum=MAX_TOKEN_BYTES, exact_mode=0o600)
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if not 32 <= len(raw) <= MAX_TOKEN_BYTES or any(byte < 33 or byte > 126 for byte in raw):
        raise BenchmarkError("token_file_invalid")
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError:
        raise BenchmarkError("token_file_invalid") from None


def atomic_write(path: Path, value: bytes, *, forbidden: bytes = b"") -> None:
    if not path.is_absolute() or (forbidden and forbidden in value):
        raise BenchmarkError("output_path_or_redaction_invalid")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        parent = path.parent.stat()
    except OSError:
        raise BenchmarkError("output_parent_invalid") from None
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.geteuid()
        or stat.S_IMODE(parent.st_mode) & 0o077
        or path.exists()
    ):
        raise BenchmarkError("output_path_invalid")
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except OSError:
            raise BenchmarkError("output_path_invalid") from None
        temporary.unlink()
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def validate_origin(value: str, tls_mode: str) -> tuple[str, ssl.SSLContext]:
    parsed = urlsplit(value.rstrip("/"))
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise BenchmarkError("endpoint_invalid")
    origin = f"https://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")
    if tls_mode == "verified":
        return origin, ssl.create_default_context()
    if tls_mode != "disposable-staging-insecure":
        raise BenchmarkError("tls_mode_invalid")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        raise BenchmarkError("insecure_tls_endpoint_invalid") from None
    if not isinstance(address, ipaddress.IPv4Address) or not address.is_global:
        raise BenchmarkError("insecure_tls_endpoint_invalid")
    return origin, ssl._create_unverified_context()  # noqa: SLF001 - explicit staging mode.


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: dict[str, str]
    body: bytes
    first_byte_at: str
    completed_at: str
    first_byte_monotonic_ns: int
    completed_monotonic_ns: int


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Never forward the bearer credential to a redirected origin."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def http_json(
    origin: str,
    context: ssl.SSLContext,
    token: str,
    method: str,
    path: str,
    *,
    payload: object | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
) -> HttpResult:
    body = None if payload is None else canonical_json(payload)
    request_headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request_headers.update(headers or {})
    request = urllib.request.Request(
        origin + path,
        data=body,
        headers=request_headers,
        method=method,
    )
    opener = urllib.request.build_opener(RejectRedirects(), urllib.request.HTTPSHandler(context=context))
    try:
        with opener.open(request, timeout=timeout) as response:
            first = response.read(1)
            first_wall = utc_now()
            first_at = time.monotonic_ns()
            raw = first + response.read(MAX_JSON_BYTES)
            completed_wall = utc_now()
            completed_at = time.monotonic_ns()
            if len(raw) > MAX_JSON_BYTES:
                raise BenchmarkError("http_response_too_large")
            return HttpResult(
                response.status,
                {key.lower(): value for key, value in response.headers.items()},
                raw,
                first_wall,
                completed_wall,
                first_at,
                completed_at,
            )
    except urllib.error.HTTPError as error:
        raw = error.read(min(MAX_JSON_BYTES, 1024 * 1024))
        raise BenchmarkError(f"http_status_{error.code}") from None
    except (TimeoutError, urllib.error.URLError, ssl.SSLError):
        raise BenchmarkError("http_request_failed") from None


def decode_json(result: HttpResult) -> dict[str, Any]:
    try:
        value = json.loads(result.body)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        raise BenchmarkError("http_response_json_invalid") from None
    if not isinstance(value, dict):
        raise BenchmarkError("http_response_json_invalid")
    return value


class Kubectl:
    def __init__(self, kubeconfig: Path, context: str) -> None:
        if not kubeconfig.is_absolute() or SAFE_NAME.fullmatch(context) is None:
            raise BenchmarkError("kubernetes_identity_invalid")
        self.prefix = [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig),
            "--context",
            context,
        ]

    def get(
        self,
        resource: str,
        *,
        name: str | None = None,
        namespace: str | None = None,
        selector: str | None = None,
        field_selector: str | None = None,
        all_namespaces: bool = False,
        allow_missing: bool = False,
    ) -> dict[str, Any] | None:
        for value in (resource, name, namespace):
            if value is not None and SAFE_NAME.fullmatch(value) is None:
                raise BenchmarkError("kubernetes_identity_invalid")
        command = [*self.prefix, "get", resource]
        if name is not None:
            command.append(name)
        if namespace is not None:
            command.extend(["--namespace", namespace])
        elif all_namespaces:
            command.append("--all-namespaces")
        if selector is not None:
            command.extend(["--selector", selector])
        if field_selector is not None:
            command.extend(["--field-selector", field_selector])
        command.extend(["--output", "json"])
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            if allow_missing and b"NotFound" in result.stderr:
                return None
            raise BenchmarkError("kubectl_get_failed")
        try:
            value = json.loads(result.stdout)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            raise BenchmarkError("kubectl_response_invalid") from None
        if not isinstance(value, dict):
            raise BenchmarkError("kubectl_response_invalid")
        return value

    def logs(self, namespace: str, pod: str, container: str) -> bytes:
        for value in (namespace, pod, container):
            if SAFE_NAME.fullmatch(value) is None:
                raise BenchmarkError("kubernetes_identity_invalid")
        result = subprocess.run(
            [
                *self.prefix,
                "logs",
                pod,
                "--namespace",
                namespace,
                "--container",
                container,
                "--timestamps=true",
            ],
            check=False,
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0 or len(result.stdout) > MAX_JSON_BYTES:
            raise BenchmarkError("kubectl_logs_failed")
        return result.stdout

    def gpu_identity(self, namespace: str, pod: str, container: str) -> dict[str, Any]:
        for value in (namespace, pod, container):
            if SAFE_NAME.fullmatch(value) is None:
                raise BenchmarkError("kubernetes_identity_invalid")
        result = subprocess.run(
            [
                *self.prefix,
                "exec",
                pod,
                "--namespace",
                namespace,
                "--container",
                container,
                "--",
                "nvidia-smi",
                "--query-gpu=name,memory.total,driver_version,compute_cap",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return {}
        first = result.stdout.splitlines()[0] if result.stdout.splitlines() else ""
        values = [value.strip() for value in first.split(",")]
        if len(values) != 4:
            return {}
        version = subprocess.run(
            [
                *self.prefix,
                "exec",
                pod,
                "--namespace",
                namespace,
                "--container",
                container,
                "--",
                "nvidia-smi",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        cuda_match = (
            re.search(r"CUDA Version:\s*([0-9]+\.[0-9]+)", version.stdout)
            if version.returncode == 0 and len(version.stdout) <= 1024 * 1024
            else None
        )
        try:
            memory_bytes = int(float(values[1]) * 1024 * 1024)
        except ValueError:
            memory_bytes = None
        return {
            "product": values[0] or None,
            "memory_bytes": memory_bytes,
            "driver_version": values[2] or None,
            "compute_capability": values[3] if re.fullmatch(r"[0-9]+\.[0-9]+", values[3]) else None,
            "cuda_version": cuda_match.group(1) if cuda_match is not None else None,
        }

    def delete_pod(self, namespace: str, pod: str, uid: str) -> None:
        for value in (namespace, pod):
            if SAFE_NAME.fullmatch(value) is None:
                raise BenchmarkError("kubernetes_identity_invalid")
        try:
            canonical_uid = str(UUID(uid))
        except (ValueError, AttributeError):
            raise BenchmarkError("pod_identity_invalid") from None
        deletion = canonical_json(
            {
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "preconditions": {"uid": canonical_uid},
            }
        )
        result = subprocess.run(
            [
                *self.prefix,
                "delete",
                "--raw",
                f"/api/v1/namespaces/{namespace}/pods/{pod}",
                "--filename=-",
            ],
            check=False,
            capture_output=True,
            input=deletion,
            timeout=30,
        )
        if result.returncode != 0:
            raise BenchmarkError("pod_recycle_failed")


def _condition_true(value: dict[str, Any], kind: str) -> bool:
    return any(
        isinstance(item, dict) and item.get("type") == kind and item.get("status") == "True"
        for item in value.get("status", {}).get("conditions", [])
    )


def _resource_gpu_count(spec: dict[str, Any]) -> int:
    """Return the scheduler-equivalent whole-GPU request for a PodSpec."""

    def container_count(container: dict[str, Any]) -> int:
        resources = container.get("resources", {})
        limits = resources.get("limits", {}) if isinstance(resources, dict) else {}
        requests = resources.get("requests", {}) if isinstance(resources, dict) else {}
        raw = limits.get("nvidia.com/gpu", requests.get("nvidia.com/gpu", 0))
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise BenchmarkError("gpu_resource_quantity_invalid") from None
        if value < 0:
            raise BenchmarkError("gpu_resource_quantity_invalid")
        return value

    regular = sum(container_count(item) for item in spec.get("containers", []) or [] if isinstance(item, dict))
    initial = max(
        (container_count(item) for item in spec.get("initContainers", []) or [] if isinstance(item, dict)),
        default=0,
    )
    return max(regular, initial)


def _pod_is_live(pod: dict[str, Any]) -> bool:
    return pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}


def _node_tolerated(pod_spec: dict[str, Any], node: dict[str, Any]) -> bool:
    tolerations = [item for item in pod_spec.get("tolerations", []) or [] if isinstance(item, dict)]
    for taint in node.get("spec", {}).get("taints", []) or []:
        if not isinstance(taint, dict) or taint.get("effect") not in {
            "NoSchedule",
            "NoExecute",
        }:
            continue
        key = taint.get("key")
        value = taint.get("value", "")
        tolerated = any(
            tolerance.get("key") == key
            and tolerance.get("effect") in {None, "", taint.get("effect")}
            and (
                tolerance.get("operator", "Equal") == "Exists"
                or (tolerance.get("operator", "Equal") == "Equal" and tolerance.get("value", "") == value)
            )
            for tolerance in tolerations
        )
        if not tolerated:
            return False
    return True


@dataclass(frozen=True)
class ClusterObservation:
    observed_at: str
    replicas: int
    ready_replicas: int
    endpoints: int
    capacity_requested: bool
    capacity_available: bool
    pod: dict[str, Any] | None
    node: dict[str, Any] | None
    deployment: dict[str, Any]
    pod_count: int = 0
    terminating_pods: int = 0
    hpa_desired_replicas: int = 0
    scaled_object_active: bool = False
    scaled_replicas: int = 0
    scaled_ready_replicas: int = 0
    scaled_pod_count: int = 0
    scaled_terminating_pods: int = 0
    scaler_targets_primary: bool = True
    ready_endpoint_pod_uids: tuple[str, ...] = ()
    scaled_deployment: dict[str, Any] | None = None
    service: dict[str, Any] | None = None
    scaled_object: dict[str, Any] | None = None


def observe_cluster(
    kubectl: Kubectl,
    namespace: str,
    deployment_name: str,
    service_name: str,
    scaled_object_name: str,
    scaled_deployment_name: str | None = None,
) -> ClusterObservation:
    scaled_deployment_name = scaled_deployment_name or deployment_name
    deployment = kubectl.get("deployment", name=deployment_name, namespace=namespace)
    assert deployment is not None
    scaled_deployment = (
        deployment
        if scaled_deployment_name == deployment_name
        else kubectl.get("deployment", name=scaled_deployment_name, namespace=namespace)
    )
    assert scaled_deployment is not None
    scaled_object = kubectl.get(
        "scaledobject.keda.sh",
        name=scaled_object_name,
        namespace=namespace,
    )
    assert scaled_object is not None
    target_ref = scaled_object.get("spec", {}).get("scaleTargetRef", {})
    if (
        target_ref.get("apiVersion", "apps/v1") != "apps/v1"
        or target_ref.get("kind", "Deployment") != "Deployment"
        or target_ref.get("name") != scaled_deployment_name
    ):
        raise BenchmarkError("scaled_object_target_mismatch")
    hpa_name = scaled_object.get("status", {}).get("hpaName")
    hpa = (
        kubectl.get(
            "horizontalpodautoscaler.autoscaling",
            name=hpa_name,
            namespace=namespace,
            allow_missing=True,
        )
        if isinstance(hpa_name, str) and SAFE_NAME.fullmatch(hpa_name)
        else None
    )
    if hpa is not None:
        hpa_target = hpa.get("spec", {}).get("scaleTargetRef", {})
        if (
            hpa_target.get("apiVersion", "apps/v1") != "apps/v1"
            or hpa_target.get("kind") != "Deployment"
            or hpa_target.get("name") != scaled_deployment_name
        ):
            raise BenchmarkError("hpa_target_mismatch")
    selector_labels = deployment.get("spec", {}).get("selector", {}).get("matchLabels")
    if not isinstance(selector_labels, dict) or not selector_labels:
        raise BenchmarkError("deployment_selector_invalid")
    selector = ",".join(f"{key}={value}" for key, value in sorted(selector_labels.items()))
    template_labels = deployment.get("spec", {}).get("template", {}).get("metadata", {}).get("labels")
    if not isinstance(template_labels, dict) or not all(
        template_labels.get(key) == value for key, value in selector_labels.items()
    ):
        raise BenchmarkError("deployment_template_selector_mismatch")
    service = kubectl.get("service", name=service_name, namespace=namespace)
    assert service is not None
    service_selector = service.get("spec", {}).get("selector")
    if (
        not isinstance(service_selector, dict)
        or not service_selector
        or not all(template_labels.get(key) == value for key, value in service_selector.items())
    ):
        raise BenchmarkError("service_selector_mismatch")
    pods = kubectl.get("pods", namespace=namespace, selector=selector)
    scaled_selector_labels = scaled_deployment.get("spec", {}).get("selector", {}).get("matchLabels")
    if not isinstance(scaled_selector_labels, dict) or not scaled_selector_labels:
        raise BenchmarkError("scaled_deployment_selector_invalid")
    scaled_selector = ",".join(f"{key}={value}" for key, value in sorted(scaled_selector_labels.items()))
    scaled_pods = (
        pods
        if scaled_deployment_name == deployment_name
        else kubectl.get("pods", namespace=namespace, selector=scaled_selector)
    )
    slices = kubectl.get(
        "endpointslices.discovery.k8s.io",
        namespace=namespace,
        selector=f"kubernetes.io/service-name={service_name}",
    )
    assert pods is not None and scaled_pods is not None and slices is not None
    pod_items = [item for item in pods.get("items", []) if isinstance(item, dict)]
    scaled_pod_items = [item for item in scaled_pods.get("items", []) if isinstance(item, dict)]

    # Bind ReplicaSets back to the exact Deployment UID. Label selectors alone
    # are insufficient when an old or malicious controller overlaps labels.
    deployments_by_uid = {
        item.get("metadata", {}).get("uid"): item
        for item in (deployment, scaled_deployment)
        if isinstance(item.get("metadata", {}).get("uid"), str)
    }
    replica_sets = kubectl.get("replicasets", namespace=namespace)
    assert replica_sets is not None
    replica_set_owners: dict[str, str] = {}
    for replica_set in replica_sets.get("items", []) or []:
        if not isinstance(replica_set, dict):
            continue
        replica_set_uid = replica_set.get("metadata", {}).get("uid")
        owner_uids = {
            owner.get("uid")
            for owner in replica_set.get("metadata", {}).get("ownerReferences", []) or []
            if isinstance(owner, dict) and owner.get("controller") is True and owner.get("kind") == "Deployment"
        }
        matched = owner_uids.intersection(deployments_by_uid)
        if isinstance(replica_set_uid, str) and len(matched) == 1:
            replica_set_owners[replica_set_uid] = next(iter(matched))

    def validate_pod_ownership(items: list[dict[str, Any]], expected_deployment: dict[str, Any]) -> None:
        expected_uid = expected_deployment.get("metadata", {}).get("uid")
        for item in items:
            controller_uids = {
                owner.get("uid")
                for owner in item.get("metadata", {}).get("ownerReferences", []) or []
                if isinstance(owner, dict) and owner.get("controller") is True and owner.get("kind") == "ReplicaSet"
            }
            if not any(replica_set_owners.get(owner_uid) == expected_uid for owner_uid in controller_uids):
                raise BenchmarkError("pod_controller_ownership_mismatch")

    validate_pod_ownership(pod_items, deployment)
    if scaled_deployment_name != deployment_name:
        validate_pod_ownership(scaled_pod_items, scaled_deployment)

    all_pods_by_uid = {
        item.get("metadata", {}).get("uid"): item
        for item in [*pod_items, *scaled_pod_items]
        if isinstance(item.get("metadata", {}).get("uid"), str)
    }
    ready_endpoint_uids: list[str] = []
    for item in slices.get("items", []) or []:
        if not isinstance(item, dict):
            continue
        for endpoint in item.get("endpoints", []) or []:
            if not isinstance(endpoint, dict) or endpoint.get("conditions", {}).get("ready") is not True:
                continue
            target = endpoint.get("targetRef")
            uid = target.get("uid") if isinstance(target, dict) else None
            if (
                not isinstance(target, dict)
                or target.get("kind") != "Pod"
                or target.get("namespace", namespace) != namespace
                or not isinstance(uid, str)
                or uid not in all_pods_by_uid
            ):
                raise BenchmarkError("endpoint_pod_identity_mismatch")
            ready_endpoint_uids.append(uid)

    selected_pod = next(
        (
            item
            for item in pod_items
            if item.get("metadata", {}).get("deletionTimestamp") is None
            and item.get("spec", {}).get("nodeName")
            and _condition_true(item, "Ready")
            and item.get("metadata", {}).get("uid") in ready_endpoint_uids
        ),
        next(
            (
                item
                for item in pod_items
                if item.get("metadata", {}).get("deletionTimestamp") is None and item.get("spec", {}).get("nodeName")
            ),
            pod_items[0] if pod_items else None,
        ),
    )
    selected_node = None
    capacity_available = False
    pod_spec = deployment.get("spec", {}).get("template", {}).get("spec", {})
    if not isinstance(pod_spec, dict):
        raise BenchmarkError("deployment_pod_spec_invalid")
    requested_gpus = _resource_gpu_count(pod_spec)
    if requested_gpus < 1:
        raise BenchmarkError("gpu_request_missing")
    node_selector = pod_spec.get("nodeSelector", {})
    if not isinstance(node_selector, dict) or not node_selector:
        raise BenchmarkError("gpu_node_selector_missing")

    def suitable_node(candidate: dict[str, Any], *, assigned: bool) -> bool:
        labels = candidate.get("metadata", {}).get("labels", {})
        if (
            not all(labels.get(key) == value for key, value in node_selector.items())
            or candidate.get("spec", {}).get("unschedulable") is True
            or not _condition_true(candidate, "Ready")
            or not _node_tolerated(pod_spec, candidate)
        ):
            return False
        try:
            allocatable = int(candidate.get("status", {}).get("allocatable", {}).get("nvidia.com/gpu", "0"))
        except (TypeError, ValueError):
            return False
        if assigned:
            return allocatable >= requested_gpus
        node_name = candidate.get("metadata", {}).get("name")
        if not isinstance(node_name, str) or SAFE_NAME.fullmatch(node_name) is None:
            raise BenchmarkError("node_identity_invalid")
        node_pods = kubectl.get(
            "pods",
            all_namespaces=True,
            field_selector=f"spec.nodeName={node_name}",
        )
        assert node_pods is not None
        allocated = sum(
            _resource_gpu_count(item.get("spec", {}))
            for item in node_pods.get("items", []) or []
            if isinstance(item, dict) and _pod_is_live(item)
        )
        return allocatable - allocated >= requested_gpus

    if selected_pod is not None:
        node_name = selected_pod.get("spec", {}).get("nodeName")
        if isinstance(node_name, str):
            selected_node = kubectl.get("node", name=node_name, allow_missing=True)
            if selected_node is not None:
                # An assigned Pod is the scheduler's authoritative proof that
                # its requested GPUs were available; still bind the node to
                # the exact selector and Ready state.
                capacity_available = suitable_node(selected_node, assigned=True)
    if selected_node is None:
        nodes = kubectl.get("nodes")
        assert nodes is not None
        for candidate in nodes.get("items", []) or []:
            if not isinstance(candidate, dict):
                continue
            if suitable_node(candidate, assigned=False):
                selected_node = candidate
                capacity_available = True
                break
    endpoints = len(ready_endpoint_uids)
    replicas = int(deployment.get("spec", {}).get("replicas") or 0)
    scaled_replicas = int(scaled_deployment.get("spec", {}).get("replicas") or 0)
    desired = int((hpa or {}).get("status", {}).get("desiredReplicas") or 0)
    active = _condition_true(scaled_object, "Active")
    capacity_requested = (
        replicas > 0 or scaled_replicas > 0 or desired > 0 or bool(pod_items) or bool(scaled_pod_items) or active
    )
    return ClusterObservation(
        observed_at=utc_now(),
        replicas=replicas,
        ready_replicas=int(deployment.get("status", {}).get("readyReplicas") or 0),
        endpoints=endpoints,
        capacity_requested=capacity_requested,
        capacity_available=capacity_available,
        pod=selected_pod,
        node=selected_node,
        deployment=deployment,
        pod_count=len(pod_items),
        terminating_pods=sum(item.get("metadata", {}).get("deletionTimestamp") is not None for item in pod_items),
        hpa_desired_replicas=desired,
        scaled_object_active=active,
        scaled_replicas=scaled_replicas,
        scaled_ready_replicas=int(scaled_deployment.get("status", {}).get("readyReplicas") or 0),
        scaled_pod_count=len(scaled_pod_items),
        scaled_terminating_pods=sum(
            item.get("metadata", {}).get("deletionTimestamp") is not None for item in scaled_pod_items
        ),
        scaler_targets_primary=scaled_deployment_name == deployment_name,
        ready_endpoint_pod_uids=tuple(sorted(ready_endpoint_uids)),
        scaled_deployment=scaled_deployment,
        service=service,
        scaled_object=scaled_object,
    )


def _wire_request(model_id: str, request_document: dict[str, Any]) -> tuple[str, dict[str, Any], str, str]:
    protocol = request_document.get("protocol")
    operation = request_document.get("operation")
    payload = request_document.get("payload")
    if not isinstance(operation, str) or not isinstance(payload, dict):
        raise BenchmarkError("request_contract_invalid")
    if protocol == "openai-chat":
        if payload.get("model") != model_id or payload.get("stream") is True:
            raise BenchmarkError("request_contract_invalid")
        return "/v1/chat/completions", payload, "text", protocol
    if protocol == "native":
        if model_id != "cosmos3-nano":
            raise BenchmarkError("native_validator_not_implemented")
        return (
            f"/v1/models/{model_id}:invoke",
            {"operation": operation, "payload": payload},
            "native",
            protocol,
        )
    raise BenchmarkError("request_protocol_unsupported")


def _load_cosmos_validator() -> tuple[Any, dict[str, Any]]:
    spec = importlib.util.spec_from_file_location(
        "_fs2_cosmos3_nano_live_benchmark_validator",
        COSMOS_VALIDATOR_PATH,
    )
    if spec is None or spec.loader is None:
        raise BenchmarkError("semantic_validator_unavailable")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        contract = module.load_contract(COSMOS_CONTRACT_PATH)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, RecursionError):
        raise BenchmarkError("semantic_validator_unavailable") from None
    if not isinstance(contract, dict):
        raise BenchmarkError("semantic_validator_unavailable")
    return module, contract


def _validate_cosmos_result(result: dict[str, Any], request_payload: dict[str, Any] | None) -> tuple[bool, bytes]:
    if request_payload is None:
        return False, b""
    module, contract = _load_cosmos_validator()
    requests = contract.get("requests")
    if not isinstance(requests, list):
        raise BenchmarkError("semantic_validator_unavailable")
    matches = [item for item in requests if isinstance(item, dict) and item.get("request") == request_payload]
    if len(matches) != 1 or not isinstance(matches[0].get("id"), str):
        return False, b""
    try:
        module.validate_response(result, matches[0], matches[0]["id"])
        decoded = base64.b64decode(result["data_base64"], validate=True)
    except (KeyError, TypeError, ValueError, base64.binascii.Error):
        return False, b""
    return True, decoded


def _semantic_validator_digest(
    *,
    model_id: str,
    modality: str,
    expected_text: str | None,
    request_payload: dict[str, Any],
) -> str:
    if model_id == "cosmos3-nano":
        try:
            source_digest = sha256_bytes(COSMOS_VALIDATOR_PATH.read_bytes())
            contract_digest = sha256_bytes(COSMOS_CONTRACT_PATH.read_bytes())
        except OSError:
            raise BenchmarkError("semantic_validator_unavailable") from None
        return sha256_json(
            {
                "implementation": "catalog/runtime/validators/validate_cosmos3_nano.py",
                "source_sha256": source_digest,
                "contract_sha256": contract_digest,
                "request_sha256": sha256_json(request_payload),
            }
        )
    return sha256_json(
        {
            "implementation": "run_live_fast_start_benchmark.py:_validate_result/v2",
            "model_id": model_id,
            "modality": modality,
            "expected_text_sha256": (
                sha256_bytes(expected_text.encode("utf-8")) if expected_text is not None else None
            ),
        }
    )


def _validate_result(
    result: dict[str, Any],
    modality: str,
    expected_text: str | None,
    elapsed_seconds: float,
    *,
    model_id: str | None = None,
    request_payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    if modality == "text":
        choices = result.get("choices")
        content = (
            choices[0].get("message", {}).get("content")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict)
            else None
        )
        valid = isinstance(content, str) and bool(content.strip())
        if expected_text is not None:
            valid = valid and content.strip() == expected_text
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        completion_tokens = usage.get("completion_tokens")
        valid = valid and isinstance(completion_tokens, int) and completion_tokens > 0
        output_units = (
            {"unit": "tokens", "count": completion_tokens}
            if isinstance(completion_tokens, int) and completion_tokens > 0
            else None
        )
        throughput = (
            {
                "unit": "output-tokens-per-second",
                "value": completion_tokens / elapsed_seconds,
            }
            if output_units is not None and elapsed_seconds > 0
            else None
        )
        semantic = content.encode("utf-8") if isinstance(content, str) else b""
        return (
            {
                "modality": "text",
                "first_output_kind": "response",
                "valid_output": valid,
                "http_status": 200,
                "request_count": 1,
                "warmup_count": 0,
                "concurrency": 1,
                "input_units": (
                    {"unit": "tokens", "count": usage["prompt_tokens"]}
                    if isinstance(usage.get("prompt_tokens"), int)
                    else None
                ),
                "output_units": output_units,
                "throughput": throughput,
            },
            sha256_bytes(semantic) if semantic else "",
        )
    expected_bytes = result.get("bytes")
    frames = result.get("frames")
    valid, decoded = _validate_cosmos_result(result, request_payload) if model_id == "cosmos3-nano" else (False, b"")
    output_units = (
        {"unit": "frames", "count": frames}
        if isinstance(frames, int) and frames > 0
        else ({"unit": "bytes", "count": expected_bytes} if isinstance(expected_bytes, int) else None)
    )
    throughput = (
        {
            "unit": ("frames-per-second" if output_units["unit"] == "frames" else "bytes-per-second"),
            "value": output_units["count"] / elapsed_seconds,
        }
        if output_units is not None and elapsed_seconds > 0
        else None
    )
    return (
        {
            "modality": modality,
            "first_output_kind": "media",
            "valid_output": bool(valid),
            "http_status": 200,
            "request_count": 1,
            "warmup_count": 0,
            "concurrency": 1,
            "input_units": None,
            "output_units": output_units,
            "throughput": throughput,
        },
        sha256_bytes(decoded) if decoded else "",
    )


def _safe_env_digest(containers: list[dict[str, Any]]) -> str:
    environment: list[dict[str, Any]] = []
    for container in containers:
        for item in container.get("env", []) or []:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            environment.append(
                {
                    "container": container.get("name"),
                    "name": item["name"],
                    "value_sha256": (sha256_bytes(str(item["value"]).encode()) if "value" in item else None),
                    "value_from": item.get("valueFrom"),
                }
            )
    return sha256_json(environment)


def _sha_digest(value: Any) -> str | None:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        return None
    return value if value.startswith("sha256:") else "sha256:" + value


def _model_deployment_resource_binding(
    model_deployment: dict[str, Any],
    observation: ClusterObservation,
    *,
    namespace: str,
    model_id: str,
) -> str:
    """Bind observed runtime resources to one converged ModelDeployment revision."""

    api_version = model_deployment.get("apiVersion")
    kind = model_deployment.get("kind")
    metadata = model_deployment.get("metadata")
    status = model_deployment.get("status")
    if not isinstance(metadata, dict) or not isinstance(status, dict):
        raise BenchmarkError("model_deployment_identity_mismatch")
    generation = metadata.get("generation")
    owner_uid = metadata.get("uid")
    if (
        api_version != "inference.fs2.nebius.ai/v1alpha1"
        or kind != "ModelDeployment"
        or metadata.get("name") != model_id
        or metadata.get("namespace") != namespace
        or not isinstance(owner_uid, str)
        or not owner_uid
        or metadata.get("deletionTimestamp") is not None
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
    ):
        raise BenchmarkError("model_deployment_identity_mismatch")
    if status.get("observedGeneration") != generation:
        raise BenchmarkError("model_deployment_status_stale")
    spec_digest = _sha_digest(status.get("specDigest"))
    if spec_digest is None:
        raise BenchmarkError("model_deployment_spec_digest_missing")

    resource_statuses = status.get("resources")
    if not isinstance(resource_statuses, list) or not resource_statuses:
        raise BenchmarkError("model_deployment_resources_missing")
    status_by_identity: dict[str, dict[str, Any]] = {}
    for item in resource_statuses:
        if not isinstance(item, dict):
            raise BenchmarkError("model_deployment_resource_status_invalid")
        identity = item.get("identity")
        item_generation = item.get("generation")
        identity_fields = tuple(item.get(field) for field in ("apiVersion", "kind", "namespace", "name"))
        expected_identity = "/".join(value if isinstance(value, str) else "" for value in identity_fields)
        if (
            not isinstance(identity, str)
            or identity != expected_identity
            or identity in status_by_identity
            or any(not isinstance(value, str) or not value for value in identity_fields)
            or not isinstance(item.get("uid"), str)
            or not item["uid"]
            or isinstance(item_generation, bool)
            or not isinstance(item_generation, int)
            or item_generation < 0
            or _sha_digest(item.get("digest")) is None
        ):
            raise BenchmarkError("model_deployment_resource_status_invalid")
        status_by_identity[identity] = item

    resources = [
        observation.deployment,
        observation.scaled_deployment,
        observation.service,
        observation.scaled_object,
    ]
    if any(not isinstance(resource, dict) for resource in resources):
        raise BenchmarkError("model_deployment_observation_incomplete")

    bound: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for resource in resources:
        assert isinstance(resource, dict)
        resource_metadata = resource.get("metadata")
        if not isinstance(resource_metadata, dict):
            raise BenchmarkError("model_deployment_resource_identity_mismatch")
        resource_generation = resource_metadata.get("generation", 0)
        resource_identity = "/".join(
            str(value)
            for value in (
                resource.get("apiVersion", ""),
                resource.get("kind", ""),
                resource_metadata.get("namespace", ""),
                resource_metadata.get("name", ""),
            )
        )
        item = status_by_identity.get(resource_identity)
        if (
            item is None
            or resource_metadata.get("uid") != item.get("uid")
            or isinstance(resource_generation, bool)
            or not isinstance(resource_generation, int)
            or resource_generation != item.get("generation")
            or resource_metadata.get("deletionTimestamp") is not None
        ):
            raise BenchmarkError("model_deployment_resource_identity_mismatch")
        controllers = [
            owner
            for owner in resource_metadata.get("ownerReferences", []) or []
            if isinstance(owner, dict) and owner.get("controller") is True
        ]
        if len(controllers) != 1 or any(
            controllers[0].get(field) != value
            for field, value in (
                ("apiVersion", api_version),
                ("kind", kind),
                ("name", model_id),
                ("uid", owner_uid),
            )
        ):
            raise BenchmarkError("model_deployment_resource_ownership_mismatch")
        annotations = resource_metadata.get("annotations", {})
        if not isinstance(annotations, dict) or annotations.get("fs2-serve.nebius.ai/spec-digest") != spec_digest:
            raise BenchmarkError("model_deployment_resource_revision_mismatch")
        bound[resource_identity] = (resource, item)

    endpoint = status.get("endpoint")
    service = observation.service
    assert isinstance(service, dict)
    service_metadata = service.get("metadata")
    if not isinstance(service_metadata, dict):
        raise BenchmarkError("model_deployment_endpoint_identity_mismatch")
    service_identity = "/".join(
        (
            str(service.get("apiVersion", "")),
            str(service.get("kind", "")),
            str(service_metadata.get("namespace", "")),
            str(service_metadata.get("name", "")),
        )
    )
    service_status = bound.get(service_identity)
    service_spec = service.get("spec")
    service_ports = service_spec.get("ports", []) if isinstance(service_spec, dict) else []
    if (
        not isinstance(endpoint, dict)
        or service_status is None
        or endpoint.get("namespace") != namespace
        or endpoint.get("serviceName") != service_metadata.get("name")
        or endpoint.get("uid") != service_metadata.get("uid")
        or endpoint.get("digest") != service_status[1].get("digest")
        or not isinstance(service_ports, list)
        or not any(isinstance(port, dict) and port.get("port") == endpoint.get("servicePort") for port in service_ports)
    ):
        raise BenchmarkError("model_deployment_endpoint_identity_mismatch")
    return spec_digest


def build_compatibility_tuple(
    *,
    source_commit: str,
    bundle: dict[str, Any],
    context: str,
    namespace: str,
    model_id: str,
    model_deployment: dict[str, Any],
    observation: ClusterObservation,
    capacity_state: str,
    mechanism: str,
    payload_digest: str,
    client_placement: str,
    interface_protocol: str,
    endpoint_path: str,
    semantic_validator_digest: str,
    benchmark_client_digest: str,
    gpu_identity: dict[str, Any],
    storage_class: str | None,
    storage_mode: str | None,
) -> dict[str, Any]:
    deployment = observation.deployment
    annotations = deployment.get("metadata", {}).get("annotations", {})
    template = deployment.get("spec", {}).get("template", {})
    pod_annotations = template.get("metadata", {}).get("annotations", {})
    pod_spec = template.get("spec", {})
    containers = [item for item in pod_spec.get("containers", []) if isinstance(item, dict)]
    if not containers:
        raise BenchmarkError("runtime_container_missing")
    runtime = containers[0]
    image = runtime.get("image")
    if not isinstance(image, str) or "@sha256:" not in image:
        raise BenchmarkError("runtime_image_unpinned")
    image_digest = image.rsplit("@sha256:", 1)[1]
    node = observation.node or {}
    node_labels = node.get("metadata", {}).get("labels", {})
    selector = pod_spec.get("nodeSelector", {})
    cache = model_deployment.get("spec", {}).get("cache", {})
    snapshot = cache.get("snapshotRef") if isinstance(cache.get("snapshotRef"), dict) else {}
    volume_claim = next(
        (
            volume.get("persistentVolumeClaim", {}).get("claimName")
            for volume in pod_spec.get("volumes", []) or []
            if isinstance(volume, dict) and isinstance(volume.get("persistentVolumeClaim"), dict)
        ),
        None,
    )
    cluster = bundle.get("cluster") if isinstance(bundle.get("cluster"), dict) else {}
    spec_digest = _model_deployment_resource_binding(
        model_deployment,
        observation,
        namespace=namespace,
        model_id=model_id,
    )
    # Dynamic routes intentionally bind durable operations to the complete
    # desired-state revision, not to the currently active artifact revision.
    # Freeze that route identity before recycling or activating any Pod.
    model_revision = f"dynamic:{spec_digest}"
    model_content = _sha_digest(
        annotations.get("fs2.nebius/model-content-digest") or pod_annotations.get("fs2.nebius/model-content-digest")
    )
    desired = model_deployment.get("spec", {})
    artifact_manifest = _sha_digest(desired.get("artifact", {}).get("manifestDigest"))
    runtime_template_digest = _sha_digest(desired.get("runtime", {}).get("templateRef", {}).get("digest"))
    argv = {"command": runtime.get("command") or [], "args": runtime.get("args") or []}
    accelerator_count = _resource_gpu_count(pod_spec)
    if accelerator_count < 1:
        raise BenchmarkError("gpu_request_missing")
    return {
        "source_commit": source_commit,
        "project_id": cluster.get("project_id"),
        "region": cluster.get("region"),
        "cluster_context": context,
        "namespace": namespace,
        "model_id": model_id,
        "model_revision": model_revision,
        "model_content_digest": model_content,
        "artifact_manifest_digest": artifact_manifest,
        "runtime_image_ref": image,
        "runtime_image_digest": "sha256:" + image_digest,
        "runtime_template_digest": runtime_template_digest,
        "runtime_argv_digest": sha256_json(argv),
        "runtime_environment_digest": _safe_env_digest(containers),
        "accelerator_class": selector.get("accelerator.fs2.nebius/class")
        or node_labels.get("accelerator.fs2.nebius/class"),
        "gpu_product": gpu_identity.get("product") or node_labels.get("nvidia.com/gpu.product"),
        "gpu_compute_capability": gpu_identity.get("compute_capability"),
        "gpu_memory_bytes": gpu_identity.get("memory_bytes"),
        "gpu_count": accelerator_count,
        "driver_version": gpu_identity.get("driver_version") or node_labels.get("nebius.com/nvidia_driver_version"),
        "cuda_version": gpu_identity.get("cuda_version") or node_labels.get("nebius.com/cuda_version"),
        "pool_id": selector.get("accelerator.fs2.nebius/pool-id")
        or annotations.get("fs2-serve.nebius.ai/workload-pool-ref"),
        "capacity_type": selector.get("capacity.fs2.nebius/type") or node_labels.get("capacity.fs2.nebius/type"),
        "capacity_state": capacity_state,
        "cache_tier": cache.get("tier", "Disabled"),
        "mechanism": mechanism,
        "snapshot_digest": _sha_digest(snapshot.get("digest")),
        "storage_class": storage_class,
        "storage_mode": storage_mode or ("persistent-volume-claim" if volume_claim else "ephemeral"),
        "payload_digest": payload_digest,
        "client_placement": client_placement,
        "interface_protocol": interface_protocol,
        "endpoint_path": endpoint_path,
        "streaming": False,
        "semantic_validator_digest": semantic_validator_digest,
        "benchmark_client_digest": benchmark_client_digest,
    }


def _source_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(__file__).resolve().parents[3],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or re.fullmatch(r"[a-f0-9]{40}", value) is None:
        raise BenchmarkError("source_commit_unavailable")
    return value


def _duration(start: int | None, end: int | None) -> float | None:
    if start is None or end is None:
        return None
    return round(max(0, end - start) / 1_000_000_000, 6)


def _floor_restored(observation: ClusterObservation, expected_floor: int) -> bool:
    primary_exact = (
        observation.replicas == expected_floor
        and observation.ready_replicas == expected_floor
        and observation.endpoints == expected_floor
        and observation.pod_count == expected_floor
        and observation.terminating_pods == 0
    )
    if observation.scaler_targets_primary:
        scaler_exact = (
            observation.scaled_replicas == expected_floor
            and observation.scaled_ready_replicas == expected_floor
            and observation.scaled_pod_count == expected_floor
            and observation.scaled_terminating_pods == 0
            and observation.hpa_desired_replicas == expected_floor
            and not observation.scaled_object_active
        )
    else:
        scaler_exact = (
            observation.scaled_replicas == 0
            and observation.scaled_ready_replicas == 0
            and observation.scaled_pod_count == 0
            and observation.scaled_terminating_pods == 0
            and observation.hpa_desired_replicas == 0
            and not observation.scaled_object_active
        )
    return primary_exact and scaler_exact


def wait_for_floor(
    kubectl: Kubectl,
    *,
    namespace: str,
    deployment: str,
    service: str,
    scaled_object: str,
    scaled_deployment: str,
    expected_floor: int,
    deadline: float,
) -> ClusterObservation:
    """Wait until the controller-owned deployment is back at its intended floor."""

    while time.monotonic() < deadline:
        observation = observe_cluster(
            kubectl,
            namespace,
            deployment,
            service,
            scaled_object,
            scaled_deployment,
        )
        if _floor_restored(observation, expected_floor):
            return observation
        time.sleep(2)
    raise BenchmarkError("return_to_floor_timeout")


def _operation_identity(
    value: dict[str, Any],
    *,
    operation_id: str,
    model_id: str,
    model_revision: str,
    protocol: str,
    operation: str,
) -> None:
    if (
        str(value.get("id")) != operation_id
        or value.get("model_id") != model_id
        or value.get("model_revision") != model_revision
        or value.get("protocol") != protocol
        or value.get("operation") != operation
    ):
        raise BenchmarkError("operation_identity_mismatch")


def _release_operation(
    *,
    origin: str,
    context: ssl.SSLContext,
    token: str,
    operation_id: str,
    model_id: str,
    model_revision: str,
    protocol: str,
    operation: str,
    deadline: float,
) -> dict[str, Any]:
    status_result = http_json(
        origin,
        context,
        token,
        "GET",
        f"/v1/operations/{operation_id}",
        timeout=30,
    )
    current = decode_json(status_result)
    _operation_identity(
        current,
        operation_id=operation_id,
        model_id=model_id,
        model_revision=model_revision,
        protocol=protocol,
        operation=operation,
    )
    if current.get("status") not in TERMINAL_OPERATION_STATES:
        cancelled = http_json(
            origin,
            context,
            token,
            "POST",
            f"/v1/operations/{operation_id}:cancel",
            timeout=30,
        )
        current = decode_json(cancelled)
        _operation_identity(
            current,
            operation_id=operation_id,
            model_id=model_id,
            model_revision=model_revision,
            protocol=protocol,
            operation=operation,
        )
    while current.get("status") not in TERMINAL_OPERATION_STATES:
        if time.monotonic() >= deadline:
            raise BenchmarkError("operation_cancel_timeout")
        time.sleep(1)
        current = decode_json(
            http_json(
                origin,
                context,
                token,
                "GET",
                f"/v1/operations/{operation_id}",
                timeout=30,
            )
        )
        _operation_identity(
            current,
            operation_id=operation_id,
            model_id=model_id,
            model_revision=model_revision,
            protocol=protocol,
            operation=operation,
        )
    acknowledged = decode_json(
        http_json(
            origin,
            context,
            token,
            "POST",
            f"/v1/operations/{operation_id}:acknowledge",
            timeout=30,
        )
    )
    _operation_identity(
        acknowledged,
        operation_id=operation_id,
        model_id=model_id,
        model_revision=model_revision,
        protocol=protocol,
        operation=operation,
    )
    return current


def restore_floor_after_failure(
    kubectl: Kubectl,
    args: argparse.Namespace,
    *,
    origin: str,
    context: ssl.SSLContext,
    token: str,
    operation_id: str | None,
    model_revision: str,
    protocol: str,
    operation: str,
) -> ClusterObservation:
    """Release durable demand and prove the intended floor before returning."""

    release_error: BaseException | None = None
    if operation_id is not None:
        try:
            _release_operation(
                origin=origin,
                context=context,
                token=token,
                operation_id=operation_id,
                model_id=args.model_id,
                model_revision=model_revision,
                protocol=protocol,
                operation=operation,
                deadline=time.monotonic() + 300,
            )
        except BaseException as error:
            # Exact floor verification remains mandatory even if the release
            # API fails, but the caller must not treat cleanup as successful.
            release_error = error
    observation = wait_for_floor(
        kubectl,
        namespace=args.namespace,
        deployment=args.deployment,
        service=args.service,
        scaled_object=args.scaled_object,
        scaled_deployment=args.scaled_deployment,
        expected_floor=args.expected_floor,
        deadline=time.monotonic() + args.cooldown_seconds + 900,
    )
    if release_error is not None:
        raise BenchmarkError("operation_release_failed") from release_error
    return observation


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--access-bundle", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--request-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--raw-output", type=Path, required=True)
    parser.add_argument("--runtime-log-output", type=Path)
    parser.add_argument("--namespace", default="fs2-models")
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--deployment", required=True)
    parser.add_argument(
        "--scaled-deployment",
        help="Deployment targeted by the ScaledObject; defaults to --deployment",
    )
    parser.add_argument("--service", required=True)
    parser.add_argument("--scaled-object", required=True)
    parser.add_argument("--requested-level", choices=LEVELS, required=True)
    parser.add_argument("--ordinal", type=int, required=True)
    parser.add_argument("--expected-floor", type=int, choices=(0, 1), required=True)
    parser.add_argument(
        "--capacity-state",
        choices=(
            "prepared-node-zero-pod",
            "fresh-node-zero-pod",
            "preemption-replacement",
            "durable-cache-loss-fallback",
        ),
        required=True,
    )
    parser.add_argument(
        "--mechanism",
        choices=(
            "conventional",
            "regional-cache",
            "shared-cache",
            "local-snapshot",
            "ram-resident",
        ),
        required=True,
    )
    parser.add_argument(
        "--modality",
        choices=("text", "image", "video", "audio", "embedding", "other"),
        required=True,
    )
    parser.add_argument("--expected-text")
    parser.add_argument(
        "--recycle-ready-pod",
        action="store_true",
        help="measure a prepared-node process cold start while preserving a min=1 floor",
    )
    parser.add_argument(
        "--client-placement",
        choices=(
            "same-pod",
            "same-node",
            "in-cluster",
            "same-region",
            "cross-region",
            "external",
        ),
        default="external",
    )
    parser.add_argument(
        "--tls-mode",
        choices=("verified", "disposable-staging-insecure"),
        default="verified",
    )
    parser.add_argument("--timeout-seconds", type=int, default=2400)
    parser.add_argument("--cooldown-seconds", type=int, default=30)
    args = parser.parse_args(argv)
    args.scaled_deployment = args.scaled_deployment or args.deployment
    for name in (
        "context",
        "namespace",
        "model_id",
        "deployment",
        "scaled_deployment",
        "service",
        "scaled_object",
    ):
        if SAFE_NAME.fullmatch(getattr(args, name)) is None:
            parser.error(f"--{name.replace('_', '-')} is not Kubernetes-safe")
    dns_label = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?")
    if (
        dns_label.fullmatch(args.namespace) is None
        or len(args.namespace) > 63
        or dns_label.fullmatch(args.model_id) is None
        or len(args.model_id) > 63
        or len(args.context) > 253
        or any(
            len(getattr(args, name)) > 253
            for name in (
                "deployment",
                "scaled_deployment",
                "service",
                "scaled_object",
            )
        )
    ):
        parser.error("Kubernetes identity exceeds its schema bound")
    for path_name in (
        "kubeconfig",
        "access_bundle",
        "token_file",
        "request_file",
        "output",
        "raw_output",
        "runtime_log_output",
    ):
        path = getattr(args, path_name)
        if path is not None and not path.is_absolute():
            parser.error(f"--{path_name.replace('_', '-')} must be absolute")
    input_paths = (
        args.kubeconfig,
        args.access_bundle,
        args.token_file,
        args.request_file,
    )
    output_paths = tuple(item for item in (args.output, args.raw_output, args.runtime_log_output) if item is not None)
    resolved_inputs = {item.resolve(strict=False) for item in input_paths}
    resolved_outputs = [item.resolve(strict=False) for item in output_paths]
    if (
        len(resolved_outputs) != len(set(resolved_outputs))
        or any(item in resolved_inputs for item in resolved_outputs)
        or any(item.exists() for item in output_paths)
        or not 1 <= args.ordinal <= 10000
    ):
        parser.error("output paths must be new/distinct from inputs and ordinal must be 1..10000")
    if not 30 <= args.timeout_seconds <= 14400 or not 5 <= args.cooldown_seconds <= 7200:
        parser.error("timeout or cooldown is outside the bounded range")
    if args.recycle_ready_pod and (args.expected_floor != 1 or args.capacity_state != "prepared-node-zero-pod"):
        parser.error("--recycle-ready-pod requires --expected-floor 1 and --capacity-state prepared-node-zero-pod")
    if args.expected_floor == 1 and not args.recycle_ready_pod:
        parser.error("--expected-floor 1 requires --recycle-ready-pod")
    return args


def _storage_metadata(
    kubectl: Kubectl, namespace: str, observation: ClusterObservation
) -> tuple[str | None, str | None]:
    pod_spec = observation.deployment.get("spec", {}).get("template", {}).get("spec", {})
    claim_name = next(
        (
            volume.get("persistentVolumeClaim", {}).get("claimName")
            for volume in pod_spec.get("volumes", []) or []
            if isinstance(volume, dict)
            and isinstance(volume.get("persistentVolumeClaim"), dict)
            and isinstance(volume.get("persistentVolumeClaim", {}).get("claimName"), str)
        ),
        None,
    )
    if not isinstance(claim_name, str):
        return None, "ephemeral"
    pvc = kubectl.get("persistentvolumeclaim", name=claim_name, namespace=namespace)
    assert pvc is not None
    storage_class = pvc.get("spec", {}).get("storageClassName")
    access_modes = pvc.get("spec", {}).get("accessModes", [])
    if "ReadWriteMany" in access_modes:
        storage_mode = "rwx-filesystem"
    elif "ReadWriteOnce" in access_modes:
        storage_mode = "rwo-filesystem"
    else:
        storage_mode = "persistent-volume-claim"
    return storage_class, storage_mode


def _runtime_identity(kubectl: Kubectl, namespace: str, observation: ClusterObservation) -> dict[str, Any]:
    if observation.pod is None:
        return {}
    pod_name = observation.pod.get("metadata", {}).get("name")
    containers = observation.pod.get("spec", {}).get("containers", [])
    container_name = containers[0].get("name") if containers and isinstance(containers[0], dict) else None
    if not isinstance(pod_name, str) or not isinstance(container_name, str):
        return {}
    return kubectl.gpu_identity(namespace, pod_name, container_name)


def _finalize_runtime_compatibility(
    compatibility: dict[str, Any],
    observation: ClusterObservation,
    gpu_identity: dict[str, Any],
) -> dict[str, Any]:
    """Freeze GPU facts only after the measured runtime has a ready endpoint."""

    if observation.pod is None or observation.node is None:
        raise BenchmarkError("gpu_identity_unavailable")
    pod_spec = observation.pod.get("spec", {})
    expected_gpu_count = compatibility.get("gpu_count")
    if (
        isinstance(expected_gpu_count, bool)
        or not isinstance(expected_gpu_count, int)
        or expected_gpu_count < 1
        or _resource_gpu_count(pod_spec) != expected_gpu_count
    ):
        raise BenchmarkError("gpu_identity_count_mismatch")

    node_labels = observation.node.get("metadata", {}).get("labels", {})
    if not isinstance(node_labels, dict):
        raise BenchmarkError("gpu_identity_incomplete")
    for field, label in (
        ("accelerator_class", "accelerator.fs2.nebius/class"),
        ("pool_id", "accelerator.fs2.nebius/pool-id"),
        ("capacity_type", "capacity.fs2.nebius/type"),
    ):
        observed = node_labels.get(label)
        if observed is not None and observed != compatibility.get(field):
            raise BenchmarkError("gpu_identity_node_mismatch")

    product = gpu_identity.get("product") or node_labels.get("nvidia.com/gpu.product")
    compute_capability = gpu_identity.get("compute_capability")
    memory_bytes = gpu_identity.get("memory_bytes")
    driver_version = gpu_identity.get("driver_version") or node_labels.get("nebius.com/nvidia_driver_version")
    cuda_version = gpu_identity.get("cuda_version") or node_labels.get("nebius.com/cuda_version")
    if (
        not isinstance(product, str)
        or not product
        or not isinstance(compute_capability, str)
        or re.fullmatch(r"[0-9]+\.[0-9]+", compute_capability) is None
        or isinstance(memory_bytes, bool)
        or not isinstance(memory_bytes, int)
        or memory_bytes < 1
        or not isinstance(driver_version, str)
        or not driver_version
        or not isinstance(cuda_version, str)
        or not cuda_version
    ):
        raise BenchmarkError("gpu_identity_incomplete")

    finalized = dict(compatibility)
    finalized.update(
        {
            "gpu_product": product,
            "gpu_compute_capability": compute_capability,
            "gpu_memory_bytes": memory_bytes,
            "driver_version": driver_version,
            "cuda_version": cuda_version,
        }
    )
    return finalized


def _validate_attempt(attempt: dict[str, Any]) -> None:
    path = Path(__file__).with_name("aggregate_fast_start_benchmark.py")
    spec = importlib.util.spec_from_file_location("_fs2_fast_start_aggregate_validation", path)
    if spec is None or spec.loader is None:
        raise BenchmarkError("attempt_validator_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        module.validate_attempt(attempt)
    except BenchmarkError:
        raise
    except BaseException:
        raise BenchmarkError("attempt_validation_failed") from None


def _attempt_durations(clocks: dict[str, int | None], activation_ns: int) -> dict[str, float | None]:
    return {
        # The contract defines capacity wait from accepted activation, not
        # from the first asynchronously observed scheduler demand signal.
        "capacity_wait": _duration(activation_ns, clocks["capacity_available"]),
        "gpu_capacity_available_to_ready": _duration(clocks["capacity_available"], clocks["endpoint_ready"]),
        "activation_to_ready": _duration(activation_ns, clocks["endpoint_ready"]),
        "request_to_first_byte": _duration(clocks["request_started"], clocks["first_byte"]),
        "request_to_first_semantic_output": _duration(clocks["request_started"], clocks["first_semantic"]),
        "request_completion": _duration(clocks["request_started"], clocks["completed"]),
        "activation_to_first_semantic_output": _duration(activation_ns, clocks["first_semantic"]),
    }


def _failure_inference(modality: str) -> dict[str, Any]:
    return {
        "modality": modality,
        "first_output_kind": "none",
        "valid_output": False,
        "http_status": None,
        "request_count": 1,
        "warmup_count": 0,
        "concurrency": 1,
        "input_units": None,
        "output_units": None,
        "throughput": None,
    }


def _build_attempt(
    *,
    args: argparse.Namespace,
    attempt_id: str,
    status: str,
    failure_code: str | None,
    compatibility: dict[str, Any],
    timestamps: dict[str, str | None],
    clocks: dict[str, int | None],
    activation_ns: int,
    inference: dict[str, Any],
    raw_sha256: str,
    semantic_digest: str | None,
    runtime_log: bytes,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "attempt_id": attempt_id,
        "ordinal": args.ordinal,
        "observed_at": utc_now(),
        "status": status,
        "failure_code": failure_code,
        "requested_level": args.requested_level,
        "compatibility_tuple": compatibility,
        "compatibility_tuple_digest": sha256_json(compatibility),
        "timestamps": timestamps,
        "durations_seconds": _attempt_durations(clocks, activation_ns),
        "inference": inference,
        "artifacts": {
            "raw_attempt_sha256": raw_sha256,
            "semantic_output_sha256": semantic_digest,
            "runtime_log_sha256": sha256_bytes(runtime_log) if runtime_log else None,
            "gpu_metrics_sha256": None,
        },
    }


def _write_attempt(
    *,
    args: argparse.Namespace,
    token: str,
    attempt_id: str,
    status: str,
    failure_code: str | None,
    compatibility: dict[str, Any],
    timestamps: dict[str, str | None],
    clocks: dict[str, int | None],
    activation_ns: int,
    inference: dict[str, Any],
    semantic_digest: str | None,
    runtime_log: bytes,
    observations: list[dict[str, Any]],
    operation_id: str | None,
    cleanup: dict[str, Any],
    runtime_attribution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw_trace = {
        "schema": "fs2-serve.nebius.ai/fast-start-benchmark-raw-trace/v1",
        "attempt_id": attempt_id,
        "status": status,
        "failure_code": failure_code,
        "timestamps": timestamps,
        "observations": observations,
        "operation_id_sha256": (sha256_bytes(operation_id.encode()) if operation_id else None),
        "semantic_output_sha256": semantic_digest,
        "compatibility_tuple": compatibility,
        "runtime_attribution": runtime_attribution,
        "cleanup": cleanup,
    }
    raw_bytes = canonical_json(raw_trace) + b"\n"
    attempt = _build_attempt(
        args=args,
        attempt_id=attempt_id,
        status=status,
        failure_code=failure_code,
        compatibility=compatibility,
        timestamps=timestamps,
        clocks=clocks,
        activation_ns=activation_ns,
        inference=inference,
        raw_sha256=sha256_bytes(raw_bytes),
        semantic_digest=semantic_digest,
        runtime_log=runtime_log,
    )
    _validate_attempt(attempt)
    atomic_write(args.raw_output, raw_bytes, forbidden=token.encode())
    if args.runtime_log_output is not None and runtime_log:
        atomic_write(args.runtime_log_output, runtime_log, forbidden=token.encode())
    atomic_write(args.output, canonical_json(attempt) + b"\n", forbidden=token.encode())
    return attempt


def _observation_trace(observation: ClusterObservation) -> dict[str, Any]:
    return {
        "observed_at": observation.observed_at,
        "replicas": observation.replicas,
        "ready_replicas": observation.ready_replicas,
        "endpoints": observation.endpoints,
        "pod_count": observation.pod_count,
        "terminating_pods": observation.terminating_pods,
        "scaled_replicas": observation.scaled_replicas,
        "scaled_ready_replicas": observation.scaled_ready_replicas,
        "scaled_pod_count": observation.scaled_pod_count,
        "scaled_terminating_pods": observation.scaled_terminating_pods,
        "hpa_desired_replicas": observation.hpa_desired_replicas,
        "scaled_object_active": observation.scaled_object_active,
        "capacity_requested": observation.capacity_requested,
        "capacity_available": observation.capacity_available,
        "pod_present": observation.pod is not None,
        "node_present": observation.node is not None,
    }


def _canonical_operation_id(value: object) -> str:
    try:
        parsed = UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise BenchmarkError("operation_id_invalid") from None
    if parsed.version != 4:
        raise BenchmarkError("operation_id_invalid")
    return str(parsed)


def _assert_runtime_attribution(
    operation: dict[str, Any],
    observation: ClusterObservation,
    gpu_count: int,
    expected_pod_uid: str,
    *,
    model_deployment: dict[str, Any],
    namespace: str,
    model_id: str,
    expected_model_revision: str,
) -> str:
    bound_digest = _model_deployment_resource_binding(
        model_deployment,
        observation,
        namespace=namespace,
        model_id=model_id,
    )
    if expected_model_revision != f"dynamic:{bound_digest}":
        raise BenchmarkError("model_deployment_revision_changed")
    runtime = operation.get("runtime")
    pod_uid = observation.pod.get("metadata", {}).get("uid") if observation.pod is not None else None
    node_uid = observation.node.get("metadata", {}).get("uid") if observation.node is not None else None
    pod_node_name = observation.pod.get("spec", {}).get("nodeName") if observation.pod is not None else None
    node_name = observation.node.get("metadata", {}).get("name") if observation.node is not None else None
    pod_spec = observation.pod.get("spec", {}) if observation.pod is not None else {}
    exact_kubernetes_proof = (
        isinstance(runtime, dict)
        and isinstance(pod_uid, str)
        and pod_uid == expected_pod_uid
        and isinstance(node_uid, str)
        and isinstance(pod_node_name, str)
        and pod_node_name == node_name
        and observation.pod_count == 1
        and observation.terminating_pods == 0
        and observation.endpoints == 1
        and observation.ready_endpoint_pod_uids == (pod_uid,)
        and (
            observation.scaler_targets_primary
            or (observation.scaled_pod_count == 0 and observation.scaled_terminating_pods == 0)
        )
        and observation.pod.get("metadata", {}).get("deletionTimestamp") is None
        and _condition_true(observation.pod, "Ready")
        and _condition_true(observation.node, "Ready")
        and _resource_gpu_count(pod_spec) == gpu_count
        and gpu_count > 0
    )
    if not exact_kubernetes_proof:
        raise BenchmarkError("operation_runtime_identity_mismatch")

    if (
        runtime.get("pod_uid") == pod_uid
        and runtime.get("node_uid") == node_uid
        and runtime.get("gpu_count") == gpu_count
        and isinstance(runtime.get("gpu_uuids"), list)
        and len(runtime["gpu_uuids"]) == gpu_count
    ):
        return "operation-runtime-and-kubernetes-single-pod-proof"
    if runtime == {
        "pod_uid": None,
        "node_uid": None,
        "gpu_uuids": [],
        "gpu_count": 0,
        "preemptible": None,
    }:
        return "kubernetes-single-pod-proof-null-operation-runtime"
    raise BenchmarkError("operation_runtime_identity_mismatch")


def _runtime_attribution_observation(operation: dict[str, Any]) -> dict[str, Any]:
    runtime = operation.get("runtime")
    if not isinstance(runtime, dict):
        return {"authority": "rejected", "operation_runtime": None}
    pod_uid = runtime.get("pod_uid")
    node_uid = runtime.get("node_uid")
    gpu_uuids = runtime.get("gpu_uuids")
    return {
        "authority": "rejected",
        "operation_runtime": {
            "pod_uid_sha256": (sha256_bytes(pod_uid.encode()) if isinstance(pod_uid, str) else None),
            "node_uid_sha256": (sha256_bytes(node_uid.encode()) if isinstance(node_uid, str) else None),
            "gpu_uuid_sha256": (
                [sha256_bytes(value.encode()) for value in gpu_uuids if isinstance(value, str)]
                if isinstance(gpu_uuids, list)
                else None
            ),
            "gpu_count": runtime.get("gpu_count"),
            "preemptible": runtime.get("preemptible"),
        },
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    token = read_token(args.token_file)
    bundle = read_json(args.access_bundle, owner_only=True)
    request_document = read_json(args.request_file)
    endpoints = bundle.get("endpoints") if isinstance(bundle.get("endpoints"), dict) else {}
    endpoint = endpoints.get("inference_base_url")
    if not isinstance(endpoint, str):
        raise BenchmarkError("access_bundle_invalid")
    origin, context = validate_origin(endpoint, args.tls_mode)
    path, wire_payload, request_modality, interface_protocol = _wire_request(args.model_id, request_document)
    if request_modality == "text" and args.modality != "text":
        raise BenchmarkError("request_modality_mismatch")
    if request_modality == "native" and args.modality != "video":
        raise BenchmarkError("request_modality_mismatch")
    request_operation = request_document.get("operation")
    request_payload = request_document.get("payload")
    if not isinstance(request_operation, str) or not isinstance(request_payload, dict):
        raise BenchmarkError("request_contract_invalid")
    payload_digest = sha256_json(wire_payload)
    semantic_validator_digest = _semantic_validator_digest(
        model_id=args.model_id,
        modality=args.modality,
        expected_text=args.expected_text,
        request_payload=request_payload,
    )
    benchmark_client_digest = sha256_bytes(Path(__file__).resolve().read_bytes())
    kubectl = Kubectl(args.kubeconfig, args.context)
    model_deployment = kubectl.get(
        "modeldeployment.inference.fs2.nebius.ai",
        name=args.model_id,
        namespace=args.namespace,
    )
    assert model_deployment is not None
    if model_deployment.get("metadata", {}).get("name") != args.model_id:
        raise BenchmarkError("model_deployment_identity_mismatch")
    initial = observe_cluster(
        kubectl,
        args.namespace,
        args.deployment,
        args.service,
        args.scaled_object,
        args.scaled_deployment,
    )
    if not _floor_restored(initial, args.expected_floor):
        raise BenchmarkError("initial_floor_mismatch")
    if args.capacity_state == "prepared-node-zero-pod" and not initial.capacity_available:
        raise BenchmarkError("prepared_capacity_not_observed")
    if args.capacity_state == "fresh-node-zero-pod" and initial.capacity_available:
        raise BenchmarkError("fresh_capacity_state_not_observed")

    storage_class, storage_mode = _storage_metadata(kubectl, args.namespace, initial)
    compatibility = build_compatibility_tuple(
        source_commit=_source_commit(),
        bundle=bundle,
        context=args.context,
        namespace=args.namespace,
        model_id=args.model_id,
        model_deployment=model_deployment,
        observation=initial,
        capacity_state=args.capacity_state,
        mechanism=args.mechanism,
        payload_digest=payload_digest,
        client_placement=args.client_placement,
        interface_protocol=interface_protocol,
        endpoint_path=path,
        semantic_validator_digest=semantic_validator_digest,
        benchmark_client_digest=benchmark_client_digest,
        gpu_identity=_runtime_identity(kubectl, args.namespace, initial),
        storage_class=storage_class,
        storage_mode=storage_mode,
    )
    model_revision = compatibility.get("model_revision")
    if not isinstance(model_revision, str):
        raise BenchmarkError("model_revision_missing")

    # Refuse to mutate the live floor unless the exact frozen campaign tuple
    # can produce a schema- and semantics-valid attempt.
    probe_wall = utc_now()
    probe_ns = time.monotonic_ns()
    probe_timestamps: dict[str, str | None] = {
        "activation_accepted": probe_wall,
        "gpu_capacity_requested": (None if args.capacity_state == "prepared-node-zero-pod" else probe_wall),
        "gpu_capacity_available": (probe_wall if args.capacity_state == "prepared-node-zero-pod" else None),
        "endpoint_ready": None,
        "request_started": None,
        "first_response_byte": None,
        "first_semantic_output": None,
        "request_completed": None,
        "return_to_floor": None,
    }
    probe_clocks = {
        "request_started": None,
        "capacity_available": (probe_ns if args.capacity_state == "prepared-node-zero-pod" else None),
        "endpoint_ready": None,
        "first_byte": None,
        "first_semantic": None,
        "completed": None,
        "return_to_floor": None,
    }
    _validate_attempt(
        _build_attempt(
            args=args,
            attempt_id=f"{args.model_id}-{uuid4()}",
            status="FAIL",
            failure_code="preflight_probe",
            compatibility=compatibility,
            timestamps=probe_timestamps,
            clocks=probe_clocks,
            activation_ns=probe_ns,
            inference=_failure_inference(args.modality),
            raw_sha256="0" * 64,
            semantic_digest=None,
            runtime_log=b"",
        )
    )

    attempt_id = f"{args.model_id}-{uuid4()}"
    activation_wall = utc_now()
    activation_ns = time.monotonic_ns()
    deadline = time.monotonic() + args.timeout_seconds
    prepared = args.capacity_state == "prepared-node-zero-pod"
    timestamps: dict[str, str | None] = {
        "activation_accepted": activation_wall,
        "gpu_capacity_requested": None if prepared else activation_wall,
        "gpu_capacity_available": activation_wall if prepared else None,
        "endpoint_ready": None,
        "request_started": None if args.recycle_ready_pod else activation_wall,
        "first_response_byte": None,
        "first_semantic_output": None,
        "request_completed": None,
        "return_to_floor": None,
    }
    clocks: dict[str, int | None] = {
        "request_started": None if args.recycle_ready_pod else activation_ns,
        "capacity_available": activation_ns if prepared else None,
        "endpoint_ready": None,
        "first_byte": None,
        "first_semantic": None,
        "completed": None,
        "return_to_floor": None,
    }
    operation_id: str | None = None
    observations: list[dict[str, Any]] = []
    selected_observation = initial
    runtime_log = b""
    semantic_digest: str | None = None
    runtime_attribution: dict[str, Any] | None = None
    expected_runtime_pod_uid: str | None = None
    try:
        if args.recycle_ready_pod:
            if initial.pod is None:
                raise BenchmarkError("hot_floor_pod_missing")
            old_pod_name = initial.pod.get("metadata", {}).get("name")
            old_pod_uid = initial.pod.get("metadata", {}).get("uid")
            if not isinstance(old_pod_name, str) or not isinstance(old_pod_uid, str):
                raise BenchmarkError("hot_floor_pod_identity_invalid")
            kubectl.delete_pod(args.namespace, old_pod_name, old_pod_uid)
            replacement_ready = False
            while time.monotonic() < deadline:
                selected_observation = observe_cluster(
                    kubectl,
                    args.namespace,
                    args.deployment,
                    args.service,
                    args.scaled_object,
                    args.scaled_deployment,
                )
                observations.append(_observation_trace(selected_observation))
                replacement_uid = (
                    selected_observation.pod.get("metadata", {}).get("uid")
                    if selected_observation.pod is not None
                    else None
                )
                replacement_ready = (
                    isinstance(replacement_uid, str)
                    and replacement_uid != old_pod_uid
                    and replacement_uid in selected_observation.ready_endpoint_pod_uids
                    and selected_observation.capacity_available
                )
                if replacement_ready:
                    expected_runtime_pod_uid = replacement_uid
                    clocks["endpoint_ready"] = time.monotonic_ns()
                    timestamps["endpoint_ready"] = selected_observation.observed_at
                    break
                time.sleep(1)
            if not replacement_ready:
                raise BenchmarkError("replacement_endpoint_timeout")
            request_wall = utc_now()
            clocks["request_started"] = time.monotonic_ns()
            timestamps["request_started"] = request_wall

        remaining = max(1, int(deadline - time.monotonic()))
        admission = http_json(
            origin,
            context,
            token,
            "POST",
            path,
            payload=wire_payload,
            headers={
                "Idempotency-Key": f"fs2-fast-start-{uuid4()}",
                "x-fs2-wait-seconds": "30" if args.recycle_ready_pod else "0",
                "x-fs2-deadline-seconds": str(remaining),
            },
            timeout=min(45, remaining + 5),
        )
        admitted = decode_json(admission)
        header_operation_id = admission.headers.get("x-fs2-operation-id")
        body_operation_id = admitted.get("id") if admission.status == 202 else None
        if (
            header_operation_id is not None
            and body_operation_id is not None
            and header_operation_id != str(body_operation_id)
        ):
            raise BenchmarkError("operation_id_mismatch")
        operation_id = _canonical_operation_id(header_operation_id or body_operation_id)
        result_value: dict[str, Any] | None = None
        result_http: HttpResult | None = None
        if admission.status == 200:
            result_value = admitted
            result_http = admission
        elif admission.status != 202:
            raise BenchmarkError("operation_admission_invalid")

        final_operation: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            selected_observation = observe_cluster(
                kubectl,
                args.namespace,
                args.deployment,
                args.service,
                args.scaled_object,
                args.scaled_deployment,
            )
            now_ns = time.monotonic_ns()
            observations.append(_observation_trace(selected_observation))
            if selected_observation.capacity_available and clocks["capacity_available"] is None:
                clocks["capacity_available"] = now_ns
                timestamps["gpu_capacity_available"] = selected_observation.observed_at
            if (
                selected_observation.endpoints > 0
                and selected_observation.pod is not None
                and selected_observation.pod.get("metadata", {}).get("uid")
                in selected_observation.ready_endpoint_pod_uids
                and clocks["endpoint_ready"] is None
            ):
                clocks["endpoint_ready"] = now_ns
                timestamps["endpoint_ready"] = selected_observation.observed_at
                endpoint_pod_uid = selected_observation.pod.get("metadata", {}).get("uid")
                if not isinstance(endpoint_pod_uid, str):
                    raise BenchmarkError("endpoint_pod_identity_mismatch")
                expected_runtime_pod_uid = endpoint_pod_uid
            current = decode_json(
                http_json(
                    origin,
                    context,
                    token,
                    "GET",
                    f"/v1/operations/{operation_id}",
                    timeout=30,
                )
            )
            _operation_identity(
                current,
                operation_id=operation_id,
                model_id=args.model_id,
                model_revision=model_revision,
                protocol=interface_protocol,
                operation=request_operation,
            )
            if current.get("status") in TERMINAL_OPERATION_STATES:
                final_operation = current
                break
            time.sleep(1)
        if final_operation is None:
            raise BenchmarkError("operation_timeout")
        if (
            final_operation.get("status") != "succeeded"
            or final_operation.get("semantic_outcome") != "protocol_valid"
            or final_operation.get("result_available") is not True
        ):
            raise BenchmarkError("operation_failed")
        if clocks["capacity_available"] is None or clocks["endpoint_ready"] is None:
            raise BenchmarkError("runtime_readiness_not_observed")
        if not isinstance(expected_runtime_pod_uid, str):
            raise BenchmarkError("endpoint_pod_identity_mismatch")
        runtime_model_deployment = kubectl.get(
            "modeldeployment.inference.fs2.nebius.ai",
            name=args.model_id,
            namespace=args.namespace,
        )
        assert runtime_model_deployment is not None
        runtime_attribution = _runtime_attribution_observation(final_operation)
        runtime_attribution["authority"] = _assert_runtime_attribution(
            final_operation,
            selected_observation,
            compatibility["gpu_count"],
            expected_runtime_pod_uid,
            model_deployment=runtime_model_deployment,
            namespace=args.namespace,
            model_id=args.model_id,
            expected_model_revision=model_revision,
        )
        compatibility = _finalize_runtime_compatibility(
            compatibility,
            selected_observation,
            _runtime_identity(kubectl, args.namespace, selected_observation),
        )
        if result_value is None:
            result_http = http_json(
                origin,
                context,
                token,
                "GET",
                f"/v1/operations/{operation_id}/result",
                timeout=min(60, max(1, int(deadline - time.monotonic()))),
            )
            result_value = decode_json(result_http)
        assert result_http is not None and result_value is not None
        clocks["first_byte"] = result_http.first_byte_monotonic_ns
        clocks["completed"] = result_http.completed_monotonic_ns
        timestamps["first_response_byte"] = result_http.first_byte_at
        timestamps["request_completed"] = result_http.completed_at
        generation_seconds = _duration(clocks["endpoint_ready"], clocks["completed"]) or 0.0
        inference, semantic_digest = _validate_result(
            result_value,
            args.modality,
            args.expected_text,
            generation_seconds,
            model_id=args.model_id,
            request_payload=request_payload,
        )
        if not inference["valid_output"]:
            raise BenchmarkError("semantic_output_invalid")
        clocks["first_semantic"] = clocks["completed"]
        timestamps["first_semantic_output"] = timestamps["request_completed"]

        if selected_observation.pod is not None:
            pod_name = selected_observation.pod.get("metadata", {}).get("name")
            containers = selected_observation.pod.get("spec", {}).get("containers", [])
            container_name = containers[0].get("name") if containers and isinstance(containers[0], dict) else None
            if isinstance(pod_name, str) and isinstance(container_name, str):
                runtime_log = kubectl.logs(args.namespace, pod_name, container_name)

        _release_operation(
            origin=origin,
            context=context,
            token=token,
            operation_id=operation_id,
            model_id=args.model_id,
            model_revision=model_revision,
            protocol=interface_protocol,
            operation=request_operation,
            deadline=min(deadline, time.monotonic() + 300),
        )
        wait_for_floor(
            kubectl,
            namespace=args.namespace,
            deployment=args.deployment,
            service=args.service,
            scaled_object=args.scaled_object,
            scaled_deployment=args.scaled_deployment,
            expected_floor=args.expected_floor,
            deadline=min(deadline, time.monotonic() + args.cooldown_seconds + 900),
        )
        clocks["return_to_floor"] = time.monotonic_ns()
        timestamps["return_to_floor"] = utc_now()
        return _write_attempt(
            args=args,
            token=token,
            attempt_id=attempt_id,
            status="PASS",
            failure_code=None,
            compatibility=compatibility,
            timestamps=timestamps,
            clocks=clocks,
            activation_ns=activation_ns,
            inference=inference,
            semantic_digest=semantic_digest,
            runtime_log=runtime_log,
            observations=observations,
            operation_id=operation_id,
            cleanup={"status": "complete", "failure_code": None},
            runtime_attribution=runtime_attribution,
        )
    except BaseException as original_error:
        failure_code = original_error.code if isinstance(original_error, BenchmarkError) else "unexpected_failure"
        cleanup = {"status": "complete", "failure_code": None}
        try:
            restore_floor_after_failure(
                kubectl,
                args,
                origin=origin,
                context=context,
                token=token,
                operation_id=operation_id,
                model_revision=model_revision,
                protocol=interface_protocol,
                operation=request_operation,
            )
            clocks["return_to_floor"] = time.monotonic_ns()
            timestamps["return_to_floor"] = utc_now()
        except BaseException as cleanup_error:
            cleanup = {
                "status": "failed",
                "failure_code": (
                    cleanup_error.code if isinstance(cleanup_error, BenchmarkError) else "unexpected_cleanup_failure"
                ),
            }
        return _write_attempt(
            args=args,
            token=token,
            attempt_id=attempt_id,
            status="FAIL",
            failure_code=failure_code,
            compatibility=compatibility,
            timestamps=timestamps,
            clocks=clocks,
            activation_ns=activation_ns,
            inference=_failure_inference(args.modality),
            semantic_digest=None,
            runtime_log=runtime_log,
            observations=observations,
            operation_id=operation_id,
            cleanup=cleanup,
            runtime_attribution=runtime_attribution,
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        attempt = run(args)
    except BenchmarkError as error:
        print(json.dumps({"result": "FAIL", "failure_code": error.code}, sort_keys=True))
        return 1
    except BaseException:
        print(json.dumps({"result": "FAIL", "failure_code": "unexpected_failure"}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "result": attempt["status"],
                "attempt_id": attempt["attempt_id"],
                "output": str(args.output),
            },
            sort_keys=True,
        )
    )
    return 0 if attempt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
