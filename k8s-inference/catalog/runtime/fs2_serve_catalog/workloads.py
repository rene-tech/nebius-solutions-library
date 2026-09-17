#!/usr/bin/env python3
"""Digest-pinned native and KServe Standard model workload adapters."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping
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
NIM_OPERATOR_SECURITY_SCHEMA = "fs2-serve.nebius.ai/nim-operator-security-subject/v2"


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
        "operator_image_digest",
        "private_registry",
        "descendant_image",
        "custom_resource_image",
        "runtime_container_name",
        "admission_policy",
        "admission_policy_sha256",
        "pod_security_context",
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
    if (
        value["subject_sha256"] != subject_sha256
        or value["attestation_sha256"] != attestation_sha256
        or subject["schema"] != NIM_OPERATOR_SECURITY_SCHEMA
        or subject["model_id"] != record.model_id
        or subject["resource_kind"] != expected_kind
        or subject["pod_security_context"]
        != {
            "runAsNonRoot": True,
            "seccompProfile": {"type": "RuntimeDefault"},
            "supplementalGroupsPolicy": "Strict",
        }
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
            or set(contract) != {"image", "security_context", "writable_mounts"}
            or not isinstance(contract["image"], str)
            or re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", contract["image"]) is None
            or contract["image"].split("/", 1)[0] != subject["private_registry"]
            or not isinstance(contract["security_context"], Mapping)
            or set(contract["security_context"])
            != {
                "allowPrivilegeEscalation",
                "capabilities",
                "privileged",
                "readOnlyRootFilesystem",
                "runAsNonRoot",
                "runAsUser",
                "runAsGroup",
            }
            or contract["security_context"]["allowPrivilegeEscalation"] is not False
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
            or not isinstance(contract["writable_mounts"], Mapping)
            or any(
                not isinstance(path, str)
                or not path.startswith("/")
                or not isinstance(mount, Mapping)
                or set(mount) != {"kind", "reference", "sub_path"}
                or mount["kind"] not in {"emptyDir", "persistentVolumeClaim"}
                or not isinstance(mount["reference"], str)
                or mount["sub_path"] is not None
                and (
                    not isinstance(mount["sub_path"], str)
                    or mount["sub_path"].startswith("/")
                    or any(
                        part in {"", ".", ".."}
                        for part in mount["sub_path"].split("/")
                    )
                )
                for path, mount in contract["writable_mounts"].items()
            )
            for name, contract in containers.items()
        )
        or not isinstance(subject["admission_policy"], str)
        or re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?", subject["admission_policy"])
        is None
        or not isinstance(subject["admission_policy_sha256"], str)
        or re.fullmatch(r"[a-f0-9]{64}", subject["admission_policy_sha256"]) is None
        or not isinstance(subject["operator_image_digest"], str)
        or re.fullmatch(r"[a-f0-9]{64}", subject["operator_image_digest"]) is None
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


def validate_nim_operator_descendant(
    pod: Mapping[str, Any],
    *,
    security_envelope: Mapping[str, Any],
    trusted_attestors: Mapping[str, str],
    security_session_id: str,
    resource_kind: str,
    record: ModelRecord,
) -> None:
    """Admission-webhook validator for the actual Pod emitted by NIM Operator."""

    subject_sha256, _, descendant_image, _ = _nim_operator_security_envelope(
        security_envelope,
        trusted_attestors=trusted_attestors,
        expected_session_id=security_session_id,
        expected_kind=resource_kind,
        record=record,
    )
    subject = security_envelope["subject"]
    metadata = pod.get("metadata")
    spec = pod.get("spec")
    if not isinstance(metadata, Mapping) or not isinstance(spec, Mapping):
        raise CatalogError("NIM Operator descendant is not a Pod object")
    annotations = metadata.get("annotations", {})
    if (
        not isinstance(annotations, Mapping)
        or annotations.get("fs2-serve.nebius.ai/operator-security-envelope-sha256")
        != subject_sha256
        or spec.get("securityContext") != subject["pod_security_context"]
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
    observed: dict[str, dict[str, Any]] = {}
    for container_class in ("initContainers", "containers", "ephemeralContainers"):
        containers = spec.get(container_class, [])
        if not isinstance(containers, list):
            raise CatalogError("NIM Operator descendant container inventory is invalid")
        for container in containers:
            if not isinstance(container, Mapping) or container.get("volumeDevices"):
                raise CatalogError("NIM Operator descendant exposes a writable block device")
            name = container.get("name")
            if not isinstance(name, str) or name in observed:
                raise CatalogError("NIM Operator descendant container identity is invalid")
            writable_mounts: dict[str, dict[str, Any]] = {}
            mounts = container.get("volumeMounts", [])
            if not isinstance(mounts, list):
                raise CatalogError("NIM Operator descendant mount inventory is invalid")
            for mount in mounts:
                if not isinstance(mount, Mapping) or not isinstance(
                    mount.get("readOnly", False), bool
                ):
                    raise CatalogError("NIM Operator descendant mount inventory is invalid")
                if mount.get("readOnly", False) is True:
                    continue
                path = mount.get("mountPath")
                volume = volumes_by_name.get(mount.get("name"))
                if (
                    not isinstance(path, str)
                    or path in writable_mounts
                    or not isinstance(volume, Mapping)
                    or "subPathExpr" in mount
                ):
                    raise CatalogError("NIM Operator descendant writable mount is unsafe")
                if isinstance(volume.get("emptyDir"), Mapping):
                    kind = "emptyDir"
                    reference = volume["emptyDir"].get("sizeLimit")
                elif (
                    isinstance(volume.get("persistentVolumeClaim"), Mapping)
                    and isinstance(
                        volume["persistentVolumeClaim"].get("readOnly", False), bool
                    )
                    and volume["persistentVolumeClaim"].get("readOnly", False) is False
                ):
                    kind = "persistentVolumeClaim"
                    reference = volume["persistentVolumeClaim"].get("claimName")
                else:
                    raise CatalogError("NIM Operator descendant writable volume type is not admitted")
                if not isinstance(reference, str):
                    raise CatalogError("NIM Operator descendant writable volume lacks an exact reference")
                writable_mounts[path] = {
                    "kind": kind,
                    "reference": reference,
                    "sub_path": mount.get("subPath"),
                }
            observed[name] = {
                "image": container.get("image"),
                "security_context": container.get("securityContext"),
                "writable_mounts": writable_mounts,
            }
    if (
        observed != subject["containers"]
        or observed.get(subject["runtime_container_name"], {}).get("image")
        != descendant_image
    ):
        raise CatalogError("NIM Operator actual descendants differ from signed image/security/mount admission")


def validate_nim_operator_admission_review(
    review: Mapping[str, Any],
    *,
    security_envelope: Mapping[str, Any],
    trusted_attestors: Mapping[str, str],
    security_session_id: str,
    resource_kind: str,
    record: ModelRecord,
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
    if (
        not isinstance(uid, str)
        or not uid
        or request.get("operation") not in {"CREATE", "UPDATE"}
        or request.get("namespace") != "fs2-models"
        or not isinstance(resource, Mapping)
        or not isinstance(admitted_object, Mapping)
        or not isinstance(admitted_object.get("metadata"), Mapping)
        or admitted_object["metadata"].get("namespace", "fs2-models") != "fs2-models"
    ):
        raise CatalogError("NIM Operator admission request does not target one model resource")
    if resource == {"group": "", "version": "v1", "resource": "pods"}:
        if admitted_object.get("apiVersion") != "v1" or admitted_object.get("kind") != "Pod":
            raise CatalogError("NIM Operator descendant admission object is not a Pod")
        validate_nim_operator_descendant(
            admitted_object,
            security_envelope=security_envelope,
            trusted_attestors=trusted_attestors,
            security_session_id=security_session_id,
            resource_kind=resource_kind,
            record=record,
        )
    else:
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
        subject_sha256, admission_policy, descendant_image, custom_resource_image = _nim_operator_security_envelope(
            security_envelope,
            trusted_attestors=trusted_attestors,
            expected_session_id=security_session_id,
            expected_kind=resource_kind,
            record=record,
        )
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
    return {
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
    return {
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
