#!/usr/bin/env python3
"""Digest-pinned native and KServe Standard model workload adapters."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from .artifacts import canonical_bytes
from .attestations import verify_signed_attestation
from .capabilities import BackendCapability, require_local_capability
from .loader import (
    CatalogError,
    ModelRecord,
    canonical_content_uri,
    strong_sha256,
)
from .prerequisites import PrerequisiteBinding


MODEL_RUNTIME_SERVICE_ACCOUNT = "fs2-models/model-runtime-service-account"
NGC_PULL_SECRET = "fs2-models/ngc-pull-secret"
NGC_RUNTIME_SECRET = "fs2-models/ngc-runtime-secret"
SHARED_CACHE_PVC = "fs2-models/shared-cache-pvc"
MODEL_CONTENT_PATH_TOKEN = "{FS2_MODEL_CONTENT_PATH}"
REPLICA_SCALER_OWNER = "fs2-model-activation-controller"
REPLICA_FIELD_MANAGER = "fs2-model-activation-controller"
REPLICA_OWNERSHIP_SCHEMA = "fs2-serve.nebius.ai/replica-field-ownership/v1"
MOUNTED_CONTENT_MODELS = frozenset({"qwen3-8b", "glm-5-2-fp8", "nv-reason-cxr-3b"})
RUNTIME_NETWORK_POLICY_SCHEMA = "fs2-serve.nebius.ai/runtime-startup-network-policy/v1"
NIM_OPERATOR_SECURITY_SCHEMA = "fs2-serve.nebius.ai/nim-operator-security-subject/v5"
NIM_ENVELOPE_ANNOTATION = "fs2-serve.nebius.ai/operator-security-envelope-sha256"
NIM_POLICY_ANNOTATION = "fs2-serve.nebius.ai/operator-security-admission-policy"
NIM_IMAGE_ANNOTATION = "fs2-serve.nebius.ai/expected-descendant-image"
NIM_ENVELOPE_TOKEN = "{subject_sha256}"
NIM_POLICY_TOKEN = "{admission_policy}"
NIM_IMAGE_TOKEN = "{descendant_image}"
NIM_DYNAMIC_NAME_TOKEN = "{kubernetes-controller-name}"
NIM_DYNAMIC_LABELS = frozenset(
    {
        "batch.kubernetes.io/controller-uid",
        "batch.kubernetes.io/job-name",
        "controller-revision-hash",
        "controller-uid",
        "job-name",
        "pod-template-generation",
        "pod-template-hash",
        "statefulset.kubernetes.io/pod-name",
    }
)
NIM_DYNAMIC_ANNOTATIONS = frozenset({"deployment.kubernetes.io/revision"})
RESTRICTED_VOLUME_SOURCES = frozenset(
    {"configMap", "csi", "downwardAPI", "emptyDir", "ephemeral", "persistentVolumeClaim", "projected", "secret"}
)


def replica_field_ownership(api_version: str, kind: str) -> dict[str, Any]:
    """Return the immutable zero-bootstrap/activation-owned replica contract."""

    group = api_version.split("/", 1)[0]
    if (api_version, kind) not in {
        ("apps/v1", "Deployment"),
        ("apps.nvidia.com/v1alpha1", "NIMService"),
    }:
        raise CatalogError("replica ownership is defined only for activation targets")
    return {
        "schema": REPLICA_OWNERSHIP_SCHEMA,
        "target": {"api_version": api_version, "group": group, "kind": kind},
        "field": "/spec/replicas",
        "bootstrap_value": 0,
        "field_manager": REPLICA_FIELD_MANAGER,
        "replica_scaler_owner": REPLICA_SCALER_OWNER,
        "ordinary_api_mutation": "forbidden",
        "gitops": {
            "ignore_differences_json_pointers": ["/spec/replicas"],
            "respect_ignore_differences": True,
            "post_bootstrap_desired_field": "omitted-or-ignored",
            "force_apply_conflicts": "forbidden",
        },
    }


def _replica_annotations(api_version: str, kind: str) -> dict[str, str]:
    contract = replica_field_ownership(api_version, kind)
    digest = hashlib.sha256(
        (json.dumps(contract, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    return {
        "fs2-serve.nebius.ai/replica-field-owner": REPLICA_SCALER_OWNER,
        "fs2-serve.nebius.ai/replica-field-manager": REPLICA_FIELD_MANAGER,
        "fs2-serve.nebius.ai/replica-field-path": "/spec/replicas",
        "fs2-serve.nebius.ai/replica-bootstrap-value": "0",
        "fs2-serve.nebius.ai/replica-ownership-digest": digest,
        "fs2-serve.nebius.ai/gitops-replica-policy": "ignore-differences-after-zero-bootstrap",
        "argocd.argoproj.io/sync-options": "RespectIgnoreDifferences=true",
    }


def _runtime_image(record: ModelRecord) -> str:
    image = record.to_dict()["runtime"]["image"]
    reference = image["reference"]
    digest = image["digest"]
    if (
        image["state"] != "resolved"
        or not isinstance(reference, str)
        or not isinstance(digest, str)
    ):
        raise CatalogError("workload creation requires a resolved immutable runtime image")
    strong_sha256(digest, "workload runtime image digest", image=True)
    if not reference.endswith("@" + digest):
        raise CatalogError("workload creation requires a resolved immutable runtime image")
    return reference


def _nim_operator_security_envelope(
    value: Mapping[str, Any] | None,
    *,
    trusted_attestors: Mapping[str, str] | None,
    expected_session_id: str | None,
    expected_kind: str,
    record: ModelRecord,
) -> tuple[str, str, str, Mapping[str, str]]:
    """Verify the signed CR and operator-descendant admission subject."""

    if (
        value is None
        or set(value) != {"subject", "subject_sha256", "attestation", "attestation_sha256"}
        or not trusted_attestors
        or expected_session_id is None
    ):
        raise CatalogError("NIM Operator path lacks a signed external-trust admission envelope")
    subject = value["subject"]
    attestation = value["attestation"]
    if not isinstance(subject, Mapping) or not isinstance(attestation, Mapping):
        raise CatalogError("NIM Operator signed admission envelope is invalid")
    if set(subject) != {
        "schema",
        "model_id",
        "resource_kind",
        "private_registry",
        "descendant_image",
        "custom_resource_image",
        "custom_resource_sha256",
        "runtime_container_name",
        "admission_policy",
        "admission_policy_sha256",
        "actor_identities",
        "descendant_resources",
        "pod_spec",
        "volumes",
        "containers",
        "volume_devices",
    }:
        raise CatalogError("NIM Operator signed admission subject fields differ")
    subject_sha256 = hashlib.sha256(canonical_bytes(subject)).hexdigest()
    attestation_sha256 = hashlib.sha256(canonical_bytes(attestation)).hexdigest()
    runtime_digest = record.to_dict()["runtime"]["image"]["digest"]
    descendant_image = subject["descendant_image"]
    containers = subject["containers"]
    runtime_container_name = subject["runtime_container_name"]
    custom_resource_image = subject["custom_resource_image"]
    actor_identities = subject["actor_identities"]
    descendant_resources = subject["descendant_resources"]
    expected_resource_graph = (
        {
            "Job": {
                "api_version": "batch/v1",
                "group": "batch",
                "version": "v1",
                "resource": "jobs",
                "actor_identity": "nim_operator",
                "owner_kinds": ["NIMCache"],
                "pod_template": True,
            },
            "Pod": {
                "api_version": "v1",
                "group": "",
                "version": "v1",
                "resource": "pods",
                "actor_identity": "kube_controller_manager",
                "owner_kinds": ["Job"],
                "pod_template": False,
            },
        }
        if expected_kind == "NIMCache"
        else {
            "Deployment": {
                "api_version": "apps/v1",
                "group": "apps",
                "version": "v1",
                "resource": "deployments",
                "actor_identity": "nim_operator",
                "owner_kinds": ["NIMService"],
                "pod_template": True,
            },
            "ReplicaSet": {
                "api_version": "apps/v1",
                "group": "apps",
                "version": "v1",
                "resource": "replicasets",
                "actor_identity": "kube_controller_manager",
                "owner_kinds": ["Deployment"],
                "pod_template": True,
            },
            "StatefulSet": {
                "api_version": "apps/v1",
                "group": "apps",
                "version": "v1",
                "resource": "statefulsets",
                "actor_identity": "nim_operator",
                "owner_kinds": ["NIMService"],
                "pod_template": True,
            },
            "Job": {
                "api_version": "batch/v1",
                "group": "batch",
                "version": "v1",
                "resource": "jobs",
                "actor_identity": "nim_operator",
                "owner_kinds": ["NIMService"],
                "pod_template": True,
            },
            "Pod": {
                "api_version": "v1",
                "group": "",
                "version": "v1",
                "resource": "pods",
                "actor_identity": "kube_controller_manager",
                "owner_kinds": ["ReplicaSet", "StatefulSet", "Job"],
                "pod_template": False,
            },
        }
    )
    if (
        value["subject_sha256"] != subject_sha256
        or value["attestation_sha256"] != attestation_sha256
        or subject["schema"] != NIM_OPERATOR_SECURITY_SCHEMA
        or subject["model_id"] != record.model_id
        or subject["resource_kind"] != expected_kind
        or not isinstance(subject["pod_spec"], Mapping)
        or subject["pod_spec"]
        != {
            "automountServiceAccountToken": False,
            "hostIPC": False,
            "hostNetwork": False,
            "hostPID": False,
            "runtimeClassName": subject["pod_spec"].get("runtimeClassName"),
            "securityContext": {
                "runAsNonRoot": True,
                "seccompProfile": {"type": "RuntimeDefault"},
                "supplementalGroupsPolicy": "Strict",
            },
            "serviceAccountName": subject["pod_spec"].get("serviceAccountName"),
            "shareProcessNamespace": False,
        }
        or not isinstance(subject["pod_spec"].get("serviceAccountName"), str)
        or not subject["pod_spec"]["serviceAccountName"]
        or subject["pod_spec"].get("runtimeClassName") is not None
        and not isinstance(subject["pod_spec"]["runtimeClassName"], str)
        or not isinstance(subject["custom_resource_sha256"], str)
        or re.fullmatch(r"[a-f0-9]{64}", subject["custom_resource_sha256"]) is None
        or not isinstance(actor_identities, Mapping)
        or set(actor_identities)
        != {"custom_resource", "nim_operator", "kube_controller_manager"}
        or any(
            not isinstance(identity, Mapping)
            for identity in actor_identities.values()
        )
        or any(
            set(actor_identities[name])
            != {
                "kind",
                "username",
                "namespace",
                "service_account_name",
                "deployment_name",
                "deployment_uid",
                "container_name",
                "image",
            }
            or actor_identities[name]["kind"] != "pod-bound-service-account"
            or actor_identities[name]["username"]
            != f"system:serviceaccount:{actor_identities[name]['namespace']}:{actor_identities[name]['service_account_name']}"
            or re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", actor_identities[name]["namespace"])
            is None
            or re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?", actor_identities[name]["deployment_name"])
            is None
            or re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", actor_identities[name]["service_account_name"])
            is None
            or re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", actor_identities[name]["container_name"])
            is None
            or re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                actor_identities[name]["deployment_uid"],
            )
            is None
            or re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", actor_identities[name]["image"])
            is None
            or actor_identities[name]["image"].split("/", 1)[0]
            != subject["private_registry"]
            for name in ("custom_resource", "nim_operator")
        )
        or set(actor_identities["kube_controller_manager"])
        != {"kind", "username", "groups"}
        or actor_identities["kube_controller_manager"]["kind"]
        != "kubernetes-control-plane"
        or actor_identities["kube_controller_manager"]["username"]
        != "system:kube-controller-manager"
        or actor_identities["kube_controller_manager"]["groups"]
        != ["system:authenticated"]
        or not isinstance(descendant_resources, Mapping)
        or set(descendant_resources) != set(expected_resource_graph)
        or any(
            not isinstance(descendant_resources[kind], Mapping)
            or set(descendant_resources[kind])
            != {
                "api_version",
                "group",
                "version",
                "resource",
                "actor_identity",
                "owner_kinds",
                "pod_template",
                "name_pattern",
                "object_sha256",
            }
            or {
                key: descendant_resources[kind][key]
                for key in (
                    "api_version",
                    "group",
                    "version",
                    "resource",
                    "actor_identity",
                    "owner_kinds",
                    "pod_template",
                )
            }
            != contract
            or not isinstance(descendant_resources[kind]["object_sha256"], str)
            or re.fullmatch(r"[a-f0-9]{64}", descendant_resources[kind]["object_sha256"])
            is None
            or not isinstance(descendant_resources[kind]["name_pattern"], str)
            or len(descendant_resources[kind]["name_pattern"]) > 256
            or re.fullmatch(descendant_resources[kind]["name_pattern"], record.model_id)
            is None
            for kind, contract in expected_resource_graph.items()
        )
        or subject["volume_devices"] != "forbidden"
        or not isinstance(descendant_image, str)
        or not descendant_image.endswith("@" + runtime_digest)
        or not isinstance(subject["private_registry"], str)
        or descendant_image.split("/", 1)[0] != subject["private_registry"]
        or subject["private_registry"].endswith(".invalid")
        or not isinstance(containers, Mapping)
        or not containers
        or not isinstance(runtime_container_name, str)
        or runtime_container_name not in containers
        or any(
            not isinstance(name, str)
            or not isinstance(contract, Mapping)
            or set(contract) != {"container_class", "image", "security_context", "ports", "mounts"}
            or contract["container_class"] not in {"initContainers", "containers", "ephemeralContainers"}
            or not isinstance(contract["image"], str)
            or re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", contract["image"]) is None
            or contract["image"].split("/", 1)[0] != subject["private_registry"]
            or not isinstance(contract["security_context"], Mapping)
            or set(contract["security_context"])
            != {
                "allowPrivilegeEscalation",
                "appArmorProfile",
                "capabilities",
                "privileged",
                "readOnlyRootFilesystem",
                "runAsNonRoot",
                "runAsUser",
                "runAsGroup",
                "seccompProfile",
            }
            or contract["security_context"]["allowPrivilegeEscalation"] is not False
            or contract["security_context"]["appArmorProfile"] != {"type": "RuntimeDefault"}
            or contract["security_context"]["capabilities"] != {"add": [], "drop": ["ALL"]}
            or contract["security_context"]["privileged"] is not False
            or contract["security_context"]["readOnlyRootFilesystem"] is not True
            or contract["security_context"]["runAsNonRoot"] is not True
            or not isinstance(contract["security_context"]["runAsUser"], int)
            or isinstance(contract["security_context"]["runAsUser"], bool)
            or contract["security_context"]["runAsUser"] <= 0
            or not isinstance(contract["security_context"]["runAsGroup"], int)
            or isinstance(contract["security_context"]["runAsGroup"], bool)
            or contract["security_context"]["runAsGroup"] <= 0
            or contract["security_context"]["seccompProfile"] != {"type": "RuntimeDefault"}
            or not isinstance(contract["ports"], list)
            or any(
                not isinstance(port, Mapping)
                or "hostIP" in port
                or port.get("hostPort", 0) not in {None, 0}
                for port in contract["ports"]
            )
            or not isinstance(contract["mounts"], Mapping)
            or any(
                not isinstance(path, str)
                or not path.startswith("/")
                or not isinstance(mount, Mapping)
                or set(mount) != {"kind", "source_sha256", "sub_path", "read_only"}
                or mount["kind"] not in RESTRICTED_VOLUME_SOURCES
                or not isinstance(mount["source_sha256"], str)
                or re.fullmatch(r"[a-f0-9]{64}", mount["source_sha256"]) is None
                or not isinstance(mount["read_only"], bool)
                or mount["sub_path"] is not None
                and (
                    not isinstance(mount["sub_path"], str)
                    or mount["sub_path"].startswith("/")
                    or any(
                        part in {"", ".", ".."}
                        for part in mount["sub_path"].split("/")
                    )
                )
                for path, mount in contract["mounts"].items()
            )
            for name, contract in containers.items()
        )
        or not isinstance(subject["admission_policy"], str)
        or re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?", subject["admission_policy"])
        is None
        or not isinstance(subject["admission_policy_sha256"], str)
        or re.fullmatch(r"[a-f0-9]{64}", subject["admission_policy_sha256"]) is None
        or not isinstance(subject["volumes"], Mapping)
        or not subject["volumes"]
        or any(
            not isinstance(name, str)
            or not isinstance(volume, Mapping)
            or set(volume) != {"kind", "source_sha256"}
            or volume["kind"] not in RESTRICTED_VOLUME_SOURCES
            or not isinstance(volume["source_sha256"], str)
            or re.fullmatch(r"[a-f0-9]{64}", volume["source_sha256"]) is None
            for name, volume in subject["volumes"].items()
        )
        or not isinstance(custom_resource_image, Mapping)
        or (
            expected_kind == "NIMService"
            and (
                set(custom_resource_image) != {"repository", "tag"}
                or custom_resource_image.get("repository")
                != descendant_image.rsplit("@", 1)[0]
                or not isinstance(custom_resource_image.get("tag"), str)
                or not 1 <= len(custom_resource_image["tag"]) <= 128
            )
        )
        or (
            expected_kind == "NIMCache"
            and custom_resource_image != {"modelPuller": descendant_image}
        )
    ):
        raise CatalogError("NIM Operator restricted descendant admission subject differs")
    if containers[runtime_container_name]["image"] != descendant_image:
        raise CatalogError("NIM Operator runtime container differs from its signed descendant image")
    verified = verify_signed_attestation(
        attestation,
        trusted_attestors=trusted_attestors,
        expected_session_id=expected_session_id,
        expected_kind="nim-operator-descendant-admission",
        expected_schema=NIM_OPERATOR_SECURITY_SCHEMA,
        expected_digest="sha256:" + subject_sha256,
        expected_model_id=record.model_id,
    )
    if verified["claims"] != {
        "decision": "accepted",
        "reviewer_role": "independent-platform-security",
        "admission_policy_sha256": subject["admission_policy_sha256"],
    }:
        raise CatalogError("NIM Operator signed admission claims differ")
    return (
        subject_sha256,
        str(subject["admission_policy"]),
        descendant_image,
        custom_resource_image,
    )


def _nim_normalized_annotations(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    annotations = dict(value)
    if NIM_ENVELOPE_ANNOTATION in annotations:
        annotations[NIM_ENVELOPE_ANNOTATION] = NIM_ENVELOPE_TOKEN
    if NIM_POLICY_ANNOTATION in annotations:
        annotations[NIM_POLICY_ANNOTATION] = NIM_POLICY_TOKEN
    if NIM_IMAGE_ANNOTATION in annotations:
        annotations[NIM_IMAGE_ANNOTATION] = NIM_IMAGE_TOKEN
    for name in NIM_DYNAMIC_ANNOTATIONS.intersection(annotations):
        annotations.pop(name)
    return annotations


def _nim_normalized_labels(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    labels = dict(value)
    for name in NIM_DYNAMIC_LABELS.intersection(labels):
        labels.pop(name)
    return labels


def _nim_normalized_descendant_value(value: object) -> object:
    """Normalize controller-assigned values inside an otherwise exact object."""

    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if key in {"labels", "matchLabels"}:
                normalized[str(key)] = _nim_normalized_labels(item)
            elif key == "annotations":
                normalized[str(key)] = _nim_normalized_annotations(item)
            else:
                normalized[str(key)] = _nim_normalized_descendant_value(item)
        return normalized
    if isinstance(value, list):
        return [_nim_normalized_descendant_value(item) for item in value]
    return value


def _nim_admission_object_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return the exact security-relevant object with API-assigned fields removed."""

    metadata = value.get("metadata")
    spec = value.get("spec")
    if not isinstance(metadata, Mapping) or not isinstance(spec, Mapping):
        raise CatalogError("NIM admission object metadata/spec is absent")
    unexpected = set(value) - {"apiVersion", "kind", "metadata", "spec", "status"}
    if unexpected:
        raise CatalogError("NIM admission object carries unsigned top-level fields")
    custom_resource = value.get("kind") in {"NIMCache", "NIMService"}
    projected_metadata = {
        "name": metadata.get("name") if custom_resource else NIM_DYNAMIC_NAME_TOKEN,
        "namespace": metadata.get("namespace", "fs2-models"),
        "labels": (
            dict(metadata.get("labels", {}))
            if custom_resource and isinstance(metadata.get("labels", {}), Mapping)
            else _nim_normalized_labels(metadata.get("labels", {}))
        ),
        "annotations": _nim_normalized_annotations(metadata.get("annotations", {})),
        "finalizers": metadata.get("finalizers", []),
    }
    projected_spec = (
        json.loads(json.dumps(spec))
        if custom_resource
        else _nim_normalized_descendant_value(json.loads(json.dumps(spec)))
    )
    template = projected_spec.get("template") if isinstance(projected_spec, dict) else None
    if isinstance(template, dict) and isinstance(template.get("metadata"), dict):
        template["metadata"]["annotations"] = _nim_normalized_annotations(
            template["metadata"].get("annotations", {})
        )
    return {
        "apiVersion": value.get("apiVersion"),
        "kind": value.get("kind"),
        "metadata": projected_metadata,
        "spec": projected_spec,
    }


def _nim_admission_object_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(_nim_admission_object_projection(value))).hexdigest()


def _validate_nim_custom_resource(
    value: Mapping[str, Any],
    *,
    subject: Mapping[str, Any],
    resource_kind: str,
    record: ModelRecord,
) -> None:
    metadata = value.get("metadata")
    allowed_metadata = {
        "annotations",
        "creationTimestamp",
        "deletionGracePeriodSeconds",
        "deletionTimestamp",
        "finalizers",
        "generation",
        "labels",
        "managedFields",
        "name",
        "namespace",
        "resourceVersion",
        "uid",
    }
    if (
        value.get("apiVersion") != "apps.nvidia.com/v1alpha1"
        or value.get("kind") != resource_kind
        or not isinstance(metadata, Mapping)
        or bool(set(metadata) - allowed_metadata)
        or "ownerReferences" in metadata
        or metadata.get("name") != record.model_id
        or metadata.get("namespace", "fs2-models") != "fs2-models"
        or _nim_admission_object_sha256(value) != subject["custom_resource_sha256"]
    ):
        raise CatalogError("NIM custom resource differs from its complete signed projection")


def validate_persisted_nim_operator_root(
    value: Mapping[str, Any],
    *,
    security_envelope: Mapping[str, Any],
    trusted_attestors: Mapping[str, str],
    security_session_id: str,
    resource_kind: str,
    record: ModelRecord,
) -> str:
    """Validate a persisted NIM root before its API-assigned UID is enrolled.

    This entry point is intentionally actor-independent: the admission webhook
    has already authenticated and validated creation, while the root-enrollment
    reconciler reads the persisted object back from the Kubernetes API.  The
    reconciler therefore proves the complete signed object projection and the
    externally attested security envelope before recording the server-assigned
    UID.  It never treats the root's own annotations as authority.
    """

    subject_sha256, _, _, _ = _nim_operator_security_envelope(
        security_envelope,
        trusted_attestors=trusted_attestors,
        expected_session_id=security_session_id,
        expected_kind=resource_kind,
        record=record,
    )
    metadata = value.get("metadata")
    uid = metadata.get("uid") if isinstance(metadata, Mapping) else None
    if (
        not isinstance(uid, str)
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            uid,
        )
        is None
    ):
        raise CatalogError("persisted NIM root lacks its API-assigned UID")
    _validate_nim_custom_resource(
        value,
        subject=security_envelope["subject"],
        resource_kind=resource_kind,
        record=record,
    )
    return subject_sha256


def _nim_pod_view(value: Mapping[str, Any], *, contract: Mapping[str, Any]) -> Mapping[str, Any]:
    if contract["pod_template"] is False:
        return value
    spec = value.get("spec")
    template = spec.get("template") if isinstance(spec, Mapping) else None
    if not isinstance(template, Mapping):
        raise CatalogError("NIM controller descendant lacks its signed Pod template")
    return template


def validate_nim_operator_descendant(
    descendant: Mapping[str, Any],
    *,
    security_envelope: Mapping[str, Any],
    trusted_attestors: Mapping[str, str],
    security_session_id: str,
    resource_kind: str,
    record: ModelRecord,
) -> None:
    """Validate one actual Pod-bearing object in a signed NIM owner graph."""

    subject_sha256, _, descendant_image, _ = _nim_operator_security_envelope(
        security_envelope,
        trusted_attestors=trusted_attestors,
        expected_session_id=security_session_id,
        expected_kind=resource_kind,
        record=record,
    )
    subject = security_envelope["subject"]
    kind = descendant.get("kind")
    contract = subject["descendant_resources"].get(kind)
    metadata = descendant.get("metadata")
    if (
        not isinstance(kind, str)
        or not isinstance(contract, Mapping)
        or descendant.get("apiVersion") != contract["api_version"]
        or not isinstance(metadata, Mapping)
        or metadata.get("namespace", "fs2-models") != "fs2-models"
        or not isinstance(metadata.get("name"), str)
        or re.fullmatch(contract["name_pattern"], metadata["name"]) is None
        or _nim_admission_object_sha256(descendant) != contract["object_sha256"]
    ):
        raise CatalogError("NIM Operator descendant object differs from its signed resource projection")
    annotations = metadata.get("annotations", {})
    template = _nim_pod_view(descendant, contract=contract)
    template_metadata = template.get("metadata")
    template_annotations = (
        template_metadata.get("annotations", {})
        if isinstance(template_metadata, Mapping)
        else {}
    )
    owner_references = metadata.get("ownerReferences")
    if (
        not isinstance(annotations, Mapping)
        or not isinstance(template_annotations, Mapping)
        or annotations.get(NIM_ENVELOPE_ANNOTATION) not in {None, subject_sha256}
        or template_annotations.get(NIM_ENVELOPE_ANNOTATION)
        not in {None, subject_sha256}
        or annotations.get(NIM_POLICY_ANNOTATION)
        not in {None, subject["admission_policy"]}
        or template_annotations.get(NIM_POLICY_ANNOTATION)
        not in {None, subject["admission_policy"]}
        or annotations.get(NIM_IMAGE_ANNOTATION) not in {None, descendant_image}
        or template_annotations.get(NIM_IMAGE_ANNOTATION)
        not in {None, descendant_image}
        or not isinstance(owner_references, list)
        or len(owner_references) != 1
        or not isinstance(owner_references[0], Mapping)
        or set(owner_references[0])
        != {"apiVersion", "kind", "name", "uid", "controller", "blockOwnerDeletion"}
        or owner_references[0].get("kind") not in contract["owner_kinds"]
        or not isinstance(owner_references[0].get("name"), str)
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            str(owner_references[0].get("uid", "")),
        )
        is None
        or owner_references[0].get("controller") is not True
        or owner_references[0].get("blockOwnerDeletion") is not True
    ):
        raise CatalogError("NIM Operator descendant lost its signed selector or owner edge")
    pod = template
    metadata = pod.get("metadata")
    spec = pod.get("spec")
    if not isinstance(metadata, Mapping) or not isinstance(spec, Mapping):
        raise CatalogError("NIM Operator descendant is not a Pod object")
    observed_pod_spec = {
        "automountServiceAccountToken": spec.get("automountServiceAccountToken", True),
        "hostIPC": spec.get("hostIPC", False),
        "hostNetwork": spec.get("hostNetwork", False),
        "hostPID": spec.get("hostPID", False),
        "runtimeClassName": spec.get("runtimeClassName"),
        "securityContext": spec.get("securityContext"),
        "serviceAccountName": spec.get("serviceAccountName", "default"),
        "shareProcessNamespace": spec.get("shareProcessNamespace", False),
    }
    if (
        observed_pod_spec != subject["pod_spec"]
    ):
        raise CatalogError("NIM Operator descendant lost its signed Pod security binding")
    volumes = spec.get("volumes", [])
    if not isinstance(volumes, list):
        raise CatalogError("NIM Operator descendant volumes are invalid")
    volumes_by_name = {
        volume.get("name"): volume
        for volume in volumes
        if isinstance(volume, Mapping) and isinstance(volume.get("name"), str)
    }
    if len(volumes_by_name) != len(volumes):
        raise CatalogError("NIM Operator descendant volume identities are invalid")
    observed_volumes: dict[str, dict[str, str]] = {}
    for name, volume in volumes_by_name.items():
        sources = [key for key in RESTRICTED_VOLUME_SOURCES if key in volume]
        if set(volume) != {"name", *sources} or len(sources) != 1:
            raise CatalogError("NIM Operator descendant volume source is outside Restricted")
        source = volume[sources[0]]
        if not isinstance(source, Mapping):
            raise CatalogError("NIM Operator descendant volume source is invalid")
        if sources[0] == "emptyDir" and (
            not isinstance(source.get("sizeLimit"), str)
            or re.fullmatch(r"[1-9][0-9]*(?:Ki|Mi|Gi|Ti)", source["sizeLimit"])
            is None
        ):
            raise CatalogError("NIM Operator descendant emptyDir is not bounded")
        observed_volumes[name] = {
            "kind": sources[0],
            "source_sha256": hashlib.sha256(canonical_bytes({sources[0]: source})).hexdigest(),
        }
    if observed_volumes != subject["volumes"]:
        raise CatalogError("NIM Operator descendant volume sources differ from signed admission")
    observed: dict[str, dict[str, Any]] = {}
    for container_class in ("initContainers", "containers", "ephemeralContainers"):
        containers = spec.get(container_class, [])
        if not isinstance(containers, list):
            raise CatalogError("NIM Operator descendant container inventory is invalid")
        for container in containers:
            if not isinstance(container, Mapping) or container.get("volumeDevices"):
                raise CatalogError("NIM Operator descendant exposes a writable block device")
            ports = container.get("ports", [])
            if not isinstance(ports, list) or any(
                not isinstance(port, Mapping)
                or "hostIP" in port
                or port.get("hostPort", 0) not in {None, 0}
                for port in ports
            ):
                raise CatalogError("NIM Operator descendant exposes a host port")
            name = container.get("name")
            if not isinstance(name, str) or name in observed:
                raise CatalogError("NIM Operator descendant container identity is invalid")
            exact_mounts: dict[str, dict[str, Any]] = {}
            mounts = container.get("volumeMounts", [])
            if not isinstance(mounts, list):
                raise CatalogError("NIM Operator descendant mount inventory is invalid")
            for mount in mounts:
                if not isinstance(mount, Mapping) or not isinstance(
                    mount.get("readOnly", False), bool
                ):
                    raise CatalogError("NIM Operator descendant mount inventory is invalid")
                if "subPathExpr" in mount or set(mount) - {"name", "mountPath", "readOnly", "subPath"}:
                    raise CatalogError("NIM Operator descendant mount projection is unsafe")
                path = mount.get("mountPath")
                volume = volumes_by_name.get(mount.get("name"))
                if (
                    not isinstance(path, str)
                    or path in exact_mounts
                    or not isinstance(volume, Mapping)
                ):
                    raise CatalogError("NIM Operator descendant mount is unsafe")
                sub_path = mount.get("subPath")
                if sub_path is not None and (
                    not isinstance(sub_path, str)
                    or sub_path.startswith("/")
                    or any(part in {"", ".", ".."} for part in sub_path.split("/"))
                ):
                    raise CatalogError("NIM Operator descendant mount subPath is unsafe")
                signed_volume = observed_volumes[str(mount["name"])]
                source = volume[signed_volume["kind"]]
                exact_mounts[path] = {
                    **signed_volume,
                    "sub_path": mount.get("subPath"),
                    "read_only": mount.get("readOnly", False) is True
                    or (
                        signed_volume["kind"] == "persistentVolumeClaim"
                        and source.get("readOnly", False) is True
                    ),
                }
            observed[name] = {
                "container_class": container_class,
                "image": container.get("image"),
                "security_context": container.get("securityContext"),
                "ports": ports,
                "mounts": exact_mounts,
            }
    if (
        observed != subject["containers"]
        or observed.get(subject["runtime_container_name"], {}).get("image")
        != descendant_image
    ):
        raise CatalogError("NIM Operator actual descendants differ from signed image/security/mount admission")


def _nim_owner_reference(value: Mapping[str, Any]) -> Mapping[str, Any]:
    metadata = value.get("metadata")
    references = metadata.get("ownerReferences") if isinstance(metadata, Mapping) else None
    if not isinstance(references, list) or len(references) != 1 or not isinstance(references[0], Mapping):
        raise CatalogError("NIM owner graph requires one controller edge")
    return references[0]


def _validate_nim_owner_chain(
    descendant: Mapping[str, Any],
    owners: Sequence[Mapping[str, Any]],
    *,
    security_envelope: Mapping[str, Any],
    trusted_attestors: Mapping[str, str],
    security_session_id: str,
    resource_kind: str,
    record: ModelRecord,
) -> None:
    if not owners or len(owners) > 4:
        raise CatalogError("NIM descendant lacks a bounded live owner chain")
    subject = security_envelope["subject"]
    child = descendant
    seen_uids: set[str] = set()
    for index, owner in enumerate(owners):
        edge = _nim_owner_reference(child)
        metadata = owner.get("metadata")
        if not isinstance(metadata, Mapping):
            raise CatalogError("NIM live owner metadata is absent")
        owner_uid = metadata.get("uid")
        if (
            not isinstance(owner_uid, str)
            or owner_uid in seen_uids
            or edge.get("apiVersion") != owner.get("apiVersion")
            or edge.get("kind") != owner.get("kind")
            or edge.get("name") != metadata.get("name")
            or edge.get("uid") != owner_uid
            or edge.get("controller") is not True
            or edge.get("blockOwnerDeletion") is not True
        ):
            raise CatalogError("NIM descendant owner edge differs from the persisted owner")
        seen_uids.add(owner_uid)
        if owner.get("kind") == resource_kind:
            if index != len(owners) - 1:
                raise CatalogError("NIM owner chain continues beyond its custom resource")
            _validate_nim_custom_resource(
                owner,
                subject=subject,
                resource_kind=resource_kind,
                record=record,
            )
            return
        if owner.get("kind") not in subject["descendant_resources"]:
            raise CatalogError("NIM owner chain contains an unsigned controller kind")
        validate_nim_operator_descendant(
            owner,
            security_envelope=security_envelope,
            trusted_attestors=trusted_attestors,
            security_session_id=security_session_id,
            resource_kind=resource_kind,
            record=record,
        )
        child = owner
    raise CatalogError("NIM owner chain does not terminate at the persisted custom resource")


def _validate_nim_actor_identity(
    user_info: Mapping[str, Any],
    identity: Mapping[str, Any],
    resolved_actor_chain: Sequence[Mapping[str, Any]],
) -> None:
    if user_info.get("username") != identity["username"]:
        raise CatalogError("NIM admission actor differs from its signed identity")
    if identity["kind"] == "kubernetes-control-plane":
        groups = user_info.get("groups", [])
        if not isinstance(groups, list) or any(
            group not in groups for group in identity["groups"]
        ):
            raise CatalogError("NIM controller-manager group identity differs")
        if resolved_actor_chain:
            raise CatalogError("Kubernetes control-plane actor may not supply a workload identity")
        return
    if len(resolved_actor_chain) != 3:
        raise CatalogError("NIM service-account actor lacks its live Pod owner chain")
    pod, replica_set, deployment = resolved_actor_chain
    pod_metadata = pod.get("metadata")
    pod_spec = pod.get("spec")
    extra = user_info.get("extra")
    if not isinstance(pod_metadata, Mapping) or not isinstance(pod_spec, Mapping) or not isinstance(extra, Mapping):
        raise CatalogError("NIM service-account actor identity is incomplete")
    pod_names = extra.get("authentication.kubernetes.io/pod-name", [])
    pod_uids = extra.get("authentication.kubernetes.io/pod-uid", [])
    if (
        pod.get("apiVersion") != "v1"
        or pod.get("kind") != "Pod"
        or pod_metadata.get("namespace") != identity["namespace"]
        or pod_names != [pod_metadata.get("name")]
        or pod_uids != [pod_metadata.get("uid")]
        or pod_spec.get("serviceAccountName") != identity["service_account_name"]
    ):
        raise CatalogError("NIM service-account token is not bound to its persisted Pod")
    pod_containers = pod_spec.get("containers", [])
    if not isinstance(pod_containers, list) or not any(
        isinstance(container, Mapping)
        and container.get("name") == identity["container_name"]
        and container.get("image") == identity["image"]
        for container in pod_containers
    ):
        raise CatalogError("NIM actor Pod does not run the signed controller image")
    pod_owner = _nim_owner_reference(pod)
    rs_metadata = replica_set.get("metadata")
    if (
        replica_set.get("apiVersion") != "apps/v1"
        or replica_set.get("kind") != "ReplicaSet"
        or not isinstance(rs_metadata, Mapping)
        or pod_owner.get("apiVersion") != "apps/v1"
        or pod_owner.get("kind") != "ReplicaSet"
        or pod_owner.get("name") != rs_metadata.get("name")
        or pod_owner.get("uid") != rs_metadata.get("uid")
        or pod_owner.get("controller") is not True
        or pod_owner.get("blockOwnerDeletion") is not True
    ):
        raise CatalogError("NIM actor Pod is not owned by the resolved ReplicaSet")
    rs_owner = _nim_owner_reference(replica_set)
    deployment_metadata = deployment.get("metadata")
    deployment_spec = deployment.get("spec")
    deployment_template = (
        deployment_spec.get("template") if isinstance(deployment_spec, Mapping) else None
    )
    deployment_pod_spec = (
        deployment_template.get("spec") if isinstance(deployment_template, Mapping) else None
    )
    deployment_containers = (
        deployment_pod_spec.get("containers", [])
        if isinstance(deployment_pod_spec, Mapping)
        else []
    )
    if (
        deployment.get("apiVersion") != "apps/v1"
        or deployment.get("kind") != "Deployment"
        or not isinstance(deployment_metadata, Mapping)
        or deployment_metadata.get("namespace") != identity["namespace"]
        or deployment_metadata.get("name") != identity["deployment_name"]
        or deployment_metadata.get("uid") != identity["deployment_uid"]
        or rs_owner.get("apiVersion") != "apps/v1"
        or rs_owner.get("kind") != "Deployment"
        or rs_owner.get("name") != identity["deployment_name"]
        or rs_owner.get("uid") != identity["deployment_uid"]
        or rs_owner.get("controller") is not True
        or rs_owner.get("blockOwnerDeletion") is not True
        or not isinstance(deployment_containers, list)
        or not any(
            isinstance(container, Mapping)
            and container.get("name") == identity["container_name"]
            and container.get("image") == identity["image"]
            for container in deployment_containers
        )
    ):
        raise CatalogError("NIM actor ReplicaSet is not owned by the signed controller Deployment")


def validate_nim_operator_admission_review(
    review: Mapping[str, Any],
    *,
    security_envelope: Mapping[str, Any],
    trusted_attestors: Mapping[str, str],
    security_session_id: str,
    resource_kind: str,
    record: ModelRecord,
    resolved_owner_chain: Sequence[Mapping[str, Any]] = (),
    resolved_actor_chain: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Validate the exact AdmissionReview for a NIM CR or emitted Pod.

    The admission server must select ``security_envelope`` by the immutable
    subject-hash annotation before calling this function. A missing envelope,
    annotation, container, or writable-path binding therefore denies creation;
    the validator never infers a policy from the primary runtime image.
    """

    if set(review) != {"apiVersion", "kind", "request"} or (
        review.get("apiVersion") != "admission.k8s.io/v1"
        or review.get("kind") != "AdmissionReview"
    ):
        raise CatalogError("NIM Operator admission review envelope differs")
    request = review["request"]
    if not isinstance(request, Mapping):
        raise CatalogError("NIM Operator admission request is absent")
    uid = request.get("uid")
    resource = request.get("resource")
    admitted_object = request.get("object")
    user_info = request.get("userInfo")
    if (
        not isinstance(uid, str)
        or not uid
        or request.get("operation") not in {"CREATE", "UPDATE"}
        or request.get("namespace") != "fs2-models"
        or not isinstance(resource, Mapping)
        or not isinstance(user_info, Mapping)
        or not isinstance(user_info.get("username"), str)
        or not isinstance(admitted_object, Mapping)
        or not isinstance(admitted_object.get("metadata"), Mapping)
        or admitted_object["metadata"].get("namespace", "fs2-models") != "fs2-models"
    ):
        raise CatalogError("NIM Operator admission request does not target one model resource")
    subject_sha256, admission_policy, descendant_image, custom_resource_image = _nim_operator_security_envelope(
        security_envelope,
        trusted_attestors=trusted_attestors,
        expected_session_id=security_session_id,
        expected_kind=resource_kind,
        record=record,
    )
    signed_subject = security_envelope["subject"]
    descendant_contract = next(
        (
            contract
            for contract in signed_subject["descendant_resources"].values()
            if resource
            == {
                "group": contract["group"],
                "version": contract["version"],
                "resource": contract["resource"],
            }
        ),
        None,
    )
    if descendant_contract is not None:
        identity = signed_subject["actor_identities"][
            descendant_contract["actor_identity"]
        ]
        _validate_nim_actor_identity(user_info, identity, resolved_actor_chain)
        validate_nim_operator_descendant(
            admitted_object,
            security_envelope=security_envelope,
            trusted_attestors=trusted_attestors,
            security_session_id=security_session_id,
            resource_kind=resource_kind,
            record=record,
        )
        _validate_nim_owner_chain(
            admitted_object,
            resolved_owner_chain,
            security_envelope=security_envelope,
            trusted_attestors=trusted_attestors,
            security_session_id=security_session_id,
            resource_kind=resource_kind,
            record=record,
        )
    else:
        _validate_nim_actor_identity(
            user_info,
            signed_subject["actor_identities"]["custom_resource"],
            resolved_actor_chain,
        )
        expected_resource = {
            "NIMCache": "nimcaches",
            "NIMService": "nimservices",
        }.get(resource_kind)
        if (
            expected_resource is None
            or resource
            != {
                "group": "apps.nvidia.com",
                "version": "v1alpha1",
                "resource": expected_resource,
            }
            or admitted_object.get("apiVersion") != "apps.nvidia.com/v1alpha1"
            or admitted_object.get("kind") != resource_kind
        ):
            raise CatalogError("NIM Operator custom-resource admission target differs")
        metadata = admitted_object["metadata"]
        annotations = metadata.get("annotations")
        spec = admitted_object.get("spec")
        if (
            metadata.get("name") != record.model_id
            or not isinstance(annotations, Mapping)
            or annotations.get("fs2-serve.nebius.ai/operator-security-envelope-sha256")
            != subject_sha256
            or annotations.get("fs2-serve.nebius.ai/operator-security-admission-policy")
            != admission_policy
            or annotations.get("fs2-serve.nebius.ai/expected-descendant-image")
            != descendant_image
            or not isinstance(spec, Mapping)
        ):
            raise CatalogError("NIM Operator custom resource lost its signed security binding")
        _validate_nim_custom_resource(
            admitted_object,
            subject=signed_subject,
            resource_kind=resource_kind,
            record=record,
        )
        if resolved_owner_chain:
            raise CatalogError("NIM custom-resource admission may not supply an owner chain")
        if request.get("operation") == "CREATE" and "uid" in metadata:
            raise CatalogError("NIM custom-resource UID must be assigned by the API server")
        if request.get("operation") == "UPDATE":
            old_object = request.get("oldObject")
            old_metadata = old_object.get("metadata") if isinstance(old_object, Mapping) else None
            if (
                not isinstance(old_metadata, Mapping)
                or not isinstance(metadata.get("uid"), str)
                or metadata.get("uid") != old_metadata.get("uid")
            ):
                raise CatalogError("NIM custom-resource update changed its persisted UID")
            _validate_nim_custom_resource(
                old_object,
                subject=signed_subject,
                resource_kind=resource_kind,
                record=record,
            )
        if resource_kind == "NIMCache":
            source = spec.get("source")
            ngc = source.get("ngc") if isinstance(source, Mapping) else None
            if not isinstance(ngc, Mapping) or ngc.get("modelPuller") != custom_resource_image["modelPuller"]:
                raise CatalogError("NIMCache custom resource changed its digest-pinned puller")
        else:
            image = spec.get("image")
            if (
                not isinstance(image, Mapping)
                or image.get("repository") != custom_resource_image["repository"]
                or image.get("tag") != custom_resource_image["tag"]
                or spec.get("replicas") != 0
                or annotations.get("fs2-serve.nebius.ai/route-state")
                != "disabled-pending-pod-imageid-and-semantic-receipts"
            ):
                raise CatalogError("NIMService custom resource is not fail-closed on admission")
    return {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "response": {"uid": uid, "allowed": True},
    }


def _content_path(
    record: ModelRecord, artifact_uri: str, capability: BackendCapability
) -> str:
    parsed = urlsplit(artifact_uri)
    content_digest = parsed.path.rsplit("/", 1)[-1]
    scheme = (
        "nvme"
        if capability.storage_mode == "local-nvme"
        else "pvc"
        if capability.storage_mode == "provider-block-pvc"
        else "sfs"
    )
    canonical_content_uri(
        artifact_uri,
        model_id=record.model_id,
        content_digest=content_digest,
        scheme=scheme,
    )
    if capability.storage_mode == "provider-block-pvc":
        claim_prefix = "/qwen3-8b-weights"
        if not parsed.path.startswith(claim_prefix + "/"):
            raise CatalogError("provider block URI differs from the exact claim identity")
        storage = capability.storage
        assert storage is not None
        return storage["mount_path"] + parsed.path.removeprefix(claim_prefix)
    return parsed.path


def _container(
    record: ModelRecord,
    artifact_uri: str,
    capability: BackendCapability,
    prerequisites: PrerequisiteBinding,
) -> dict[str, Any]:
    value = record.to_dict()
    gpu_count = value["resources"]["gpu"]["count"]
    resources = {
        "requests": {
            "cpu": f"{value['resources']['cpu_millis']}m",
            "memory": str(value["resources"]["memory_bytes"]),
            "nvidia.com/gpu": gpu_count,
        },
        "limits": {
            "cpu": f"{value['resources']['cpu_millis']}m",
            "memory": str(value["resources"]["memory_bytes"]),
            "nvidia.com/gpu": gpu_count,
        },
    }
    content_path = _content_path(record, artifact_uri, capability)
    runtime_command = [
        content_path if item == MODEL_CONTENT_PATH_TOKEN else item
        for item in value["runtime"]["command"]
    ]
    if value["runtime"]["kind"] == "vllm":
        if runtime_command.count(content_path) != 1:
            raise CatalogError("vLLM workload must consume the exact mounted content path")
        try:
            served_name_index = runtime_command.index("--served-model-name") + 1
        except ValueError as exc:
            raise CatalogError(
                "vLLM workload must expose the stable catalog model ID"
            ) from exc
        if (
            served_name_index >= len(runtime_command)
            or runtime_command[served_name_index] != record.model_id
        ):
            raise CatalogError("vLLM workload must expose the stable catalog model ID")
        repository = value["model"]["source"]["repository"]
        if repository in runtime_command or "--revision" in runtime_command:
            raise CatalogError("vLLM workload may not redownload the pinned staged artifact")
    container: dict[str, Any] = {
        "name": "model",
        "image": _runtime_image(record),
        "imagePullPolicy": "IfNotPresent",
        "ports": [{"name": "http", "containerPort": 8000, "protocol": "TCP"}],
        "resources": resources,
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "runAsNonRoot": True,
            "runAsUser": 1000,
            "runAsGroup": 1000,
            "capabilities": {"drop": ["ALL"]},
        },
        "volumeMounts": [
            {
                "name": "model-cache",
                "mountPath": value["cache"]["local_path"],
                "readOnly": True,
            },
            {"name": "tmp", "mountPath": "/tmp"},
        ],
        "env": [
            {"name": "FS2_MODEL_ID", "value": record.model_id},
            {"name": "HOME", "value": "/tmp/fs2-home"},
            {"name": "XDG_CACHE_HOME", "value": "/tmp/fs2-cache/xdg"},
            {
                "name": "FS2_MODEL_CONTENT_PATH",
                "value": content_path,
            },
        ],
    }
    if record.model_id in MOUNTED_CONTENT_MODELS:
        container["env"].extend(
            [
                {"name": "HF_HUB_OFFLINE", "value": "1"},
                {"name": "HF_DATASETS_OFFLINE", "value": "1"},
                {"name": "TRANSFORMERS_OFFLINE", "value": "1"},
            ]
        )
    if runtime_command and runtime_command[0].startswith("-"):
        # Digest-pinned vLLM images provide the executable via ENTRYPOINT. CXR's
        # catalog contract is an argv-only contract and must not replace it.
        container["args"] = runtime_command
    else:
        container["command"] = runtime_command
    readiness = value["interface"]["readiness"]
    if readiness["method"] == "GET":
        container["readinessProbe"] = {
            "httpGet": {"path": readiness["path"], "port": "http"},
            "periodSeconds": 5,
            "timeoutSeconds": 2,
            "failureThreshold": max(1, readiness["timeout_seconds"] // 5),
        }
    if value["runtime"]["kind"] == "nim":
        prerequisites.require([NGC_RUNTIME_SECRET])
        runtime_secret = prerequisites.resource(NGC_RUNTIME_SECRET)
        container["env"].append(
            {
                "name": "NGC_API_KEY",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": runtime_secret["name"],
                        "key": "NGC_API_KEY",
                    }
                },
            }
        )
    return container


def _pod_spec(
    record: ModelRecord,
    artifact_uri: str,
    capability: BackendCapability,
    prerequisites: PrerequisiteBinding,
) -> dict[str, Any]:
    value = record.to_dict()
    require_local_capability(
        record,
        capability,
        storage_modes={"provider-block-pvc", "sfs-pvc", "local-nvme"},
    )
    required = [MODEL_RUNTIME_SERVICE_ACCOUNT]
    if value["runtime"]["kind"] == "nim":
        required.extend([NGC_PULL_SECRET, NGC_RUNTIME_SECRET])
    prerequisites.require(required)
    service_account = prerequisites.resource(MODEL_RUNTIME_SERVICE_ACCOUNT)
    pod: dict[str, Any] = {
        "serviceAccountName": service_account["name"],
        "automountServiceAccountToken": False,
        "terminationGracePeriodSeconds": 90,
        "nodeSelector": capability.node_selector,
        "tolerations": capability.tolerations,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 1000,
            "runAsGroup": 1000,
            "fsGroup": 1000,
            "seccompProfile": {"type": "RuntimeDefault"},
            "supplementalGroupsPolicy": "Strict",
        },
        "containers": [_container(record, artifact_uri, capability, prerequisites)],
        "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "8Gi"}}],
    }
    storage = capability.storage
    assert storage is not None
    if capability.storage_mode == "local-nvme":
        node_identity = capability.node_identity
        local_pv_pvc = capability.local_pv_pvc
        if node_identity is None or local_pv_pvc is None:
            raise CatalogError("node-local workload lacks reviewed local-PV/PVC lifecycle evidence")
        claim = local_pv_pvc["persistent_volume_claim"]
        if claim["namespace"] != "fs2-models":
            raise CatalogError("node-local model PVC is outside the model namespace")
        pod["volumes"].insert(
            0,
            {
                "name": "model-cache",
                "persistentVolumeClaim": {"claimName": claim["name"]},
            },
        )
    elif capability.storage_mode == "provider-block-pvc":
        provider = capability.provider_block_pvc
        if provider is None:
            raise CatalogError("provider block workload lacks its exact live claim identity")
        claim = provider["claim"]
        if claim["namespace"] != "fs2-models":
            raise CatalogError("provider block claim is outside the model namespace")
        pod["volumes"].insert(
            0,
            {
                "name": "model-cache",
                "persistentVolumeClaim": {
                    "claimName": claim["name"],
                    "readOnly": True,
                },
            },
        )
        pod["containers"][0]["volumeMounts"][0]["mountPath"] = storage["mount_path"]
        pod["containers"][0]["volumeMounts"][0]["readOnly"] = True
    else:
        requirement_id = storage["pvc_requirement_id"]
        prerequisites.require([requirement_id])
        pvc = prerequisites.resource(requirement_id)
        pod["volumes"].insert(
            0,
            {
                "name": "model-cache",
                "persistentVolumeClaim": {"claimName": pvc["name"]},
            },
        )
        pod["containers"][0]["volumeMounts"][0]["mountPath"] = storage["mount_path"]
    if value["runtime"]["kind"] == "nim":
        pull_secret = prerequisites.resource(NGC_PULL_SECRET)
        pod["imagePullSecrets"] = [{"name": pull_secret["name"]}]
    return pod


def _metadata(record: ModelRecord, capability: BackendCapability) -> dict[str, Any]:
    runtime_tuple_digest = capability.runtime_tuple_digest
    strong_sha256(runtime_tuple_digest, "workload runtime tuple digest")
    result = {
        "labels": {
            "app.kubernetes.io/name": record.model_id,
            "app.kubernetes.io/part-of": "fs2-serve",
            "app.kubernetes.io/managed-by": "fs2-serve-models",
            "fs2-serve.nebius.ai/model-id": record.model_id,
        },
        "annotations": {
            "fs2-serve.nebius.ai/model-digest": record.digest,
            "fs2-serve.nebius.ai/runtime-tuple-digest": runtime_tuple_digest,
            "fs2-serve.nebius.ai/backend-identity-digest": capability.to_dict()[
                "backend_identity_digest"
            ],
            "fs2-serve.nebius.ai/admission-scope": capability.admission_scope,
            "fs2-serve.nebius.ai/node-scaler-owner": record.to_dict()["resources"]["scaler_owner"],
        },
    }
    if record.model_id in MOUNTED_CONTENT_MODELS:
        result["annotations"].update(
            {
                "fs2-serve.nebius.ai/runtime-artifact-source": (
                    "exact-mounted-content-address-only"
                ),
                "fs2-serve.nebius.ai/runtime-startup-egress": "deny-all",
                "fs2-serve.nebius.ai/runtime-network-policy-name": (
                    f"{record.model_id}-runtime-deny-egress"
                ),
            }
        )
    node_identity = capability.node_identity
    if node_identity is not None:
        result["annotations"].update(
            {
                "fs2-serve.nebius.ai/serving-node-name": node_identity["name"],
                "fs2-serve.nebius.ai/serving-node-uid": node_identity["uid"],
                "fs2-serve.nebius.ai/serving-node-provider-id-sha256": node_identity[
                    "provider_id_sha256"
                ],
            }
        )
    local_pv_pvc = capability.local_pv_pvc
    if local_pv_pvc is not None:
        result["annotations"].update(
            {
                "fs2-serve.nebius.ai/local-pv-pvc-lifecycle-receipt-digest": local_pv_pvc[
                    "lifecycle_receipt_digest"
                ],
                "fs2-serve.nebius.ai/local-pv-pvc-activation-generation": str(
                    local_pv_pvc["activation_generation"]
                ),
                "fs2-serve.nebius.ai/local-pvc-uid": local_pv_pvc[
                    "persistent_volume_claim"
                ]["uid"],
            }
        )
    provider_block_pvc = capability.provider_block_pvc
    if provider_block_pvc is not None:
        result["annotations"].update(
            {
                "fs2-serve.nebius.ai/provider-block-lifecycle-receipt-digest": provider_block_pvc[
                    "lifecycle_receipt_digest"
                ],
                "fs2-serve.nebius.ai/provider-block-pvc-uid": provider_block_pvc["claim"][
                    "uid"
                ],
                "fs2-serve.nebius.ai/provider-block-volume-name": provider_block_pvc[
                    "claim"
                ]["volume_name"],
            }
        )
    return result


def render_runtime_network_policy(
    record: ModelRecord, *, namespace: str
) -> dict[str, Any]:
    """Render the exact deny-all egress policy required for mounted-content startup."""

    if record.model_id not in MOUNTED_CONTENT_MODELS:
        raise CatalogError("runtime deny-egress policy is reviewed only for mounted-content models")
    if namespace != "fs2-models":
        raise CatalogError("model runtime NetworkPolicy is owned only in fs2-models")
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": f"{record.model_id}-runtime-deny-egress",
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/part-of": "fs2-serve",
                "app.kubernetes.io/managed-by": "fs2-serve-models",
                "fs2-serve.nebius.ai/model-id": record.model_id,
            },
            "annotations": {
                "fs2-serve.nebius.ai/network-contract": RUNTIME_NETWORK_POLICY_SCHEMA,
                "fs2-serve.nebius.ai/model-digest": record.digest,
            },
        },
        "spec": {
            "podSelector": {
                "matchLabels": {"fs2-serve.nebius.ai/model-id": record.model_id}
            },
            "policyTypes": ["Egress"],
            "egress": [],
        },
    }


def render_native_http_workload(
    record: ModelRecord,
    *,
    prerequisites: PrerequisiteBinding,
    namespace: str,
    artifact_uri: str,
    backend_capability: BackendCapability,
) -> dict[str, Any]:
    """Render a conventional digest-pinned Deployment and ClusterIP Service."""

    value = record.to_dict()
    if value["runtime"]["kind"] == "nim":
        raise CatalogError("NIM runtimes must use the NIMService/NIMCache adapter")
    require_local_capability(
        record,
        backend_capability,
        storage_modes={"provider-block-pvc", "sfs-pvc", "local-nvme"},
    )
    if namespace != "fs2-models":
        raise CatalogError("native model Services are owned only in namespace fs2-models")
    if value["interface"]["execution_mode"] != "http":
        raise CatalogError("native HTTP adapter cannot serve a batch-only model")
    metadata = _metadata(record, backend_capability)
    deployment_annotations = {
        **metadata["annotations"],
        **_replica_annotations("apps/v1", "Deployment"),
    }
    labels = metadata["labels"]
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": record.model_id,
            "namespace": namespace,
            "labels": labels,
            "annotations": deployment_annotations,
        },
        "spec": {
            "replicas": 0,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": {"fs2-serve.nebius.ai/model-id": record.model_id}},
            "template": {
                "metadata": {"labels": labels, "annotations": metadata["annotations"]},
                "spec": _pod_spec(
                    record, artifact_uri, backend_capability, prerequisites
                ),
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": record.model_id, "namespace": namespace, **metadata},
        "spec": {
            "type": "ClusterIP",
            "selector": {"fs2-serve.nebius.ai/model-id": record.model_id},
            "ports": [{"name": "http", "port": 8000, "targetPort": "http"}],
        },
    }
    items = [deployment, service]
    if record.model_id in MOUNTED_CONTENT_MODELS:
        items.append(render_runtime_network_policy(record, namespace=namespace))
    return {"apiVersion": "v1", "kind": "List", "items": items}


def render_kserve_standard_workload(
    record: ModelRecord,
    *,
    prerequisites: PrerequisiteBinding,
    namespace: str,
    artifact_uri: str,
    backend_capability: BackendCapability,
) -> dict[str, Any]:
    """Render KServe Standard mode with a custom digest-pinned predictor container."""

    value = record.to_dict()
    require_local_capability(
        record,
        backend_capability,
        storage_modes={"provider-block-pvc", "sfs-pvc", "local-nvme"},
    )
    if namespace != "fs2-models":
        raise CatalogError("KServe model workloads are owned only in namespace fs2-models")
    if value["runtime"]["kind"] not in {"vllm", "custom", "diffusers"}:
        raise CatalogError("KServe custom predictor is not the NIM Operator adapter")
    if value["interface"]["execution_mode"] != "http":
        raise CatalogError("KServe HTTP adapter cannot serve a batch-only model")
    metadata = _metadata(record, backend_capability)
    annotations = dict(metadata["annotations"])
    annotations["serving.kserve.io/deploymentMode"] = "Standard"
    pod = _pod_spec(record, artifact_uri, backend_capability, prerequisites)
    result = {
        "apiVersion": "serving.kserve.io/v1beta1",
        "kind": "InferenceService",
        "metadata": {
            "name": record.model_id,
            "namespace": namespace,
            "labels": metadata["labels"],
            "annotations": annotations,
        },
        "spec": {
            "predictor": {
                "serviceAccountName": pod["serviceAccountName"],
                "automountServiceAccountToken": False,
                "securityContext": pod["securityContext"],
                "terminationGracePeriodSeconds": pod["terminationGracePeriodSeconds"],
                "nodeSelector": pod["nodeSelector"],
                "tolerations": pod["tolerations"],
                "containers": pod["containers"],
                "volumes": pod["volumes"],
            }
        },
    }
    if "imagePullSecrets" in pod:
        result["spec"]["predictor"]["imagePullSecrets"] = pod["imagePullSecrets"]
    return result


def render_nim_operator_cache(
    record: ModelRecord,
    *,
    prerequisites: PrerequisiteBinding,
    namespace: str,
    backend_capability: BackendCapability,
    llm_engine: str | None = None,
    security_envelope: Mapping[str, Any] | None = None,
    trusted_attestors: Mapping[str, str] | None = None,
    security_session_id: str | None = None,
) -> dict[str, Any]:
    """Render NIMCache with one NIM-Operator-owned PVC path and digest-pinned puller."""

    value = record.to_dict()
    require_local_capability(
        record, backend_capability, storage_modes={"nimcache-pvc"}
    )
    if namespace != "fs2-models":
        raise CatalogError("NIMCache resources are owned only in namespace fs2-models")
    if value["runtime"]["kind"] != "nim" or value["cache"]["owner"] != "nim-operator-nimcache":
        raise CatalogError("NIMCache adapter requires an exact NIM record and owner")
    if llm_engine not in {None, "vllm", "sglang"}:
        raise CatalogError("NIMCache LLM engine is outside the Operator contract")
    security_envelope_sha256, admission_policy, descendant_image, custom_resource_image = _nim_operator_security_envelope(
        security_envelope,
        trusted_attestors=trusted_attestors,
        expected_session_id=security_session_id,
        expected_kind="NIMCache",
        record=record,
    )
    prerequisites.require([NGC_PULL_SECRET, NGC_RUNTIME_SECRET, SHARED_CACHE_PVC])
    pull_secret = prerequisites.resource(NGC_PULL_SECRET)
    runtime_secret = prerequisites.resource(NGC_RUNTIME_SECRET)
    pvc = prerequisites.resource(SHARED_CACHE_PVC)
    ngc_source: dict[str, Any] = {
        "modelPuller": custom_resource_image["modelPuller"],
        "pullSecret": pull_secret["name"],
        "authSecret": runtime_secret["name"],
    }
    if llm_engine is not None:
        ngc_source["model"] = {
            "engine": llm_engine,
            "tensorParallelism": str(value["resources"]["gpu"]["count"]),
        }
    metadata = _metadata(record, backend_capability)
    annotations = dict(metadata["annotations"])
    annotations.update(
        {
            "fs2-serve.nebius.ai/cache-owner": "nim-operator-nimcache",
            "fs2-serve.nebius.ai/cache-pvc-requirement-id": SHARED_CACHE_PVC,
            "fs2-serve.nebius.ai/expected-runtime-image-digest": value["runtime"][
                "image"
            ]["digest"],
            "fs2-serve.nebius.ai/operator-security-envelope-sha256": security_envelope_sha256,
            "fs2-serve.nebius.ai/operator-security-admission-policy": admission_policy,
            "fs2-serve.nebius.ai/expected-descendant-image": descendant_image,
        }
    )
    result = {
        "apiVersion": "apps.nvidia.com/v1alpha1",
        "kind": "NIMCache",
        "metadata": {
            "name": record.model_id,
            "namespace": namespace,
            "labels": metadata["labels"],
            "annotations": annotations,
        },
        "spec": {
            "source": {"ngc": ngc_source},
            "storage": {"pvc": {"create": False, "name": pvc["name"]}},
            "nodeSelector": backend_capability.node_selector,
            "tolerations": backend_capability.tolerations,
        },
    }
    _validate_nim_custom_resource(
        result,
        subject=security_envelope["subject"],
        resource_kind="NIMCache",
        record=record,
    )
    return result


def render_nim_operator_service(
    record: ModelRecord,
    *,
    prerequisites: PrerequisiteBinding,
    namespace: str,
    backend_capability: BackendCapability,
    nim_cache_name: str | None = None,
    profile: str = "",
    security_envelope: Mapping[str, Any] | None = None,
    trusted_attestors: Mapping[str, str] | None = None,
    security_session_id: str | None = None,
) -> dict[str, Any]:
    """Render a disabled NIMService candidate pending post-reconcile evidence."""

    value = record.to_dict()
    require_local_capability(
        record, backend_capability, storage_modes={"nimcache-pvc"}
    )
    if namespace != "fs2-models":
        raise CatalogError("NIMService resources are owned only in namespace fs2-models")
    if value["runtime"]["kind"] != "nim":
        raise CatalogError("NIMService adapter requires an exact NIM record")
    if value["interface"]["execution_mode"] != "http":
        raise CatalogError("batch-only NIM records require the async Job adapter")
    if nim_cache_name not in {None, record.model_id}:
        raise CatalogError("NIMService cache identity differs from the exact model record")
    if not isinstance(profile, str) or len(profile) > 256:
        raise CatalogError("NIM profile must be bounded text")
    security_envelope_sha256, admission_policy, descendant_image, custom_resource_image = _nim_operator_security_envelope(
        security_envelope,
        trusted_attestors=trusted_attestors,
        expected_session_id=security_session_id,
        expected_kind="NIMService",
        record=record,
    )
    prerequisites.require([NGC_PULL_SECRET, NGC_RUNTIME_SECRET, SHARED_CACHE_PVC])
    pull_secret = prerequisites.resource(NGC_PULL_SECRET)
    runtime_secret = prerequisites.resource(NGC_RUNTIME_SECRET)
    image = backend_capability.nim_image
    if image is None or image["tag"] != custom_resource_image["tag"]:
        raise CatalogError("NIMService requires an exact tag-to-digest evidence reference")
    metadata = _metadata(record, backend_capability)
    annotations = {
        **metadata["annotations"],
        **_replica_annotations("apps.nvidia.com/v1alpha1", "NIMService"),
    }
    annotations.update(
        {
            "fs2-serve.nebius.ai/expected-runtime-image-digest": image[
                "expected_digest"
            ],
            "fs2-serve.nebius.ai/nim-tag-binding-receipt-digest": image[
                "tag_binding_receipt_digest"
            ],
            "fs2-serve.nebius.ai/route-state": "disabled-pending-pod-imageid-and-semantic-receipts",
            "fs2-serve.nebius.ai/cache-pvc-requirement-id": SHARED_CACHE_PVC,
            "fs2-serve.nebius.ai/operator-security-envelope-sha256": security_envelope_sha256,
            "fs2-serve.nebius.ai/operator-security-admission-policy": admission_policy,
            "fs2-serve.nebius.ai/expected-descendant-image": descendant_image,
        }
    )
    gpu_count = value["resources"]["gpu"]["count"]
    resources = {
        "requests": {
            "cpu": f"{value['resources']['cpu_millis']}m",
            "memory": str(value["resources"]["memory_bytes"]),
            "nvidia.com/gpu": gpu_count,
        },
        "limits": {
            "cpu": f"{value['resources']['cpu_millis']}m",
            "memory": str(value["resources"]["memory_bytes"]),
            "nvidia.com/gpu": gpu_count,
        },
    }
    result = {
        "apiVersion": "apps.nvidia.com/v1alpha1",
        "kind": "NIMService",
        "metadata": {
            "name": record.model_id,
            "namespace": namespace,
            "labels": metadata["labels"],
            "annotations": annotations,
        },
        "spec": {
            "image": {
                "repository": custom_resource_image["repository"],
                "tag": custom_resource_image["tag"],
                "pullPolicy": "Always",
                "pullSecrets": [pull_secret["name"]],
            },
            "authSecret": runtime_secret["name"],
            "command": list(value["runtime"]["command"]),
            "storage": {
                "nimCache": {
                    "name": record.model_id,
                    "profile": profile,
                },
                "readOnly": True,
            },
            "nodeSelector": backend_capability.node_selector,
            "tolerations": backend_capability.tolerations,
            "resources": resources,
            "replicas": 0,
            "inferencePlatform": "standalone",
            "expose": {"service": {"type": "ClusterIP", "port": 8000}},
        },
    }
    _validate_nim_custom_resource(
        result,
        subject=security_envelope["subject"],
        resource_kind="NIMService",
        record=record,
    )
    return result
