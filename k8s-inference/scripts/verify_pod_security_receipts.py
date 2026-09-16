#!/usr/bin/env python3
"""Authenticate, live-verify, and consume one SAI-07 rollout transition.

The v3 signature covers the complete canonical bundle: authority, deployment
context, transition, expiry, and every live observation. The owner consumer
re-reads every named object and collection immediately before atomically
advancing a Kubernetes ConfigMap ledger with a resourceVersion compare-and-swap.
The downstream consumer may consume that exact phase authorization once. A
plan-time signature check is deliberately insufficient and is not supported.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlencode


BUNDLE_SCHEMA = "fs2-serve.nebius.ai/pod-security-rollout-receipt/v3"
LEDGER_SCHEMA = "fs2-serve.nebius.ai/pod-security-rollout-ledger/v1"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9](?:[-A-Za-z0-9._:@/]{0,251}[A-Za-z0-9])?$")
DNS_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
MAX_BUNDLE_AGE = dt.timedelta(minutes=15)
MAX_OBSERVATION_AGE = dt.timedelta(minutes=2)
MAX_CLOCK_SKEW = dt.timedelta(seconds=30)
MAX_OBJECTS = 2048
MAX_INVENTORIES = 256

PHASE_TRANSITIONS = {
    "migrate-reference-data": ("unmanaged", "exception-ready"),
    "cleanup-legacy-resources": ("exception-ready", "reference-data-ready"),
    "enforce": ("reference-data-ready", "baseline-ready"),
    "rollback-remove-enforcement": ("baseline-ready", "baseline-enforced"),
    "rollback-restore-host-agents": ("baseline-enforced", "enforcement-removed"),
    "rollback-remove-exception": ("enforcement-removed", "host-agents-restored"),
}

RESOURCE_PATHS = {
    ("v1", "ConfigMap"): "configmaps",
    ("v1", "Namespace"): "namespaces",
    ("v1", "PersistentVolumeClaim"): "persistentvolumeclaims",
    ("v1", "Pod"): "pods",
    ("v1", "ReplicationController"): "replicationcontrollers",
    ("v1", "ServiceAccount"): "serviceaccounts",
    ("apps/v1", "DaemonSet"): "daemonsets",
    ("apps/v1", "Deployment"): "deployments",
    ("apps/v1", "ReplicaSet"): "replicasets",
    ("apps/v1", "StatefulSet"): "statefulsets",
    ("batch/v1", "CronJob"): "cronjobs",
    ("batch/v1", "Job"): "jobs",
    ("networking.k8s.io/v1", "NetworkPolicy"): "networkpolicies",
    ("storage.k8s.io/v1", "StorageClass"): "storageclasses",
    ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy"): "validatingadmissionpolicies",
    ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicyBinding"): "validatingadmissionpolicybindings",
    ("jobset.x-k8s.io/v1alpha2", "JobSet"): "jobsets",
    ("inference.fs2.nebius.ai/v1alpha1", "ModelDeployment"): "modeldeployments",
    ("keda.sh/v1alpha1", "ScaledObject"): "scaledobjects",
}

CLUSTER_SCOPED = {
    ("v1", "Namespace"),
    ("storage.k8s.io/v1", "StorageClass"),
    ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy"),
    ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicyBinding"),
}

BASELINE_INVENTORY_KINDS = {
    ("v1", "Pod"),
    ("v1", "ReplicationController"),
    ("apps/v1", "Deployment"),
    ("apps/v1", "ReplicaSet"),
    ("apps/v1", "StatefulSet"),
    ("apps/v1", "DaemonSet"),
    ("batch/v1", "Job"),
    ("batch/v1", "CronJob"),
    ("jobset.x-k8s.io/v1alpha2", "JobSet"),
    ("inference.fs2.nebius.ai/v1alpha1", "ModelDeployment"),
    ("keda.sh/v1alpha1", "ScaledObject"),
}

BASELINE_CAPABILITIES = {
    "AUDIT_WRITE",
    "CHOWN",
    "DAC_OVERRIDE",
    "FOWNER",
    "FSETID",
    "KILL",
    "MKNOD",
    "NET_BIND_SERVICE",
    "SETFCAP",
    "SETGID",
    "SETPCAP",
    "SETUID",
    "SYS_CHROOT",
}

EXCEPTION_DAEMONSETS = {
    "fs2-dcgm-exporter",
    "fs2-node-exporter",
    "fs2-otel-node-agent",
    "fs2-serve-control-plane-gpu-observer",
}
EXCEPTION_SERVICE_ACCOUNTS = {
    "fs2-dcgm-exporter",
    "fs2-node-exporter",
    "fs2-otel-node",
    "fs2-serve-control-plane-gpu-observer",
}


class ReceiptError(ValueError):
    """The bundle, live state, or ledger cannot authorize this transition."""


class ConflictError(ReceiptError):
    """The durable ledger changed during compare-and-swap."""


class KubeClient(Protocol):
    def get_object(
        self, api_version: str, kind: str, namespace: str, name: str
    ) -> dict[str, Any] | None: ...

    def list_objects(
        self,
        api_version: str,
        kind: str,
        namespace: str,
        label_selector: str,
        field_selector: str,
    ) -> list[dict[str, Any]]: ...

    def replace_config_map(self, value: dict[str, Any]) -> dict[str, Any]: ...


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReceiptError(f"{label} must be an object")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReceiptError(f"{label} must be a non-empty string")
    return value


def _integer(value: Any, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ReceiptError(f"{label} must be an integer >= {minimum}")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ReceiptError(f"{label} fields differ from the canonical schema")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _instant(value: Any, label: str) -> dt.datetime:
    raw = _string(value, label)
    if not raw.endswith("Z"):
        raise ReceiptError(f"{label} must be an RFC3339 UTC instant")
    try:
        parsed = dt.datetime.fromisoformat(raw[:-1] + "+00:00")
    except ValueError as error:
        raise ReceiptError(f"{label} must be an RFC3339 UTC instant") from error
    if parsed.tzinfo != dt.timezone.utc:
        raise ReceiptError(f"{label} must use UTC")
    return parsed


def _read_regular_file(path: Path, label: str, limit: int = 1_048_576) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise ReceiptError(f"cannot safely open {label}: {error.strerror}") from error
    try:
        stat = os.fstat(descriptor)
        if not stat.st_mode & 0o100000:
            raise ReceiptError(f"{label} is not a regular file")
        if stat.st_size > limit:
            raise ReceiptError(f"{label} exceeds the size limit")
        payload = b""
        while len(payload) < stat.st_size:
            chunk = os.read(descriptor, min(65536, stat.st_size - len(payload)))
            if not chunk:
                break
            payload += chunk
    finally:
        os.close(descriptor)
    return payload


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return _object(json.loads(_read_regular_file(path, path.name)), "receipt bundle")
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReceiptError(f"{path.name} is not JSON") from error


def _verify_signature(
    bundle: dict[str, Any], public_key_bytes: bytes, expected_key_id: str
) -> None:
    signature = _object(bundle.get("signature"), "signature")
    _exact_keys(signature, {"algorithm", "key_id", "value"}, "signature")
    if signature["algorithm"] != "ed25519" or signature["key_id"] != expected_key_id:
        raise ReceiptError("bundle signature authority differs")
    try:
        signature_bytes = base64.b64decode(signature["value"], validate=True)
    except (binascii.Error, TypeError) as error:
        raise ReceiptError("bundle signature is not strict base64") from error
    if len(signature_bytes) != 64:
        raise ReceiptError("Ed25519 signature must be exactly 64 bytes")

    unsigned = dict(bundle)
    del unsigned["signature"]
    with tempfile.TemporaryDirectory(prefix="fs2-psa-receipt-") as directory:
        root = Path(directory)
        message_path = root / "bundle.json"
        signature_path = root / "signature.bin"
        public_key_path = root / "public-key.pem"
        message_path.write_bytes(_canonical(unsigned))
        signature_path.write_bytes(signature_bytes)
        # Verify the same descriptor-fenced bytes whose digest was checked.
        # Reopening the operator-supplied path here would permit a pathname or
        # symlink swap between digest and signature verification.
        public_key_path.write_bytes(public_key_bytes)
        os.chmod(message_path, 0o600)
        os.chmod(signature_path, 0o600)
        os.chmod(public_key_path, 0o600)
        completed = subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-verify",
                "-pubin",
                "-inkey",
                str(public_key_path),
                "-rawin",
                "-in",
                str(message_path),
                "-sigfile",
                str(signature_path),
            ],
            check=False,
            capture_output=True,
            timeout=10,
        )
    if completed.returncode != 0:
        raise ReceiptError("whole-bundle signature verification failed")


def _validate_context(context: dict[str, Any], expected: dict[str, Any]) -> None:
    _exact_keys(
        context,
        {
            "cluster_id",
            "run_id",
            "kube_system_uid",
            "deployment_nonce",
            "exception_admission_sha256",
            "psa_version",
            "scientific_namespaces",
            "pvc",
            "dataset",
            "storage",
            "baseline",
        },
        "context",
    )
    if context != expected:
        raise ReceiptError("signed context differs from the exact deployment context")
    for field in ("cluster_id", "run_id", "kube_system_uid", "deployment_nonce"):
        if not IDENTIFIER_RE.fullmatch(_string(context[field], f"context.{field}")):
            raise ReceiptError(f"context.{field} is malformed")
    if not SHA256_RE.fullmatch(
        _string(context["exception_admission_sha256"], "context.exception_admission_sha256")
    ):
        raise ReceiptError("context.exception_admission_sha256 is malformed")
    if not re.fullmatch(r"v1\.[0-9]{1,2}", _string(context["psa_version"], "context.psa_version")):
        raise ReceiptError("context.psa_version must pin one Kubernetes minor")
    namespaces = context["scientific_namespaces"]
    if (
        not isinstance(namespaces, list)
        or not namespaces
        or namespaces != sorted(set(namespaces))
        or not all(isinstance(item, str) and DNS_RE.fullmatch(item) for item in namespaces)
    ):
        raise ReceiptError("scientific namespace inventory must be non-empty, sorted, and unique")

    pvc = _object(context["pvc"], "context.pvc")
    _exact_keys(pvc, {"namespace", "name", "uid", "storage_class"}, "context.pvc")
    if pvc["namespace"] != "fs2-reference-data" or pvc["name"] != "fs2-reference-data-rwx":
        raise ReceiptError("receipt must bind the canonical reference-data claim")
    if not IDENTIFIER_RE.fullmatch(_string(pvc["uid"], "context.pvc.uid")):
        raise ReceiptError("context.pvc.uid is malformed")
    if pvc["storage_class"] != "fs2-reference-data-retained-sc":
        raise ReceiptError("receipt must bind the dedicated retained StorageClass")

    dataset = _object(context["dataset"], "context.dataset")
    _exact_keys(dataset, {"id", "revision", "tree_sha256"}, "context.dataset")
    _string(dataset["id"], "context.dataset.id")
    _string(dataset["revision"], "context.dataset.revision")
    if not SHA256_RE.fullmatch(_string(dataset["tree_sha256"], "context.dataset.tree_sha256")):
        raise ReceiptError("context.dataset.tree_sha256 is malformed")

    storage = _object(context["storage"], "context.storage")
    _exact_keys(
        storage,
        {"filesystem_id", "capacity_gib", "claim_size_gib", "forbid_deletion", "retention_mode"},
        "context.storage",
    )
    _string(storage["filesystem_id"], "context.storage.filesystem_id")
    capacity = _integer(storage["capacity_gib"], "context.storage.capacity_gib", 1611)
    claim_size = _integer(storage["claim_size_gib"], "context.storage.claim_size_gib", 1611)
    if claim_size > capacity:
        raise ReceiptError("claim size exceeds retained filesystem capacity")
    if storage["forbid_deletion"] is not True or storage["retention_mode"] != "retain":
        raise ReceiptError("reference-data storage is not durably retained")

    baseline = _object(context["baseline"], "context.baseline")
    _exact_keys(
        baseline,
        {
            "artifact_sha256",
            "inventory_sha256",
            "reference_host_paths",
            "baseline_incompatible_objects",
            "restricted_incompatible_objects",
        },
        "context.baseline",
    )
    for field in ("artifact_sha256", "inventory_sha256"):
        if not SHA256_RE.fullmatch(_string(baseline[field], f"context.baseline.{field}")):
            raise ReceiptError(f"context.baseline.{field} is malformed")
    for field in (
        "reference_host_paths",
        "baseline_incompatible_objects",
        "restricted_incompatible_objects",
    ):
        _integer(baseline[field], f"context.baseline.{field}")


def _validate_baseline_artifact(payload: bytes, context: dict[str, Any]) -> None:
    baseline = context["baseline"]
    if _sha256(payload) != baseline["artifact_sha256"]:
        raise ReceiptError("baseline artifact bytes differ from the signed context")
    try:
        artifact = _object(json.loads(payload), "baseline artifact")
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReceiptError("baseline artifact is not JSON") from error
    expected_fields = {
        "schema",
        "captured_at",
        "cluster",
        "scientific_namespaces",
        "inspected_namespaces",
        "collections",
        "objects",
        "reference_host_paths",
        "baseline_incompatible_objects",
        "restricted_incompatible_objects",
        "unauthorized_exception_objects",
        "inventory_sha256",
    }
    _exact_keys(artifact, expected_fields, "baseline artifact")
    if artifact["schema"] != "fs2-serve.nebius.ai/sai07-baseline-inventory/v2":
        raise ReceiptError("baseline artifact schema is unsupported")
    unsigned = dict(artifact)
    self_digest = unsigned.pop("inventory_sha256")
    if self_digest != _sha256(_canonical(unsigned)):
        raise ReceiptError("baseline artifact self-digest differs")
    if (
        self_digest != baseline["inventory_sha256"]
        or artifact["reference_host_paths"] != baseline["reference_host_paths"]
        or artifact["baseline_incompatible_objects"] != baseline["baseline_incompatible_objects"]
        or artifact["restricted_incompatible_objects"] != baseline["restricted_incompatible_objects"]
        or artifact["scientific_namespaces"] != context["scientific_namespaces"]
        or artifact.get("cluster", {}).get("kube_system_uid") != context["kube_system_uid"]
    ):
        raise ReceiptError("baseline artifact inventory differs from the signed context")


def _validate_cleanup_result(payload: bytes, assertions: dict[str, Any], context: dict[str, Any]) -> None:
    if _sha256(payload) != assertions["cleanup_result_sha256"]:
        raise ReceiptError("cleanup result bytes differ from the signed assertion")
    try:
        result = _object(json.loads(payload), "cleanup result")
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReceiptError("cleanup result is not JSON") from error
    required = {
        "schema",
        "mode",
        "manifest_sha256",
        "baseline_artifact_sha256",
        "prior_inventory_sha256",
        "cluster_id",
        "run_id",
        "kube_system_uid",
        "checked_objects",
        "removed_objects",
        "result_sha256",
    }
    _exact_keys(result, required, "cleanup result")
    unsigned = dict(result)
    result_self_digest = unsigned.pop("result_sha256")
    if (
        result["schema"] != "fs2-serve.nebius.ai/sai07-legacy-cleanup-result/v2"
        or result["mode"] != "execute"
        or result_self_digest != _sha256(_canonical(unsigned))
        or result["manifest_sha256"] != assertions["cleanup_manifest_sha256"]
        or result["baseline_artifact_sha256"] != context["baseline"]["artifact_sha256"]
        or result["cluster_id"] != context["cluster_id"]
        or result["run_id"] != context["run_id"]
        or result["kube_system_uid"] != context["kube_system_uid"]
        or result["checked_objects"] != assertions["removed_objects"]
        or result["removed_objects"] != assertions["removed_objects"]
    ):
        raise ReceiptError("cleanup result does not bind the signed manifest, context, and removed objects")


def _resource_path(api_version: str, kind: str, namespace: str, name: str = "") -> str:
    identity = (api_version, kind)
    if identity not in RESOURCE_PATHS:
        raise ReceiptError(f"live observation kind {api_version}/{kind} is not allowed")
    resource = RESOURCE_PATHS[identity]
    if "/" in api_version:
        group, version = api_version.split("/", 1)
        prefix = f"/apis/{quote(group, safe='')}/{quote(version, safe='')}"
    else:
        prefix = f"/api/{quote(api_version, safe='')}"
    if identity in CLUSTER_SCOPED:
        if namespace:
            raise ReceiptError(f"cluster-scoped {kind} must not name a namespace")
        path = f"{prefix}/{resource}"
    else:
        if not DNS_RE.fullmatch(namespace):
            raise ReceiptError(f"namespaced {kind} has an invalid namespace")
        path = f"{prefix}/namespaces/{quote(namespace, safe='')}/{resource}"
    if name:
        path += f"/{quote(name, safe='')}"
    return path


def _metadata_projection(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace", ""),
        "uid": metadata.get("uid"),
        "resourceVersion": metadata.get("resourceVersion"),
        "generation": metadata.get("generation"),
        "labels": metadata.get("labels", {}),
        "annotations": metadata.get("annotations", {}),
        "ownerReferences": metadata.get("ownerReferences", []),
        "deletionTimestamp": metadata.get("deletionTimestamp"),
    }


def _live_projection(value: dict[str, Any]) -> dict[str, Any]:
    metadata = _object(value.get("metadata"), "live object metadata")
    projection: dict[str, Any] = {
        "apiVersion": value.get("apiVersion"),
        "kind": value.get("kind"),
        "metadata": _metadata_projection(metadata),
    }
    for key in ("spec", "status", "data"):
        if key in value:
            projection[key] = value[key]
    return projection


def _object_key(value: dict[str, Any]) -> str:
    return "/".join(
        (
            value["api_version"],
            value["kind"],
            value["namespace"] or "_cluster",
            value["name"],
        )
    )


def _validate_object_observation(raw: Any, label: str) -> dict[str, Any]:
    value = _object(raw, label)
    _exact_keys(
        value,
        {
            "api_version",
            "kind",
            "namespace",
            "name",
            "exists",
            "uid",
            "resource_version",
            "object_sha256",
        },
        label,
    )
    api_version = _string(value["api_version"], f"{label}.api_version")
    kind = _string(value["kind"], f"{label}.kind")
    namespace = value["namespace"]
    if not isinstance(namespace, str):
        raise ReceiptError(f"{label}.namespace must be a string")
    name = _string(value["name"], f"{label}.name")
    if not DNS_RE.fullmatch(name):
        raise ReceiptError(f"{label}.name is malformed")
    _resource_path(api_version, kind, namespace, name)
    if value["exists"] is True:
        for field in ("uid", "resource_version"):
            if not IDENTIFIER_RE.fullmatch(_string(value[field], f"{label}.{field}")):
                raise ReceiptError(f"{label}.{field} is malformed")
        if not SHA256_RE.fullmatch(_string(value["object_sha256"], f"{label}.object_sha256")):
            raise ReceiptError(f"{label}.object_sha256 is malformed")
    elif value["exists"] is False:
        if any(value[field] is not None for field in ("uid", "resource_version", "object_sha256")):
            raise ReceiptError(f"{label} absent-object identity fields must be null")
    else:
        raise ReceiptError(f"{label}.exists must be a boolean")
    return value


def _validate_inventory(raw: Any, label: str) -> dict[str, Any]:
    value = _object(raw, label)
    _exact_keys(
        value,
        {
            "api_version",
            "kind",
            "namespace",
            "label_selector",
            "field_selector",
            "item_count",
            "items_sha256",
        },
        label,
    )
    api_version = _string(value["api_version"], f"{label}.api_version")
    kind = _string(value["kind"], f"{label}.kind")
    namespace = value["namespace"]
    if not isinstance(namespace, str):
        raise ReceiptError(f"{label}.namespace must be a string")
    _resource_path(api_version, kind, namespace)
    for field in ("label_selector", "field_selector"):
        if not isinstance(value[field], str) or len(value[field]) > 1024:
            raise ReceiptError(f"{label}.{field} is malformed")
    _integer(value["item_count"], f"{label}.item_count")
    if not SHA256_RE.fullmatch(_string(value["items_sha256"], f"{label}.items_sha256")):
        raise ReceiptError(f"{label}.items_sha256 is malformed")
    return value


def _validate_observation_contract(
    state: str,
    observations: dict[str, Any],
    context: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    _exact_keys(observations, {"observed_at", "objects", "inventories", "assertions"}, "observations")
    objects_raw = observations["objects"]
    inventories_raw = observations["inventories"]
    if not isinstance(objects_raw, list) or not 1 <= len(objects_raw) <= MAX_OBJECTS:
        raise ReceiptError("observations.objects must be a non-empty bounded list")
    if not isinstance(inventories_raw, list) or len(inventories_raw) > MAX_INVENTORIES:
        raise ReceiptError("observations.inventories must be a bounded list")
    objects = [
        _validate_object_observation(value, f"observations.objects[{index}]")
        for index, value in enumerate(objects_raw)
    ]
    inventories = [
        _validate_inventory(value, f"observations.inventories[{index}]")
        for index, value in enumerate(inventories_raw)
    ]
    object_keys = [_object_key(value) for value in objects]
    if object_keys != sorted(set(object_keys)):
        raise ReceiptError("observed objects must be sorted and unique")
    inventory_keys = [
        (
            value["api_version"],
            value["kind"],
            value["namespace"],
            value["label_selector"],
            value["field_selector"],
        )
        for value in inventories
    ]
    if inventory_keys != sorted(set(inventory_keys)):
        raise ReceiptError("observed inventories must be sorted and unique")

    assertions = _object(observations["assertions"], "observations.assertions")
    present = {
        (value["api_version"], value["kind"], value["namespace"], value["name"])
        for value in objects
        if value["exists"]
    }
    absent = {
        (value["api_version"], value["kind"], value["namespace"], value["name"])
        for value in objects
        if not value["exists"]
    }
    if state == "exception-ready":
        _exact_keys(assertions, {"legacy_agents_ready", "exception_agents_ready"}, "exception-ready assertions")
        if assertions != {"legacy_agents_ready": True, "exception_agents_ready": True}:
            raise ReceiptError("exception-ready assertions must both pass")
        required = {
            ("v1", "Namespace", "", "fs2-node-observability"),
            *{
                ("apps/v1", "DaemonSet", "fs2-node-observability", name)
                for name in EXCEPTION_DAEMONSETS
            },
            *{
                ("v1", "ServiceAccount", "fs2-node-observability", name)
                for name in EXCEPTION_SERVICE_ACCOUNTS
            },
            ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy", "", "fs2-node-observability-pods"),
            ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicyBinding", "", "fs2-node-observability-pods"),
            ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy", "", "fs2-node-observability-daemonsets"),
            ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicyBinding", "", "fs2-node-observability-daemonsets"),
        }
        if not required.issubset(present):
            raise ReceiptError("exception-ready receipt omits a required live object")
    elif state == "reference-data-ready":
        _exact_keys(assertions, {"read_probe_passed", "source_tree_sha256", "target_tree_sha256"}, "reference-data-ready assertions")
        tree = context["dataset"]["tree_sha256"]
        if assertions != {
            "read_probe_passed": True,
            "source_tree_sha256": tree,
            "target_tree_sha256": tree,
        }:
            raise ReceiptError("reference-data-ready assertions do not bind the exact dataset")
        required = {
            ("v1", "PersistentVolumeClaim", "fs2-reference-data", "fs2-reference-data-rwx"),
            ("apps/v1", "Deployment", "fs2-reference-data", "fs2-reference-data-status"),
            ("storage.k8s.io/v1", "StorageClass", "", "fs2-reference-data-retained-sc"),
        }
        if not required.issubset(present):
            raise ReceiptError("reference-data-ready receipt omits PVC, status, or StorageClass state")
    elif state == "baseline-ready":
        _exact_keys(
            assertions,
            {
                "baseline_artifact_sha256",
                "baseline_inventory_sha256",
                "baseline_reference_host_paths",
                "baseline_incompatible_objects",
                "baseline_restricted_incompatible_objects",
                "cleanup_manifest_sha256",
                "cleanup_result_sha256",
                "removed_objects",
                "live_inventory_sha256",
                "live_reference_host_paths",
                "live_baseline_incompatible_objects",
                "live_restricted_incompatible_objects",
            },
            "baseline-ready assertions",
        )
        baseline = context["baseline"]
        if (
            assertions["baseline_artifact_sha256"] != baseline["artifact_sha256"]
            or assertions["baseline_inventory_sha256"] != baseline["inventory_sha256"]
            or assertions["baseline_reference_host_paths"] != baseline["reference_host_paths"]
            or assertions["baseline_incompatible_objects"] != baseline["baseline_incompatible_objects"]
            or assertions["baseline_restricted_incompatible_objects"]
            != baseline["restricted_incompatible_objects"]
        ):
            raise ReceiptError("baseline-ready assertions do not bind the frozen baseline artifact")
        for field in (
            "cleanup_manifest_sha256",
            "cleanup_result_sha256",
            "live_inventory_sha256",
        ):
            if not SHA256_RE.fullmatch(_string(assertions[field], f"{field} digest")):
                raise ReceiptError(f"{field} is malformed")
        for field in (
            "live_reference_host_paths",
            "live_baseline_incompatible_objects",
            "live_restricted_incompatible_objects",
        ):
            if assertions[field] != 0:
                raise ReceiptError("baseline-ready live inventory is not clean")
        removed = assertions["removed_objects"]
        if not isinstance(removed, list) or not all(isinstance(item, dict) for item in removed):
            raise ReceiptError("removed_objects must be a list of exact object identities")
        removed_keys: list[str] = []
        for index, item in enumerate(removed):
            _exact_keys(
                item,
                {"api_version", "kind", "namespace", "name", "uid", "resource_version", "object_sha256"},
                f"removed_objects[{index}]",
            )
            for field in ("api_version", "kind", "namespace", "name", "uid", "resource_version"):
                _string(item[field], f"removed_objects[{index}].{field}")
            if not SHA256_RE.fullmatch(item["object_sha256"]):
                raise ReceiptError("removed object hash is malformed")
            removed_keys.append("/".join((item["api_version"], item["kind"], item["namespace"], item["name"])))
        if removed_keys != sorted(set(removed_keys)):
            raise ReceiptError("removed_objects must be sorted and unique")
        absent_keys = {
            "/".join((item[0], item[1], item[2], item[3]))
            for item in absent
        }
        if set(removed_keys) != absent_keys:
            raise ReceiptError("baseline-ready absent objects differ from the signed cleanup result")
    elif state == "baseline-enforced":
        _exact_keys(assertions, {"privileged_probe_rejected", "positive_smoke_passed"}, "baseline-enforced assertions")
        if not all(assertions.values()):
            raise ReceiptError("baseline enforcement probes did not pass")
    elif state in {"enforcement-removed", "host-agents-restored"}:
        _exact_keys(assertions, {"host_agents_ready"}, f"{state} assertions")
        if assertions["host_agents_ready"] is not True:
            raise ReceiptError(f"{state} host-agent assertion did not pass")
    else:
        raise ReceiptError(f"unsupported observed state {state}")

    if state in {"baseline-ready", "baseline-enforced"}:
        target_namespaces = {
            "fs2-data",
            "fs2-models",
            "fs2-observability",
            "fs2-reference-data",
            "fs2-system",
            *context["scientific_namespaces"],
        }
        namespace_objects = {
            value["name"]
            for value in objects
            if value["exists"] and value["api_version"] == "v1" and value["kind"] == "Namespace"
        }
        if namespace_objects != target_namespaces:
            raise ReceiptError("baseline receipt does not cover the exact frozen namespace inventory")
        coverage = {
            (value["namespace"], value["api_version"], value["kind"])
            for value in inventories
            if not value["label_selector"] and not value["field_selector"]
        }
        required_coverage = {
            (namespace, api_version, kind)
            for namespace in target_namespaces
            for api_version, kind in BASELINE_INVENTORY_KINDS
        }
        if not required_coverage.issubset(coverage):
            raise ReceiptError("baseline receipt omits a complete live workload inventory")
    return objects, inventories, assertions


def _pod_templates(value: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return every Pod spec and its template annotations from a live object."""

    kind = value.get("kind")
    metadata = value.get("metadata", {})
    spec = value.get("spec", {})
    if not isinstance(spec, dict):
        return []
    if kind == "Pod":
        return [(spec, metadata.get("annotations", {}) if isinstance(metadata, dict) else {})]
    template: Any = None
    if kind in {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "ReplicationController", "Job"}:
        template = spec.get("template")
    elif kind == "CronJob":
        template = ((spec.get("jobTemplate") or {}).get("spec") or {}).get("template")
    if isinstance(template, dict) and isinstance(template.get("spec"), dict):
        template_metadata = template.get("metadata", {})
        return [(template["spec"], template_metadata.get("annotations", {}) if isinstance(template_metadata, dict) else {})]
    if kind == "JobSet":
        results: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for replicated_job in spec.get("replicatedJobs", []) or []:
            if not isinstance(replicated_job, dict):
                continue
            job_template = replicated_job.get("template", {})
            pod_template = (job_template.get("spec", {}) if isinstance(job_template, dict) else {}).get("template", {})
            if isinstance(pod_template, dict) and isinstance(pod_template.get("spec"), dict):
                template_metadata = pod_template.get("metadata", {})
                results.append(
                    (
                        pod_template["spec"],
                        template_metadata.get("annotations", {}) if isinstance(template_metadata, dict) else {},
                    )
                )
        return results
    return []


def _pod_security_findings(spec: dict[str, Any], annotations: dict[str, Any]) -> tuple[bool, bool, int]:
    baseline: set[str] = set()
    restricted: set[str] = set()
    host_paths = 0
    for field in ("hostNetwork", "hostPID", "hostIPC"):
        if spec.get(field) is True:
            baseline.add(field)
    for volume in spec.get("volumes", []) or []:
        if isinstance(volume, dict) and "hostPath" in volume:
            baseline.add("hostPath")
            host_paths += 1
    pod_security = spec.get("securityContext", {}) or {}
    if not isinstance(pod_security, dict):
        pod_security = {}
    if pod_security.get("runAsUser") == 0 or pod_security.get("runAsNonRoot") is False:
        restricted.add("root")
    if pod_security.get("runAsNonRoot") is not True:
        restricted.add("runAsNonRoot")
    pod_seccomp = pod_security.get("seccompProfile", {}) or {}
    if not pod_seccomp:
        restricted.add("seccompProfile")
    elif isinstance(pod_seccomp, dict) and pod_seccomp.get("type") == "Unconfined":
        baseline.add("unconfinedSeccomp")
        restricted.add("unconfinedSeccomp")
    containers = [
        *(spec.get("initContainers", []) or []),
        *(spec.get("containers", []) or []),
        *(spec.get("ephemeralContainers", []) or []),
    ]
    for container in containers:
        if not isinstance(container, dict):
            continue
        security = container.get("securityContext", {}) or {}
        if not isinstance(security, dict):
            security = {}
        if security.get("privileged") is True:
            baseline.add("privileged")
            restricted.add("privileged")
        if security.get("runAsUser") == 0 or security.get("runAsNonRoot") is False:
            restricted.add("root")
        if security.get("runAsNonRoot") is not True and pod_security.get("runAsNonRoot") is not True:
            restricted.add("runAsNonRoot")
        if security.get("allowPrivilegeEscalation") is not False:
            restricted.add("allowPrivilegeEscalation")
        if security.get("procMount") not in (None, "Default"):
            baseline.add("procMount")
        seccomp = security.get("seccompProfile", {}) or {}
        if isinstance(seccomp, dict) and seccomp.get("type") == "Unconfined":
            baseline.add("unconfinedSeccomp")
            restricted.add("unconfinedSeccomp")
        if not seccomp and not pod_seccomp:
            restricted.add("seccompProfile")
        apparmor = security.get("appArmorProfile", {}) or {}
        if isinstance(apparmor, dict) and apparmor.get("type") == "Unconfined":
            baseline.add("unconfinedAppArmor")
            restricted.add("unconfinedAppArmor")
        capabilities = security.get("capabilities", {}) or {}
        added = set(capabilities.get("add", []) or []) if isinstance(capabilities, dict) else set()
        dropped = set(capabilities.get("drop", []) or []) if isinstance(capabilities, dict) else set()
        if not added.issubset(BASELINE_CAPABILITIES):
            baseline.add("capabilities")
        if added or "ALL" not in dropped:
            restricted.add("capabilities")
        for port in container.get("ports", []) or []:
            if isinstance(port, dict) and int(port.get("hostPort", 0) or 0):
                baseline.add("hostPort")
    if any(
        key.startswith("container.apparmor.security.beta.kubernetes.io/") and value == "unconfined"
        for key, value in annotations.items()
    ):
        baseline.add("unconfinedAppArmor")
        restricted.add("unconfinedAppArmor")
    return bool(baseline), bool(restricted), host_paths


def _validate_live_observations(
    client: KubeClient,
    objects: list[dict[str, Any]],
    inventories: list[dict[str, Any]],
) -> dict[str, Any]:
    live_inventory_projections: list[dict[str, Any]] = []
    baseline_incompatible_objects = 0
    restricted_incompatible_objects = 0
    reference_host_paths = 0
    for expected in objects:
        live = client.get_object(
            expected["api_version"],
            expected["kind"],
            expected["namespace"],
            expected["name"],
        )
        if not expected["exists"]:
            if live is not None:
                raise ReceiptError(f"expected absent object is present: {_object_key(expected)}")
            continue
        if live is None:
            raise ReceiptError(f"expected live object is absent: {_object_key(expected)}")
        metadata = _object(live.get("metadata"), "live object metadata")
        if (
            live.get("apiVersion") != expected["api_version"]
            or live.get("kind") != expected["kind"]
            or metadata.get("name") != expected["name"]
            or metadata.get("namespace", "") != expected["namespace"]
            or metadata.get("uid") != expected["uid"]
            or metadata.get("resourceVersion") != expected["resource_version"]
            or _sha256(_canonical(_live_projection(live))) != expected["object_sha256"]
        ):
            raise ReceiptError(f"live UID/resourceVersion/object hash differs: {_object_key(expected)}")

    for expected in inventories:
        items = client.list_objects(
            expected["api_version"],
            expected["kind"],
            expected["namespace"],
            expected["label_selector"],
            expected["field_selector"],
        )
        projections = sorted(
            (_live_projection(value) for value in items),
            key=lambda value: (
                value["metadata"]["namespace"],
                value["metadata"]["name"],
                value["metadata"]["uid"],
            ),
        )
        if len(projections) != expected["item_count"] or _sha256(_canonical(projections)) != expected["items_sha256"]:
            raise ReceiptError(
                f"live inventory differs: {expected['api_version']}/{expected['kind']}/{expected['namespace']}"
            )
        live_inventory_projections.extend(projections)
        for item in items:
            template_findings = [_pod_security_findings(spec, annotations) for spec, annotations in _pod_templates(item)]
            if any(result[0] for result in template_findings):
                baseline_incompatible_objects += 1
            if any(result[1] for result in template_findings):
                restricted_incompatible_objects += 1
            reference_host_paths += sum(result[2] for result in template_findings)
    live_inventory_projections.sort(
        key=lambda value: (
            str(value.get("apiVersion", "")),
            str(value.get("kind", "")),
            str(value.get("metadata", {}).get("namespace", "")),
            str(value.get("metadata", {}).get("name", "")),
            str(value.get("metadata", {}).get("uid", "")),
        )
    )
    return {
        "live_inventory_sha256": _sha256(_canonical(live_inventory_projections)),
        "live_reference_host_paths": reference_host_paths,
        "live_baseline_incompatible_objects": baseline_incompatible_objects,
        "live_restricted_incompatible_objects": restricted_incompatible_objects,
    }


def _validate_bundle(
    bundle: dict[str, Any],
    query: dict[str, Any],
    public_key_bytes: bytes,
    now: dt.datetime,
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
    str,
]:
    _exact_keys(bundle, {"schema", "authority", "context", "transition", "observations", "signature"}, "receipt bundle")
    if bundle["schema"] != BUNDLE_SCHEMA:
        raise ReceiptError("receipt bundle schema is unsupported")
    authority = _object(bundle["authority"], "authority")
    _exact_keys(authority, {"algorithm", "key_id", "signer_identity", "public_key_sha256"}, "authority")
    if authority != {
        "algorithm": "ed25519",
        "key_id": query["expected_key_id"],
        "signer_identity": query["expected_signer_identity"],
        "public_key_sha256": query["public_key_sha256"],
    }:
        raise ReceiptError("signed authority differs from the reviewed signer identity")
    _verify_signature(bundle, public_key_bytes, query["expected_key_id"])

    context = _object(bundle["context"], "context")
    _validate_context(context, query["expected_context"])
    transition = _object(bundle["transition"], "transition")
    _exact_keys(
        transition,
        {
            "receipt_id",
            "sequence",
            "from_state",
            "to_state",
            "authorizes_phase",
            "nonce",
            "prior_ledger_sha256",
            "issued_at",
            "expires_at",
        },
        "transition",
    )
    phase = query["expected_phase"]
    expected_from, expected_to = PHASE_TRANSITIONS[phase]
    if (
        transition["authorizes_phase"] != phase
        or transition["from_state"] != expected_from
        or transition["to_state"] != expected_to
    ):
        raise ReceiptError("signed transition does not match the exact phase edge")
    for field in ("receipt_id", "nonce"):
        if not IDENTIFIER_RE.fullmatch(_string(transition[field], f"transition.{field}")):
            raise ReceiptError(f"transition.{field} is malformed")
    _integer(transition["sequence"], "transition.sequence", 1)
    if not SHA256_RE.fullmatch(_string(transition["prior_ledger_sha256"], "transition.prior_ledger_sha256")):
        raise ReceiptError("transition.prior_ledger_sha256 is malformed")
    issued = _instant(transition["issued_at"], "transition.issued_at")
    expires = _instant(transition["expires_at"], "transition.expires_at")
    observed = _instant(bundle["observations"].get("observed_at"), "observations.observed_at")
    if issued > now + MAX_CLOCK_SKEW or expires <= now or expires - issued > MAX_BUNDLE_AGE:
        raise ReceiptError("bundle is future-dated, expired, or valid for more than 15 minutes")
    if observed > now + MAX_CLOCK_SKEW or now - observed > MAX_OBSERVATION_AGE:
        raise ReceiptError("live observations are future-dated or older than two minutes")
    if observed < issued - MAX_CLOCK_SKEW or observed > expires:
        raise ReceiptError("live observation time is outside the signed transition window")
    objects, inventories, assertions = _validate_observation_contract(
        expected_to,
        _object(bundle["observations"], "observations"),
        context,
    )
    return transition, objects, inventories, assertions, _sha256(_canonical(bundle))


def _initial_ledger(query: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": LEDGER_SCHEMA,
        "context_sha256": _sha256(_canonical(query["expected_context"])),
        "authority": {
            "key_id": query["expected_key_id"],
            "signer_identity": query["expected_signer_identity"],
            "public_key_sha256": query["public_key_sha256"],
        },
        "sequence": 0,
        "state": "unmanaged",
        "last_bundle_sha256": None,
        "last_receipt_id": None,
        "last_nonce": None,
        "authorization": None,
    }


def _ledger_from_config_map(value: dict[str, Any], query: dict[str, Any]) -> dict[str, Any]:
    metadata = _object(value.get("metadata"), "ledger ConfigMap metadata")
    if (
        value.get("apiVersion") != "v1"
        or value.get("kind") != "ConfigMap"
        or metadata.get("namespace") != query["ledger_namespace"]
        or metadata.get("name") != query["ledger_name"]
        or not metadata.get("resourceVersion")
    ):
        raise ReceiptError("durable ledger ConfigMap identity is invalid")
    data = _object(value.get("data"), "ledger ConfigMap data")
    if set(data) != {"ledger.json"}:
        raise ReceiptError("durable ledger ConfigMap has unexpected keys")
    try:
        ledger = _object(json.loads(data["ledger.json"]), "ledger")
    except (TypeError, json.JSONDecodeError) as error:
        raise ReceiptError("durable ledger JSON is invalid") from error
    _exact_keys(
        ledger,
        {
            "schema",
            "context_sha256",
            "authority",
            "sequence",
            "state",
            "last_bundle_sha256",
            "last_receipt_id",
            "last_nonce",
            "authorization",
        },
        "ledger",
    )
    initial = _initial_ledger(query)
    if (
        ledger["schema"] != LEDGER_SCHEMA
        or ledger["context_sha256"] != initial["context_sha256"]
        or ledger["authority"] != initial["authority"]
    ):
        raise ReceiptError("durable ledger context or authority differs")
    _integer(ledger["sequence"], "ledger.sequence")
    return ledger


def _replace_ledger(
    client: KubeClient,
    config_map: dict[str, Any],
    ledger: dict[str, Any],
) -> None:
    replacement = deepcopy(config_map)
    replacement["data"] = {"ledger.json": _canonical(ledger).decode("utf-8")}
    try:
        client.replace_config_map(replacement)
    except ConflictError:
        raise
    except ReceiptError:
        raise
    except Exception as error:  # pragma: no cover - defensive adapter boundary
        raise ReceiptError("durable ledger compare-and-swap failed") from error


def verify_and_consume(
    query: dict[str, Any], client: KubeClient, now: dt.datetime | None = None
) -> dict[str, str]:
    _exact_keys(
        query,
        {
            "mode",
            "receipt_path",
            "public_key_path",
            "public_key_sha256",
            "expected_key_id",
            "expected_signer_identity",
            "expected_context",
            "expected_phase",
            "baseline_artifact_path",
            "cleanup_result_path",
            "ledger_namespace",
            "ledger_name",
        },
        "query",
    )
    mode = query["mode"]
    if mode not in {"owner-transition", "downstream-authorization"}:
        raise ReceiptError("query.mode is unsupported")
    phase = _string(query["expected_phase"], "expected_phase")
    if phase not in PHASE_TRANSITIONS:
        raise ReceiptError("prepare has no consumable receipt; expected_phase is unsupported")
    for field in ("expected_key_id", "expected_signer_identity", "ledger_name", "ledger_namespace"):
        if not IDENTIFIER_RE.fullmatch(_string(query[field], field)):
            raise ReceiptError(f"{field} is malformed")
    key_digest = _string(query["public_key_sha256"], "public_key_sha256")
    if not SHA256_RE.fullmatch(key_digest):
        raise ReceiptError("public_key_sha256 is malformed")
    public_key = Path(_string(query["public_key_path"], "public_key_path"))
    key_bytes = _read_regular_file(public_key, "receipt public key", 65536)
    if _sha256(key_bytes) != key_digest:
        raise ReceiptError("receipt public key digest differs from the reviewed digest")
    receipt_path = Path(_string(query["receipt_path"], "receipt_path"))
    bundle = _load_json(receipt_path)
    current_time = now or dt.datetime.now(dt.timezone.utc)
    transition, objects, inventories, assertions, bundle_sha256 = _validate_bundle(
        bundle, query, key_bytes, current_time
    )
    baseline_artifact = Path(_string(query["baseline_artifact_path"], "baseline_artifact_path"))
    _validate_baseline_artifact(
        _read_regular_file(baseline_artifact, "baseline artifact", 128 * 1024 * 1024),
        _object(bundle["context"], "context"),
    )
    if transition["to_state"] == "baseline-ready":
        cleanup_path = Path(_string(query["cleanup_result_path"], "cleanup_result_path"))
        _validate_cleanup_result(
            _read_regular_file(cleanup_path, "cleanup result", 8 * 1024 * 1024),
            assertions,
            _object(bundle["context"], "context"),
        )
    elif query["cleanup_result_path"] is not None:
        raise ReceiptError("cleanup_result_path is valid only for the baseline-ready transition")

    ledger_config_map = client.get_object(
        "v1", "ConfigMap", query["ledger_namespace"], query["ledger_name"]
    )
    if ledger_config_map is None:
        raise ReceiptError("durable rollout ledger is absent")
    ledger = _ledger_from_config_map(ledger_config_map, query)

    if mode == "owner-transition":
        if (
            ledger["state"] != transition["from_state"]
            or ledger["sequence"] + 1 != transition["sequence"]
            or transition["prior_ledger_sha256"] != _sha256(_canonical(ledger))
        ):
            raise ReceiptError("signed transition does not extend the current durable ledger")
        if transition["receipt_id"] == ledger["last_receipt_id"] or transition["nonce"] == ledger["last_nonce"]:
            raise ReceiptError("receipt ID or nonce was already consumed")
        live_inventory = _validate_live_observations(client, objects, inventories)
        if transition["to_state"] == "baseline-ready" and live_inventory != {
            "live_inventory_sha256": assertions["live_inventory_sha256"],
            "live_reference_host_paths": assertions["live_reference_host_paths"],
            "live_baseline_incompatible_objects": assertions["live_baseline_incompatible_objects"],
            "live_restricted_incompatible_objects": assertions[
                "live_restricted_incompatible_objects"
            ],
        }:
            raise ReceiptError("immediate live workload inventory differs from baseline-ready assertions")
        ledger.update(
            {
                "sequence": transition["sequence"],
                "state": transition["to_state"],
                "last_bundle_sha256": bundle_sha256,
                "last_receipt_id": transition["receipt_id"],
                "last_nonce": transition["nonce"],
                "authorization": {
                    "phase": phase,
                    "bundle_sha256": bundle_sha256,
                    "nonce": transition["nonce"],
                    "downstream_consumed": False,
                },
            }
        )
    else:
        authorization = _object(ledger["authorization"], "ledger.authorization")
        _exact_keys(
            authorization,
            {"phase", "bundle_sha256", "nonce", "downstream_consumed"},
            "ledger.authorization",
        )
        if (
            ledger["state"] != transition["to_state"]
            or ledger["sequence"] != transition["sequence"]
            or ledger["last_bundle_sha256"] != bundle_sha256
            or authorization["phase"] != phase
            or authorization["bundle_sha256"] != bundle_sha256
            or authorization["nonce"] != transition["nonce"]
        ):
            raise ReceiptError("durable ledger does not carry this exact downstream authorization")
        if authorization["downstream_consumed"] is not False:
            raise ReceiptError("downstream phase authorization was already consumed")
        authorization["downstream_consumed"] = True
        ledger["authorization"] = authorization

    _replace_ledger(client, ledger_config_map, ledger)
    return {
        "valid": "true",
        "terminal_state": transition["to_state"],
        "bundle_sha256": bundle_sha256,
        "sequence": str(transition["sequence"]),
        "consumer": mode,
    }


class KubectlClient:
    def __init__(self, kubeconfig: Path, context: str) -> None:
        if not kubeconfig.is_absolute() or ".." in kubeconfig.parts:
            raise ReceiptError("kubeconfig_path must be absolute without parent traversal")
        if not IDENTIFIER_RE.fullmatch(context):
            raise ReceiptError("kube_context is malformed")
        self._base = [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig),
            "--context",
            context,
            "--as=system:serviceaccount:fs2-system:fs2-pod-security-rollout-manager",
        ]

    def _raw(self, path: str) -> dict[str, Any] | None:
        completed = subprocess.run(
            [*self._base, "get", "--raw", path],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            lowered = completed.stderr.lower()
            if "not found" in lowered or "404" in lowered:
                return None
            raise ReceiptError(f"live Kubernetes read failed for {path}")
        try:
            return _object(json.loads(completed.stdout), "live Kubernetes response")
        except json.JSONDecodeError as error:
            raise ReceiptError("live Kubernetes response is not JSON") from error

    def get_object(
        self, api_version: str, kind: str, namespace: str, name: str
    ) -> dict[str, Any] | None:
        return self._raw(_resource_path(api_version, kind, namespace, name))

    def list_objects(
        self,
        api_version: str,
        kind: str,
        namespace: str,
        label_selector: str,
        field_selector: str,
    ) -> list[dict[str, Any]]:
        query = urlencode(
            {key: value for key, value in {"labelSelector": label_selector, "fieldSelector": field_selector}.items() if value}
        )
        path = _resource_path(api_version, kind, namespace)
        value = self._raw(f"{path}?{query}" if query else path)
        if value is None or not isinstance(value.get("items"), list):
            raise ReceiptError("live Kubernetes collection response is malformed")
        return value["items"]

    def replace_config_map(self, value: dict[str, Any]) -> dict[str, Any]:
        completed = subprocess.run(
            [*self._base, "replace", "-f", "-"],
            input=_canonical(value),
            check=False,
            capture_output=True,
            timeout=30,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace").lower()
            if "conflict" in stderr or "the object has been modified" in stderr:
                raise ConflictError("durable ledger changed during compare-and-swap")
            raise ReceiptError("durable ledger replace failed")
        try:
            return _object(json.loads(completed.stdout), "ledger replace response")
        except json.JSONDecodeError as error:
            raise ReceiptError("ledger replace response is not JSON") from error


def _query_from_environment() -> dict[str, Any]:
    raw = os.environ.get("FS2_POD_SECURITY_QUERY")
    if raw is None:
        return _object(json.load(sys.stdin), "query")
    try:
        return _object(json.loads(raw), "query")
    except json.JSONDecodeError as error:
        raise ReceiptError("FS2_POD_SECURITY_QUERY is not JSON") from error


def main() -> int:
    try:
        query = _query_from_environment()
        kubeconfig = Path(_string(os.environ.get("FS2_KUBECONFIG"), "FS2_KUBECONFIG"))
        kube_context = _string(os.environ.get("FS2_KUBE_CONTEXT"), "FS2_KUBE_CONTEXT")
        result = verify_and_consume(query, KubectlClient(kubeconfig, kube_context))
    except (OSError, ReceiptError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(f"pod-security receipt consumption failed: {error}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
