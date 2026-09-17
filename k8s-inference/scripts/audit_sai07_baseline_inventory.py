#!/usr/bin/env python3
"""Produce and verify a canonical pre-enforcement workload inventory.

The first read becomes a private frozen baseline artifact.  Every later gate
must read that exact artifact through a descriptor-fenced path and bind its
digest into the signed rollout context.  ``--mode initial`` proves that the
live cluster still equals the artifact object-for-object; ``--mode clean``
proves the same cluster and namespace inventory now have no Baseline or
restricted/root/AppArmor exceptions.  This tool is read-only and is not
rollout authority by itself.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sai07_inventory_projection import ProjectionError, live_projection  # noqa: E402

SCHEMA = "fs2-serve.nebius.ai/sai07-baseline-inventory/v5"
VERIFICATION_SCHEMA = "fs2-serve.nebius.ai/sai07-baseline-verification/v1"
SECRET_METADATA_SCHEMA = "fs2-serve.nebius.ai/sai07-secret-metadata/v1"
SECRET_METADATA_MEDIA_TYPE = (
    "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1"
)
SCIENTIFIC_NAMESPACES = (
    "fs2-academic-poc",
    "fs2-bioir-boltz2",
    "fs2-bioir-coverage",
    "fs2-bioir-openfold",
    "fs2-bioir-protenix",
    "fs2-bioir-snapshot",
)
BASELINE_NAMESPACES = (
    "fs2-data",
    "fs2-models",
    "fs2-observability",
    "fs2-reference-data",
    "fs2-system",
    *SCIENTIFIC_NAMESPACES,
)
EXCEPTION_NAMESPACE = "fs2-node-observability"
SNAPSHOT_EXCEPTION_NAMESPACE = "fs2-snapshot-operations"
EXCEPTION_OWNERS = {
    "fs2-dcgm-exporter": "fs2-dcgm-exporter",
    "fs2-node-exporter": "fs2-node-exporter",
    "fs2-otel-node-agent": "fs2-otel-node",
    "fs2-serve-control-plane-gpu-observer": "fs2-serve-control-plane-gpu-observer",
}
EXCEPTION_CONFIG_COMPONENTS = {
    "dcgm-cold-config": ("fs2-dcgm-config-", "config.yaml"),
    "dcgm-metrics-config": ("fs2-dcgm-metrics-", "metrics"),
    "otel-node-config": ("fs2-otel-node-relay-", "relay"),
}
COLLECTIONS = {
    "ConfigMap": ("v1", "/api/v1/namespaces/{namespace}/configmaps", ()),
    "Pod": ("v1", "/api/v1/namespaces/{namespace}/pods", ("spec",)),
    "PodTemplate": (
        "v1",
        "/api/v1/namespaces/{namespace}/podtemplates",
        ("template", "spec"),
    ),
    "ServiceAccount": (
        "v1",
        "/api/v1/namespaces/{namespace}/serviceaccounts",
        (),
    ),
    "ReplicationController": (
        "v1",
        "/api/v1/namespaces/{namespace}/replicationcontrollers",
        ("spec", "template", "spec"),
    ),
    "Deployment": (
        "apps/v1",
        "/apis/apps/v1/namespaces/{namespace}/deployments",
        ("spec", "template", "spec"),
    ),
    "StatefulSet": (
        "apps/v1",
        "/apis/apps/v1/namespaces/{namespace}/statefulsets",
        ("spec", "template", "spec"),
    ),
    "DaemonSet": (
        "apps/v1",
        "/apis/apps/v1/namespaces/{namespace}/daemonsets",
        ("spec", "template", "spec"),
    ),
    "ReplicaSet": (
        "apps/v1",
        "/apis/apps/v1/namespaces/{namespace}/replicasets",
        ("spec", "template", "spec"),
    ),
    "Job": ("batch/v1", "/apis/batch/v1/namespaces/{namespace}/jobs", ("spec", "template", "spec")),
    "CronJob": (
        "batch/v1",
        "/apis/batch/v1/namespaces/{namespace}/cronjobs",
        ("spec", "jobTemplate", "spec", "template", "spec"),
    ),
    "JobSet": (
        "jobset.x-k8s.io/v1alpha2",
        "/apis/jobset.x-k8s.io/v1alpha2/namespaces/{namespace}/jobsets",
        (),
    ),
    "ModelDeployment": (
        "inference.fs2.nebius.ai/v1alpha1",
        "/apis/inference.fs2.nebius.ai/v1alpha1/namespaces/{namespace}/modeldeployments",
        (),
    ),
    "ScaledObject": (
        "keda.sh/v1alpha1",
        "/apis/keda.sh/v1alpha1/namespaces/{namespace}/scaledobjects",
        (),
    ),
    "NetworkPolicy": (
        "networking.k8s.io/v1",
        "/apis/networking.k8s.io/v1/namespaces/{namespace}/networkpolicies",
        (),
    ),
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


class InventoryError(ValueError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


class Kubectl:
    def __init__(self, kubeconfig: Path, context: str) -> None:
        self.base = ["kubectl", "--kubeconfig", str(kubeconfig), "--context", context]

    def raw(self, uri: str, *, optional: bool = False) -> dict[str, Any]:
        result = subprocess.run([*self.base, "get", "--raw", uri], check=False, capture_output=True, text=True)
        if result.returncode != 0 and optional and ("not found" in result.stderr.lower() or "404" in result.stderr):
            return {"apiVersion": "v1", "kind": "List", "items": [], "metadata": {"resourceVersion": "0"}}
        if result.returncode != 0:
            raise InventoryError(f"read failed for {uri}: {result.stderr.strip()}")
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise InventoryError(f"read returned a non-object for {uri}")
        return value


def nested(value: object, path: tuple[str, ...]) -> dict[str, Any]:
    current = value
    for component in path:
        current = current.get(component, {}) if isinstance(current, dict) else {}
    return current if isinstance(current, dict) else {}


def pod_templates(value: dict[str, Any], path: tuple[str, ...]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return every Pod template, including multi-template custom controllers."""

    metadata = value.get("metadata", {})
    if path:
        spec = nested(value, path)
        if not spec:
            return []
        if value.get("kind") == "Pod":
            annotations = metadata.get("annotations", {}) if isinstance(metadata, dict) else {}
        else:
            template = nested(value, path[:-1])
            template_metadata = template.get("metadata", {}) if isinstance(template, dict) else {}
            annotations = template_metadata.get("annotations", {}) if isinstance(template_metadata, dict) else {}
        return [(spec, annotations)]

    spec = value.get("spec", {})
    if value.get("kind") != "JobSet" or not isinstance(spec, dict):
        return []
    results: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for replicated_job in spec.get("replicatedJobs", []) or []:
        if not isinstance(replicated_job, dict):
            continue
        job_template = replicated_job.get("template", {})
        job_spec = job_template.get("spec", {}) if isinstance(job_template, dict) else {}
        pod_template = job_spec.get("template", {}) if isinstance(job_spec, dict) else {}
        pod_spec = pod_template.get("spec", {}) if isinstance(pod_template, dict) else {}
        if not isinstance(pod_spec, dict) or not pod_spec:
            continue
        template_metadata = pod_template.get("metadata", {})
        annotations = template_metadata.get("annotations", {}) if isinstance(template_metadata, dict) else {}
        results.append((pod_spec, annotations))
    return results


def pod_findings(spec: dict[str, Any], annotations: dict[str, Any] | None = None) -> tuple[list[str], list[str]]:
    findings: set[str] = set()
    restricted: set[str] = set()
    for field in ("hostNetwork", "hostPID", "hostIPC"):
        if spec.get(field) is True:
            findings.add(field)
    for volume in spec.get("volumes", []) or []:
        if isinstance(volume, dict) and "hostPath" in volume:
            findings.add("hostPath")
    containers = [
        *(spec.get("initContainers", []) or []),
        *(spec.get("containers", []) or []),
        *(spec.get("ephemeralContainers", []) or []),
    ]
    for container in containers:
        security = container.get("securityContext", {}) if isinstance(container, dict) else {}
        if security.get("privileged") is True:
            findings.add("privileged")
            restricted.add("privileged")
        if security.get("runAsUser") == 0 or security.get("runAsNonRoot") is False:
            restricted.add("root")
        if (
            security.get("runAsNonRoot") is not True
            and (spec.get("securityContext") or {}).get("runAsNonRoot") is not True
        ):
            restricted.add("runAsNonRoot")
        if security.get("allowPrivilegeEscalation") is not False:
            restricted.add("allowPrivilegeEscalation")
        if security.get("procMount") not in (None, "Default"):
            findings.add("procMount")
        seccomp = security.get("seccompProfile", {})
        if isinstance(seccomp, dict) and seccomp.get("type") == "Unconfined":
            findings.add("unconfinedSeccomp")
            restricted.add("unconfinedSeccomp")
        if not seccomp and not (spec.get("securityContext") or {}).get("seccompProfile"):
            restricted.add("seccompProfile")
        apparmor = security.get("appArmorProfile", {})
        if isinstance(apparmor, dict) and apparmor.get("type") == "Unconfined":
            findings.add("unconfinedAppArmor")
            restricted.add("unconfinedAppArmor")
        added = set((security.get("capabilities", {}) or {}).get("add", []) or [])
        if not added.issubset(BASELINE_CAPABILITIES):
            findings.add("capabilities")
        if added:
            restricted.add("capabilities")
        dropped = set((security.get("capabilities", {}) or {}).get("drop", []) or [])
        if "ALL" not in dropped:
            restricted.add("capabilitiesDrop")
        for port in container.get("ports", []) if isinstance(container, dict) else []:
            if isinstance(port, dict) and int(port.get("hostPort", 0) or 0) != 0:
                findings.add("hostPort")
    pod_security = spec.get("securityContext", {}) or {}
    if pod_security.get("runAsUser") == 0 or pod_security.get("runAsNonRoot") is False:
        restricted.add("root")
    if pod_security.get("runAsNonRoot") is not True:
        restricted.add("runAsNonRoot")
    seccomp = pod_security.get("seccompProfile", {}) if isinstance(pod_security, dict) else {}
    if isinstance(seccomp, dict) and seccomp.get("type") == "Unconfined":
        findings.add("unconfinedSeccomp")
        restricted.add("unconfinedSeccomp")
    if not seccomp:
        restricted.add("seccompProfile")
    for key, value in (annotations or {}).items():
        if key.startswith("container.apparmor.security.beta.kubernetes.io/") and value == "unconfined":
            findings.add("unconfinedAppArmor")
            restricted.add("unconfinedAppArmor")
    return sorted(findings), sorted(restricted)


def baseline_findings(spec: dict[str, Any]) -> list[str]:
    """Backward-compatible focused helper used by contract tests."""

    return pod_findings(spec)[0]


def read_regular(path: Path, limit: int = 128 * 1024 * 1024) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise InventoryError(f"cannot safely open baseline artifact: {error.strerror}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise InventoryError("baseline artifact must be a bounded regular file")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) != metadata.st_size:
            raise InventoryError("baseline artifact changed while it was read")
        return payload
    finally:
        os.close(descriptor)


def validate_artifact(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise InventoryError("baseline artifact schema is unsupported")
    required = {
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
        "legacy_controller_objects",
        "legacy_service_account_token_secret_collection_resource_version",
        "legacy_service_account_token_secrets",
        "unauthorized_exception_objects",
        "inventory_sha256",
    }
    if set(value) != required:
        raise InventoryError("baseline artifact fields differ from the canonical schema")
    unsigned = dict(value)
    digest = unsigned.pop("inventory_sha256")
    if digest != hashlib.sha256(canonical(unsigned)).hexdigest():
        raise InventoryError("baseline artifact self-digest differs")
    if value["scientific_namespaces"] != list(SCIENTIFIC_NAMESPACES):
        raise InventoryError("baseline artifact scientific namespace inventory differs")
    if value["inspected_namespaces"] != list(BASELINE_NAMESPACES):
        raise InventoryError("baseline artifact inspected namespace inventory differs")
    if not isinstance(value["objects"], list) or not isinstance(value["collections"], list):
        raise InventoryError("baseline artifact inventories must be lists")
    collection_kinds = COLLECTIONS
    expected_collections = {
        (namespace, api_version, kind)
        for namespace in BASELINE_NAMESPACES
        for kind, (api_version, _, _) in collection_kinds.items()
    }
    observed_collections: dict[tuple[str, str, str], int] = {}
    for collection in value["collections"]:
        if not isinstance(collection, dict) or set(collection) != {
            "api_version",
            "kind",
            "namespace",
            "resource_version",
            "item_count",
        }:
            raise InventoryError("baseline collection fields differ from the canonical schema")
        key = (collection["namespace"], collection["api_version"], collection["kind"])
        if key in observed_collections or key not in expected_collections:
            raise InventoryError("baseline collections are duplicated or outside the frozen inventory")
        if (
            not isinstance(collection["resource_version"], str)
            or not collection["resource_version"]
            or not isinstance(collection["item_count"], int)
            or isinstance(collection["item_count"], bool)
            or collection["item_count"] < 0
        ):
            raise InventoryError("baseline collection identity/count is malformed")
        observed_collections[key] = collection["item_count"]
    if set(observed_collections) != expected_collections:
        raise InventoryError("baseline artifact omits a frozen namespace/workload collection")
    observed_object_counts = {key: 0 for key in expected_collections}
    for item in value["objects"]:
        if not isinstance(item, dict):
            raise InventoryError("baseline object inventory is malformed")
        key = (item.get("namespace"), item.get("api_version"), item.get("kind"))
        if key not in observed_object_counts:
            raise InventoryError("baseline object is outside the frozen inventory")
        observed_object_counts[key] += 1
    if observed_object_counts != observed_collections:
        raise InventoryError("baseline object counts differ from collection snapshots")
    for field in (
        "reference_host_paths",
        "baseline_incompatible_objects",
        "restricted_incompatible_objects",
    ):
        if not isinstance(value[field], int) or isinstance(value[field], bool) or value[field] < 0:
            raise InventoryError("baseline artifact finding counts are malformed")
    if not isinstance(value["legacy_controller_objects"], list):
        raise InventoryError("baseline artifact legacy controller inventory must be a list")
    token_secret_resource_version = value["legacy_service_account_token_secret_collection_resource_version"]
    token_secrets = value["legacy_service_account_token_secrets"]
    if not isinstance(token_secret_resource_version, str) or not token_secret_resource_version:
        raise InventoryError("legacy token Secret collection resourceVersion is missing")
    if not isinstance(token_secrets, list):
        raise InventoryError("legacy token Secret inventory must be a list")
    legacy_service_accounts = {
        item.get("name")
        for item in value["legacy_controller_objects"]
        if isinstance(item, dict) and item.get("kind") == "ServiceAccount"
    }
    seen_token_secrets: set[str] = set()
    for item in token_secrets:
        if not isinstance(item, dict) or set(item) != {
            "name",
            "uid",
            "resource_version",
            "service_account_name",
        }:
            raise InventoryError("legacy token Secret metadata fields differ")
        if (
            not all(isinstance(item[field], str) and item[field] for field in item)
            or item["name"] in seen_token_secrets
            or item["service_account_name"] not in legacy_service_accounts
        ):
            raise InventoryError("legacy token Secret metadata is malformed or outside the legacy SA inventory")
        seen_token_secrets.add(item["name"])
    if value["unauthorized_exception_objects"] != []:
        raise InventoryError("baseline artifact contains unauthorized exception objects")
    return value


def validate_secret_metadata_artifact(
    artifact_bytes: bytes, expected_sha256: str
) -> tuple[str, list[dict[str, str]]]:
    """Consume only the separately collected PartialObjectMetadata artifact."""

    if not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
        raise InventoryError("Secret metadata artifact SHA-256 is malformed")
    if hashlib.sha256(artifact_bytes).hexdigest() != expected_sha256:
        raise InventoryError("Secret metadata artifact digest differs")
    try:
        value = json.loads(artifact_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InventoryError("Secret metadata artifact is not JSON") from error
    required = {
        "schema",
        "media_type",
        "namespace",
        "collection_resource_version",
        "items",
        "items_sha256",
        "item_count",
        "contains_secret_payload",
        "reader_service_account_uid",
        "token_bound_object_ref",
        "token_jti_sha256",
        "observed_at",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise InventoryError("Secret metadata artifact fields differ from the v1 contract")
    if canonical(value) != artifact_bytes:
        raise InventoryError("Secret metadata artifact is not canonical JSON")
    if (
        value["schema"] != SECRET_METADATA_SCHEMA
        or value["media_type"] != SECRET_METADATA_MEDIA_TYPE
        or value["namespace"] != "fs2-models"
        or value["contains_secret_payload"] is not False
    ):
        raise InventoryError("Secret inventory was not collected as PartialObjectMetadataList")
    if not isinstance(value["items"], list):
        raise InventoryError("Secret metadata items must be a list")
    if (
        hashlib.sha256(canonical(value["items"])).hexdigest() != value["items_sha256"]
        or value["item_count"] != len(value["items"])
    ):
        raise InventoryError("Secret metadata item count or digest differs")
    bound = value["token_bound_object_ref"]
    if (
        not isinstance(bound, dict)
        or set(bound) != {"api_version", "kind", "namespace", "name", "uid"}
        or bound["api_version"] != "v1"
        or bound["kind"] != "Secret"
        or bound["namespace"] != "fs2-system"
        or not re.fullmatch(
            r"fs2-pod-security-token-anchor-v(?:3|4)-[a-f0-9]{64}",
            str(bound["name"]),
        )
        or not isinstance(bound["uid"], str)
        or not bound["uid"]
    ):
        raise InventoryError("metadata reader token is not bound to the exact custody anchor")
    if not isinstance(value["reader_service_account_uid"], str) or not value["reader_service_account_uid"]:
        raise InventoryError("metadata reader ServiceAccount UID is missing")
    if not re.fullmatch(r"[a-f0-9]{64}", str(value["token_jti_sha256"])):
        raise InventoryError("metadata reader token JTI digest is malformed")
    try:
        observed_at = dt.datetime.fromisoformat(
            str(value["observed_at"]).removesuffix("Z") + "+00:00"
        )
    except ValueError as error:
        raise InventoryError("Secret metadata observed_at is malformed") from error
    if observed_at.tzinfo != dt.UTC or abs(dt.datetime.now(dt.UTC) - observed_at) > dt.timedelta(minutes=2, seconds=30):
        raise InventoryError("Secret metadata artifact is stale")
    for item in value["items"]:
        if not isinstance(item, dict) or set(item) != {
            "namespace",
            "name",
            "uid",
            "resource_version",
            "service_account_name",
        }:
            raise InventoryError("Secret metadata item fields differ")
        if item["namespace"] != "fs2-models" or not all(isinstance(field, str) and field for field in item.values()):
            raise InventoryError("Secret metadata item identity is incomplete")
    if value["items"] != sorted(value["items"], key=lambda item: (item["service_account_name"], item["name"])):
        raise InventoryError("Secret metadata items are not canonically ordered")
    resource_version = value["collection_resource_version"]
    if not isinstance(resource_version, str) or not resource_version:
        raise InventoryError("Secret metadata collection resourceVersion is absent")
    return resource_version, value["items"]


def legacy_controller_identity(
    namespace: str,
    kind: str,
    metadata: dict[str, Any],
) -> dict[str, str] | None:
    if namespace != "fs2-models" or kind not in {"NetworkPolicy", "ServiceAccount", "DaemonSet"}:
        return None
    labels = metadata.get("labels", {}) or {}
    if not isinstance(labels, dict):
        return None
    name = str(metadata.get("name", ""))
    controller_owned = labels.get("app.kubernetes.io/managed-by") == "fs2-model-controller"
    legacy_policy = (
        kind == "NetworkPolicy"
        and labels.get("app.kubernetes.io/part-of") == "fs2-serve"
        and labels.get("app.kubernetes.io/managed-by") != "terraform"
        and not name.startswith("fs2-network-profile-")
    )
    if not (controller_owned or legacy_policy):
        return None
    return {
        "api_version": "v1"
        if kind == "ServiceAccount"
        else ("apps/v1" if kind == "DaemonSet" else "networking.k8s.io/v1"),
        "kind": kind,
        "namespace": namespace,
        "name": name,
        "uid": str(metadata.get("uid", "")),
        "resource_version": str(metadata.get("resourceVersion", "")),
    }


def authorized_exception_config_map(value: dict[str, Any]) -> bool:
    metadata = value.get("metadata", {})
    if not isinstance(metadata, dict):
        return False
    name = str(metadata.get("name", ""))
    if name == "kube-root-ca.crt":
        return True
    labels = metadata.get("labels", {}) or {}
    annotations = metadata.get("annotations", {}) or {}
    data = value.get("data", {})
    if not isinstance(labels, dict) or not isinstance(annotations, dict) or not isinstance(data, dict):
        return False
    component = labels.get("app.kubernetes.io/component")
    if component not in EXCEPTION_CONFIG_COMPONENTS or value.get("immutable") is not True:
        return False
    prefix, key = EXCEPTION_CONFIG_COMPONENTS[component]
    content = data.get(key)
    if not isinstance(content, str):
        return False
    digest = hashlib.sha256(content.encode()).hexdigest()
    return (
        name == f"{prefix}{digest[:16]}"
        and annotations.get("security.fs2.nebius.ai/content-sha256") == f"sha256:{digest}"
        and set(data) == {key}
    )


def inventory(
    client: Kubectl,
    secret_metadata: tuple[str, list[dict[str, str]]],
) -> dict[str, Any]:
    namespaces = client.raw("/api/v1/namespaces")
    discovered = sorted(
        item.get("metadata", {}).get("name", "")
        for item in namespaces.get("items", [])
        if item.get("metadata", {}).get("name", "") == "fs2-academic-poc"
        or item.get("metadata", {}).get("name", "").startswith("fs2-bioir-")
    )
    if discovered != list(SCIENTIFIC_NAMESPACES):
        raise InventoryError("live scientific namespace inventory differs from the exact frozen six-name contract")

    objects: list[dict[str, Any]] = []
    collections: list[dict[str, Any]] = []
    reference_host_paths = 0
    incompatible_objects = 0
    restricted_incompatible_objects = 0
    legacy_controller_objects: list[dict[str, str]] = []
    for namespace in BASELINE_NAMESPACES:
        for kind, (api_version, uri, path) in COLLECTIONS.items():
            collection = client.raw(
                uri.format(namespace=namespace), optional=kind in {"JobSet", "ModelDeployment", "ScaledObject"}
            )
            items = collection.get("items", [])
            if not isinstance(items, list):
                raise InventoryError(f"{api_version}/{kind} collection is malformed")
            collection_metadata = collection.get("metadata", {})
            collections.append(
                {
                    "api_version": api_version,
                    "kind": kind,
                    "namespace": namespace,
                    "resource_version": str(collection_metadata.get("resourceVersion", "")),
                    "item_count": len(items),
                }
            )
            for item in items:
                metadata = item.get("metadata", {})
                templates = pod_templates(item, path)
                template_results = [pod_findings(spec, annotations) for spec, annotations in templates]
                findings = sorted({finding for result, _ in template_results for finding in result})
                restricted_findings = sorted({finding for _, result in template_results for finding in result})
                if findings:
                    incompatible_objects += 1
                if restricted_findings:
                    restricted_incompatible_objects += 1
                if "hostPath" in findings:
                    reference_host_paths += 1
                specs = [spec for spec, _ in templates]
                normalized = dict(item)
                normalized.setdefault("apiVersion", api_version)
                normalized.setdefault("kind", kind)
                normalized_metadata = dict(metadata)
                normalized_metadata.setdefault("namespace", namespace)
                normalized["metadata"] = normalized_metadata
                try:
                    projection = live_projection(normalized)
                except ProjectionError as error:
                    raise InventoryError(str(error)) from error
                objects.append(
                    {
                        "api_version": api_version,
                        "kind": kind,
                        "namespace": namespace,
                        "name": metadata.get("name"),
                        "uid": metadata.get("uid"),
                        "resource_version": metadata.get("resourceVersion"),
                        "object_sha256": hashlib.sha256(canonical(projection)).hexdigest(),
                        "pod_spec_sha256": hashlib.sha256(canonical(specs)).hexdigest(),
                        "baseline_findings": findings,
                        "restricted_findings": restricted_findings,
                    }
                )
                legacy = legacy_controller_identity(namespace, kind, metadata)
                if legacy is not None:
                    legacy["object_sha256"] = hashlib.sha256(canonical(projection)).hexdigest()
                    legacy_controller_objects.append(legacy)

    legacy_service_account_names = {
        item["name"] for item in legacy_controller_objects if item["kind"] == "ServiceAccount"
    }
    secret_collection_resource_version, metadata_token_secrets = secret_metadata
    legacy_token_secrets = [
        {
            "name": item["name"],
            "uid": item["uid"],
            "resource_version": item["resource_version"],
            "service_account_name": item["service_account_name"],
        }
        for item in metadata_token_secrets
        if item["service_account_name"] in legacy_service_account_names
    ]

    unauthorized_exception: list[str] = []
    for exception_namespace in (EXCEPTION_NAMESPACE, SNAPSHOT_EXCEPTION_NAMESPACE):
        for kind, (_, uri, path) in COLLECTIONS.items():
            collection = client.raw(
                uri.format(namespace=exception_namespace),
                optional=True,
            )
            for item in collection.get("items", []):
                metadata = item.get("metadata", {})
                name = str(metadata.get("name", ""))
                templates = pod_templates(item, path)
                spec = templates[0][0] if len(templates) == 1 else {}
                if exception_namespace == SNAPSHOT_EXCEPTION_NAMESPACE:
                    labels = metadata.get("labels", {}) if isinstance(metadata, dict) else {}
                    if not (
                        (
                            kind == "Pod"
                            and name.startswith("fs2-snapshot-")
                            and spec.get("serviceAccountName") == "fs2-snapshot-runtime"
                            and labels.get("security.fs2.nebius.ai/snapshot-profile") == "esmfold2-h100-v1"
                        )
                        or (kind == "ServiceAccount" and name in {"default", "fs2-snapshot-runtime"})
                        or (kind == "NetworkPolicy" and name == "fs2-snapshot-default-deny")
                        or (kind == "ConfigMap" and authorized_exception_config_map(item))
                    ):
                        unauthorized_exception.append(f"{exception_namespace}/{kind}/{name}")
                elif kind == "ConfigMap":
                    if not authorized_exception_config_map(item):
                        unauthorized_exception.append(f"{exception_namespace}/{kind}/{name}")
                elif kind == "DaemonSet":
                    if name not in EXCEPTION_OWNERS or spec.get("serviceAccountName") != EXCEPTION_OWNERS.get(name):
                        unauthorized_exception.append(f"{exception_namespace}/{kind}/{name}")
                elif kind == "ServiceAccount":
                    if name != "default" and name not in set(EXCEPTION_OWNERS.values()):
                        unauthorized_exception.append(f"{exception_namespace}/{kind}/{name}")
                elif kind == "Pod":
                    owners = metadata.get("ownerReferences", []) or []
                    owner_names = {
                        owner.get("name")
                        for owner in owners
                        if isinstance(owner, dict) and owner.get("kind") == "DaemonSet"
                    }
                    expected = next((owner for owner in owner_names if owner in EXCEPTION_OWNERS), None)
                    if expected is None or spec.get("serviceAccountName") != EXCEPTION_OWNERS[expected]:
                        unauthorized_exception.append(f"{exception_namespace}/{kind}/{name}")
                else:
                    unauthorized_exception.append(f"{exception_namespace}/{kind}/{name}")

    result: dict[str, Any] = {
        "schema": SCHEMA,
        "captured_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "cluster": {
            "kube_system_uid": client.raw("/api/v1/namespaces/kube-system").get("metadata", {}).get("uid"),
        },
        "scientific_namespaces": list(SCIENTIFIC_NAMESPACES),
        "inspected_namespaces": list(BASELINE_NAMESPACES),
        "collections": sorted(collections, key=lambda item: (item["namespace"], item["api_version"], item["kind"])),
        "objects": sorted(
            objects, key=lambda item: (item["namespace"], item["api_version"], item["kind"], item["name"])
        ),
        "reference_host_paths": reference_host_paths,
        "baseline_incompatible_objects": incompatible_objects,
        "restricted_incompatible_objects": restricted_incompatible_objects,
        "legacy_controller_objects": sorted(
            legacy_controller_objects,
            key=lambda item: (item["api_version"], item["kind"], item["namespace"], item["name"]),
        ),
        "legacy_service_account_token_secret_collection_resource_version": secret_collection_resource_version,
        "legacy_service_account_token_secrets": sorted(
            legacy_token_secrets,
            key=lambda item: (item["service_account_name"], item["name"]),
        ),
        "unauthorized_exception_objects": sorted(unauthorized_exception),
    }
    result["inventory_sha256"] = hashlib.sha256(canonical(result)).hexdigest()
    return result


def verify_against_artifact(
    live: dict[str, Any], artifact_bytes: bytes, expected_sha256: str, mode: str
) -> dict[str, Any]:
    artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    if artifact_sha256 != expected_sha256:
        raise InventoryError("baseline artifact bytes differ from the reviewed SHA-256")
    try:
        artifact = validate_artifact(json.loads(artifact_bytes))
    except json.JSONDecodeError as error:
        raise InventoryError("baseline artifact is not JSON") from error
    if live["cluster"] != artifact["cluster"]:
        raise InventoryError("live cluster identity differs from the frozen baseline artifact")
    if mode == "initial":
        comparable = (
            "scientific_namespaces",
            "inspected_namespaces",
            "collections",
            "objects",
            "reference_host_paths",
            "baseline_incompatible_objects",
            "restricted_incompatible_objects",
            "legacy_controller_objects",
            "legacy_service_account_token_secret_collection_resource_version",
            "legacy_service_account_token_secrets",
            "unauthorized_exception_objects",
        )
        if any(live[field] != artifact[field] for field in comparable):
            raise InventoryError("live initial inventory differs from the frozen baseline artifact")
    elif mode == "clean":
        if (
            live["reference_host_paths"] != 0
            or live["baseline_incompatible_objects"] != 0
            or live["restricted_incompatible_objects"] != 0
            or live["legacy_controller_objects"]
            or live["legacy_service_account_token_secrets"]
            or live["unauthorized_exception_objects"]
        ):
            raise InventoryError("live pre-enforcement inventory is not clean")
    else:  # pragma: no cover - argparse constrains this boundary
        raise InventoryError("unsupported verification mode")
    return {
        "schema": VERIFICATION_SCHEMA,
        "mode": mode,
        "baseline_artifact_sha256": artifact_sha256,
        "baseline_inventory_sha256": artifact["inventory_sha256"],
        "baseline_reference_host_paths": artifact["reference_host_paths"],
        "baseline_incompatible_objects": artifact["baseline_incompatible_objects"],
        "baseline_restricted_incompatible_objects": artifact["restricted_incompatible_objects"],
        "live_inventory_sha256": live["inventory_sha256"],
        "live_reference_host_paths": live["reference_host_paths"],
        "live_baseline_incompatible_objects": live["baseline_incompatible_objects"],
        "live_restricted_incompatible_objects": live["restricted_incompatible_objects"],
        "live_legacy_controller_objects": live["legacy_controller_objects"],
        "live_legacy_service_account_token_secrets": live["legacy_service_account_token_secrets"],
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--kubeconfig", required=True, type=Path)
    result.add_argument("--context", required=True)
    result.add_argument("--baseline-artifact", type=Path)
    result.add_argument("--baseline-sha256")
    result.add_argument("--secret-metadata-artifact", required=True, type=Path)
    result.add_argument("--secret-metadata-sha256", required=True)
    result.add_argument("--mode", choices=("capture", "initial", "clean"), default="capture")
    result.add_argument("--require-clean", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        secret_metadata = validate_secret_metadata_artifact(
            read_regular(args.secret_metadata_artifact, limit=4 * 1024 * 1024),
            args.secret_metadata_sha256,
        )
        live = inventory(Kubectl(args.kubeconfig, args.context), secret_metadata)
        if args.mode == "capture":
            if args.baseline_artifact is not None or args.baseline_sha256 is not None:
                raise InventoryError("capture mode does not accept a baseline artifact")
            result = live
        else:
            if args.baseline_artifact is None or args.baseline_sha256 is None:
                raise InventoryError("initial/clean verification requires the exact baseline artifact and SHA-256")
            if len(args.baseline_sha256) != 64 or any(
                character not in "0123456789abcdef" for character in args.baseline_sha256
            ):
                raise InventoryError("baseline SHA-256 is malformed")
            result = verify_against_artifact(
                live,
                read_regular(args.baseline_artifact),
                args.baseline_sha256,
                args.mode,
            )
        if args.require_clean and (
            live["reference_host_paths"] != 0
            or live["baseline_incompatible_objects"] != 0
            or live["restricted_incompatible_objects"] != 0
            or live["legacy_controller_objects"]
            or live["legacy_service_account_token_secrets"]
            or live["unauthorized_exception_objects"]
        ):
            raise InventoryError("pre-enforcement inventory is not clean")
    except (InventoryError, OSError, json.JSONDecodeError) as error:
        print(f"SAI-07 inventory refused: {error}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
