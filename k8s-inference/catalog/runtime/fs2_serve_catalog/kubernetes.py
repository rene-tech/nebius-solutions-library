#!/usr/bin/env python3
"""Model-owned Kubernetes objects; this module never emits cluster-scoped Kueue policy."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .acquisition import (
    ACQUISITION_FS_GROUP,
    ACQUISITION_RUN_AS_GID,
    ACQUISITION_RUN_AS_UID,
    FRESH_WRITE_PROOF_OPERATION,
)
from .capabilities import BackendCapability, require_local_capability
from .evidence import (
    AcquisitionHelperImageAdmission,
    FaststartJobAdmission,
    ProtectedStorageClassAdmission,
    ProviderBlockWriterAdmission,
)
from .loader import AcquisitionPlan, CatalogError, ModelRecord, strong_sha256
from .prerequisites import PrerequisiteBinding


LOCAL_QUEUE_API = "kueue.x-k8s.io/v1beta2"
ASYNC_JOB_KINDS = {"batch", "cache", "donor", "snapshot", "evaluation"}
FASTSTART_JOB_KINDS = {"donor", "snapshot"}
NAMESPACE_BY_KIND = {
    "batch": "fs2-models",
    "cache": "fs2-models",
    "donor": "fs2-faststart",
    "snapshot": "fs2-faststart",
    "evaluation": "fs2-models",
}
QUEUE_BY_NAMESPACE = {
    "fs2-models": "fs2-models-async",
}
SERVICE_ACCOUNT_REQUIREMENT_BY_KIND = {
    "batch": "fs2-models/batch-service-account",
    "cache": "fs2-models/cache-service-account",
    "donor": "fs2-faststart/donor-service-account",
    "snapshot": "fs2-faststart/snapshot-service-account",
    "evaluation": "fs2-models/evaluation-service-account",
}
RUNTIME_REGISTRY_REQUIREMENT_BY_NAMESPACE = {
    "fs2-models": "fs2-models/runtime-registry-secret",
    "fs2-faststart": "fs2-faststart/runtime-registry-secret",
}
CLUSTER_QUEUE_NAME = "fs2-b300-async"
DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")


def canonical_object_digest(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode() + b"\n"
    return hashlib.sha256(payload).hexdigest()


def render_local_queue(namespace: str) -> dict[str, Any]:
    """Render one namespace-scoped LocalQueue bound to cluster-owned policy."""

    queue_name = _queue_name(namespace)
    return {
        "apiVersion": LOCAL_QUEUE_API,
        "kind": "LocalQueue",
        "metadata": {
            "name": queue_name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/part-of": "fs2-serve",
                "app.kubernetes.io/managed-by": "fs2-serve-models",
            },
        },
        "spec": {"clusterQueue": CLUSTER_QUEUE_NAME},
    }


def _queue_name(namespace: str) -> str:
    try:
        return QUEUE_BY_NAMESPACE[namespace]
    except KeyError as exc:
        raise CatalogError(
            "LocalQueue namespace is outside the deployed model-lane ownership boundary"
        ) from exc


def render_local_queues() -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [render_local_queue(namespace) for namespace in sorted(QUEUE_BY_NAMESPACE)],
    }


def _image(reference: str) -> str:
    if not isinstance(reference, str) or "@" not in reference:
        raise CatalogError("Job image must be an immutable reference@digest")
    _, digest = reference.rsplit("@", 1)
    strong_sha256(digest, "Job image digest", image=True)
    return reference


def _bind_backend_subject(
    manifest: dict[str, Any], capability: BackendCapability
) -> None:
    """Bind an emitted Job to the exact admitted backend/runtime subject."""

    value = capability.to_dict()
    runtime_tuple_digest = capability.runtime_tuple_digest
    strong_sha256(runtime_tuple_digest, "Job backend runtime tuple digest")
    backend_identity_digest = value["backend_identity_digest"]
    strong_sha256(backend_identity_digest, "Job backend identity digest")
    annotations = {
        "fs2-serve.nebius.ai/backend-id": capability.backend_id,
        "fs2-serve.nebius.ai/backend-class": capability.backend_class,
        "fs2-serve.nebius.ai/backend-identity-digest": backend_identity_digest,
        "fs2-serve.nebius.ai/runtime-tuple-digest": runtime_tuple_digest,
        "fs2-serve.nebius.ai/gpu-class": capability.gpu_class,
    }
    manifest["metadata"]["annotations"].update(annotations)
    manifest["spec"]["template"]["metadata"]["annotations"].update(annotations)


def render_async_job(
    record: ModelRecord,
    *,
    prerequisites: PrerequisiteBinding,
    job_kind: str,
    operation_id: str,
    image: str,
    command: list[str],
    faststart_admission: FaststartJobAdmission | None = None,
    artifact_manifest_digest: str | None = None,
    image_pull_requirement_id: str | None,
    backend_capability: BackendCapability | None = None,
) -> dict[str, Any]:
    """Render a suspended Kueue Job; admission and cluster policy stay external."""

    if job_kind not in ASYNC_JOB_KINDS:
        raise CatalogError("async Job kind is outside the closed model-owned set")
    if not isinstance(operation_id, str) or DNS_LABEL.fullmatch(operation_id) is None:
        raise CatalogError("async operation ID must be a DNS label")
    if not command or any(not isinstance(item, str) or not item for item in command):
        raise CatalogError("async Job command must be direct non-empty argv")
    image = _image(image)
    namespace = NAMESPACE_BY_KIND[job_kind]
    service_account_id = SERVICE_ACCOUNT_REQUIREMENT_BY_KIND[job_kind]
    required_ids = [service_account_id]
    if image_pull_requirement_id is not None:
        required_ids.append(image_pull_requirement_id)
    prerequisites.require(required_ids)
    service_account = prerequisites.resource(service_account_id)
    value = record.to_dict()
    gpu = value["resources"]["gpu"]
    if job_kind != "cache":
        if backend_capability is None:
            raise CatalogError("GPU Job requires a model-bound backend capability")
        mechanism = "snapshot" if job_kind in FASTSTART_JOB_KINDS else "conventional"
        storage_modes = (
            {"local-nvme"}
            if job_kind in FASTSTART_JOB_KINDS
            else {"sfs-pvc", "local-nvme", "nimcache-pvc"}
        )
        require_local_capability(
            record,
            backend_capability,
            storage_modes=storage_modes,
            mechanism=mechanism,
        )
    if job_kind in FASTSTART_JOB_KINDS:
        if gpu["count"] != 1 or gpu["topology"] != "single-gpu":
            raise CatalogError("initial donor/snapshot Jobs are restricted to a single B300")
        if faststart_admission is None:
            raise CatalogError("donor/snapshot Job requires reopened signed admission")
        faststart_admission.authorize(
            record,
            job_kind=job_kind,
            image=image,
            command=command,
            artifact_manifest_digest=artifact_manifest_digest,
        )
        runtime_tuple_digest = faststart_admission.runtime_tuple_digest
        if runtime_tuple_digest != backend_capability.runtime_tuple_digest:
            raise CatalogError("donor/snapshot runtime tuple differs from the backend capability")
    elif faststart_admission is not None:
        raise CatalogError("only donor/snapshot Jobs accept signed fast-start admission")
    else:
        runtime_tuple_digest = None
    if artifact_manifest_digest is not None:
        strong_sha256(artifact_manifest_digest, "async Job artifact manifest digest")

    queue_name = _queue_name(namespace)
    labels = {
        "app.kubernetes.io/name": f"fs2-{job_kind}",
        "app.kubernetes.io/part-of": "fs2-serve",
        "app.kubernetes.io/managed-by": "fs2-serve-models",
        "fs2-serve.nebius.ai/model-id": record.model_id,
        "fs2-serve.nebius.ai/job-kind": job_kind,
        "fs2-serve.nebius.ai/operation-id": operation_id,
        "kueue.x-k8s.io/queue-name": queue_name,
    }
    annotations = {
        "fs2-serve.nebius.ai/model-digest": record.digest,
        "fs2-serve.nebius.ai/node-scaler-owner": value["resources"]["scaler_owner"],
    }
    if runtime_tuple_digest is not None:
        annotations["fs2-serve.nebius.ai/runtime-tuple-digest"] = runtime_tuple_digest
        annotations["fs2-serve.nebius.ai/faststart-admission-digest"] = (
            faststart_admission.admission_digest
        )
    if artifact_manifest_digest is not None:
        annotations["fs2-serve.nebius.ai/artifact-manifest-digest"] = artifact_manifest_digest

    resources: dict[str, Any] = {
        "requests": {
            "cpu": f"{value['resources']['cpu_millis']}m",
            "memory": str(value["resources"]["memory_bytes"]),
        },
        "limits": {
            "cpu": f"{value['resources']['cpu_millis']}m",
            "memory": str(value["resources"]["memory_bytes"]),
        },
    }
    if job_kind != "cache":
        resources["requests"]["nvidia.com/gpu"] = gpu["count"]
        resources["limits"]["nvidia.com/gpu"] = gpu["count"]

    pod_security: dict[str, Any] = {
        "runAsNonRoot": True,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    container_security: dict[str, Any] = {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    if job_kind in FASTSTART_JOB_KINDS:
        # CRIU/cuda-checkpoint requires a separately isolated privileged namespace.
        # The exact tuple gate above prevents this template from being emitted early.
        pod_security = {"runAsUser": 0, "seccompProfile": {"type": "Unconfined"}}
        container_security = {
            "privileged": True,
            "allowPrivilegeEscalation": True,
            "readOnlyRootFilesystem": False,
            "runAsUser": 0,
        }

    pod_spec: dict[str, Any] = {
        "serviceAccountName": service_account["name"],
        "automountServiceAccountToken": False,
        "restartPolicy": "Never",
        "terminationGracePeriodSeconds": 60,
        "securityContext": pod_security,
        "containers": [
            {
                "name": job_kind,
                "image": image,
                "imagePullPolicy": "IfNotPresent",
                "command": command,
                "resources": resources,
                "securityContext": container_security,
            }
        ],
    }
    if backend_capability is not None:
        pod_spec["nodeSelector"] = backend_capability.node_selector
        pod_spec["tolerations"] = backend_capability.tolerations
    if image_pull_requirement_id is not None:
        pull_secret = prerequisites.resource(image_pull_requirement_id)
        if pull_secret["kind"] != "Secret":
            raise CatalogError("image pull prerequisite is not a Secret")
        pod_spec["imagePullSecrets"] = [{"name": pull_secret["name"]}]
    manifest = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": f"{record.model_id}-{job_kind}-{operation_id}",
            "namespace": namespace,
            "labels": labels,
            "annotations": annotations,
        },
        "spec": {
            "suspend": True,
            "backoffLimit": 0,
            "activeDeadlineSeconds": 7200,
            "ttlSecondsAfterFinished": 3600,
            "template": {
                "metadata": {"labels": labels, "annotations": annotations},
                "spec": pod_spec,
            },
        },
    }
    if backend_capability is not None:
        _bind_backend_subject(manifest, backend_capability)
    return manifest


def render_image_prepull_job(
    record: ModelRecord,
    *,
    prerequisites: PrerequisiteBinding,
    operation_id: str,
    backend_capability: BackendCapability,
) -> dict[str, Any]:
    """Render an admitted cache Job whose only side effect is pulling the exact runtime image."""

    value = record.to_dict()
    if value["model"]["source"]["kind"] == "ngc-nim":
        raise CatalogError("NGC images require the target-node pull/runtime canary")
    reference = value["runtime"]["image"]["reference"]
    if not isinstance(reference, str):
        raise CatalogError("image pre-pull requires a resolved immutable runtime reference")
    require_local_capability(record, backend_capability)
    job = render_async_job(
        record,
        prerequisites=prerequisites,
        job_kind="cache",
        operation_id=operation_id,
        image=reference,
        command=["/usr/bin/true"],
        image_pull_requirement_id=None,
    )
    pod = job["spec"]["template"]["spec"]
    pod["nodeSelector"] = backend_capability.node_selector
    pod["tolerations"] = backend_capability.tolerations
    _bind_backend_subject(job, backend_capability)
    resources = pod["containers"][0]["resources"]
    resources["requests"] = {"cpu": "100m", "memory": "128Mi"}
    resources["limits"] = {"cpu": "1", "memory": "1Gi"}
    job["metadata"]["annotations"]["fs2-serve.nebius.ai/prepull-image-digest"] = value[
        "runtime"
    ]["image"]["digest"]
    return job


def render_localization_job(
    record: ModelRecord,
    *,
    prerequisites: PrerequisiteBinding,
    operation_id: str,
    localizer_image: str,
    artifact_manifest_digest: str,
    artifact_content_digest: str,
    backend_capability: BackendCapability,
) -> dict[str, Any]:
    """Render the unprivileged SFS-to-local-PVC localizer after lifecycle review."""

    value = record.to_dict()
    if value["cache"]["owner"] != "fs2-serve-localizer":
        raise CatalogError("fs2 localizer cannot write a NIM Operator-owned cache")
    require_local_capability(
        record, backend_capability, storage_modes={"local-nvme"}
    )
    strong_sha256(artifact_content_digest, "localizer content digest")
    job = render_async_job(
        record,
        prerequisites=prerequisites,
        job_kind="cache",
        operation_id=operation_id,
        image=localizer_image,
        command=[
            "python3",
            "-m",
            "fs2_serve_catalog.cli",
            "stage",
            "--manifest",
            f"/mnt/fs2-serve-cache/models/.manifests/{artifact_manifest_digest}.json",
            "--source-root",
            value["cache"]["shared_path"] + f"/sha256/{artifact_content_digest}",
            "--cache-root",
            value["cache"]["local_path"],
            "--controller-owner",
            "fs2-serve-localizer",
        ],
        artifact_manifest_digest=artifact_manifest_digest,
        image_pull_requirement_id="fs2-models/runtime-registry-secret",
    )
    prerequisites.require(["fs2-models/shared-cache-pvc"])
    shared_pvc_name = prerequisites.resource("fs2-models/shared-cache-pvc")["name"]
    pod = job["spec"]["template"]["spec"]
    pod["nodeSelector"] = backend_capability.node_selector
    pod["tolerations"] = backend_capability.tolerations
    _bind_backend_subject(job, backend_capability)
    node_identity = backend_capability.node_identity
    local_pv_pvc = backend_capability.local_pv_pvc
    if node_identity is None or local_pv_pvc is None:
        raise CatalogError("localizer requires reviewed local-PV/PVC lifecycle evidence")
    claim = local_pv_pvc["persistent_volume_claim"]
    if claim["namespace"] != job["metadata"]["namespace"]:
        raise CatalogError("localizer and local PVC must share the exact cache namespace")
    command = pod["containers"][0]["command"]
    command.extend(
        [
            "--serving-node-name",
            node_identity["name"],
            "--serving-node-uid",
            node_identity["uid"],
            "--serving-node-provider-id-sha256",
            node_identity["provider_id_sha256"],
        ]
    )
    node_annotations = {
        "fs2-serve.nebius.ai/serving-node-name": node_identity["name"],
        "fs2-serve.nebius.ai/serving-node-uid": node_identity["uid"],
        "fs2-serve.nebius.ai/serving-node-provider-id-sha256": node_identity[
            "provider_id_sha256"
        ],
        "fs2-serve.nebius.ai/local-pv-pvc-lifecycle-receipt-digest": local_pv_pvc[
            "lifecycle_receipt_digest"
        ],
        "fs2-serve.nebius.ai/local-pv-pvc-activation-generation": str(
            local_pv_pvc["activation_generation"]
        ),
        "fs2-serve.nebius.ai/local-pvc-uid": claim["uid"],
    }
    job["metadata"]["annotations"].update(node_annotations)
    job["spec"]["template"]["metadata"]["annotations"].update(node_annotations)
    pod["volumes"] = [
        {"name": "shared-cache", "persistentVolumeClaim": {"claimName": shared_pvc_name}},
        {
            "name": "local-cache",
            "persistentVolumeClaim": {"claimName": claim["name"]},
        },
    ]
    pod["containers"][0]["volumeMounts"] = [
        {"name": "shared-cache", "mountPath": "/mnt/fs2-serve-cache", "readOnly": True},
        {"name": "local-cache", "mountPath": value["cache"]["local_path"]},
    ]
    return job


def render_artifact_acquisition_job(
    record: ModelRecord,
    plan: AcquisitionPlan,
    *,
    prerequisites: PrerequisiteBinding,
    operation_id: str,
    helper_image_admission: AcquisitionHelperImageAdmission,
    storage_class_admission: ProtectedStorageClassAdmission | None = None,
    writer_admission: ProviderBlockWriterAdmission | None = None,
) -> dict[str, Any]:
    """Render an exact-plan public-HF acquisition Job for its reviewed PVC."""

    if plan.model_id != record.model_id or plan.method != "huggingface-public-snapshot":
        raise CatalogError("artifact acquisition Job requires the exact public HF plan")
    prerequisites.require(plan.required_prerequisite_ids)
    helper = plan.to_dict().get("helper_image")
    if not isinstance(helper, dict):
        raise CatalogError("artifact acquisition plan lacks its catalog-owned helper image")
    admitted_helper = helper_image_admission.authorize(record, plan)
    helper_contract_sha256 = canonical_object_digest(helper)
    acquisition_plan_sha256 = canonical_object_digest(plan.to_dict())
    job = render_async_job(
        record,
        prerequisites=prerequisites,
        job_kind="cache",
        operation_id=operation_id,
        image=admitted_helper["reference"],
        command=[
            *helper["entrypoint"],
            "--catalog-root",
            "/catalog",
            "--model-id",
            record.model_id,
        ],
        image_pull_requirement_id="fs2-models/runtime-registry-secret",
    )
    security = helper["security_context"]
    pod = job["spec"]["template"]["spec"]
    pod["securityContext"] = {
        "runAsNonRoot": security["run_as_non_root"],
        "runAsUser": security["run_as_uid"],
        "runAsGroup": security["run_as_gid"],
        "fsGroup": security["fs_group"],
        "fsGroupChangePolicy": "OnRootMismatch",
        "supplementalGroupsPolicy": security["supplemental_groups_policy"],
        "seccompProfile": {"type": security["seccomp_profile"]},
    }
    container = pod["containers"][0]
    container["securityContext"].update(
        {
            "runAsNonRoot": security["run_as_non_root"],
            "runAsUser": security["run_as_uid"],
            "runAsGroup": security["run_as_gid"],
        }
    )
    container["env"] = [
        {"name": "HOME", "value": "/tmp"},
        {"name": "HF_HOME", "value": "/tmp/huggingface"},
        {"name": "FS2_ACQUISITION_OPERATION_ID", "value": operation_id},
        {"name": "FS2_ACQUISITION_JOB_NAMESPACE", "value": "fs2-models"},
        {"name": "FS2_ACQUISITION_JOB_NAME", "value": job["metadata"]["name"]},
        {
            "name": "FS2_ACQUISITION_JOB_UID",
            "valueFrom": {
                "fieldRef": {
                    "fieldPath": (
                        "metadata.annotations['fs2-serve.nebius.ai/job-uid']"
                    )
                }
            },
        },
        {
            "name": "FS2_ACQUISITION_POD_NAME",
            "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}},
        },
        {
            "name": "FS2_ACQUISITION_POD_UID",
            "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}},
        },
        {"name": "FS2_ACQUISITION_HELPER_IMAGE", "value": admitted_helper["reference"]},
        {
            "name": "FS2_ACQUISITION_HELPER_IMAGE_DIGEST",
            "value": admitted_helper["digest"],
        },
        {
            "name": "FS2_ACQUISITION_HELPER_ADMISSION_DIGEST",
            "value": admitted_helper["receipt_digest"],
        },
        {
            "name": "FS2_ACQUISITION_HELPER_REGISTRY_IDENTITY_SHA256",
            "value": admitted_helper["registry_identity_sha256"],
        },
        {
            "name": "FS2_ACQUISITION_HELPER_BUILD_IDENTITY_SHA256",
            "value": admitted_helper["build_identity_sha256"],
        },
        {"name": "FS2_ACQUISITION_PLAN_SHA256", "value": acquisition_plan_sha256},
        {"name": "FS2_ACQUISITION_HELPER_CONTRACT_SHA256", "value": helper_contract_sha256},
    ]
    placement = record.to_dict()["resources"]["gpu"]["placement"]
    if plan.to_dict()["publication"] == "atomic-content-addressed-provider-block-pvc":
        if record.model_id != "qwen3-8b" or placement is None:
            raise CatalogError("provider block acquisition is reviewed only for Qwen")
        provider = placement["provider_block_pvc"]
        if provider["state"] != "candidate-unqualified":
            raise CatalogError("provider block acquisition contract has an unexpected state")
        claim = provider["claim"]
        if storage_class_admission is None or writer_admission is None:
            raise CatalogError(
                "provider block acquisition requires signed StorageClass and writer admissions"
            )
        storage_class = storage_class_admission.authorize(record)
        service_account = prerequisites.resource("fs2-models/cache-service-account")
        writer_admission.authorize(
            record,
            operation_id=operation_id,
            claim_name=claim["name"],
            writer_job_name=job["metadata"]["name"],
            writer_service_account_uid=service_account["uid"],
            storage_class_receipt_digest=storage_class_admission.receipt_digest,
        )
        pod["nodeSelector"] = placement["node_selector"]
        pod["tolerations"] = placement["tolerations"]
        pod["volumes"] = [
            {
                "name": "provider-block",
                "persistentVolumeClaim": {"claimName": claim["name"]},
            },
            {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}},
        ]
        container["volumeMounts"] = [
            {"name": "provider-block", "mountPath": "/mnt/fs2-provider-block"},
            {"name": "tmp", "mountPath": "/tmp"},
        ]
        resources = container["resources"]
        if "nvidia.com/gpu" in resources["requests"] or "nvidia.com/gpu" in resources["limits"]:
            raise CatalogError("provider block acquisition Job must not request a GPU")
        job["metadata"]["annotations"].update(
            {
                "fs2-serve.nebius.ai/storage-mode": "provider-block-pvc",
                "fs2-serve.nebius.ai/first-consumer": "true",
                "fs2-serve.nebius.ai/sole-writer": "true",
                "fs2-serve.nebius.ai/pvc-name": claim["name"],
                "fs2-serve.nebius.ai/pvc-uid": writer_admission.claim_uid,
                "fs2-serve.nebius.ai/pvc-resource-version": (
                    writer_admission.claim_resource_version
                ),
                "fs2-serve.nebius.ai/protected-storage-class-receipt-digest": (
                    storage_class_admission.receipt_digest
                ),
                "fs2-serve.nebius.ai/protected-storage-class-uid": storage_class[
                    "metadata"
                ]["uid"],
                "fs2-serve.nebius.ai/protected-storage-class-resource-version": (
                    storage_class["metadata"]["resourceVersion"]
                ),
                "fs2-serve.nebius.ai/writer-admission-receipt-digest": (
                    writer_admission.receipt_digest
                ),
                "fs2-serve.nebius.ai/writer-admission-controller-identity-sha256": (
                    writer_admission.controller_identity_sha256
                ),
                "fs2-serve.nebius.ai/writer-lease-uid": writer_admission.lease_uid,
                "fs2-serve.nebius.ai/writer-lease-resource-version": (
                    writer_admission.lease_resource_version
                ),
                "fs2-serve.nebius.ai/writer-lease-holder-identity": (
                    writer_admission.lease_holder_identity
                ),
                "fs2-serve.nebius.ai/writer-fencing-token": str(
                    writer_admission.fencing_token
                ),
                "fs2-serve.nebius.ai/writer-complete-mount-set-sha256": (
                    writer_admission.complete_mount_set_sha256
                ),
                "fs2-serve.nebius.ai/required-filesystem": "ext4",
                "fs2-serve.nebius.ai/fresh-write-proof": FRESH_WRITE_PROOF_OPERATION,
                "fs2-serve.nebius.ai/acquisition-run-as": (
                    f"{ACQUISITION_RUN_AS_UID}:{ACQUISITION_RUN_AS_GID}:"
                    f"fsGroup={ACQUISITION_FS_GROUP}"
                ),
            }
        )
        job["spec"]["template"]["metadata"]["annotations"].update(
            {
                key: value
                for key, value in job["metadata"]["annotations"].items()
                if key.startswith("fs2-serve.nebius.ai/writer-")
                or key.startswith("fs2-serve.nebius.ai/pvc-")
                or key.startswith("fs2-serve.nebius.ai/protected-storage-class-")
            }
        )
    else:
        if storage_class_admission is not None or writer_admission is not None:
            raise CatalogError(
                "non-provider acquisition cannot consume provider block admissions"
            )
        pvc = prerequisites.resource("fs2-models/shared-cache-pvc")
        pod["volumes"] = [
            {"name": "shared-cache", "persistentVolumeClaim": {"claimName": pvc["name"]}},
            {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}},
        ]
        container["volumeMounts"] = [
            {"name": "shared-cache", "mountPath": "/mnt/fs2-serve-cache"},
            {"name": "tmp", "mountPath": "/tmp"},
        ]
    job["metadata"]["annotations"]["fs2-serve.nebius.ai/acquisition-method"] = plan.method
    helper_annotations = {
        "fs2-serve.nebius.ai/acquisition-helper-image-digest": admitted_helper["digest"],
        "fs2-serve.nebius.ai/acquisition-helper-admission-digest": admitted_helper[
            "receipt_digest"
        ],
        "fs2-serve.nebius.ai/acquisition-helper-registry-identity-sha256": admitted_helper[
            "registry_identity_sha256"
        ],
        "fs2-serve.nebius.ai/acquisition-helper-build-identity-sha256": admitted_helper[
            "build_identity_sha256"
        ],
        "fs2-serve.nebius.ai/acquisition-plan-sha256": acquisition_plan_sha256,
        "fs2-serve.nebius.ai/acquisition-helper-contract-sha256": helper_contract_sha256,
        "fs2-serve.nebius.ai/job-uid-gate": "patch-server-observed-uid-before-unsuspend",
        "fs2-serve.nebius.ai/cleanup-contract": "uid-precondition-plus-replacement-observation",
    }
    job["metadata"]["annotations"].update(helper_annotations)
    job["spec"]["template"]["metadata"]["annotations"].update(helper_annotations)
    return job


def render_provider_block_pvc(
    record: ModelRecord,
    *,
    storage_class_admission: ProtectedStorageClassAdmission,
) -> dict[str, Any]:
    """Render the retained Qwen claim; the cluster lane owns its StorageClass."""

    value = record.to_dict()
    placement = value["resources"]["gpu"]["placement"]
    if record.model_id != "qwen3-8b" or placement is None:
        raise CatalogError("provider block PVC is reviewed only for Qwen")
    contract = placement["provider_block_pvc"]
    if contract["state"] != "candidate-unqualified":
        raise CatalogError("provider block PVC renderer accepts only the unqualified candidate")
    storage_class = contract["storage_class"]
    claim = contract["claim"]
    if storage_class["owner"] != "fs2-serve-cluster":
        raise CatalogError("models lane cannot own the cluster-scoped StorageClass")
    observed_storage_class = storage_class_admission.authorize(record)
    if observed_storage_class["metadata"]["name"] != storage_class["name"]:
        raise CatalogError("PVC renderer received another protected StorageClass")
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "namespace": claim["namespace"],
            "name": claim["name"],
            "labels": {
                "app.kubernetes.io/part-of": "fs2-serve",
                "app.kubernetes.io/managed-by": "fs2-serve-models",
                "fs2-serve.nebius.ai/model-id": record.model_id,
            },
            "annotations": {
                "helm.sh/resource-policy": "keep",
                "fs2-serve.nebius.ai/deletion-policy": "retain",
                "fs2-serve.nebius.ai/model-digest": record.digest,
                "fs2-serve.nebius.ai/storage-contract": contract["contract"],
                "fs2-serve.nebius.ai/filesystem": claim["fs_type"],
                "fs2-serve.nebius.ai/protected-storage-class-receipt-digest": (
                    storage_class_admission.receipt_digest
                ),
                "fs2-serve.nebius.ai/protected-storage-class-uid": (
                    observed_storage_class["metadata"]["uid"]
                ),
                "fs2-serve.nebius.ai/protected-storage-class-resource-version": (
                    observed_storage_class["metadata"]["resourceVersion"]
                ),
                "fs2-serve.nebius.ai/protected-storage-class-observer-identity-sha256": (
                    storage_class_admission.observer_identity_sha256
                ),
            },
        },
        "spec": {
            "accessModes": claim["access_modes"],
            "volumeMode": claim["volume_mode"],
            "storageClassName": storage_class["name"],
            "resources": {"requests": {"storage": "64Gi"}},
        },
    }


def render_ngc_target_node_canary_job(
    record: ModelRecord,
    plan: AcquisitionPlan,
    *,
    prerequisites: PrerequisiteBinding,
    operation_id: str,
    backend_capability: BackendCapability,
) -> dict[str, Any]:
    """Render a value-suppressed target-node NGC pull and runtime canary."""

    value = record.to_dict()
    if plan.model_id != record.model_id or plan.method != "ngc-target-node-nimcache":
        raise CatalogError("NGC canary requires the exact target-node acquisition plan")
    require_local_capability(
        record, backend_capability, storage_modes={"nimcache-pvc"}
    )
    prerequisites.require(plan.required_prerequisite_ids)
    reference = value["runtime"]["image"]["reference"]
    if not isinstance(reference, str):
        raise CatalogError("NGC canary requires an immutable runtime image")
    job = render_async_job(
        record,
        prerequisites=prerequisites,
        job_kind="cache",
        operation_id=operation_id,
        image=reference,
        command=list(value["runtime"]["command"]),
        image_pull_requirement_id="fs2-models/ngc-pull-secret",
    )
    pod = job["spec"]["template"]["spec"]
    pod["nodeSelector"] = backend_capability.node_selector
    pod["tolerations"] = backend_capability.tolerations
    _bind_backend_subject(job, backend_capability)
    pvc = prerequisites.resource("fs2-models/shared-cache-pvc")
    container = pod["containers"][0]
    gpu_count = value["resources"]["gpu"]["count"]
    container["resources"]["requests"]["nvidia.com/gpu"] = gpu_count
    container["resources"]["limits"]["nvidia.com/gpu"] = gpu_count
    runtime_secret = prerequisites.resource("fs2-models/ngc-runtime-secret")
    container["env"] = [
        {
            "name": "NGC_API_KEY",
            "valueFrom": {
                "secretKeyRef": {"name": runtime_secret["name"], "key": "NGC_API_KEY"}
            },
        },
        {"name": "FS2_NIM_CACHE_ROOT", "value": value["cache"]["shared_path"]},
    ]
    pod["volumes"] = [
        {"name": "shared-cache", "persistentVolumeClaim": {"claimName": pvc["name"]}}
    ]
    container["volumeMounts"] = [
        {"name": "shared-cache", "mountPath": "/mnt/fs2-serve-cache", "readOnly": True}
    ]
    readiness = value["interface"]["readiness"]
    if readiness is not None and readiness["method"] == "GET":
        container["readinessProbe"] = {
            "httpGet": {"path": readiness["path"], "port": 8000},
            "periodSeconds": 5,
            "timeoutSeconds": 2,
            "failureThreshold": max(1, readiness["timeout_seconds"] // 5),
        }
    job["metadata"]["annotations"].update(
        {
            "fs2-serve.nebius.ai/canary-contract": "target-node-pull-runtime-semantic/v1",
            "fs2-serve.nebius.ai/output-policy": "values-suppressed",
        }
    )
    return job


def write_local_queue_contract(path: Path | str) -> str:
    """Write deterministic JSON only when explicitly invoked by a build/release step."""

    value = render_local_queues()
    output = Path(path)
    output.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    return canonical_object_digest(value)
