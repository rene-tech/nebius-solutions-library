#!/usr/bin/env python3
"""Digest-pinned native and KServe Standard model workload adapters."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlsplit

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
            "readOnlyRootFilesystem": False,
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
    prerequisites.require([NGC_PULL_SECRET, NGC_RUNTIME_SECRET, SHARED_CACHE_PVC])
    pull_secret = prerequisites.resource(NGC_PULL_SECRET)
    runtime_secret = prerequisites.resource(NGC_RUNTIME_SECRET)
    pvc = prerequisites.resource(SHARED_CACHE_PVC)
    ngc_source: dict[str, Any] = {
        "modelPuller": _runtime_image(record),
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
    prerequisites.require([NGC_PULL_SECRET, NGC_RUNTIME_SECRET, SHARED_CACHE_PVC])
    pull_secret = prerequisites.resource(NGC_PULL_SECRET)
    runtime_secret = prerequisites.resource(NGC_RUNTIME_SECRET)
    image = backend_capability.nim_image
    if image is None:
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
                "repository": image["repository"],
                "tag": image["tag"],
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
