#!/usr/bin/env python3
"""Fail-closed validators for live staging and B300 qualification receipts."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import weakref
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit
from uuid import UUID

from .artifacts import ArtifactManifest, artifact_manifest_from_value, canonical_bytes
from .attestations import verify_signed_attestation
from .capabilities import validate_provider_block_storage_class_observation
from .loader import (
    AcquisitionPlan,
    Catalog,
    CatalogError,
    ModelRecord,
    ScaleContract,
    SemanticRequestContract,
    _boolean,
    _enum,
    _exact,
    _list,
    _positive_int,
    _text,
    canonical_content_uri,
    execution_identity,
    strong_sha256,
)
from .prerequisites import bind_runtime_prerequisites


STAGING_RECEIPT_SCHEMA = "fs2-serve.nebius.ai/staging-receipt/v3"
NIM_CACHE_RECEIPT_SCHEMA = "fs2-serve.nebius.ai/nim-cache-readiness-receipt/v2"
RUNTIME_TUPLE_SCHEMA = "fs2-serve.nebius.ai/b300-runtime-tuple/v5"
SEMANTIC_RECEIPT_SCHEMA = "fs2-serve.nebius.ai/semantic-receipt/v3"
SEMANTIC_VALIDATION_SCHEMA = "fs2-serve.nebius.ai/semantic-validation-result/v3"
QUALIFICATION_RECEIPT_SCHEMA = "fs2-serve.nebius.ai/qualification-cohort/v4"
READINESS_RECEIPT_SCHEMA = "fs2-serve.nebius.ai/readiness-receipt/v2"
CLEANUP_RECEIPT_SCHEMA = "fs2-serve.nebius.ai/uid-cleanup-receipt/v4"
ACQUISITION_RECEIPT_SCHEMA = "fs2-serve.nebius.ai/artifact-acquisition-receipt/v4"
ACQUISITION_HELPER_IMAGE_ADMISSION_SCHEMA = (
    "fs2-serve.nebius.ai/acquisition-helper-image-admission/v1"
)
PROVIDER_BLOCK_PVC_RECEIPT_SCHEMA = (
    "fs2-serve.nebius.ai/provider-block-pvc-lifecycle-receipt/v4"
)
PROTECTED_STORAGE_CLASS_RECEIPT_SCHEMA = (
    "fs2-serve.nebius.ai/protected-storage-class-receipt/v1"
)
PROVIDER_BLOCK_WRITER_ADMISSION_SCHEMA = (
    "fs2-serve.nebius.ai/provider-block-writer-admission/v2"
)
PREREQUISITE_RECEIPT_SCHEMA = "fs2-serve.nebius.ai/runtime-prerequisite-receipt/v4"
TARGET_NODE_CANARY_SCHEMA = "fs2-serve.nebius.ai/target-node-pull-canary/v2"
BACKEND_IDENTITY_RECEIPT_SCHEMA = "fs2-serve.nebius.ai/backend-identity-receipt/v1"
FEDERATED_QUALIFICATION_RECEIPT_SCHEMA = (
    "fs2-serve.nebius.ai/federated-qualification-receipt/v2"
)
FASTSTART_ADMISSION_SCHEMA = "fs2-serve.nebius.ai/faststart-job-admission/v2"
ZERO_TO_READY_RECEIPT_SCHEMA = "fs2-serve.nebius.ai/zero-to-ready-receipt/v5"
RETURN_TO_ZERO_RECEIPT_SCHEMA = "fs2-serve.nebius.ai/return-to-zero-receipt/v5"

TARGET_PROJECT_SHA256 = "4bbabe330d3a6ca777209264b4407554760c5121f9d0c91d91374394d1697caf"
TARGET_PROJECT_ALIAS = "rene-us-north"
TARGET_REGION = "us-north1"
FORBIDDEN_CLUSTER_SHA256 = "50b1bc3494757398cb0acef78306945ae892ff31a7166eda40579cb3b6cb4bc9"

DRIVER_VERSION = re.compile(r"^[0-9]{3}\.[0-9]{2,3}\.[0-9]{2}$")
CUDA_VERSION = re.compile(r"^[0-9]+\.[0-9]+(?:\.[0-9]+)?$")
K8S_UID = re.compile(r"^[0-9a-f]{8}-[0-9a-f-]{27,}$")
UTC_TIMESTAMP = re.compile(r"^20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z$")
MAX_EVENT_CLOCK_SKEW = timedelta(minutes=5)
MAX_READINESS_AGE = timedelta(minutes=15)
MAX_DURATION_ERROR_SECONDS = 0.001
MAX_EVIDENCE_SUBJECT_BYTES = 64 * 1024 * 1024
EVIDENCE_PATH_COMPONENT = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
MAX_EVIDENCE_JSON_DEPTH = 64
MAX_EVIDENCE_JSON_NODES = 100_000
MAX_EVIDENCE_JSON_COLLECTION_ITEMS = 10_000
MAX_EVIDENCE_JSON_STRING_CHARS = 1024 * 1024
MAX_EVIDENCE_JSON_INTEGER = 2**63 - 1
MAX_EVIDENCE_JSON_FLOAT = 1e18
_VERIFIED_FASTSTART_SEAL = object()
_VERIFIED_STORAGE_CLASS_SEAL = object()
_VERIFIED_WRITER_ADMISSION_SEAL = object()
_VERIFIED_ACQUISITION_HELPER_IMAGE_SEAL = object()


@dataclass(frozen=True)
class FaststartJobAdmission:
    """Opaque authority created only after reopening signed tuple/admission subjects."""

    model_id: str
    model_digest: str
    job_kind: str
    runtime_tuple_digest: str
    artifact_manifest_digest: str
    image: str
    command: tuple[str, ...]
    admission_digest: str
    _seal: object

    def authorize(
        self,
        record: ModelRecord,
        *,
        job_kind: str,
        image: str,
        command: list[str],
        artifact_manifest_digest: str | None,
    ) -> None:
        if self._seal is not _VERIFIED_FASTSTART_SEAL:
            raise CatalogError("fast-start Job admission was not created by signed evidence")
        if (
            self.model_id != record.model_id
            or self.model_digest != record.digest
            or self.job_kind != job_kind
            or self.image != image
            or self.command != tuple(command)
            or self.artifact_manifest_digest != artifact_manifest_digest
        ):
            raise CatalogError("fast-start Job differs from its reopened signed admission")


@dataclass(frozen=True)
class ProtectedStorageClassAdmission:
    """Opaque pre-PVC authority reopened from a signed API-server observation."""

    model_id: str
    model_digest: str
    receipt_digest: str
    storage_class: Mapping[str, Any]
    observer_identity_sha256: str
    _seal: object

    def authorize(self, record: ModelRecord) -> dict[str, Any]:
        if self._seal is not _VERIFIED_STORAGE_CLASS_SEAL:
            raise CatalogError("protected StorageClass authority was not signed")
        if self.model_id != record.model_id or self.model_digest != record.digest:
            raise CatalogError("protected StorageClass authority names another model")
        return dict(self.storage_class)


@dataclass(frozen=True)
class ProviderBlockWriterAdmission:
    """Opaque single-writer authority created by the admission/Lease controller."""

    model_id: str
    model_digest: str
    operation_id: str
    receipt_digest: str
    storage_class_receipt_digest: str
    claim_uid: str
    claim_resource_version: str
    writer_job_name: str
    writer_service_account_uid: str
    controller_identity_sha256: str
    lease_uid: str
    lease_resource_version: str
    lease_holder_identity: str
    fencing_token: int
    complete_mount_set_sha256: str
    _seal: object

    def authorize(
        self,
        record: ModelRecord,
        *,
        operation_id: str,
        claim_name: str,
        writer_job_name: str,
        writer_service_account_uid: str,
        storage_class_receipt_digest: str,
    ) -> None:
        if self._seal is not _VERIFIED_WRITER_ADMISSION_SEAL:
            raise CatalogError("provider block writer authority was not signed")
        if (
            self.model_id != record.model_id
            or self.model_digest != record.digest
            or self.operation_id != operation_id
            or claim_name != "qwen3-8b-weights"
            or self.writer_job_name != writer_job_name
            or self.writer_service_account_uid != writer_service_account_uid
            or self.storage_class_receipt_digest != storage_class_receipt_digest
        ):
            raise CatalogError("provider block writer differs from its signed admission")


@dataclass(frozen=True)
class AcquisitionHelperImageAdmission:
    """Opaque catalog and supply-chain authority for one acquisition helper image."""

    model_id: str
    model_digest: str
    acquisition_plan_sha256: str
    helper_contract_sha256: str
    receipt_digest: str
    image_reference: str
    image_digest: str
    registry_identity_sha256: str
    build_identity_sha256: str
    valid_until: str
    _seal: object

    def authorize(self, record: ModelRecord, plan: AcquisitionPlan) -> dict[str, str]:
        if self._seal is not _VERIFIED_ACQUISITION_HELPER_IMAGE_SEAL:
            raise CatalogError("acquisition helper image authority was not signed")
        plan_value = plan.to_dict()
        helper = plan_value.get("helper_image")
        if not isinstance(helper, dict):
            raise CatalogError("acquisition plan does not own a helper image contract")
        helper_digest = hashlib.sha256(canonical_bytes(helper)).hexdigest()
        image_digest = strong_sha256(
            self.image_digest, "admitted acquisition helper image digest", image=True
        )
        strong_sha256(
            self.registry_identity_sha256, "admitted acquisition helper registry identity"
        )
        strong_sha256(
            self.build_identity_sha256, "admitted acquisition helper build identity"
        )
        if (
            self.model_id != record.model_id
            or self.model_digest != record.digest
            or plan.model_id != record.model_id
            or self.acquisition_plan_sha256 != _plan_digest(plan)
            or self.helper_contract_sha256 != helper_digest
            or not self.image_reference.endswith(
                helper["repository_suffix"] + "@" + image_digest
            )
        ):
            raise CatalogError("acquisition helper image admission differs from the model plan")
        return {
            "reference": self.image_reference,
            "digest": self.image_digest,
            "receipt_digest": self.receipt_digest,
            "registry_identity_sha256": self.registry_identity_sha256,
            "build_identity_sha256": self.build_identity_sha256,
            "valid_until": self.valid_until,
        }


def _plan_digest(plan: AcquisitionPlan) -> str:
    return hashlib.sha256(canonical_bytes(plan.to_dict())).hexdigest()


def _validate_acquisition(
    store: EvidenceStore,
    digest: str,
    record: ModelRecord,
    manifest: ArtifactManifest,
    plan: AcquisitionPlan,
) -> dict[str, Any]:
    value = _exact(
        store.receipt(
            "acquisition", digest, ACQUISITION_RECEIPT_SCHEMA, record.model_id
        ),
        {
            "schema",
            "receipt_digest",
            "worker_result_digest",
            "operation_id",
            "model_id",
            "model_digest",
            "method",
            "source",
            "artifact_manifest_digest",
            "artifact_content_digest",
            "content_uri",
            "prerequisite_ids",
            "storage",
            "credential_source",
            "token_used",
            "publication",
            "controller_owner",
            "acquisition_plan_sha256",
            "helper_image",
            "execution",
            "filesystem_write_proof",
            "lock_path",
            "capacity_bound_bytes",
            "reserve_bytes",
            "free_bytes_before",
            "free_bytes_after",
            "outcome",
            "cleanup",
        },
        "artifact acquisition receipt",
    )
    plan_value = plan.to_dict()
    if (
        value["model_id"] != record.model_id
        or value["model_digest"] != record.digest
        or value["method"] != plan.method
    ):
        raise CatalogError("artifact acquisition receipt differs from the model plan")
    if value["acquisition_plan_sha256"] != _plan_digest(plan):
        raise CatalogError("artifact acquisition receipt differs from its exact plan digest")
    source = _exact(value["source"], {"repository", "revision"}, "acquisition source")
    if source != {
        "repository": plan_value["repository"],
        "revision": plan_value["revision"],
    }:
        raise CatalogError("artifact acquisition source differs from the exact plan")
    if (
        value["artifact_manifest_digest"] != manifest.digest
        or value["artifact_content_digest"] != manifest.content_digest
    ):
        raise CatalogError("artifact acquisition receipt differs from its manifest")
    provider_block = plan_value["publication"] == "atomic-content-addressed-provider-block-pvc"
    content_uri = canonical_content_uri(
        value["content_uri"],
        model_id=record.model_id,
        content_digest=manifest.content_digest,
        scheme="pvc" if provider_block else "sfs",
    )
    prerequisite_ids = _list(value["prerequisite_ids"], "acquisition prerequisites")
    if prerequisite_ids != list(plan.required_prerequisite_ids):
        raise CatalogError("artifact acquisition prerequisites differ from the plan")
    storage = _exact(
        value["storage"],
        {"mode", "contract", "pvc_namespace", "pvc_name"},
        "artifact acquisition storage",
    )
    expected_storage = (
        {
            "mode": "provider-block-pvc",
            "contract": "fs2-serve.nebius.ai/provider-block-pvc/v1",
            "pvc_namespace": "fs2-models",
            "pvc_name": "qwen3-8b-weights",
        }
        if provider_block
        else {
            "mode": "sfs-pvc",
            "contract": "fs2-models/shared-cache-pvc",
            "pvc_namespace": "fs2-models",
            "pvc_name": "fs2-cache",
        }
    )
    if storage != expected_storage:
        raise CatalogError("artifact acquisition storage differs from the exact plan")
    if value["publication"] != plan_value["publication"]:
        raise CatalogError("artifact acquisition publication is not atomic/content-addressed")
    expected_owner = (
        "fs2-serve-acquirer"
        if plan.method == "huggingface-public-snapshot"
        else "nim-operator-nimcache"
    )
    if value["controller_owner"] != expected_owner:
        raise CatalogError("artifact acquisition has a foreign cache controller")
    execution = _exact(
        value["execution"],
        {
            "run_as_non_root",
            "run_as_uid",
            "run_as_gid",
            "fs_group",
            "supplemental_groups_policy",
            "seccomp_profile",
            "job",
            "pod",
        },
        "artifact acquisition execution identity",
    )
    expected_security = {
        "run_as_non_root": True,
        "run_as_uid": 10001,
        "run_as_gid": 10001,
        "fs_group": 10001,
        "supplemental_groups_policy": "Strict",
        "seccomp_profile": "RuntimeDefault",
    }
    if any(execution[key] != expected_security[key] for key in expected_security):
        raise CatalogError("artifact acquisition did not use the deterministic non-root identity")
    operation_id = _text(value["operation_id"], "artifact acquisition operation ID")
    job = _exact(
        execution["job"],
        {"api_version", "kind", "namespace", "name", "uid"},
        "artifact acquisition Job identity",
    )
    pod = _exact(
        execution["pod"],
        {"api_version", "kind", "namespace", "name", "uid", "owner_job_uid"},
        "artifact acquisition Pod identity",
    )
    if (
        operation_id is None
        or job["api_version"] != "batch/v1"
        or job["kind"] != "Job"
        or job["namespace"] != "fs2-models"
        or job["name"] != f"{record.model_id}-cache-{operation_id}"
        or K8S_UID.fullmatch(job["uid"]) is None
        or pod["api_version"] != "v1"
        or pod["kind"] != "Pod"
        or pod["namespace"] != "fs2-models"
        or not pod["name"].startswith(job["name"] + "-")
        or K8S_UID.fullmatch(pod["uid"]) is None
        or pod["owner_job_uid"] != job["uid"]
    ):
        raise CatalogError("artifact acquisition lacks the exact server-observed Job/Pod join")
    filesystem_write_proof = value["filesystem_write_proof"]
    if provider_block:
        proof = _exact(
            filesystem_write_proof,
            {
                "filesystem_type",
                "probe_path",
                "operation",
                "bytes_written",
                "payload_sha256",
                "file_uid",
                "file_gid",
                "file_mode",
                "marker_removed",
                "directory_fsync",
            },
            "provider block fresh filesystem write proof",
        )
        if (
            proof["filesystem_type"] != "ext4"
            or proof["probe_path"] != plan_value["destination_prefix"]
            or proof["operation"] != "exclusive-create-write-fsync-read-unlink"
            or proof["bytes_written"] <= 0
            or strong_sha256(proof["payload_sha256"], "fresh write proof payload")
            != hashlib.sha256(b"fs2-provider-block-fresh-write-proof/v1\n").hexdigest()
            or proof["file_uid"] != execution["run_as_uid"]
            or proof["file_gid"] != execution["run_as_gid"]
            or proof["file_mode"] != "0600"
            or proof["marker_removed"] is not True
            or proof["directory_fsync"] is not True
        ):
            raise CatalogError("provider block fresh ext4 write proof is invalid")
    elif filesystem_write_proof is not None:
        raise CatalogError("non-provider acquisition cannot claim a provider ext4 write proof")
    lock_path = _text(value["lock_path"], "artifact acquisition lock path")
    if lock_path is None or record.model_id not in lock_path:
        raise CatalogError("artifact acquisition lock does not identify the model path")
    capacity_bound = record.to_dict()["cache"]["artifact"]["capacity_bound_bytes"]
    if value["capacity_bound_bytes"] != capacity_bound:
        raise CatalogError("artifact acquisition differs from the reviewed capacity bound")
    for field in ("reserve_bytes", "free_bytes_before", "free_bytes_after"):
        measured = value[field]
        if isinstance(measured, bool) or not isinstance(measured, int) or measured < 0:
            raise CatalogError(f"artifact acquisition {field} is invalid")
    if value["free_bytes_before"] < capacity_bound + value["reserve_bytes"]:
        raise CatalogError("artifact acquisition did not prove sufficient destination free space")
    _enum(value["outcome"], {"acquired", "already-present"}, "acquisition outcome")
    cleanup = _exact(
        value["cleanup"],
        {
            "completed_at",
            "observer_identity_sha256",
            "controller_identity_sha256",
            "api_server_observed",
            "expected_resources",
            "resources",
            "temporary_path_absent",
            "write_marker_absent",
            "foreign_uids_touched",
        },
        "acquisition cleanup",
    )
    cleanup_completed = _utc(cleanup["completed_at"], "acquisition cleanup completion")
    if cleanup_completed > store.attestation_issued_at("acquisition", digest) + MAX_EVENT_CLOCK_SKEW:
        raise CatalogError("artifact acquisition cleanup was signed before observation")
    strong_sha256(cleanup["observer_identity_sha256"], "acquisition cleanup observer")
    strong_sha256(cleanup["controller_identity_sha256"], "acquisition cleanup controller")
    expected_resources = [
        {key: job[key] for key in ("api_version", "kind", "namespace", "name", "uid")},
        {
            key: pod[key]
            for key in (
                "api_version",
                "kind",
                "namespace",
                "name",
                "uid",
                "owner_job_uid",
            )
        },
    ]
    if (
        cleanup["api_server_observed"] is not True
        or cleanup["expected_resources"] != expected_resources
        or cleanup["temporary_path_absent"] is not True
        or cleanup["write_marker_absent"] is not True
        or cleanup["foreign_uids_touched"] is not False
        or not isinstance(cleanup["resources"], list)
        or len(cleanup["resources"]) != 2
    ):
        raise CatalogError("artifact acquisition cleanup did not close its exact UID set")
    for expected, raw in zip(expected_resources, cleanup["resources"], strict=True):
        resource_fields = {
            "api_version",
            "kind",
            "namespace",
            "name",
            "uid",
            "delete_precondition_uid",
            "final_state",
            "replacement_uid",
            "replacement_touched",
        }
        if expected["kind"] == "Pod":
            resource_fields.add("owner_job_uid")
        observed = _exact(
            raw,
            resource_fields,
            "artifact acquisition cleaned resource",
        )
        replacement_uid = observed["replacement_uid"]
        if (
            any(observed[key] != expected[key] for key in expected)
            or observed["delete_precondition_uid"] != expected["uid"]
            or observed["final_state"] != "absent"
            or observed["replacement_touched"] is not False
            or (
                replacement_uid is not None
                and (
                    not isinstance(replacement_uid, str)
                    or K8S_UID.fullmatch(replacement_uid) is None
                    or replacement_uid == expected["uid"]
                )
            )
        ):
            raise CatalogError("artifact acquisition cleanup is not replacement-safe")
    if plan.method == "huggingface-public-snapshot":
        if value["credential_source"] != "none-public-revision" or value["token_used"] is not False:
            raise CatalogError("public Hugging Face acquisition cannot use a credential")
        worker_result_digest = strong_sha256(
            value["worker_result_digest"], "artifact acquisition worker result digest"
        )
        helper_image = _exact(
            value["helper_image"],
            {
                "id",
                "reference",
                "digest",
                "admission_receipt_digest",
                "registry_identity_sha256",
                "build_identity_sha256",
                "helper_contract_sha256",
            },
            "artifact acquisition helper image subject",
        )
        helper_admission = _load_acquisition_helper_image_admission_from_store(
            record,
            plan,
            store,
            receipt_digest=helper_image["admission_receipt_digest"],
        )
        admitted = helper_admission.authorize(record, plan)
        if helper_image != {
            "id": "fs2-acquisition-helper",
            "reference": admitted["reference"],
            "digest": admitted["digest"],
            "admission_receipt_digest": admitted["receipt_digest"],
            "registry_identity_sha256": admitted["registry_identity_sha256"],
            "build_identity_sha256": admitted["build_identity_sha256"],
            "helper_contract_sha256": helper_admission.helper_contract_sha256,
        }:
            raise CatalogError("artifact acquisition used another helper image")
    elif plan.method == "ngc-target-node-nimcache":
        if (
            value["credential_source"] != "kubernetes-secret-value-suppressed"
            or value["token_used"] is not True
        ):
            raise CatalogError("NGC acquisition must use the value-suppressed target-node Secret")
        if value["worker_result_digest"] is not None or value["helper_image"] is not None:
            raise CatalogError("NIMCache acquisition cannot claim the public helper image")
        worker_result_digest = None
        helper_image = None
    else:
        raise CatalogError("blocked acquisition plan cannot produce a promotion receipt")
    store.assert_claims(
        "acquisition",
        digest,
        {
            "model_digest": record.digest,
            "acquisition_plan_sha256": _plan_digest(plan),
            "artifact_manifest_digest": manifest.digest,
            "artifact_content_digest": manifest.content_digest,
            "content_uri": content_uri,
            "prerequisite_set_sha256": hashlib.sha256(
                canonical_bytes(prerequisite_ids)
            ).hexdigest(),
            "storage_contract_sha256": hashlib.sha256(
                canonical_bytes(storage)
            ).hexdigest(),
            "execution_identity_sha256": hashlib.sha256(
                canonical_bytes(execution)
            ).hexdigest(),
            "worker_result_digest": worker_result_digest,
            "helper_image_identity_sha256": (
                hashlib.sha256(canonical_bytes(helper_image)).hexdigest()
                if helper_image is not None
                else None
            ),
            "cleanup_identity_sha256": hashlib.sha256(
                canonical_bytes(cleanup)
            ).hexdigest(),
            "filesystem_write_proof_sha256": (
                hashlib.sha256(canonical_bytes(filesystem_write_proof)).hexdigest()
                if filesystem_write_proof is not None
                else None
            ),
        },
    )
    return value


def _validate_prerequisites(
    store: EvidenceStore,
    digest: str,
    record: ModelRecord,
    catalog: Catalog,
    plan: AcquisitionPlan,
) -> dict[str, Any]:
    value = _exact(
        store.receipt(
            "prerequisites", digest, PREREQUISITE_RECEIPT_SCHEMA, record.model_id
        ),
        {
            "schema",
            "receipt_digest",
            "status",
            "checked_at",
            "model_id",
            "model_digest",
            "acquisition_plan_sha256",
            "observation",
        },
        "runtime prerequisite receipt",
    )
    if (
        value["status"] != "PASS"
        or value["model_id"] != record.model_id
        or value["model_digest"] != record.digest
        or value["acquisition_plan_sha256"] != _plan_digest(plan)
    ):
        raise CatalogError("runtime prerequisite receipt differs from the model plan")
    checked_at = _utc(value["checked_at"], "prerequisite check time")
    if checked_at > store.attestation_issued_at("prerequisites", digest) + MAX_EVENT_CLOCK_SKEW:
        raise CatalogError("prerequisite receipt was signed before observation completed")
    prerequisite_binding = bind_runtime_prerequisites(
        catalog,
        value["observation"],
        required_ids=plan.required_prerequisite_ids,
    )
    resources = value["observation"]["resources"]
    observed_ids = [item["id"] for item in resources]
    if observed_ids != list(plan.required_prerequisite_ids):
        raise CatalogError("prerequisite receipt must contain exactly the plan resources")
    materialization = prerequisite_binding.ngc_credential_materialization
    materialization_digest = (
        hashlib.sha256(canonical_bytes(dict(materialization))).hexdigest()
        if materialization is not None
        else None
    )
    if plan.method == "ngc-target-node-nimcache":
        if materialization is None:
            raise CatalogError("NGC route lacks fresh platform credential materialization")
        materialized_at = _utc(
            materialization["materialized_at"], "NGC credential materialization time"
        )
        valid_until = _utc(
            materialization["valid_until"], "NGC credential validity time"
        )
        if materialized_at > checked_at or valid_until <= store.now():
            raise CatalogError("NGC credential receipt is stale or postdates observation")
    elif materialization is not None:
        raise CatalogError("non-NGC route may not claim NGC credential materialization")
    store.assert_claims(
        "prerequisites",
        digest,
        {
            "model_digest": record.digest,
            "acquisition_plan_sha256": _plan_digest(plan),
            "resource_identity_set_sha256": hashlib.sha256(
                canonical_bytes(resources)
            ).hexdigest(),
            "values_suppressed": True,
            "legacy_ngc_secret_copied": False,
            "legacy_plaintext_rotation_source_used": False,
            "legacy_phase_7c_hmac_reused": False,
            "exposed_evo_bearer_reused": False,
            "ngc_credential_materialization_sha256": materialization_digest,
        },
    )
    return value


def _validate_target_node_canary(
    store: EvidenceStore,
    digest: str,
    record: ModelRecord,
    plan: AcquisitionPlan,
    runtime: Mapping[str, Any],
    acquisition_digest: str,
    prerequisite_digest: str,
    semantic_digest: str,
    readiness_digest: str,
    placement_digest: str,
    credential_materialization_sha256: str,
) -> None:
    value = _exact(
        store.receipt(
            "target-node-canaries", digest, TARGET_NODE_CANARY_SCHEMA, record.model_id
        ),
        {
            "schema",
            "receipt_digest",
            "status",
            "checked_at",
            "model_id",
            "model_digest",
            "acquisition_plan_sha256",
            "acquisition_receipt_digest",
            "prerequisite_receipt_digest",
            "runtime_identity_digest",
            "semantic_receipt_digest",
            "readiness_receipt_digest",
            "nim_cache_receipt_digest",
            "credential_materialization_sha256",
            "image",
            "worker",
            "secret_requirement_ids",
            "values_suppressed",
            "readiness",
        },
        "target-node pull/runtime canary",
    )
    runtime_digest = runtime["receipt_digest"]
    if (
        value["status"] != "PASS"
        or value["model_id"] != record.model_id
        or value["model_digest"] != record.digest
        or value["acquisition_plan_sha256"] != _plan_digest(plan)
        or value["acquisition_receipt_digest"] != acquisition_digest
        or value["prerequisite_receipt_digest"] != prerequisite_digest
        or value["runtime_identity_digest"] != runtime_digest
        or value["semantic_receipt_digest"] != semantic_digest
        or value["readiness_receipt_digest"] != readiness_digest
        or value["nim_cache_receipt_digest"] != placement_digest
        or value["credential_materialization_sha256"]
        != credential_materialization_sha256
    ):
        raise CatalogError("target-node canary subjects differ from the live route")
    if value["values_suppressed"] is not True:
        raise CatalogError("target-node canary must suppress credential values")
    checked_at = _utc(value["checked_at"], "target-node canary check time")
    if checked_at > store.attestation_issued_at(
        "target-node-canaries", digest
    ) + MAX_EVENT_CLOCK_SKEW:
        raise CatalogError("target-node canary was signed before checks completed")
    record_value = record.to_dict()
    image = _exact(value["image"], {"reference", "digest"}, "canary image")
    if image != {
        "reference": record_value["runtime"]["image"]["reference"],
        "digest": record_value["runtime"]["image"]["digest"],
    }:
        raise CatalogError("target-node canary image differs from the runtime")
    runtime_worker = runtime["worker"]
    worker = _exact(
        value["worker"],
        {
            "node_uid",
            "node_image_id",
            "worker_image_digest",
            "driver_version",
            "cuda_version",
            "device_plugin_image_digest",
            "gpu_tuple_sha256",
        },
        "target-node canary worker",
    )
    node_uid = _text(worker["node_uid"], "target-node canary node UID")
    _text(worker["node_image_id"], "target-node canary node image ID")
    if node_uid is None or K8S_UID.fullmatch(node_uid) is None:
        raise CatalogError("target-node canary lacks the exact Kubernetes Node UID")
    expected_worker = {
        "worker_image_digest": runtime_worker["image_digest"],
        "driver_version": runtime_worker["nvidia_driver_version"],
        "cuda_version": runtime_worker["cuda_version"],
        "device_plugin_image_digest": runtime_worker["device_plugin"]["image_digest"],
        "gpu_tuple_sha256": hashlib.sha256(
            canonical_bytes(runtime_worker["gpu"])
        ).hexdigest(),
    }
    if any(worker[key] != expected_worker[key] for key in expected_worker):
        raise CatalogError("target-node canary worker differs from the exact B300 tuple")
    secret_ids = _list(value["secret_requirement_ids"], "canary Secret requirements")
    expected_secrets = [
        "fs2-models/ngc-pull-secret",
        "fs2-models/ngc-runtime-secret",
    ]
    if secret_ids != expected_secrets:
        raise CatalogError("target-node canary does not bind both NGC Secret identities")
    readiness = _exact(value["readiness"], {"path", "http_status"}, "canary readiness")
    expected_readiness = record_value["interface"]["readiness"]
    if readiness != {
        "path": expected_readiness["path"],
        "http_status": expected_readiness["expected_status"],
    }:
        raise CatalogError("target-node canary readiness differs from the model contract")
    claims = {
        "model_digest": record.digest,
        "acquisition_plan_sha256": _plan_digest(plan),
        "acquisition_receipt_digest": acquisition_digest,
        "prerequisite_receipt_digest": prerequisite_digest,
        "runtime_tuple_digest": runtime_digest,
        "semantic_receipt_digest": semantic_digest,
        "readiness_receipt_digest": readiness_digest,
        "nim_cache_receipt_digest": placement_digest,
        "credential_materialization_sha256": credential_materialization_sha256,
        "image_digest": image["digest"],
        "worker_identity_sha256": hashlib.sha256(canonical_bytes(worker)).hexdigest(),
        "secret_requirement_set_sha256": hashlib.sha256(
            canonical_bytes(secret_ids)
        ).hexdigest(),
    }
    store.assert_claims("target-node-canaries", digest, claims)


def _utc(value: Any, label: str) -> datetime:
    text = _text(value, label)
    assert text is not None
    if UTC_TIMESTAMP.fullmatch(text) is None:
        raise CatalogError(f"{label} is not an exact UTC timestamp")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise CatalogError(f"{label} is not an exact UTC timestamp") from exc
    if parsed.tzinfo != timezone.utc:
        raise CatalogError(f"{label} is not UTC")
    return parsed


def _node_identity(value: Any, label: str) -> dict[str, str]:
    node = _exact(value, {"name", "uid", "provider_id_sha256"}, label)
    name = _text(node["name"], f"{label} name")
    uid = _text(node["uid"], f"{label} UID")
    if name is None or len(name) > 253 or uid is None or K8S_UID.fullmatch(uid) is None:
        raise CatalogError(f"{label} is not an exact Kubernetes Node identity")
    provider = strong_sha256(
        node["provider_id_sha256"], f"{label} provider identity"
    )
    return {"name": name, "uid": uid, "provider_id_sha256": provider}


def _evidence_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise CatalogError("qualification evidence contains a duplicate JSON key")
        value[key] = item
    return value


def _preflight_evidence_json(text: str) -> None:
    depth = 0
    nodes = 0
    string_chars = 0
    collections: list[list[Any]] = []
    in_string = False
    escaped = False
    in_scalar = False
    for character in text:
        if in_string:
            string_chars += 1
            if string_chars > MAX_EVIDENCE_JSON_STRING_CHARS:
                raise CatalogError("qualification evidence JSON string exceeds its bound")
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
            string_chars = 0
            in_scalar = False
            nodes += 1
        elif character in "[{":
            depth += 1
            nodes += 1
            collections.append([character, 0])
            in_scalar = False
            if depth > MAX_EVIDENCE_JSON_DEPTH:
                raise CatalogError("qualification evidence JSON nesting exceeds its bound")
        elif character in "]}":
            expected = "[" if character == "]" else "{"
            if not collections or collections[-1][0] != expected:
                raise CatalogError("qualification evidence JSON delimiters are unbalanced")
            collections.pop()
            depth -= 1
            in_scalar = False
            if depth < 0:
                raise CatalogError("qualification evidence JSON delimiters are unbalanced")
        elif character == ",":
            if collections:
                collections[-1][1] += 1
                if collections[-1][1] >= MAX_EVIDENCE_JSON_COLLECTION_ITEMS:
                    raise CatalogError(
                        "qualification evidence JSON collection size exceeds its bound"
                    )
            in_scalar = False
        elif character == ":":
            in_scalar = False
        elif character.isspace():
            in_scalar = False
        elif not in_scalar:
            nodes += 1
            in_scalar = True
        if nodes > MAX_EVIDENCE_JSON_NODES:
            raise CatalogError("qualification evidence JSON node count exceeds its bound")
    if in_string or escaped or depth != 0 or collections:
        raise CatalogError("qualification evidence JSON is truncated or unbalanced")


def _bounded_json_int(value: str) -> int:
    if len(value) > 20:
        raise CatalogError("qualification evidence JSON integer exceeds its bound")
    try:
        parsed = int(value)
    except (ValueError, OverflowError) as exc:
        raise CatalogError("qualification evidence JSON integer is invalid") from exc
    if abs(parsed) > MAX_EVIDENCE_JSON_INTEGER:
        raise CatalogError("qualification evidence JSON integer exceeds its bound")
    return parsed


def _bounded_json_float(value: str) -> float:
    if len(value) > 128:
        raise CatalogError("qualification evidence JSON number exceeds its bound")
    try:
        parsed = float(value)
    except (ValueError, OverflowError) as exc:
        raise CatalogError("qualification evidence JSON number is invalid") from exc
    if not math.isfinite(parsed) or abs(parsed) > MAX_EVIDENCE_JSON_FLOAT:
        raise CatalogError("qualification evidence JSON number exceeds its bound")
    return parsed


def _reject_json_constant(value: str) -> None:
    raise CatalogError(f"qualification evidence JSON constant is forbidden: {value}")


def _strict_evidence_json_object(raw: bytes, label: str) -> dict[str, Any]:
    if not raw or len(raw) > MAX_EVIDENCE_SUBJECT_BYTES:
        raise CatalogError(f"{label} byte length is outside its bound")
    try:
        text = raw.decode("utf-8")
        _preflight_evidence_json(text)
        value = json.loads(
            text,
            object_pairs_hook=_evidence_json_pairs,
            parse_int=_bounded_json_int,
            parse_float=_bounded_json_float,
            parse_constant=_reject_json_constant,
        )
        if not isinstance(value, dict):
            raise CatalogError(f"{label} must be a JSON object")
        stack: list[Any] = [value]
        nodes = 0
        while stack:
            item = stack.pop()
            nodes += 1
            if nodes > MAX_EVIDENCE_JSON_NODES:
                raise CatalogError(f"{label} node count exceeds its bound")
            if isinstance(item, dict):
                if len(item) > MAX_EVIDENCE_JSON_COLLECTION_ITEMS:
                    raise CatalogError(f"{label} object size exceeds its bound")
                for key, child in item.items():
                    if len(key) > MAX_EVIDENCE_JSON_STRING_CHARS:
                        raise CatalogError(f"{label} key length exceeds its bound")
                    stack.append(child)
            elif isinstance(item, list):
                if len(item) > MAX_EVIDENCE_JSON_COLLECTION_ITEMS:
                    raise CatalogError(f"{label} array size exceeds its bound")
                stack.extend(item)
            elif isinstance(item, str):
                if len(item) > MAX_EVIDENCE_JSON_STRING_CHARS:
                    raise CatalogError(f"{label} string length exceeds its bound")
            elif isinstance(item, bool) or item is None:
                continue
            elif isinstance(item, int):
                if abs(item) > MAX_EVIDENCE_JSON_INTEGER:
                    raise CatalogError(f"{label} integer exceeds its bound")
            elif isinstance(item, float):
                if not math.isfinite(item) or abs(item) > MAX_EVIDENCE_JSON_FLOAT:
                    raise CatalogError(f"{label} number exceeds its bound")
            else:
                raise CatalogError(f"{label} contains an unsupported JSON value")
        return value
    except CatalogError:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
        OverflowError,
        MemoryError,
    ) as exc:
        raise CatalogError(f"{label} is not bounded strict UTF-8 JSON") from exc


class EvidenceStore:
    """Loads signed, fresh, session-bound evidence from a closed directory layout."""

    @staticmethod
    def _open_root_descriptor(root: Path, flags: int) -> int:
        """Open an absolute evidence root without following any path component."""

        descriptor = os.open(os.sep, flags)
        try:
            for component in root.parts[1:]:
                child = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def __init__(
        self,
        root: Path | str,
        *,
        session_id: str,
        trusted_attestors: Mapping[str, str],
        validation_time: datetime | None,
    ):
        self.root = Path(os.path.abspath(os.fspath(root)))
        self._expected_uid = os.geteuid()
        self._expected_gid = os.getegid()
        root_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            self._root_fd = self._open_root_descriptor(self.root, root_flags)
            root_info = os.fstat(self._root_fd)
        except OSError as exc:
            raise CatalogError(
                "qualification evidence root cannot be opened without following links"
            ) from exc
        if not stat.S_ISDIR(root_info.st_mode):
            os.close(self._root_fd)
            raise CatalogError("qualification evidence root must be a directory")
        try:
            self._validate_metadata(root_info, directory=True, label="evidence root")
        except CatalogError:
            os.close(self._root_fd)
            raise
        self._root_identity = self._security_identity(root_info)
        self._root_finalizer = weakref.finalize(self, os.close, self._root_fd)
        self.session_id = strong_sha256(session_id, "qualification evidence session ID")
        if not isinstance(trusted_attestors, Mapping) or not trusted_attestors:
            raise CatalogError("enabled routing requires explicit trusted attestor public keys")
        self.trusted_attestors = trusted_attestors
        self.validation_time = validation_time
        self._attestations: dict[tuple[str, str], dict[str, Any]] = {}
        self._nonces: set[str] = set()
        self._subjects: set[tuple[str, str]] = set()
        self._valid_until: datetime | None = None

    def close(self) -> None:
        """Release the pinned evidence-root descriptor."""

        self._root_finalizer()

    def __enter__(self) -> EvidenceStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _identity(info: os.stat_result) -> tuple[int, ...]:
        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_nlink,
            info.st_uid,
            info.st_gid,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    @staticmethod
    def _security_identity(info: os.stat_result) -> tuple[int, ...]:
        """Identity and policy fields unaffected by a safe root rename."""

        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_nlink,
            info.st_uid,
            info.st_gid,
        )

    def _validate_metadata(
        self, info: os.stat_result, *, directory: bool, label: str
    ) -> None:
        expected_kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if not expected_kind:
            raise CatalogError(f"qualification {label} has the wrong file type")
        if info.st_uid != self._expected_uid or info.st_gid != self._expected_gid:
            raise CatalogError(f"qualification {label} has an unexpected owner")
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise CatalogError(f"qualification {label} is group/world writable")
        # POSIX directory link counts include '.' and child '..' entries and
        # therefore cannot portably equal one. Directory hard links are not
        # user-creatable on the supported Linux filesystems; their positive
        # link count is pinned and rechecked in _identity. Regular subjects
        # must have exactly one name.
        if (directory and info.st_nlink < 2) or (not directory and info.st_nlink != 1):
            raise CatalogError(f"qualification {label} has an unsafe link count")

    def _read_subject(self, kind: str, digest: str, suffix: str = "json") -> bytes:
        """Read one regular subject through pinned no-follow descriptors only."""

        strong_sha256(digest, "qualification evidence digest")
        components = kind.split("/")
        if (
            suffix not in {"json", "bin"}
            or not components
            or any(EVIDENCE_PATH_COMPONENT.fullmatch(item) is None for item in components)
        ):
            raise CatalogError("qualification evidence kind/path is not canonical")
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        leaf_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        directory_fds: list[int] = []
        directory_entries: list[tuple[int, str, tuple[int, ...]]] = []
        leaf_fd: int | None = None
        leaf_name = f"{digest}.{suffix}"
        try:
            root_before = os.fstat(self._root_fd)
            self._validate_metadata(root_before, directory=True, label="evidence root")
            if self._security_identity(root_before) != self._root_identity:
                raise CatalogError(
                    "qualification evidence root changed during its custodied read"
                )
            directory_fds.append(os.dup(self._root_fd))
            for component in components:
                parent_fd = directory_fds[-1]
                child_fd = os.open(component, directory_flags, dir_fd=parent_fd)
                child_info = os.fstat(child_fd)
                try:
                    self._validate_metadata(
                        child_info,
                        directory=True,
                        label=f"evidence directory {component}",
                    )
                except CatalogError:
                    os.close(child_fd)
                    raise
                directory_entries.append(
                    (parent_fd, component, self._identity(child_info))
                )
                directory_fds.append(child_fd)
            parent_fd = directory_fds[-1]
            leaf_fd = os.open(leaf_name, leaf_flags, dir_fd=parent_fd)
            before = os.fstat(leaf_fd)
            self._validate_metadata(before, directory=False, label="evidence subject")
            if (
                before.st_size <= 0
                or before.st_size > MAX_EVIDENCE_SUBJECT_BYTES
            ):
                raise CatalogError("qualification evidence subject is not a bounded regular file")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(leaf_fd, min(1024 * 1024, MAX_EVIDENCE_SUBJECT_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_EVIDENCE_SUBJECT_BYTES:
                    raise CatalogError("qualification evidence subject exceeds its size bound")
            after = os.fstat(leaf_fd)
            current_leaf = os.stat(leaf_name, dir_fd=parent_fd, follow_symlinks=False)
            self._validate_metadata(
                current_leaf, directory=False, label="current evidence subject"
            )
            if (
                self._identity(before) != self._identity(after)
                or self._identity(after) != self._identity(current_leaf)
                or total != after.st_size
            ):
                raise CatalogError("qualification evidence subject changed during its custodied read")
            for ancestor_fd, component, expected in directory_entries:
                current = os.stat(component, dir_fd=ancestor_fd, follow_symlinks=False)
                self._validate_metadata(
                    current,
                    directory=True,
                    label=f"current evidence directory {component}",
                )
                if self._identity(current) != expected:
                    raise CatalogError("qualification evidence directory changed during its custodied read")
            root_after = os.fstat(self._root_fd)
            self._validate_metadata(root_after, directory=True, label="evidence root")
            if self._security_identity(root_after) != self._root_identity:
                raise CatalogError(
                    "qualification evidence root changed during its custodied read"
                )
            return b"".join(chunks)
        except CatalogError:
            raise
        except OSError as exc:
            raise CatalogError(
                f"qualification evidence is absent or unsafe: {kind}/{leaf_name}"
            ) from exc
        finally:
            if leaf_fd is not None:
                os.close(leaf_fd)
            for descriptor in reversed(directory_fds):
                os.close(descriptor)

    def _read_json_subject(
        self,
        kind: str,
        digest: str,
        *,
        label: str,
        expected_raw_sha256: str | None = None,
    ) -> tuple[dict[str, Any], bytes]:
        """Read once from the custodied descriptor and parse those exact bytes."""

        raw = self._read_subject(kind, digest)
        if (
            expected_raw_sha256 is not None
            and hashlib.sha256(raw).hexdigest() != expected_raw_sha256
        ):
            raise CatalogError(f"{label} filename/digest binding failed")
        return _strict_evidence_json_object(raw, label), raw

    def _read_json(self, kind: str, digest: str) -> dict[str, Any]:
        value, _ = self._read_json_subject(
            kind, digest, label="qualification evidence subject"
        )
        return value

    def _attest(
        self,
        *,
        kind: str,
        digest: str,
        schema: str,
        model_id: str,
    ) -> dict[str, Any]:
        subject = (kind, digest)
        if subject in self._subjects:
            raise CatalogError("signed evidence subject was replayed within one route qualification")
        value = verify_signed_attestation(
            self._read_json(f"attestations/{kind}", digest),
            trusted_attestors=self.trusted_attestors,
            expected_session_id=self.session_id,
            expected_kind=kind,
            expected_schema=schema,
            expected_digest=digest,
            expected_model_id=model_id,
            validation_time=self.validation_time,
        )
        nonce = value["nonce"]
        if nonce in self._nonces:
            raise CatalogError("signed evidence nonce was replayed within one route qualification")
        self._nonces.add(nonce)
        self._subjects.add(subject)
        self._attestations[subject] = value
        expires_at = _utc(value["expires_at"], f"{kind} attestation expires_at")
        if self._valid_until is None or expires_at < self._valid_until:
            self._valid_until = expires_at
        return value

    def assert_claims(self, kind: str, digest: str, expected: Mapping[str, Any]) -> None:
        try:
            actual = self._attestations[(kind, digest)]["claims"]
        except KeyError as exc:
            raise CatalogError("signed evidence claims were checked before its subject") from exc
        if actual != dict(expected):
            raise CatalogError(f"{kind} signed claims do not match the reopened evidence subject")

    def attestation_issued_at(self, kind: str, digest: str) -> datetime:
        try:
            value = self._attestations[(kind, digest)]["issued_at"]
        except KeyError as exc:
            raise CatalogError("signed evidence time was checked before its subject") from exc
        return _utc(value, f"{kind} attestation issued_at")

    def attestation_key_id(self, kind: str, digest: str) -> str:
        """Return the verified signer identity for an already reopened subject."""

        try:
            value = self._attestations[(kind, digest)]["key_id"]
        except KeyError as exc:
            raise CatalogError("signed evidence signer was checked before its subject") from exc
        key_id = _text(value, f"{kind} attestation key ID")
        assert key_id is not None
        return key_id

    def now(self) -> datetime:
        value = self.validation_time or datetime.now(timezone.utc).replace(microsecond=0)
        if value.tzinfo is None:
            raise CatalogError("evidence validation time must be timezone-aware")
        return value.astimezone(timezone.utc)

    def valid_until(self) -> str:
        if self._valid_until is None:
            raise CatalogError("route evidence did not establish an attestation expiry")
        return self._valid_until.isoformat().replace("+00:00", "Z")

    def artifact(self, digest: str, model_id: str) -> ArtifactManifest:
        manifest = artifact_manifest_from_value(self._read_json("artifacts", digest))
        if manifest.digest != digest:
            raise CatalogError("artifact manifest filename/digest binding failed")
        self._attest(
            kind="artifacts",
            digest=digest,
            schema=manifest.to_dict()["schema"],
            model_id=model_id,
        )
        return manifest

    def receipt(self, kind: str, digest: str, schema: str, model_id: str) -> dict[str, Any]:
        value = self._read_json(kind, digest)
        if value.get("schema") != schema or value.get("receipt_digest") != digest:
            raise CatalogError(f"{kind} receipt schema or filename binding failed")
        unsigned = dict(value)
        unsigned.pop("receipt_digest", None)
        calculated = hashlib.sha256(canonical_bytes(unsigned)).hexdigest()
        if calculated != digest:
            raise CatalogError(f"{kind} receipt content digest failed")
        self._attest(kind=kind, digest=digest, schema=schema, model_id=model_id)
        return value

    def raw_object(
        self, kind: str, digest: str, schema: str, model_id: str
    ) -> tuple[dict[str, Any], bytes]:
        """Read exact immutable JSON bytes once and verify their signature."""

        value, raw = self._read_json_subject(
            kind,
            digest,
            label=f"{kind} raw object",
            expected_raw_sha256=digest,
        )
        if value.get("schema") != schema:
            raise CatalogError(f"{kind} raw object schema binding failed")
        self._attest(kind=kind, digest=digest, schema=schema, model_id=model_id)
        return value, raw

    def raw_bytes(self, kind: str, digest: str, schema: str, model_id: str) -> bytes:
        """Read an exact immutable byte subject once and verify its attestation."""

        raw = self._read_subject(kind, digest, "bin")
        if not raw or hashlib.sha256(raw).hexdigest() != digest:
            raise CatalogError(f"{kind} raw-byte filename/digest binding failed")
        self._attest(kind=kind, digest=digest, schema=schema, model_id=model_id)
        return raw


def _git_object(value: Any, label: str) -> str:
    text = _text(value, label)
    assert text is not None
    if re.fullmatch(r"[0-9a-f]{40}", text) is None or len(set(text)) < 8:
        raise CatalogError(f"{label} is not an immutable non-placeholder Git object")
    return text


def _load_acquisition_helper_image_admission_from_store(
    record: ModelRecord,
    plan: AcquisitionPlan,
    store: EvidenceStore,
    *,
    receipt_digest: str,
) -> AcquisitionHelperImageAdmission:
    """Reopen the exact signed OCI helper and its supply-chain attestations."""

    plan_value = plan.to_dict()
    helper = plan_value.get("helper_image")
    if plan.method != "huggingface-public-snapshot" or not isinstance(helper, dict):
        raise CatalogError("only a public-HF acquisition plan owns a helper image")
    helper_contract_sha256 = hashlib.sha256(canonical_bytes(helper)).hexdigest()
    value = _exact(
        store.receipt(
            "acquisition-helper-images",
            receipt_digest,
            ACQUISITION_HELPER_IMAGE_ADMISSION_SCHEMA,
            record.model_id,
        ),
        {
            "schema",
            "receipt_digest",
            "status",
            "verified_at",
            "valid_until",
            "model_id",
            "model_digest",
            "acquisition_plan_sha256",
            "helper_contract_sha256",
            "image",
            "build",
            "attestations",
            "review",
        },
        "acquisition helper image admission",
    )
    if (
        value["status"] != "PASS"
        or value["model_id"] != record.model_id
        or value["model_digest"] != record.digest
        or value["acquisition_plan_sha256"] != _plan_digest(plan)
        or value["helper_contract_sha256"] != helper_contract_sha256
    ):
        raise CatalogError("acquisition helper image admission differs from the model plan")
    verified_at = _utc(value["verified_at"], "helper image verification time")
    valid_until = _utc(value["valid_until"], "helper image validity time")
    if (
        verified_at > store.attestation_issued_at(
            "acquisition-helper-images", receipt_digest
        )
        + MAX_EVENT_CLOCK_SKEW
        or valid_until <= store.now()
        or valid_until
        > _utc(store.valid_until(), "helper image outer attestation expiry")
    ):
        raise CatalogError("acquisition helper image admission is stale or time-inconsistent")
    image = _exact(
        value["image"],
        {"id", "reference", "digest", "registry_identity_sha256", "os", "architecture"},
        "acquisition helper image",
    )
    image_digest = strong_sha256(image["digest"], "acquisition helper image digest", image=True)
    registry_identity = strong_sha256(
        image["registry_identity_sha256"], "acquisition helper registry identity"
    )
    if (
        image["id"] != "fs2-acquisition-helper"
        or image["os"] != helper["platform"]["os"]
        or image["architecture"] != helper["platform"]["architecture"]
        or not isinstance(image["reference"], str)
        or not image["reference"].endswith(
            helper["repository_suffix"] + "@" + image_digest
        )
    ):
        raise CatalogError("acquisition helper image differs from its catalog contract")
    build = _exact(
        value["build"],
        {
            "repository",
            "source_commit",
            "source_tree",
            "source_path",
            "package",
            "package_version",
            "wheel_sha256",
            "pyproject_sha256",
            "uv_lock_sha256",
            "entrypoint",
        },
        "acquisition helper build",
    )
    source = helper["build_source"]
    for field in (
        "repository",
        "path",
        "package",
        "package_version",
        "pyproject_sha256",
        "uv_lock_sha256",
    ):
        build_field = "source_path" if field == "path" else field
        if build[build_field] != source[field]:
            raise CatalogError("acquisition helper build differs from its catalog source")
    _git_object(build["source_commit"], "acquisition helper source commit")
    _git_object(build["source_tree"], "acquisition helper source tree")
    strong_sha256(build["wheel_sha256"], "acquisition helper wheel digest")
    if build["entrypoint"] != helper["entrypoint"]:
        raise CatalogError("acquisition helper entrypoint differs from its catalog contract")
    build_identity = hashlib.sha256(canonical_bytes(build)).hexdigest()
    attestations = _exact(
        value["attestations"], {"signature", "provenance", "sbom"}, "helper attestations"
    )
    signature = _exact(
        attestations["signature"],
        {
            "verified",
            "subject_image_digest",
            "registry_identity_sha256",
            "bundle_sha256",
            "signer_identity_sha256",
        },
        "helper image signature",
    )
    if (
        signature["verified"] is not True
        or signature["registry_identity_sha256"] != registry_identity
    ):
        raise CatalogError("acquisition helper image signature is not verified")
    strong_sha256(signature["bundle_sha256"], "helper signature bundle")
    strong_sha256(signature["signer_identity_sha256"], "helper signer identity")
    provenance = _exact(
        attestations["provenance"],
        {
            "predicate_type",
            "statement_sha256",
            "subject_image_digest",
            "source_commit",
            "source_tree",
            "build_identity_sha256",
            "helper_contract_sha256",
            "builder_identity_sha256",
            "build_type",
            "materials_sha256",
            "all_container_images_digest_pinned",
        },
        "helper provenance attestation",
    )
    if (
        provenance["predicate_type"] != "https://slsa.dev/provenance/v1"
        or provenance["subject_image_digest"] != image_digest
        or provenance["source_commit"] != build["source_commit"]
        or provenance["source_tree"] != build["source_tree"]
        or provenance["build_identity_sha256"] != build_identity
        or provenance["helper_contract_sha256"] != helper_contract_sha256
        or provenance["all_container_images_digest_pinned"] is not True
        or not isinstance(provenance["build_type"], str)
        or not provenance["build_type"]
    ):
        raise CatalogError("acquisition helper provenance differs from its build subject")
    strong_sha256(provenance["statement_sha256"], "helper provenance statement")
    strong_sha256(provenance["builder_identity_sha256"], "helper builder identity")
    strong_sha256(provenance["materials_sha256"], "helper provenance materials")
    sbom = _exact(
        attestations["sbom"],
        {
            "predicate_type",
            "statement_sha256",
            "subject_image_digest",
            "package",
            "package_version",
            "wheel_sha256",
        },
        "helper SBOM attestation",
    )
    if (
        sbom["predicate_type"] != "https://spdx.dev/Document"
        or sbom["subject_image_digest"] != image_digest
        or sbom["package"] != build["package"]
        or sbom["package_version"] != build["package_version"]
        or sbom["wheel_sha256"] != build["wheel_sha256"]
    ):
        raise CatalogError("acquisition helper SBOM differs from its packaged wheel")
    strong_sha256(sbom["statement_sha256"], "helper SBOM statement")
    if signature["subject_image_digest"] != image_digest:
        raise CatalogError("acquisition helper signature names another image")
    review = _exact(
        value["review"], {"review_commit", "reviewer_identity_sha256"}, "helper review"
    )
    _git_object(review["review_commit"], "acquisition helper review commit")
    strong_sha256(review["reviewer_identity_sha256"], "helper reviewer identity")
    attestation_identity = hashlib.sha256(canonical_bytes(attestations)).hexdigest()
    store.assert_claims(
        "acquisition-helper-images",
        receipt_digest,
        {
            "model_digest": record.digest,
            "acquisition_plan_sha256": _plan_digest(plan),
            "helper_contract_sha256": helper_contract_sha256,
            "image_reference": image["reference"],
            "image_digest": image_digest,
            "registry_identity_sha256": registry_identity,
            "build_identity_sha256": build_identity,
            "attestation_identity_sha256": attestation_identity,
            "review_identity_sha256": hashlib.sha256(
                canonical_bytes(review)
            ).hexdigest(),
        },
    )
    return AcquisitionHelperImageAdmission(
        model_id=record.model_id,
        model_digest=record.digest,
        acquisition_plan_sha256=_plan_digest(plan),
        helper_contract_sha256=helper_contract_sha256,
        receipt_digest=receipt_digest,
        image_reference=image["reference"],
        image_digest=image_digest,
        registry_identity_sha256=registry_identity,
        build_identity_sha256=build_identity,
        valid_until=value["valid_until"],
        _seal=_VERIFIED_ACQUISITION_HELPER_IMAGE_SEAL,
    )


def load_acquisition_helper_image_admission(
    record: ModelRecord,
    plan: AcquisitionPlan,
    evidence_root: Path | str,
    *,
    receipt_digest: str,
    evidence_session_id: str,
    trusted_attestors: Mapping[str, str],
    validation_time: datetime | None = None,
) -> AcquisitionHelperImageAdmission:
    """Public signed-evidence loader used by acquisition controllers."""

    return _load_acquisition_helper_image_admission_from_store(
        record,
        plan,
        EvidenceStore(
            evidence_root,
            session_id=evidence_session_id,
            trusted_attestors=trusted_attestors,
            validation_time=validation_time,
        ),
        receipt_digest=receipt_digest,
    )


def _load_protected_storage_class_admission_from_store(
    record: ModelRecord,
    store: EvidenceStore,
    *,
    receipt_digest: str,
) -> ProtectedStorageClassAdmission:
    """Reopen one protected-class observation through an existing evidence store."""
    value = _exact(
        store.receipt(
            "protected-storage-classes",
            receipt_digest,
            PROTECTED_STORAGE_CLASS_RECEIPT_SCHEMA,
            record.model_id,
        ),
        {
            "schema",
            "receipt_digest",
            "status",
            "observed_at",
            "model_id",
            "model_digest",
            "observer",
            "storage_class",
            "intended_claim",
        },
        "protected StorageClass receipt",
    )
    if (
        value["status"] != "PASS"
        or value["model_id"] != record.model_id
        or value["model_digest"] != record.digest
    ):
        raise CatalogError("protected StorageClass receipt differs from the model")
    observed_at = _utc(value["observed_at"], "protected StorageClass observed_at")
    if observed_at > store.attestation_issued_at(
        "protected-storage-classes", receipt_digest
    ) + MAX_EVENT_CLOCK_SKEW:
        raise CatalogError("protected StorageClass receipt was signed before observation")
    observer = _exact(
        value["observer"],
        {
            "source",
            "api_server_observed",
            "cluster_identity_sha256",
            "api_server_identity_sha256",
            "service_account_namespace",
            "service_account_name",
            "service_account_uid",
        },
        "protected StorageClass observer",
    )
    if (
        observer["source"] != "kubernetes-apiserver-get"
        or observer["api_server_observed"] is not True
        or observer["service_account_namespace"] != "fs2-system"
        or observer["service_account_name"] != "fs2-storage-contract-observer"
    ):
        raise CatalogError("protected StorageClass was not observed by the exact API reader")
    for key in ("cluster_identity_sha256", "api_server_identity_sha256"):
        strong_sha256(observer[key], f"protected StorageClass {key}")
    observer_uid = _text(
        observer["service_account_uid"], "protected StorageClass observer UID"
    )
    if observer_uid is None or K8S_UID.fullmatch(observer_uid) is None:
        raise CatalogError("protected StorageClass observer lacks an exact ServiceAccount UID")
    storage_class = validate_provider_block_storage_class_observation(
        value["storage_class"]
    )
    static = record.to_dict()["resources"]["gpu"]["placement"]
    if (
        record.model_id != "qwen3-8b"
        or static is None
        or storage_class["metadata"]["name"]
        != static["provider_block_pvc"]["storage_class"]["name"]
    ):
        raise CatalogError("protected StorageClass receipt names another claim contract")
    intended_claim = _exact(
        value["intended_claim"],
        {"namespace", "name", "model_id", "model_digest"},
        "protected StorageClass intended claim",
    )
    expected_claim = static["provider_block_pvc"]["claim"]
    if intended_claim != {
        "namespace": expected_claim["namespace"],
        "name": expected_claim["name"],
        "model_id": record.model_id,
        "model_digest": record.digest,
    }:
        raise CatalogError("protected StorageClass intended claim differs from the catalog")
    observer_identity = hashlib.sha256(canonical_bytes(observer)).hexdigest()
    store.assert_claims(
        "protected-storage-classes",
        receipt_digest,
        {
            "model_digest": record.digest,
            "storage_class_identity_sha256": hashlib.sha256(
                canonical_bytes(storage_class)
            ).hexdigest(),
            "intended_claim_sha256": hashlib.sha256(
                canonical_bytes(intended_claim)
            ).hexdigest(),
            "observer_identity_sha256": observer_identity,
        },
    )
    return ProtectedStorageClassAdmission(
        model_id=record.model_id,
        model_digest=record.digest,
        receipt_digest=receipt_digest,
        storage_class=storage_class,
        observer_identity_sha256=observer_identity,
        _seal=_VERIFIED_STORAGE_CLASS_SEAL,
    )


def load_protected_storage_class_admission(
    record: ModelRecord,
    evidence_root: Path | str,
    *,
    receipt_digest: str,
    evidence_session_id: str,
    trusted_attestors: Mapping[str, str],
    validation_time: datetime | None = None,
) -> ProtectedStorageClassAdmission:
    """Reopen the signed API-server observation required before PVC creation."""

    store = EvidenceStore(
        evidence_root,
        session_id=evidence_session_id,
        trusted_attestors=trusted_attestors,
        validation_time=validation_time,
    )
    return _load_protected_storage_class_admission_from_store(
        record, store, receipt_digest=receipt_digest
    )


def load_provider_block_writer_admission(
    record: ModelRecord,
    storage_class_admission: ProtectedStorageClassAdmission,
    evidence_root: Path | str,
    *,
    receipt_digest: str,
    evidence_session_id: str,
    trusted_attestors: Mapping[str, str],
    validation_time: datetime | None = None,
) -> ProviderBlockWriterAdmission:
    """Reopen the API-set claim/Lease fence required before a writer Job."""

    store = EvidenceStore(
        evidence_root,
        session_id=evidence_session_id,
        trusted_attestors=trusted_attestors,
        validation_time=validation_time,
    )
    return _load_provider_block_writer_admission_from_store(
        record,
        storage_class_admission,
        store,
        receipt_digest=receipt_digest,
    )


def _load_provider_block_writer_admission_from_store(
    record: ModelRecord,
    storage_class_admission: ProtectedStorageClassAdmission,
    store: EvidenceStore,
    *,
    receipt_digest: str,
) -> ProviderBlockWriterAdmission:
    """Reopen one writer admission through the route's non-replayable store."""

    storage_class_admission.authorize(record)
    value = _exact(
        store.receipt(
            "provider-block-writer-admissions",
            receipt_digest,
            PROVIDER_BLOCK_WRITER_ADMISSION_SCHEMA,
            record.model_id,
        ),
        {
            "schema",
            "receipt_digest",
            "status",
            "admitted_at",
            "model_id",
            "model_digest",
            "operation_id",
            "storage_class_receipt_digest",
            "claim",
            "writer",
            "controller",
            "lease",
            "mount_set",
            "api_fence",
        },
        "provider block writer admission",
    )
    if (
        value["status"] != "admitted"
        or value["model_id"] != record.model_id
        or value["model_digest"] != record.digest
        or value["storage_class_receipt_digest"]
        != storage_class_admission.receipt_digest
    ):
        raise CatalogError("provider block writer admission differs from its model/class")
    admitted_at = _utc(value["admitted_at"], "provider block writer admitted_at")
    if admitted_at > store.attestation_issued_at(
        "provider-block-writer-admissions", receipt_digest
    ) + MAX_EVENT_CLOCK_SKEW:
        raise CatalogError("provider block writer admission was signed before admission")
    operation_id = _text(value["operation_id"], "provider block writer operation ID")
    if operation_id is None or re.fullmatch(
        r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", operation_id
    ) is None:
        raise CatalogError("provider block writer operation ID is not a DNS label")
    claim = _exact(
        value["claim"],
        {"namespace", "name", "uid", "resource_version"},
        "provider block writer claim",
    )
    claim_uid = _text(claim["uid"], "provider block writer claim UID")
    claim_rv = _text(
        claim["resource_version"], "provider block writer claim resourceVersion"
    )
    if (
        claim["namespace"] != "fs2-models"
        or claim["name"] != "qwen3-8b-weights"
        or claim_uid is None
        or K8S_UID.fullmatch(claim_uid) is None
        or claim_rv is None
    ):
        raise CatalogError("provider block writer admission lacks the exact PVC identity")
    writer = _exact(
        value["writer"],
        {
            "api_version",
            "kind",
            "namespace",
            "name",
            "service_account_name",
            "service_account_uid",
        },
        "provider block admitted writer",
    )
    writer_uid = _text(
        writer["service_account_uid"], "provider block writer ServiceAccount UID"
    )
    expected_job_name = f"{record.model_id}-cache-{operation_id}"
    if (
        writer["api_version"] != "batch/v1"
        or writer["kind"] != "Job"
        or writer["namespace"] != "fs2-models"
        or writer["name"] != expected_job_name
        or writer["service_account_name"] != "cache-service-account"
        or writer_uid is None
        or K8S_UID.fullmatch(writer_uid) is None
    ):
        raise CatalogError("provider block writer admission names another Job identity")
    controller = _exact(
        value["controller"],
        {
            "namespace",
            "deployment_name",
            "deployment_uid",
            "pod_name",
            "pod_uid",
            "pod_owner_deployment_uid",
            "service_account_name",
            "service_account_uid",
            "validating_admission_policy_name",
            "validating_admission_policy_uid",
            "writer_create_role_name",
            "writer_create_role_uid",
            "writer_create_role_binding_name",
            "writer_create_role_binding_uid",
            "identity_sha256",
        },
        "provider block writer admission controller",
    )
    expected_controller = {
        "namespace": "fs2-system",
        "deployment_name": "fs2-provider-block-writer-admission",
        "service_account_name": "fs2-provider-block-writer-admission",
        "validating_admission_policy_name": "fs2-provider-block-sole-writer",
        "writer_create_role_name": "fs2-provider-block-writer-create",
        "writer_create_role_binding_name": "fs2-provider-block-writer-create",
    }
    if any(controller[key] != expected for key, expected in expected_controller.items()):
        raise CatalogError("provider block writer admission used a foreign controller")
    for key in (
        "deployment_uid",
        "pod_uid",
        "pod_owner_deployment_uid",
        "service_account_uid",
        "validating_admission_policy_uid",
        "writer_create_role_uid",
        "writer_create_role_binding_uid",
    ):
        uid = _text(controller[key], f"provider block controller {key}")
        if uid is None or K8S_UID.fullmatch(uid) is None:
            raise CatalogError("provider block writer controller lacks exact API identities")
    if controller["pod_owner_deployment_uid"] != controller["deployment_uid"]:
        raise CatalogError("provider block writer controller Pod owner differs")
    controller_subject = {
        **expected_controller,
        "deployment_uid": controller["deployment_uid"],
        "pod_name": controller["pod_name"],
        "pod_uid": controller["pod_uid"],
        "pod_owner_deployment_uid": controller["pod_owner_deployment_uid"],
        "service_account_uid": controller["service_account_uid"],
        "validating_admission_policy_uid": controller[
            "validating_admission_policy_uid"
        ],
        "writer_create_role_uid": controller["writer_create_role_uid"],
        "writer_create_role_binding_uid": controller[
            "writer_create_role_binding_uid"
        ],
    }
    controller_identity = strong_sha256(
        controller["identity_sha256"], "provider block writer controller identity"
    )
    if controller_identity != hashlib.sha256(
        canonical_bytes(controller_subject)
    ).hexdigest():
        raise CatalogError("provider block writer controller identity digest differs")
    lease = _exact(
        value["lease"],
        {
            "api_version",
            "kind",
            "namespace",
            "name",
            "uid",
            "resource_version",
            "holder_identity",
            "fencing_token",
            "renew_time",
            "lease_duration_seconds",
        },
        "provider block writer Lease",
    )
    lease_uid = _text(lease["uid"], "provider block writer Lease UID")
    lease_rv = _text(
        lease["resource_version"], "provider block writer Lease resourceVersion"
    )
    holder = f"{operation_id}:{expected_job_name}"
    if (
        lease["api_version"] != "coordination.k8s.io/v1"
        or lease["kind"] != "Lease"
        or lease["namespace"] != "fs2-models"
        or lease["name"] != "qwen3-8b-weights-writer"
        or lease_uid is None
        or K8S_UID.fullmatch(lease_uid) is None
        or lease_rv is None
        or lease["holder_identity"] != holder
        or _positive_int(lease["fencing_token"], "provider block writer fence") is None
        or lease["lease_duration_seconds"] != 900
    ):
        raise CatalogError("provider block writer Lease is not the exact live fence")
    renew_time = _utc(lease["renew_time"], "provider block writer Lease renew_time")
    if renew_time > admitted_at:
        raise CatalogError("provider block writer Lease was renewed after admission observation")
    mount_set = _exact(
        value["mount_set"],
        {
            "api_server_identity_sha256",
            "namespace",
            "claim_uid",
            "list_resource_version",
            "continue_token",
            "remaining_item_count",
            "complete",
            "observed_at",
            "mounts",
        },
        "provider block complete mount set",
    )
    strong_sha256(
        mount_set["api_server_identity_sha256"],
        "provider block mount-set API server identity",
    )
    _text(
        mount_set["list_resource_version"],
        "provider block mount-set list resourceVersion",
    )
    mount_observed = _utc(
        mount_set["observed_at"], "provider block mount-set observation"
    )
    if (
        mount_set["namespace"] != "fs2-models"
        or mount_set["claim_uid"] != claim_uid
        or mount_set["continue_token"] is not None
        or mount_set["remaining_item_count"] != 0
        or mount_set["complete"] is not True
        or mount_set["mounts"] != []
        or mount_observed > admitted_at
    ):
        raise CatalogError(
            "provider block writer admission lacks a complete empty API-observed mount set"
        )
    mount_set_sha256 = hashlib.sha256(canonical_bytes(mount_set)).hexdigest()
    api_fence = _exact(
        value["api_fence"],
        {
            "enforcement",
            "api_server_applied",
            "claim_resource_version",
            "allowed_operation_id",
            "allowed_writer_name",
            "allowed_creator_service_account_uid",
            "lease_uid",
            "fencing_token",
            "complete_mount_set_sha256",
            "writer_create_role_uid",
            "writer_create_role_binding_uid",
            "race_window",
            "second_writer_denied",
        },
        "provider block API writer fence",
    )
    if api_fence != {
        "enforcement": (
            "controller-owned-job-create-plus-validating-admission-policy-plus-lease-cas"
        ),
        "api_server_applied": True,
        "claim_resource_version": claim_rv,
        "allowed_operation_id": operation_id,
        "allowed_writer_name": expected_job_name,
        "allowed_creator_service_account_uid": controller["service_account_uid"],
        "lease_uid": lease_uid,
        "fencing_token": lease["fencing_token"],
        "complete_mount_set_sha256": mount_set_sha256,
        "writer_create_role_uid": controller["writer_create_role_uid"],
        "writer_create_role_binding_uid": controller[
            "writer_create_role_binding_uid"
        ],
        "race_window": (
            "closed-by-controller-held-lease-through-job-create-and-completion"
        ),
        "second_writer_denied": True,
    }:
        raise CatalogError("provider block writer fence was not API-set and exclusive")
    store.assert_claims(
        "provider-block-writer-admissions",
        receipt_digest,
        {
            "model_digest": record.digest,
            "storage_class_receipt_digest": storage_class_admission.receipt_digest,
            "operation_id": operation_id,
            "claim_identity_sha256": hashlib.sha256(canonical_bytes(claim)).hexdigest(),
            "writer_identity_sha256": hashlib.sha256(canonical_bytes(writer)).hexdigest(),
            "controller_identity_sha256": controller_identity,
            "lease_identity_sha256": hashlib.sha256(canonical_bytes(lease)).hexdigest(),
            "complete_mount_set_sha256": mount_set_sha256,
            "api_fence_sha256": hashlib.sha256(canonical_bytes(api_fence)).hexdigest(),
        },
    )
    return ProviderBlockWriterAdmission(
        model_id=record.model_id,
        model_digest=record.digest,
        operation_id=operation_id,
        receipt_digest=receipt_digest,
        storage_class_receipt_digest=storage_class_admission.receipt_digest,
        claim_uid=claim_uid,
        claim_resource_version=claim_rv,
        writer_job_name=expected_job_name,
        writer_service_account_uid=writer_uid,
        controller_identity_sha256=controller_identity,
        lease_uid=lease_uid,
        lease_resource_version=lease_rv,
        lease_holder_identity=holder,
        fencing_token=lease["fencing_token"],
        complete_mount_set_sha256=mount_set_sha256,
        _seal=_VERIFIED_WRITER_ADMISSION_SEAL,
    )


def _validate_staging(
    store: EvidenceStore,
    digest: str,
    record: ModelRecord,
    manifest: ArtifactManifest,
    content_uri: str,
) -> dict[str, Any]:
    value = _exact(
        store.receipt("staging", digest, STAGING_RECEIPT_SCHEMA, record.model_id),
        {
            "schema",
            "receipt_digest",
            "operation_id",
            "model_id",
            "artifact_manifest_digest",
            "content_digest",
            "source_path",
            "source_uri",
            "destination_path",
            "content_uri",
            "controller_owner",
            "lock_path",
            "serving_node",
            "max_concurrent_files",
            "expanded_bytes",
            "reserve_bytes",
            "free_bytes_before",
            "free_bytes_after",
            "outcome",
            "cleanup",
        },
        "staging receipt",
    )
    if value["model_id"] != record.model_id or value["artifact_manifest_digest"] != manifest.digest:
        raise CatalogError("staging receipt differs from the model/artifact binding")
    if value["content_digest"] != manifest.content_digest or value["content_uri"] != content_uri:
        raise CatalogError("staging receipt differs from the artifact content address")
    source_uri = canonical_content_uri(
        value["source_uri"],
        model_id=record.model_id,
        content_digest=manifest.content_digest,
        scheme="sfs",
    )
    canonical_content_uri(
        value["content_uri"],
        model_id=record.model_id,
        content_digest=manifest.content_digest,
        scheme="nvme",
    )
    if value["controller_owner"] != "fs2-serve-localizer":
        raise CatalogError("staging receipt has a foreign cache controller")
    if value["expanded_bytes"] != manifest.expanded_bytes:
        raise CatalogError("staging receipt expanded bytes differ from the manifest")
    _enum(value["outcome"], {"staged", "already-present"}, "staging outcome")
    concurrency = _positive_int(value["max_concurrent_files"], "staging concurrency")
    if concurrency is None or concurrency > 16:
        raise CatalogError("staging receipt concurrency exceeds the closed bound")
    for key in ("reserve_bytes", "free_bytes_before", "free_bytes_after"):
        raw = value[key]
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise CatalogError(f"staging receipt {key} is invalid")
    destination = _text(value["destination_path"], "staging destination")
    if destination is None or not destination.endswith(f"/sha256/{manifest.content_digest}"):
        raise CatalogError("staging destination is not versioned by the content digest")
    lock_path = _text(value["lock_path"], "staging lock path")
    model_root = destination[: -len(f"/sha256/{manifest.content_digest}")]
    if lock_path is None or not lock_path.startswith(model_root + "/.locks/"):
        raise CatalogError("staging lock is outside the writable model cache mount")
    serving_node = _node_identity(value["serving_node"], "staging serving node")
    cleanup = _exact(value["cleanup"], {"temporary_path_absent"}, "staging cleanup")
    if _boolean(cleanup["temporary_path_absent"], "staging temporary-path cleanup") is not True:
        raise CatalogError("staging temporary path was not cleaned")
    store.assert_claims(
        "staging",
        digest,
        {
            "artifact_manifest_digest": manifest.digest,
            "artifact_content_digest": manifest.content_digest,
            "source_uri": source_uri,
            "content_uri": content_uri,
            "serving_node_identity_sha256": hashlib.sha256(
                canonical_bytes(serving_node)
            ).hexdigest(),
        },
    )
    return value


def _validate_provider_block_pvc(
    store: EvidenceStore,
    digest: str,
    record: ModelRecord,
    manifest: ArtifactManifest,
    content_uri: str,
    *,
    acquisition_receipt_digest: str,
    semantic_receipt_digest: str,
    return_to_zero_receipt_digest: str,
    runtime_tuple_digest: str,
    runtime_tuple: Mapping[str, Any],
) -> dict[str, Any]:
    value = _exact(
        store.receipt(
            "provider-block-pvc",
            digest,
            PROVIDER_BLOCK_PVC_RECEIPT_SCHEMA,
            record.model_id,
        ),
        {
            "schema",
            "receipt_digest",
            "status",
            "observed_at",
            "model_id",
            "model_digest",
            "artifact_manifest_digest",
            "artifact_content_digest",
            "content_uri",
            "acquisition_receipt_digest",
            "storage_class_admission_receipt_digest",
            "writer_admission_receipt_digest",
            "storage_class",
            "claim",
            "acquirer_job",
            "writer_fence",
            "handoff",
            "replacement",
            "runtime",
            "scale_to_zero",
        },
        "provider block PVC lifecycle receipt",
    )
    if (
        value["status"] != "PASS"
        or value["model_id"] != record.model_id
        or value["model_digest"] != record.digest
        or value["artifact_manifest_digest"] != manifest.digest
        or value["artifact_content_digest"] != manifest.content_digest
        or value["content_uri"] != content_uri
        or value["acquisition_receipt_digest"] != acquisition_receipt_digest
    ):
        raise CatalogError("provider block PVC receipt differs from the route subjects")
    canonical_content_uri(
        content_uri,
        model_id=record.model_id,
        content_digest=manifest.content_digest,
        scheme="pvc",
    )
    placement = record.to_dict()["resources"]["gpu"]["placement"]
    if placement is None:
        raise CatalogError("provider block PVC receipt lacks a static placement")
    static = placement["provider_block_pvc"]
    storage_class_admission = _load_protected_storage_class_admission_from_store(
        record,
        store,
        receipt_digest=value["storage_class_admission_receipt_digest"],
    )
    storage_class = validate_provider_block_storage_class_observation(
        value["storage_class"]
    )
    if (
        storage_class["metadata"]["name"] != static["storage_class"]["name"]
        or storage_class != storage_class_admission.authorize(record)
    ):
        raise CatalogError("provider block StorageClass differs from the static contract")
    claim = _exact(
        value["claim"],
        {
            "namespace",
            "name",
            "uid",
            "resource_version",
            "volume_name",
            "capacity_bytes",
            "access_modes",
            "volume_mode",
            "phase",
            "bound_at",
            "bound_after_acquirer_scheduled",
        },
        "provider block claim receipt",
    )
    static_claim = static["claim"]
    if (
        claim["namespace"] != static_claim["namespace"]
        or claim["name"] != static_claim["name"]
        or K8S_UID.fullmatch(_text(claim["uid"], "provider block PVC UID") or "") is None
        or not _text(claim["resource_version"], "provider block PVC resourceVersion")
        or not _text(claim["volume_name"], "provider block volume name")
        or claim["capacity_bytes"] < static_claim["requested_bytes"]
        or claim["access_modes"] != ["ReadWriteOnce"]
        or claim["volume_mode"] != "Filesystem"
        or claim["phase"] != "Bound"
        or claim["bound_after_acquirer_scheduled"] is not True
    ):
        raise CatalogError("provider block claim is not the exact Bound RWO subject")
    acquirer = _exact(
        value["acquirer_job"],
        {
            "namespace",
            "name",
            "uid",
            "resource_version",
            "scheduled_at",
            "completed_at",
            "node",
            "node_selector",
            "tolerations",
            "gpu_count",
            "first_consumer",
            "sole_writer",
            "phase",
        },
        "provider block acquirer Job",
    )
    acquirer_node = _node_identity(acquirer["node"], "provider block acquirer Node")
    if (
        acquirer["namespace"] != "fs2-models"
        or not str(acquirer["name"]).startswith("qwen3-8b-cache-")
        or K8S_UID.fullmatch(_text(acquirer["uid"], "provider block acquirer UID") or "") is None
        or acquirer["node_selector"] != placement["node_selector"]
        or acquirer["tolerations"] != placement["tolerations"]
        or acquirer["gpu_count"] != 0
        or acquirer["first_consumer"] is not True
        or acquirer["sole_writer"] is not True
        or acquirer["phase"] != "Succeeded"
    ):
        raise CatalogError("provider block acquirer is not the targeted zero-GPU sole writer")
    writer_admission = _load_provider_block_writer_admission_from_store(
        record,
        storage_class_admission,
        store,
        receipt_digest=value["writer_admission_receipt_digest"],
    )
    if (
        writer_admission.claim_uid != claim["uid"]
        or writer_admission.writer_job_name != acquirer["name"]
    ):
        raise CatalogError("provider block acquirer differs from its writer admission")
    writer_fence = _exact(
        value["writer_fence"],
        {
            "controller_identity_sha256",
            "writer_admission_complete_mount_set_sha256",
            "lease",
            "api_observation",
        },
        "provider block live writer fence",
    )
    fence_lease = _exact(
        writer_fence["lease"],
        {
            "uid",
            "resource_version",
            "holder_identity",
            "fencing_token",
        },
        "provider block live writer Lease",
    )
    if fence_lease != {
        "uid": writer_admission.lease_uid,
        "resource_version": writer_admission.lease_resource_version,
        "holder_identity": writer_admission.lease_holder_identity,
        "fencing_token": writer_admission.fencing_token,
    } or (
        writer_fence["controller_identity_sha256"]
        != writer_admission.controller_identity_sha256
        or writer_fence["writer_admission_complete_mount_set_sha256"]
        != writer_admission.complete_mount_set_sha256
    ):
        raise CatalogError("provider block live Lease differs from its admitted fence")
    api_observation = _exact(
        writer_fence["api_observation"],
        {
            "observed_at",
            "api_server_identity_sha256",
            "claim_uid",
            "claim_resource_version",
            "active_writer_uids",
            "denied_second_writer_request_uid",
            "denial_reason",
        },
        "provider block live API writer observation",
    )
    denied_uid = _text(
        api_observation["denied_second_writer_request_uid"],
        "provider block denied writer request UID",
    )
    strong_sha256(
        api_observation["api_server_identity_sha256"],
        "provider block API server identity",
    )
    if (
        api_observation["claim_uid"] != claim["uid"]
        or api_observation["claim_resource_version"] != claim["resource_version"]
        or api_observation["active_writer_uids"] != [acquirer["uid"]]
        or denied_uid is None
        or K8S_UID.fullmatch(denied_uid) is None
        or api_observation["denial_reason"]
        != "fs2-provider-block-sole-writer-fence-conflict"
    ):
        raise CatalogError("provider block writer exclusivity lacks exact API proof")
    handoff = _exact(
        value["handoff"],
        {
            "closed_at",
            "writer_admission_receipt_digest",
            "writer_admission_complete_mount_set_sha256",
            "no_active_writers",
            "active_writer_uids",
            "sole_writer_uid",
            "payload_read_only_after_handoff",
            "runtime_read_only_admitted",
            "lease_uid",
            "lease_resource_version_after_release",
            "lease_holder_identity_after_release",
            "released_fencing_token",
            "api_server_observed",
        },
        "provider block writer handoff",
    )
    if (
        handoff["writer_admission_receipt_digest"]
        != writer_admission.receipt_digest
        or handoff["writer_admission_complete_mount_set_sha256"]
        != writer_admission.complete_mount_set_sha256
        or handoff["no_active_writers"] is not True
        or handoff["active_writer_uids"] != []
        or handoff["sole_writer_uid"] != acquirer["uid"]
        or handoff["payload_read_only_after_handoff"] is not True
        or handoff["runtime_read_only_admitted"] is not True
        or handoff["lease_uid"] != writer_admission.lease_uid
        or handoff["lease_resource_version_after_release"]
        == writer_admission.lease_resource_version
        or handoff["lease_holder_identity_after_release"] is not None
        or handoff["released_fencing_token"] != writer_admission.fencing_token
        or handoff["api_server_observed"] is not True
    ):
        raise CatalogError("provider block writer handoff is not closed")
    _text(
        handoff["lease_resource_version_after_release"],
        "provider block released Lease resourceVersion",
    )
    replacement = _exact(
        value["replacement"],
        {
            "controlled",
            "original_node",
            "replacement_node",
            "detached_at",
            "attached_at",
            "no_multi_attach",
            "attach_attempts",
            "manifest_reverified",
        },
        "provider block replacement",
    )
    original = _node_identity(replacement["original_node"], "provider block original Node")
    replacement_node = _node_identity(
        replacement["replacement_node"], "provider block replacement Node"
    )
    if (
        replacement["controlled"] is not True
        or original != acquirer_node
        or original == replacement_node
        or replacement["no_multi_attach"] is not True
        or _positive_int(replacement["attach_attempts"], "provider block attach attempts") is None
        or replacement["manifest_reverified"] is not True
    ):
        raise CatalogError("provider block detach/reattach or no-Multi-Attach proof failed")
    runtime = _exact(
        value["runtime"],
        {
            "deployment_namespace",
            "deployment_name",
            "deployment_uid",
            "runtime_tuple_digest",
            "node",
            "pvc_read_only",
            "gpu_count",
            "semantic_receipt_digest",
        },
        "provider block runtime handoff",
    )
    runtime_node = _node_identity(runtime["node"], "provider block runtime Node")
    if (
        runtime["deployment_namespace"] != "fs2-models"
        or runtime["deployment_name"] != record.model_id
        or K8S_UID.fullmatch(_text(runtime["deployment_uid"], "provider block Deployment UID") or "")
        is None
        or runtime["runtime_tuple_digest"] != runtime_tuple_digest
        or runtime_node != replacement_node
        or runtime_node != runtime_tuple["worker"]["node"]
        or runtime["pvc_read_only"] is not True
        or runtime["gpu_count"] != 1
        or runtime["semantic_receipt_digest"] != semantic_receipt_digest
    ):
        raise CatalogError("provider block runtime did not consume the claim read-only")
    scale = _exact(
        value["scale_to_zero"],
        {
            "from_replicas",
            "to_replicas",
            "return_to_zero_receipt_digest",
            "claim_retained",
            "claim_state",
            "no_active_writers",
        },
        "provider block scale-to-zero",
    )
    if scale != {
        "from_replicas": 1,
        "to_replicas": 0,
        "return_to_zero_receipt_digest": return_to_zero_receipt_digest,
        "claim_retained": True,
        "claim_state": "Bound",
        "no_active_writers": True,
    }:
        raise CatalogError("provider block claim was not retained across scale-to-zero")
    scheduled_at = _utc(acquirer["scheduled_at"], "provider block scheduled_at")
    fence_observed_at = _utc(
        api_observation["observed_at"], "provider block writer fence observed_at"
    )
    bound_at = _utc(claim["bound_at"], "provider block bound_at")
    completed_at = _utc(acquirer["completed_at"], "provider block completed_at")
    closed_at = _utc(handoff["closed_at"], "provider block handoff closed_at")
    detached_at = _utc(replacement["detached_at"], "provider block detached_at")
    attached_at = _utc(replacement["attached_at"], "provider block attached_at")
    observed_at = _utc(value["observed_at"], "provider block observed_at")
    if not (
        fence_observed_at <= scheduled_at <= bound_at <= completed_at <= closed_at <= detached_at < attached_at <= observed_at
    ):
        raise CatalogError("provider block lifecycle timestamps are not causally ordered")
    store.assert_claims(
        "provider-block-pvc",
        digest,
        {
            "model_digest": record.digest,
            "artifact_manifest_digest": manifest.digest,
            "artifact_content_digest": manifest.content_digest,
            "content_uri": content_uri,
            "acquisition_receipt_digest": acquisition_receipt_digest,
            "storage_class_admission_receipt_digest": (
                storage_class_admission.receipt_digest
            ),
            "writer_admission_receipt_digest": writer_admission.receipt_digest,
            "storage_class_identity_sha256": hashlib.sha256(
                canonical_bytes(storage_class)
            ).hexdigest(),
            "claim_identity_sha256": hashlib.sha256(canonical_bytes(claim)).hexdigest(),
            "acquirer_job_identity_sha256": hashlib.sha256(
                canonical_bytes(acquirer)
            ).hexdigest(),
            "writer_fence_identity_sha256": hashlib.sha256(
                canonical_bytes(writer_fence)
            ).hexdigest(),
            "handoff_identity_sha256": hashlib.sha256(
                canonical_bytes(handoff)
            ).hexdigest(),
            "replacement_identity_sha256": hashlib.sha256(
                canonical_bytes(replacement)
            ).hexdigest(),
            "runtime_tuple_digest": runtime_tuple_digest,
            "runtime_node_identity_sha256": hashlib.sha256(
                canonical_bytes(runtime_node)
            ).hexdigest(),
            "semantic_receipt_digest": semantic_receipt_digest,
            "return_to_zero_receipt_digest": return_to_zero_receipt_digest,
        },
    )
    return value


def _validate_nim_cache_readiness(
    store: EvidenceStore,
    digest: str,
    record: ModelRecord,
    manifest: ArtifactManifest,
    content_uri: str,
    *,
    prerequisite_subject: Mapping[str, Any] | None = None,
    prerequisite_digest: str | None = None,
) -> dict[str, Any]:
    """Reopen the NIM Operator-owned cache object used instead of local NVMe staging."""

    value = _exact(
        store.receipt("nim-cache", digest, NIM_CACHE_RECEIPT_SCHEMA, record.model_id),
        {
            "schema",
            "receipt_digest",
            "status",
            "checked_at",
            "model_id",
            "model_digest",
            "artifact_manifest_digest",
            "content_digest",
            "content_uri",
            "controller_owner",
            "nim_cache",
            "persistent_volume_claim",
            "runtime_image_digest",
            "credential_authentication",
        },
        "NIMCache readiness receipt",
    )
    record_value = record.to_dict()
    if (
        value["status"] != "Ready"
        or value["model_id"] != record.model_id
        or value["model_digest"] != record.digest
        or value["artifact_manifest_digest"] != manifest.digest
        or value["content_digest"] != manifest.content_digest
    ):
        raise CatalogError("NIMCache receipt differs from the exact model/artifact subject")
    if record_value["cache"]["owner"] != "nim-operator-nimcache":
        raise CatalogError("NIMCache readiness cannot satisfy an fs2 localizer-owned model")
    if manifest.owner != "nim-operator-nimcache":
        raise CatalogError("NIMCache artifact manifest has a foreign cache owner")
    canonical_content_uri(
        value["content_uri"],
        model_id=record.model_id,
        content_digest=manifest.content_digest,
        scheme="sfs",
    )
    if value["content_uri"] != content_uri:
        raise CatalogError("NIMCache receipt differs from the bound SFS content address")
    if value["controller_owner"] != "nim-operator-nimcache":
        raise CatalogError("NIMCache receipt has a foreign controller owner")
    cache = _exact(
        value["nim_cache"],
        {
            "api_version",
            "kind",
            "namespace",
            "name",
            "uid",
            "resource_version",
            "observed_generation",
            "cache_state",
        },
        "NIMCache identity",
    )
    if (
        cache["api_version"] != "apps.nvidia.com/v1alpha1"
        or cache["kind"] != "NIMCache"
        or cache["namespace"] != "fs2-models"
        or cache["name"] != record.model_id
        or cache["cache_state"] != "Ready"
    ):
        raise CatalogError("NIMCache receipt is not the ready model-owned cache")
    uid = _text(cache["uid"], "NIMCache UID")
    if uid is None or K8S_UID.fullmatch(uid) is None:
        raise CatalogError("NIMCache receipt lacks an exact object UID")
    _text(cache["resource_version"], "NIMCache resource version")
    _positive_int(cache["observed_generation"], "NIMCache observed generation")
    pvc = _exact(
        value["persistent_volume_claim"],
        {"namespace", "name", "uid", "resource_version", "state"},
        "NIMCache PVC identity",
    )
    if pvc["namespace"] != "fs2-models" or pvc["state"] != "Bound":
        raise CatalogError("NIMCache readiness lacks a bound model-namespace PVC")
    pvc_uid = _text(pvc["uid"], "NIMCache PVC UID")
    _text(pvc["name"], "NIMCache PVC name")
    _text(pvc["resource_version"], "NIMCache PVC resource version")
    if pvc_uid is None or K8S_UID.fullmatch(pvc_uid) is None:
        raise CatalogError("NIMCache readiness lacks an exact PVC UID")
    if value["runtime_image_digest"] != record_value["runtime"]["image"]["digest"]:
        raise CatalogError("NIMCache readiness aliases another NIM image")
    credential = _exact(
        value["credential_authentication"],
        {
            "status",
            "prerequisite_receipt_digest",
            "credential_materialization_sha256",
            "secret_requirement_ids",
            "secret_resource_uids",
            "values_suppressed",
        },
        "NIMCache credential authentication",
    )
    secret_ids = [
        "fs2-models/ngc-pull-secret",
        "fs2-models/ngc-runtime-secret",
    ]
    if (
        credential["status"] != "PASS"
        or credential["values_suppressed"] is not True
        or credential["secret_requirement_ids"] != secret_ids
    ):
        raise CatalogError("NIMCache did not authenticate with both fresh NGC Secrets")
    strong_sha256(
        credential["prerequisite_receipt_digest"],
        "NIMCache prerequisite receipt digest",
    )
    strong_sha256(
        credential["credential_materialization_sha256"],
        "NIMCache credential materialization digest",
    )
    secret_uids = _list(
        credential["secret_resource_uids"], "NIMCache credential Secret UIDs"
    )
    if [item.get("requirement_id") for item in secret_uids] != secret_ids:
        raise CatalogError("NIMCache credential Secret UID set is incomplete")
    for index, raw_secret in enumerate(secret_uids):
        secret = _exact(
            raw_secret,
            {"requirement_id", "uid"},
            f"NIMCache credential Secret UIDs[{index}]",
        )
        uid = _text(secret["uid"], "NIMCache credential Secret UID")
        if uid is None or K8S_UID.fullmatch(uid) is None:
            raise CatalogError("NIMCache credential Secret UID is invalid")
    if prerequisite_subject is not None:
        materialization = prerequisite_subject["observation"][
            "ngc_credential_materialization"
        ]
        expected_materialization_digest = hashlib.sha256(
            canonical_bytes(materialization)
        ).hexdigest()
        expected_uids = [
            {"requirement_id": item["requirement_id"], "uid": item["uid"]}
            for item in materialization["secrets"]
        ]
        if (
            prerequisite_digest is None
            or credential["prerequisite_receipt_digest"] != prerequisite_digest
            or credential["credential_materialization_sha256"]
            != expected_materialization_digest
            or secret_uids != expected_uids
        ):
            raise CatalogError("NIMCache authentication differs from fresh credential evidence")
    checked_at = _utc(value["checked_at"], "NIMCache readiness time")
    if checked_at > store.attestation_issued_at("nim-cache", digest) + MAX_EVENT_CLOCK_SKEW:
        raise CatalogError("NIMCache readiness was signed before observation completed")
    store.assert_claims(
        "nim-cache",
        digest,
        {
            "model_digest": record.digest,
            "artifact_manifest_digest": manifest.digest,
            "artifact_content_digest": manifest.content_digest,
            "content_uri": content_uri,
            "runtime_image_digest": value["runtime_image_digest"],
            "nim_cache_identity_sha256": hashlib.sha256(canonical_bytes(cache)).hexdigest(),
            "pvc_identity_sha256": hashlib.sha256(canonical_bytes(pvc)).hexdigest(),
            "credential_authentication_sha256": hashlib.sha256(
                canonical_bytes(credential)
            ).hexdigest(),
        },
    )
    return value


def _validate_runtime_tuple(
    store: EvidenceStore,
    digest: str,
    record: ModelRecord,
    manifest: ArtifactManifest,
    placement_digest: str,
    content_uri: str,
    staging_subject: Mapping[str, Any],
) -> dict[str, Any]:
    value = _exact(
        store.receipt("runtime-tuples", digest, RUNTIME_TUPLE_SCHEMA, record.model_id),
        {
            "schema",
            "receipt_digest",
            "status",
            "captured_at",
            "model_id",
            "model_digest",
            "project_id_sha256",
            "project_alias",
            "region",
            "cluster_id_sha256",
            "cluster_alias",
            "worker",
            "runtime",
            "artifact",
        },
        "B300 runtime tuple",
    )
    if value["status"] != "verified" or value["model_id"] != record.model_id:
        raise CatalogError("runtime tuple is not verified for this model")
    if value["model_digest"] != record.digest:
        raise CatalogError("runtime tuple model digest differs from the catalog")
    if (
        value["project_id_sha256"] != TARGET_PROJECT_SHA256
        or value["project_alias"] != TARGET_PROJECT_ALIAS
        or value["region"] != TARGET_REGION
    ):
        raise CatalogError("runtime tuple is not from the target project and region")
    cluster_digest = value["cluster_id_sha256"]
    cluster_alias = _text(value["cluster_alias"], "runtime tuple cluster alias")
    if cluster_digest == FORBIDDEN_CLUSTER_SHA256:
        raise CatalogError("runtime tuple names the forbidden cluster")
    strong_sha256(cluster_digest, "runtime tuple cluster identity")
    if cluster_alias is None:
        raise CatalogError("runtime tuple cluster alias is absent")
    captured_at = _utc(value["captured_at"], "runtime tuple capture time")
    if captured_at > store.attestation_issued_at("runtime-tuples", digest) + MAX_EVENT_CLOCK_SKEW:
        raise CatalogError("runtime tuple was signed before its capture completed")

    worker = _exact(
        value["worker"],
        {
            "image_reference",
            "image_digest",
            "nvidia_driver_version",
            "nvidia_smi_sha256",
            "cuda_version",
            "device_plugin",
            "gpu",
            "node",
            "node_selector",
            "nvme_inventory_sha256",
        },
        "runtime tuple worker",
    )
    image_digest = worker["image_digest"]
    image_reference = _text(worker["image_reference"], "worker image reference")
    strong_sha256(image_digest, "worker image digest", image=True)
    if image_reference is None or not image_reference.endswith("@" + image_digest):
        raise CatalogError("worker image reference does not bind its immutable digest")
    driver = _text(worker["nvidia_driver_version"], "NVIDIA driver version")
    cuda = _text(worker["cuda_version"], "CUDA version")
    if driver is None or DRIVER_VERSION.fullmatch(driver) is None:
        raise CatalogError("runtime tuple requires the exact patch NVIDIA driver version")
    if cuda is None or CUDA_VERSION.fullmatch(cuda) is None:
        raise CatalogError("runtime tuple requires the exact CUDA version")
    for field in ("nvidia_smi_sha256", "nvme_inventory_sha256"):
        strong_sha256(worker[field], f"runtime tuple {field}")
    serving_node = _node_identity(worker["node"], "runtime tuple serving node")
    staged_node = staging_subject.get("serving_node")
    if staged_node is not None and serving_node != _node_identity(
        staged_node, "staging serving node"
    ):
        raise CatalogError("runtime tuple and node-local staging identify different Nodes")
    plugin = _exact(
        worker["device_plugin"], {"owner", "image_digest", "singleton"}, "device plugin"
    )
    plugin_owner = _text(plugin["owner"], "device-plugin owner")
    if plugin_owner is None or plugin["singleton"] is not True:
        raise CatalogError("runtime tuple requires the discovered singleton device-plugin owner")
    strong_sha256(plugin["image_digest"], "device-plugin image digest", image=True)
    node_selector = worker["node_selector"]
    if (
        not isinstance(node_selector, dict)
        or not node_selector
        or any(
            not isinstance(key, str)
            or not key
            or not isinstance(item, str)
            or not item
            for key, item in node_selector.items()
        )
    ):
        raise CatalogError("runtime tuple requires the cluster-published node selector")
    gpu = _exact(
        worker["gpu"],
        {
            "class",
            "node_preset",
            "node_count",
            "node_topology",
            "workload_count",
            "workload_topology",
            "allocated_uuids",
            "node_inventory_sha256",
        },
        "worker GPU",
    )
    expected = record.to_dict()["resources"]["gpu"]
    placement = expected["placement"]
    if placement is None:
        raise CatalogError("runtime tuple requires a reviewed model node placement")
    expected_node_topology = (
        "eight-gpu-nvlink" if placement["node_gpu_count"] == 8 else "single-gpu"
    )
    if (
        gpu["class"] != expected["class"]
        or gpu["node_preset"] != placement["node_preset"]
        or gpu["node_count"] != placement["node_gpu_count"]
        or gpu["node_topology"] != expected_node_topology
        or gpu["workload_count"] != expected["count"]
        or gpu["workload_topology"] != expected["topology"]
    ):
        raise CatalogError("runtime tuple node placement or workload allocation differs from the catalog")
    for key, item in placement["node_selector"].items():
        if node_selector.get(key) != item:
            raise CatalogError("runtime tuple selector differs from the catalog placement")
    if staged_node is not None and node_selector.get("kubernetes.io/hostname") != serving_node["name"]:
        raise CatalogError("node-local runtime tuple lacks the exact serving-node selector")
    strong_sha256(gpu["node_inventory_sha256"], "worker GPU node inventory")
    uuids = _list(gpu["allocated_uuids"], "allocated worker GPU UUIDs", nonempty=True)
    if len(uuids) != expected["count"] or len(set(uuids)) != len(uuids):
        raise CatalogError("allocated GPU UUID cardinality differs from the workload request")
    if any(not isinstance(item, str) or not item.startswith("GPU-") for item in uuids):
        raise CatalogError("runtime tuple contains an invalid GPU UUID")

    runtime = _exact(
        value["runtime"],
        {
            "image_digest",
            "model_revision",
            "startup_mechanism",
            "command_sha256",
            "execution_identity_sha256",
            "checkpoint",
        },
        "runtime tuple runtime",
    )
    record_value = record.to_dict()
    if runtime["image_digest"] != record_value["runtime"]["image"]["digest"]:
        raise CatalogError("runtime tuple image differs from the catalog")
    if runtime["model_revision"] != record_value["model"]["source"]["revision"]:
        raise CatalogError("runtime tuple model revision differs from the catalog")
    mechanism = runtime["startup_mechanism"]
    if mechanism not in record_value["startup"]["enabled_mechanisms"]:
        raise CatalogError("runtime tuple startup mechanism is not enabled by the catalog")
    if mechanism == "snapshot" and expected["count"] != 1:
        raise CatalogError("multi-GPU CRIU qualification is forbidden")
    expected_command_digest = hashlib.sha256(
        canonical_bytes(record_value["runtime"]["command"])
    ).hexdigest()
    if runtime["command_sha256"] != expected_command_digest:
        raise CatalogError("runtime command differs from the catalog argv")
    expected_execution_identity = execution_identity(record_value)
    if runtime["execution_identity_sha256"] != expected_execution_identity:
        raise CatalogError("runtime executable/model/artifact identity differs from the catalog")
    checkpoint = runtime["checkpoint"]
    if mechanism == "snapshot":
        checkpoint_value = _exact(
            checkpoint,
            {"criu_version", "criu_sha256", "cuda_checkpoint_version", "cuda_checkpoint_sha256"},
            "snapshot checkpoint tooling",
        )
        for key in ("criu_version", "cuda_checkpoint_version"):
            _text(checkpoint_value[key], f"snapshot {key}")
        for key in ("criu_sha256", "cuda_checkpoint_sha256"):
            strong_sha256(checkpoint_value[key], f"snapshot {key}")
    elif checkpoint is not None:
        raise CatalogError("non-snapshot runtime cannot claim CRIU/cuda-checkpoint tooling")
    artifact = _exact(
        value["artifact"], {"manifest_digest", "content_uri", "placement_receipt_digest"}, "runtime artifact"
    )
    if (
        artifact["manifest_digest"] != manifest.digest
        or artifact["content_uri"] != content_uri
        or artifact["placement_receipt_digest"] != placement_digest
    ):
        raise CatalogError("runtime tuple artifact differs from staged content")
    gpu_tuple_digest = hashlib.sha256(canonical_bytes(gpu)).hexdigest()
    store.assert_claims(
        "runtime-tuples",
        digest,
        {
            "artifact_manifest_digest": manifest.digest,
            "placement_receipt_digest": placement_digest,
            "worker_image_digest": image_digest,
            "driver_version": driver,
            "cuda_version": cuda,
            "device_plugin_image_digest": plugin["image_digest"],
            "gpu_tuple_sha256": gpu_tuple_digest,
            "serving_node_identity_sha256": hashlib.sha256(
                canonical_bytes(serving_node)
            ).hexdigest(),
            "runtime_image_digest": runtime["image_digest"],
            "model_revision": runtime["model_revision"],
            "execution_identity_sha256": expected_execution_identity,
        },
    )
    return value


def load_faststart_job_admission(
    record: ModelRecord,
    evidence_root: Path | str,
    *,
    admission_digest: str,
    artifact_manifest_digest: str,
    placement_receipt_digest: str,
    runtime_tuple_digest: str,
    content_uri: str,
    catalog: Catalog | None = None,
    prerequisite_receipt_digest: str | None = None,
    evidence_session_id: str,
    trusted_attestors: Mapping[str, str],
    validation_time: datetime | None = None,
) -> FaststartJobAdmission:
    """Reopen signed artifact, placement, runtime and operator approval subjects."""

    record_value = record.to_dict()
    is_nim = record_value["runtime"]["kind"] == "nim"
    placement_contract = record_value["resources"]["gpu"]["placement"]
    if (
        not is_nim
        and (
            placement_contract is None
            or placement_contract["local_pv_pvc"]["state"]
            != "reviewed-implemented"
            or "node-local-pv-pvc-qualified"
            not in placement_contract["cache_capabilities"]
        )
    ):
        raise CatalogError("fast-start admission requires a reviewed local-PV/PVC lifecycle")
    if is_nim and (catalog is None or prerequisite_receipt_digest is None):
        raise CatalogError(
            "NIM fast-start admission requires the fresh NGC prerequisite receipt"
        )
    if not is_nim and (catalog is not None or prerequisite_receipt_digest is not None):
        raise CatalogError("non-NIM fast-start admission cannot claim NGC prerequisites")
    store = EvidenceStore(
        evidence_root,
        session_id=evidence_session_id,
        trusted_attestors=trusted_attestors,
        validation_time=validation_time,
    )
    prerequisite_subject: Mapping[str, Any] | None = None
    if is_nim:
        assert catalog is not None and prerequisite_receipt_digest is not None
        plan = catalog.acquisition_plan(record.model_id)
        if plan.method != "ngc-target-node-nimcache":
            raise CatalogError("NIM fast-start admission lacks the NGC acquisition plan")
        prerequisite_subject = _validate_prerequisites(
            store,
            prerequisite_receipt_digest,
            record,
            catalog,
            plan,
        )
    manifest = store.artifact(artifact_manifest_digest, record.model_id)
    if (
        manifest.model_id != record.model_id
        or manifest.source_revision != record_value["model"]["source"]["revision"]
        or manifest.license_state != "verified"
        or record_value["model"]["source"]["license"]["state"] != "verified"
        or record_value["model"]["source"]["entitlement"]["state"]
        not in {"not-required", "verified"}
        or record_value["runtime"]["image"]["state"] != "resolved"
        or record_value["resources"]["gpu"]["b300_state"] != "qualified"
        or record_value["support"]["state"] != "qualified"
    ):
        raise CatalogError("fast-start admission requires a qualified immutable model/runtime")
    store.assert_claims(
        "artifacts",
        manifest.digest,
        {
            "artifact_manifest_digest": manifest.digest,
            "artifact_content_digest": manifest.content_digest,
            "artifact_kind": manifest.kind,
            "model_revision": manifest.source_revision,
        },
    )
    cache_owner = record_value["cache"]["owner"]
    scheme = "sfs" if cache_owner == "nim-operator-nimcache" else "nvme"
    canonical_uri = canonical_content_uri(
        content_uri,
        model_id=record.model_id,
        content_digest=manifest.content_digest,
        scheme=scheme,
    )
    if cache_owner == "nim-operator-nimcache":
        placement = _validate_nim_cache_readiness(
            store,
            placement_receipt_digest,
            record,
            manifest,
            canonical_uri,
            prerequisite_subject=prerequisite_subject,
            prerequisite_digest=prerequisite_receipt_digest,
        )
    elif cache_owner == "fs2-serve-localizer":
        placement = _validate_staging(
            store,
            placement_receipt_digest,
            record,
            manifest,
            canonical_uri,
        )
    else:
        raise CatalogError("fast-start admission has no supported cache owner")
    runtime = _validate_runtime_tuple(
        store,
        runtime_tuple_digest,
        record,
        manifest,
        placement_receipt_digest,
        canonical_uri,
        placement,
    )
    value = _exact(
        store.receipt(
            "faststart-admissions",
            admission_digest,
            FASTSTART_ADMISSION_SCHEMA,
            record.model_id,
        ),
        {
            "schema",
            "receipt_digest",
            "status",
            "approved_at",
            "model_id",
            "model_digest",
            "job_kind",
            "runtime_tuple_digest",
            "artifact_manifest_digest",
            "placement_receipt_digest",
            "prerequisite_receipt_digest",
            "content_uri",
            "job",
            "review_scope",
        },
        "fast-start Job admission",
    )
    if (
        value["status"] != "approved"
        or value["model_id"] != record.model_id
        or value["model_digest"] != record.digest
        or value["runtime_tuple_digest"] != runtime_tuple_digest
        or value["artifact_manifest_digest"] != manifest.digest
        or value["placement_receipt_digest"] != placement_receipt_digest
        or value["prerequisite_receipt_digest"] != prerequisite_receipt_digest
        or value["content_uri"] != canonical_uri
        or value["review_scope"] != "reviewed-single-b300-faststart/v1"
    ):
        raise CatalogError("fast-start admission differs from reopened evidence")
    job_kind = _enum(value["job_kind"], {"donor", "snapshot"}, "fast-start job kind")
    runtime_mechanism = runtime["runtime"]["startup_mechanism"]
    experiment = next(
        (
            item
            for item in record_value["startup"]["experiments"]
            if item["mechanism"] == "snapshot"
        ),
        None,
    )
    if experiment is None or experiment["state"] not in {"gated", "qualified"}:
        raise CatalogError("fast-start admission lacks a reviewed snapshot experiment")
    if job_kind == "donor" and runtime_mechanism != "conventional":
        raise CatalogError("donor admission must reopen a qualified conventional runtime")
    if job_kind == "snapshot" and (
        runtime_mechanism != "snapshot" or experiment["state"] != "qualified"
    ):
        raise CatalogError("snapshot restore admission requires a qualified snapshot runtime")
    job = _exact(value["job"], {"image", "command", "command_sha256"}, "fast-start Job")
    image = _text(job["image"], "fast-start Job image")
    assert image is not None
    if "@" not in image:
        raise CatalogError("fast-start Job admission requires an immutable image")
    strong_sha256(image.rsplit("@", 1)[1], "fast-start Job image digest", image=True)
    command = _list(job["command"], "fast-start Job command", nonempty=True)
    if any(not isinstance(item, str) or not item for item in command):
        raise CatalogError("fast-start Job admission command is invalid")
    command_sha256 = hashlib.sha256(canonical_bytes(command)).hexdigest()
    if job["command_sha256"] != command_sha256:
        raise CatalogError("fast-start Job admission command digest differs")
    approved_at = _utc(value["approved_at"], "fast-start approval time")
    if approved_at > store.attestation_issued_at(
        "faststart-admissions", admission_digest
    ) + MAX_EVENT_CLOCK_SKEW:
        raise CatalogError("fast-start admission was signed before approval")
    store.assert_claims(
        "faststart-admissions",
        admission_digest,
        {
            "model_digest": record.digest,
            "job_kind": job_kind,
            "runtime_tuple_digest": runtime_tuple_digest,
            "artifact_manifest_digest": manifest.digest,
            "placement_receipt_digest": placement_receipt_digest,
            "prerequisite_receipt_digest": prerequisite_receipt_digest,
            "content_uri": canonical_uri,
            "job_image_digest": image.rsplit("@", 1)[1],
            "command_sha256": command_sha256,
        },
    )
    return FaststartJobAdmission(
        model_id=record.model_id,
        model_digest=record.digest,
        job_kind=job_kind,
        runtime_tuple_digest=runtime_tuple_digest,
        artifact_manifest_digest=manifest.digest,
        image=image,
        command=tuple(command),
        admission_digest=admission_digest,
        _seal=_VERIFIED_FASTSTART_SEAL,
    )


def _readiness_path_identity(
    record: ModelRecord,
    *,
    evidence_kind: str,
    evidence_digest: str,
    service_uid: str,
    observed_generation: int | None,
    observation: Mapping[str, Any],
) -> dict[str, Any]:
    """Create the canonical readiness subject carried by a gateway smoke."""

    strong_sha256(evidence_digest, "readiness path evidence digest")
    if K8S_UID.fullmatch(service_uid) is None:
        raise CatalogError("readiness path lacks an exact backend Service UID")
    if observed_generation is not None:
        _positive_int(observed_generation, "readiness path observed generation")
    static = record.to_dict()["interface"]["readiness"]
    contract = {
        "method": static["method"],
        "path": static["path"],
        "expected_status": static["expected_status"],
    }
    subject = {
        "evidence_kind": evidence_kind,
        "evidence_digest": evidence_digest,
        "service_uid": service_uid,
        "observed_generation": observed_generation,
        "contract_sha256": hashlib.sha256(canonical_bytes(contract)).hexdigest(),
        "observation_sha256": hashlib.sha256(
            canonical_bytes(dict(observation))
        ).hexdigest(),
    }
    return {
        **subject,
        "identity_sha256": hashlib.sha256(canonical_bytes(subject)).hexdigest(),
    }


def _gateway_path(
    record: ModelRecord,
    request_contract: SemanticRequestContract,
    gateway_identity: Mapping[str, Any],
    backend_subject: Mapping[str, Any],
    readiness_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind gateway, operation, transport, backend, and readiness into one route."""

    invocation = request_contract.invocation
    if request_contract.state != "qualified" or not invocation:
        raise CatalogError("gateway route lacks a qualified canonical request contract")
    return {
        "gateway": dict(gateway_identity),
        "operation": invocation["operation"],
        "transport": {
            "mode": "gateway-service-proxy",
            "protocol": invocation["protocol"],
            "method": invocation["method"],
            "endpoint": invocation["endpoint"],
            "gateway_service_uid": gateway_identity["service_uid"],
            "backend_service_uid": backend_subject["service_uid"],
        },
        "route": {
            "model_id": record.model_id,
            "protocols": record.to_dict()["interface"]["protocols"],
            "endpoints": record.to_dict()["interface"]["endpoints"],
        },
        "backend": {
            key: backend_subject[key]
            for key in (
                "class",
                "region",
                "namespace",
                "service_name",
                "service_uid",
                "port",
                "origin",
                "endpoint_identity_sha256",
                "trust_bundle_sha256",
            )
        },
        "readiness": dict(readiness_identity),
    }


def _gateway_claims(gateway_path: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return explicit signed route claims; cohorts carry an all-null subject."""

    if gateway_path is None:
        return {
            "gateway_path_sha256": None,
            "gateway_service_uid": None,
            "backend_service_uid": None,
            "operation": None,
            "transport_identity_sha256": None,
            "readiness_identity_sha256": None,
        }
    path = dict(gateway_path)
    return {
        "gateway_path_sha256": hashlib.sha256(canonical_bytes(path)).hexdigest(),
        "gateway_service_uid": path["gateway"]["service_uid"],
        "backend_service_uid": path["backend"]["service_uid"],
        "operation": path["operation"],
        "transport_identity_sha256": hashlib.sha256(
            canonical_bytes(path["transport"])
        ).hexdigest(),
        "readiness_identity_sha256": path["readiness"]["identity_sha256"],
    }


def _validate_semantic(
    store: EvidenceStore,
    digest: str,
    record: ModelRecord,
    request_contract: SemanticRequestContract,
    runtime_digest: str,
    attempt_id: str,
    *,
    gateway_path: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    value = _exact(
        store.receipt("semantic", digest, SEMANTIC_RECEIPT_SCHEMA, record.model_id),
        {
            "schema",
            "receipt_digest",
            "status",
            "model_id",
            "model_digest",
            "runtime_tuple_digest",
            "request_contract_sha256",
            "request_asset_set_sha256",
            "attempt_id",
            "observed_at",
            "validator",
            "responses",
            "distinct_requests",
            "distinct_responses",
            "gateway_path",
            "validator_result_digest",
        },
        "semantic receipt",
    )
    if value["status"] != "PASS" or value["model_id"] != record.model_id:
        raise CatalogError("semantic receipt is not a PASS for this model")
    if value["model_digest"] != record.digest or value["runtime_tuple_digest"] != runtime_digest:
        raise CatalogError("semantic receipt does not bind the model/runtime tuple")
    if (
        request_contract.model_id != record.model_id
        or request_contract.state != "qualified"
        or value["request_contract_sha256"] != request_contract.digest
        or value["request_asset_set_sha256"] != request_contract.asset_set_digest
    ):
        raise CatalogError("semantic receipt differs from the canonical request/asset contract")
    if value["attempt_id"] != attempt_id:
        raise CatalogError("semantic receipt belongs to another qualification attempt")
    if attempt_id == "gateway-smoke":
        if gateway_path is None or value["gateway_path"] != dict(gateway_path):
            raise CatalogError("gateway semantic receipt lacks the exact trusted gateway route")
    elif gateway_path is not None or value["gateway_path"] is not None:
        raise CatalogError("cohort semantic receipt cannot claim a gateway transport path")
    observed_at = _utc(value["observed_at"], "semantic observation time")
    if observed_at > store.attestation_issued_at("semantic", digest) + MAX_EVENT_CLOCK_SKEW:
        raise CatalogError("semantic receipt was signed before its calls completed")
    expected = record.to_dict()["semantic_validator"]
    validator = _exact(
        value["validator"],
        {"contract", "source_path", "source_sha256", "fixture_path", "fixture_sha256"},
        "semantic validator identity",
    )
    if any(validator[key] != expected[key] for key in validator):
        raise CatalogError("semantic receipt validator identity differs from the catalog")
    responses = _list(value["responses"], "semantic responses", nonempty=True)
    if len(responses) != 2:
        raise CatalogError("semantic qualification requires exactly two responses")
    request_ids: list[str] = []
    requests: list[str] = []
    outputs: list[str] = []
    for index, response in enumerate(responses):
        item = _exact(
            response,
            {"request_id", "request_sha256", "response_sha256", "semantic_valid"},
            f"semantic responses[{index}]",
        )
        request_id = _text(item["request_id"], "semantic request ID")
        for key in ("request_sha256", "response_sha256"):
            strong_sha256(item[key], f"semantic {key}")
        if item["semantic_valid"] is not True:
            raise CatalogError("semantic receipt contains an invalid response")
        assert request_id is not None
        request_ids.append(request_id)
        requests.append(item["request_sha256"])
        outputs.append(item["response_sha256"])
    if (
        request_ids != list(request_contract.request_ids)
        or requests != list(request_contract.request_sha256)
    ):
        raise CatalogError("semantic calls differ from the canonical request fixtures")
    if value["distinct_requests"] is not True or len(set(requests)) != 2:
        raise CatalogError("semantic qualification requires two distinct requests")
    if value["distinct_responses"] is not True or len(set(outputs)) != 2:
        raise CatalogError("semantic qualification requires two distinct responses")
    validator_result_digest = value["validator_result_digest"]
    strong_sha256(validator_result_digest, "semantic validator result digest")
    _validate_semantic_result(
        store,
        validator_result_digest,
        record,
        request_contract,
        runtime_digest,
        attempt_id,
        validator,
        requests,
        outputs,
        gateway_path,
    )
    store.assert_claims(
        "semantic",
        digest,
        {
            "runtime_tuple_digest": runtime_digest,
            "attempt_id": attempt_id,
            "request_contract_sha256": request_contract.digest,
            "request_asset_set_sha256": request_contract.asset_set_digest,
            "validator_identity_sha256": hashlib.sha256(canonical_bytes(validator)).hexdigest(),
            "call_set_sha256": hashlib.sha256(canonical_bytes(responses)).hexdigest(),
            **_gateway_claims(gateway_path),
            "validator_result_digest": validator_result_digest,
        },
    )
    return value


def _validate_semantic_result(
    store: EvidenceStore,
    digest: str,
    record: ModelRecord,
    request_contract: SemanticRequestContract,
    runtime_digest: str,
    attempt_id: str,
    validator: Mapping[str, Any],
    request_sha256: list[str],
    response_sha256: list[str],
    gateway_path: Mapping[str, Any] | None,
) -> None:
    value = _exact(
        store.receipt(
            "semantic-validations",
            digest,
            SEMANTIC_VALIDATION_SCHEMA,
            record.model_id,
        ),
        {
            "schema",
            "receipt_digest",
            "status",
            "validated_at",
            "model_id",
            "model_digest",
            "runtime_identity_digest",
            "request_contract_sha256",
            "request_asset_set_sha256",
            "gateway_path_sha256",
            "attempt_id",
            "validator",
            "request_sha256",
            "response_sha256",
        },
        "semantic validator result",
    )
    if (
        value["status"] != "PASS"
        or value["model_id"] != record.model_id
        or value["model_digest"] != record.digest
        or value["runtime_identity_digest"] != runtime_digest
        or value["request_contract_sha256"] != request_contract.digest
        or value["request_asset_set_sha256"] != request_contract.asset_set_digest
        or value["gateway_path_sha256"] != _gateway_claims(gateway_path)["gateway_path_sha256"]
        or value["attempt_id"] != attempt_id
        or value["validator"] != dict(validator)
        or value["request_sha256"] != request_sha256
        or value["response_sha256"] != response_sha256
    ):
        raise CatalogError("semantic receipt differs from its signed validator result")
    validated_at = _utc(value["validated_at"], "semantic validation time")
    if validated_at > store.attestation_issued_at(
        "semantic-validations", digest
    ) + MAX_EVENT_CLOCK_SKEW:
        raise CatalogError("semantic validator result was signed before validation")
    store.assert_claims(
        "semantic-validations",
        digest,
        {
            "runtime_identity_digest": runtime_digest,
            "attempt_id": attempt_id,
            "request_contract_sha256": request_contract.digest,
            "request_asset_set_sha256": request_contract.asset_set_digest,
            "validator_identity_sha256": hashlib.sha256(
                canonical_bytes(dict(validator))
            ).hexdigest(),
            "request_set_sha256": hashlib.sha256(
                canonical_bytes(request_sha256)
            ).hexdigest(),
            "response_set_sha256": hashlib.sha256(
                canonical_bytes(response_sha256)
            ).hexdigest(),
            **_gateway_claims(gateway_path),
        },
    )


def _validate_cleanup(
    store: EvidenceStore,
    digest: str,
    record: ModelRecord,
    *,
    attempt_id: str,
    runtime_digest: str,
    runtime_tuple: Mapping[str, Any],
    expected_resource_uids: list[Any],
) -> datetime:
    value = _exact(
        store.receipt("cleanup", digest, CLEANUP_RECEIPT_SCHEMA, record.model_id),
        {
            "schema",
            "receipt_digest",
            "status",
            "model_id",
            "model_digest",
            "attempt_id",
            "runtime_tuple_digest",
            "completed_at",
            "namespace",
            "node_identity",
            "gpu_identity",
            "expected_resource_uids",
            "resources",
            "temporary_paths_absent",
            "gpu_clients_after",
            "retained_artifact_digests",
        },
        "cleanup receipt",
    )
    if value["status"] != "PASS" or value["model_id"] != record.model_id:
        raise CatalogError("cleanup receipt is not a PASS for this model")
    if value["model_digest"] != record.digest:
        raise CatalogError("cleanup receipt differs from the immutable model")
    if value["attempt_id"] != attempt_id:
        raise CatalogError("cleanup receipt belongs to another attempt")
    if value["runtime_tuple_digest"] != runtime_digest:
        raise CatalogError("cleanup receipt belongs to another runtime tuple")
    completed_at = _utc(value["completed_at"], "cleanup completion time")
    if completed_at > store.attestation_issued_at("cleanup", digest) + MAX_EVENT_CLOCK_SKEW:
        raise CatalogError("cleanup receipt was signed before reclamation completed")
    namespace = _enum(
        value["namespace"], {"fs2-faststart", "fs2-models"}, "cleanup namespace"
    )
    runtime_worker = runtime_tuple["worker"]
    node_identity = _node_identity(value["node_identity"], "cleanup serving node")
    if node_identity != _node_identity(
        runtime_worker["node"], "runtime tuple serving node"
    ):
        raise CatalogError("cleanup receipt identifies another serving Node")
    gpu_identity = _exact(
        value["gpu_identity"],
        {
            "class",
            "node_preset",
            "node_count",
            "node_topology",
            "workload_count",
            "workload_topology",
            "allocated_uuids",
            "node_inventory_sha256",
        },
        "cleanup GPU identity",
    )
    if gpu_identity != runtime_worker["gpu"]:
        raise CatalogError("cleanup receipt identifies another runtime GPU tuple")
    expected = _list(
        expected_resource_uids, "qualification expected cleanup resources", nonempty=True
    )
    reopened_expected = _list(
        value["expected_resource_uids"], "cleanup expected resource UIDs", nonempty=True
    )
    normalized_expected: list[dict[str, Any]] = []
    for index, resource in enumerate(reopened_expected):
        item = _exact(
            resource,
            {"api_version", "kind", "namespace", "name", "uid"},
            f"cleanup expected resource UIDs[{index}]",
        )
        for key in ("api_version", "kind", "namespace", "name"):
            _text(item[key], f"cleanup expected resource {key}")
        if item["namespace"] != namespace or K8S_UID.fullmatch(item["uid"]) is None:
            raise CatalogError("cleanup expected resource has a foreign namespace or invalid UID")
        normalized_expected.append(dict(item))
    if normalized_expected != expected:
        raise CatalogError("cleanup receipt differs from the attempt's expected UID set")
    if normalized_expected != sorted(
        normalized_expected,
        key=lambda item: (
            item["api_version"],
            item["kind"],
            item["namespace"],
            item["name"],
            item["uid"],
        ),
    ):
        raise CatalogError("cleanup expected resource UID set is not canonical")
    resources = _list(value["resources"], "cleanup resources", nonempty=True)
    observed_resources: list[dict[str, Any]] = []
    for index, resource in enumerate(resources):
        item = _exact(
            resource,
            {
                "api_version",
                "kind",
                "namespace",
                "name",
                "uid",
                "precondition_uid",
                "final_state",
            },
            f"cleanup resources[{index}]",
        )
        for key in ("api_version", "kind", "namespace", "name"):
            _text(item[key], f"cleanup resource {key}")
        if item["namespace"] != namespace:
            raise CatalogError("cleanup resource is outside the receipt namespace")
        if item["uid"] != item["precondition_uid"] or K8S_UID.fullmatch(item["uid"]) is None:
            raise CatalogError("cleanup receipt lacks an exact UID precondition")
        if item["final_state"] != "absent":
            raise CatalogError("ephemeral cleanup resource is not absent")
        observed_resources.append(
            {
                key: item[key]
                for key in ("api_version", "kind", "namespace", "name", "uid")
            }
        )
    if observed_resources != normalized_expected:
        raise CatalogError("cleanup result does not reconcile the expected resource UID set")
    if value["temporary_paths_absent"] is not True or value["gpu_clients_after"] != 0:
        raise CatalogError("cleanup receipt did not prove temporary/GPU reclamation")
    retained = _list(value["retained_artifact_digests"], "retained artifact digests")
    for item in retained:
        strong_sha256(item, "cleanup retained artifact digest")
    store.assert_claims(
        "cleanup",
        digest,
        {
            "attempt_id": attempt_id,
            "runtime_tuple_digest": runtime_digest,
            "node_identity_sha256": hashlib.sha256(
                canonical_bytes(node_identity)
            ).hexdigest(),
            "gpu_identity_sha256": hashlib.sha256(
                canonical_bytes(gpu_identity)
            ).hexdigest(),
            "namespace": namespace,
            "expected_resource_uid_set_sha256": hashlib.sha256(
                canonical_bytes(normalized_expected)
            ).hexdigest(),
            "resource_set_sha256": hashlib.sha256(canonical_bytes(resources)).hexdigest(),
            "retained_artifact_set_sha256": hashlib.sha256(canonical_bytes(retained)).hexdigest(),
        },
    )
    return completed_at


def _validate_qualification(
    store: EvidenceStore,
    digest: str,
    record: ModelRecord,
    request_contract: SemanticRequestContract,
    cohort: str,
    runtime_digest: str,
    placement_digest: str,
    runtime_mechanism: str,
    runtime_tuple: Mapping[str, Any],
) -> None:
    value = _exact(
        store.receipt("qualifications", digest, QUALIFICATION_RECEIPT_SCHEMA, record.model_id),
        {
            "schema",
            "receipt_digest",
            "status",
            "cohort",
            "model_id",
            "model_digest",
            "startup_mechanism",
            "runtime_tuple_digest",
            "placement_receipt_digest",
            "attempt_count",
            "success_count",
            "failure_count",
            "attempts",
            "p50_t0_to_call2_seconds",
            "p95_t0_to_call2_seconds",
        },
        f"{cohort} qualification",
    )
    if value["status"] != "exploratory-pass" or value["cohort"] != cohort:
        raise CatalogError(f"{cohort} qualification is not an exploratory PASS")
    if value["model_id"] != record.model_id or value["model_digest"] != record.digest:
        raise CatalogError("qualification cohort differs from the catalog model")
    if value["runtime_tuple_digest"] != runtime_digest or value["placement_receipt_digest"] != placement_digest:
        raise CatalogError("qualification cohort differs from the runtime/staging tuple")
    if (
        value["startup_mechanism"] != runtime_mechanism
        or runtime_mechanism not in record.to_dict()["startup"]["enabled_mechanisms"]
    ):
        raise CatalogError("qualification cohort mechanism differs from the exact runtime tuple")
    attempt_count = _positive_int(value["attempt_count"], "qualification attempt count")
    success_count = value["success_count"]
    failure_count = value["failure_count"]
    if attempt_count is None or attempt_count < 3:
        raise CatalogError("qualification cohort requires at least three attempts")
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in (success_count, failure_count)):
        raise CatalogError("qualification success/failure counts are invalid")
    attempts = _list(value["attempts"], "qualification attempts", nonempty=True)
    if len(attempts) != attempt_count or success_count + failure_count != attempt_count:
        raise CatalogError("qualification cohort denominator does not reconcile")
    actual_failures = 0
    durations: list[float] = []
    attempt_ids: set[str] = set()
    semantic_digests: set[str] = set()
    cleanup_digests: set[str] = set()
    event_times: list[datetime] = []
    for index, attempt in enumerate(attempts):
        item = _exact(
            attempt,
            {
                "attempt_id",
                "status",
                "t0_utc",
                "completion_utc",
                "t0_to_call2_seconds",
                "semantic_receipt_digest",
                "cleanup_receipt_digest",
                "expected_resource_uids",
            },
            f"qualification attempts[{index}]",
        )
        attempt_id = _text(item["attempt_id"], "qualification attempt ID")
        if attempt_id in attempt_ids:
            raise CatalogError("qualification attempt IDs must be unique")
        assert attempt_id is not None
        attempt_ids.add(attempt_id)
        status = _enum(item["status"], {"PASS", "FAIL"}, "qualification attempt status")
        parsed_times: dict[str, datetime] = {}
        for key in ("t0_utc", "completion_utc"):
            stamp = _text(item[key], f"qualification attempt {key}", nullable=status == "FAIL")
            if stamp is not None and UTC_TIMESTAMP.fullmatch(stamp) is None:
                raise CatalogError("qualification timestamp is not exact UTC")
            if stamp is not None:
                parsed_times[key] = _utc(stamp, f"qualification attempt {key}")
                event_times.append(parsed_times[key])
        cleanup_digest = item["cleanup_receipt_digest"]
        strong_sha256(cleanup_digest, "qualification cleanup receipt digest")
        if cleanup_digest in cleanup_digests:
            raise CatalogError("qualification attempts must have distinct cleanup receipts")
        cleanup_digests.add(cleanup_digest)
        cleanup_completed_at = _validate_cleanup(
            store,
            cleanup_digest,
            record,
            attempt_id=attempt_id,
            runtime_digest=runtime_digest,
            runtime_tuple=runtime_tuple,
            expected_resource_uids=_list(
                item["expected_resource_uids"],
                "qualification expected cleanup resources",
                nonempty=True,
            ),
        )
        if status == "PASS":
            duration = item["t0_to_call2_seconds"]
            semantic_digest = item["semantic_receipt_digest"]
            if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
                raise CatalogError("successful qualification attempt lacks a positive duration")
            if parsed_times["completion_utc"] < parsed_times["t0_utc"]:
                raise CatalogError("qualification completion precedes externally accepted T0")
            if cleanup_completed_at < parsed_times["completion_utc"]:
                raise CatalogError("cleanup completed before the qualified attempt finished")
            timestamp_duration = (
                parsed_times["completion_utc"] - parsed_times["t0_utc"]
            ).total_seconds()
            if not math.isclose(
                float(duration),
                timestamp_duration,
                rel_tol=0.0,
                abs_tol=MAX_DURATION_ERROR_SECONDS,
            ):
                raise CatalogError("qualification duration differs from its ordered timestamps")
            strong_sha256(semantic_digest, "qualification semantic receipt digest")
            if semantic_digest in semantic_digests:
                raise CatalogError("successful attempts must have distinct semantic receipts")
            semantic_digests.add(semantic_digest)
            _validate_semantic(
                store,
                semantic_digest,
                record,
                request_contract,
                runtime_digest,
                attempt_id,
            )
            durations.append(float(duration))
        else:
            actual_failures += 1
            if item["semantic_receipt_digest"] is not None or item["t0_to_call2_seconds"] is not None:
                raise CatalogError("failed attempt cannot claim semantic success or completion duration")
    if actual_failures != failure_count or len(durations) != success_count or not durations:
        raise CatalogError("qualification failures/successes do not reconcile")
    if event_times and max(event_times) > store.attestation_issued_at(
        "qualifications", digest
    ) + MAX_EVENT_CLOCK_SKEW:
        raise CatalogError("qualification cohort was signed before its attempts completed")
    ranked = sorted(durations)
    p50_rank = math.ceil(0.50 * attempt_count)
    if p50_rank > success_count:
        raise CatalogError("exploratory PASS cannot have a failure-ranked null p50")
    expected_p50 = ranked[p50_rank - 1]
    p50 = value["p50_t0_to_call2_seconds"]
    if (
        isinstance(p50, bool)
        or not isinstance(p50, (int, float))
        or not math.isclose(float(p50), expected_p50, rel_tol=0.0, abs_tol=1e-9)
    ):
        raise CatalogError("qualification p50 does not match failure-ranked raw attempts")
    p95 = value["p95_t0_to_call2_seconds"]
    if attempt_count < 20 and p95 is not None:
        raise CatalogError("p95 must be withheld below n=20")
    if attempt_count >= 20:
        p95_rank = math.ceil(0.95 * attempt_count)
        expected_p95 = None if p95_rank > success_count else ranked[p95_rank - 1]
        if expected_p95 is None:
            if p95 is not None:
                raise CatalogError("qualification p95 must retain a failure-ranked null result")
        elif (
            isinstance(p95, bool)
            or not isinstance(p95, (int, float))
            or not math.isclose(float(p95), expected_p95, rel_tol=0.0, abs_tol=1e-9)
        ):
            raise CatalogError("qualification p95 does not match failure-ranked raw attempts")
    store.assert_claims(
        "qualifications",
        digest,
        {
            "cohort": cohort,
            "runtime_tuple_digest": runtime_digest,
            "placement_receipt_digest": placement_digest,
            "attempt_set_sha256": hashlib.sha256(canonical_bytes(attempts)).hexdigest(),
        },
    )


def _validate_readiness(
    store: EvidenceStore,
    digest: str,
    record: ModelRecord,
    runtime_digest: str,
    backend_identity: Mapping[str, Any],
) -> dict[str, Any]:
    value = _exact(
        store.receipt("readiness", digest, READINESS_RECEIPT_SCHEMA, record.model_id),
        {
            "schema",
            "receipt_digest",
            "status",
            "checked_at",
            "model_id",
            "model_digest",
            "runtime_tuple_digest",
            "backend",
            "observed_generation",
            "ready_endpoint",
            "http_status",
        },
        "readiness receipt",
    )
    if value["status"] != "PASS" or value["model_id"] != record.model_id:
        raise CatalogError("readiness receipt is not a PASS for this model")
    if value["model_digest"] != record.digest or value["runtime_tuple_digest"] != runtime_digest:
        raise CatalogError("readiness receipt differs from the model/runtime tuple")
    backend = _exact(
        value["backend"],
        {"namespace", "service_name", "service_uid", "port", "origin"},
        "readiness backend",
    )
    if any(backend[key] != backend_identity[key] for key in ("namespace", "service_name", "port", "origin")):
        raise CatalogError("readiness receipt differs from the exact serving backend")
    expected = record.to_dict()["interface"]["readiness"]
    if value["ready_endpoint"] != expected["path"] or value["http_status"] != expected["expected_status"]:
        raise CatalogError("readiness receipt differs from the model readiness contract")
    uid = _text(backend["service_uid"], "readiness service UID")
    if uid is None or K8S_UID.fullmatch(uid) is None:
        raise CatalogError("readiness receipt lacks the exact Service UID")
    _positive_int(value["observed_generation"], "readiness observed generation")
    checked_at = _utc(value["checked_at"], "readiness check time")
    issued_at = store.attestation_issued_at("readiness", digest)
    if checked_at > issued_at + MAX_EVENT_CLOCK_SKEW:
        raise CatalogError("readiness receipt was signed before its probe completed")
    age = store.now() - checked_at
    if age < -MAX_EVENT_CLOCK_SKEW or age > MAX_READINESS_AGE:
        raise CatalogError("readiness evidence is stale at route validation time")
    readiness_contract = {
        "path": expected["path"],
        "expected_status": expected["expected_status"],
    }
    signed_backend = {
        **dict(backend_identity),
        "service_uid": uid,
        "observed_generation": value["observed_generation"],
    }
    store.assert_claims(
        "readiness",
        digest,
        {
            "runtime_tuple_digest": runtime_digest,
            "backend_identity_sha256": hashlib.sha256(
                canonical_bytes(signed_backend)
            ).hexdigest(),
            "readiness_contract_sha256": hashlib.sha256(
                canonical_bytes(readiness_contract)
            ).hexdigest(),
        },
    )
    return signed_backend


def _validate_backend_identity(
    store: EvidenceStore,
    digest: str,
    record: ModelRecord,
    backend_identity: Mapping[str, Any],
) -> dict[str, Any]:
    value = _exact(
        store.receipt(
            "backends", digest, BACKEND_IDENTITY_RECEIPT_SCHEMA, record.model_id
        ),
        {
            "schema",
            "receipt_digest",
            "status",
            "checked_at",
            "model_id",
            "model_digest",
            "backend",
            "credential",
        },
        "backend identity receipt",
    )
    if (
        value["status"] != "PASS"
        or value["model_id"] != record.model_id
        or value["model_digest"] != record.digest
    ):
        raise CatalogError("backend identity receipt is not a PASS for this model")
    checked_at = _utc(value["checked_at"], "backend identity check time")
    issued_at = store.attestation_issued_at("backends", digest)
    if checked_at > issued_at + MAX_EVENT_CLOCK_SKEW:
        raise CatalogError("backend identity receipt was signed before its checks completed")
    age = store.now() - checked_at
    if age < -MAX_EVENT_CLOCK_SKEW or age > MAX_READINESS_AGE:
        raise CatalogError("backend identity evidence is stale at route validation time")
    backend = _exact(
        value["backend"],
        {
            "class",
            "inventory_model_id",
            "region",
            "gpu_class",
            "runtime_image_digest",
            "namespace",
            "service_name",
            "service_uid",
            "port",
            "origin",
            "endpoint_identity_sha256",
            "trust_bundle_sha256",
            "credential_requirement_id",
        },
        "backend identity subject",
    )
    for key, expected in backend_identity.items():
        if backend.get(key) != expected:
            raise CatalogError("backend identity receipt differs from the serving binding")
    uid = _text(backend["service_uid"], "backend Service UID")
    if uid is None or K8S_UID.fullmatch(uid) is None:
        raise CatalogError("backend identity receipt lacks the exact Service UID")
    strong_sha256(backend["endpoint_identity_sha256"], "backend endpoint identity")
    strong_sha256(backend["trust_bundle_sha256"], "backend trust bundle")
    record_value = record.to_dict()
    if backend["runtime_image_digest"] != record_value["runtime"]["image"]["digest"]:
        raise CatalogError("backend identity receipt aliases another runtime image")
    credential = _exact(
        value["credential"],
        {
            "requirement_id",
            "secret_uid",
            "resource_version",
            "rotated_at",
            "values_suppressed",
        },
        "backend credential identity",
    )
    if credential["values_suppressed"] is not True:
        raise CatalogError("backend credential evidence must suppress values")
    credential_requirement = backend_identity["credential_requirement_id"]
    if credential_requirement is None:
        if any(
            credential[key] is not None
            for key in ("requirement_id", "secret_uid", "resource_version", "rotated_at")
        ):
            raise CatalogError("local backend cannot claim an upstream credential")
    else:
        if credential["requirement_id"] != credential_requirement:
            raise CatalogError("backend credential receipt names another requirement")
        secret_uid = _text(credential["secret_uid"], "backend credential Secret UID")
        _text(credential["resource_version"], "backend credential resource version")
        if secret_uid is None or K8S_UID.fullmatch(secret_uid) is None:
            raise CatalogError("backend credential receipt lacks the exact Secret UID")
        rotated_at = _utc(credential["rotated_at"], "backend credential rotation time")
        if rotated_at > checked_at or store.now() - rotated_at > timedelta(hours=24):
            raise CatalogError("backend credential is not a fresh scoped rotation")
    signed_backend = {**dict(backend_identity), "service_uid": uid}
    store.assert_claims(
        "backends",
        digest,
        {
            "model_digest": record.digest,
            "runtime_image_digest": backend["runtime_image_digest"],
            "backend_identity_sha256": hashlib.sha256(
                canonical_bytes(signed_backend)
            ).hexdigest(),
            "credential_identity_sha256": hashlib.sha256(
                canonical_bytes(credential)
            ).hexdigest(),
        },
    )
    return backend


def _validate_federated_qualification(
    store: EvidenceStore,
    digest: str,
    record: ModelRecord,
    request_contract: SemanticRequestContract,
    backend_identity: Mapping[str, Any],
    backend_subject: Mapping[str, Any],
    gateway_identity: Mapping[str, Any],
    backend_evidence_digest: str,
    artifact_manifest_digest: str,
    artifact_uri: str,
) -> None:
    """Validate one exact, signed SM90 upstream readiness/semantic intersection."""

    value = _exact(
        store.receipt(
            "federated-qualifications",
            digest,
            FEDERATED_QUALIFICATION_RECEIPT_SCHEMA,
            record.model_id,
        ),
        {
            "schema",
            "receipt_digest",
            "status",
            "checked_at",
            "model_id",
            "model_digest",
            "backend_evidence_digest",
            "artifact",
            "runtime",
            "readiness",
            "semantic",
        },
        "federated qualification receipt",
    )
    if (
        value["status"] != "PASS"
        or value["model_id"] != record.model_id
        or value["model_digest"] != record.digest
        or value["backend_evidence_digest"] != backend_evidence_digest
    ):
        raise CatalogError("federated qualification subjects differ from the live route")
    checked_at = _utc(value["checked_at"], "federated qualification check time")
    issued_at = store.attestation_issued_at("federated-qualifications", digest)
    if checked_at > issued_at + MAX_EVENT_CLOCK_SKEW:
        raise CatalogError("federated qualification was signed before checks completed")
    age = store.now() - checked_at
    if age < -MAX_EVENT_CLOCK_SKEW or age > MAX_READINESS_AGE:
        raise CatalogError("federated qualification is stale at route validation time")

    record_value = record.to_dict()
    manifest = store.artifact(artifact_manifest_digest, record.model_id)
    static_artifact = record_value["cache"]["artifact"]
    if (
        manifest.digest != static_artifact["manifest_digest"]
        or manifest.model_id != record.model_id
        or manifest.kind != static_artifact["kind"]
        or manifest.source_revision != record_value["model"]["source"]["revision"]
        or manifest.license_id != record_value["model"]["source"]["license"]["id"]
        or manifest.license_state != "verified"
        or manifest.entitlement_state
        != record_value["model"]["source"]["entitlement"]["state"]
    ):
        raise CatalogError("federated artifact differs from the qualified model subject")
    store.assert_claims(
        "artifacts",
        manifest.digest,
        {
            "artifact_manifest_digest": manifest.digest,
            "artifact_content_digest": manifest.content_digest,
            "artifact_kind": manifest.kind,
            "model_revision": manifest.source_revision,
        },
    )
    expected_artifact_uri = (
        f"federated://{backend_identity['endpoint_identity_sha256']}"
        f"/models/{record.model_id}/sha256/{manifest.content_digest}"
    )
    if artifact_uri != expected_artifact_uri:
        raise CatalogError("federated artifact URI differs from the exact backend/content subject")
    artifact = _exact(
        value["artifact"],
        {
            "manifest_digest",
            "content_digest",
            "content_uri",
            "staging_state",
        },
        "federated artifact",
    )
    if artifact != {
        "manifest_digest": manifest.digest,
        "content_digest": manifest.content_digest,
        "content_uri": expected_artifact_uri,
        "staging_state": "ready-on-exact-federated-backend",
    }:
        raise CatalogError("federated staged artifact differs from the live route")

    runtime = _exact(
        value["runtime"],
        {"image_digest", "model_revision", "execution_identity_sha256"},
        "federated runtime",
    )
    expected_runtime = {
        "image_digest": record_value["runtime"]["image"]["digest"],
        "model_revision": record_value["model"]["source"]["revision"],
        "execution_identity_sha256": execution_identity(record_value),
    }
    if runtime != expected_runtime or runtime["image_digest"] != backend_identity[
        "runtime_image_digest"
    ]:
        raise CatalogError("federated runtime differs from the immutable model/backend identity")

    readiness = _exact(
        value["readiness"],
        {"method", "path", "expected_status", "observed_status", "observed_at"},
        "federated readiness",
    )
    readiness_contract = record_value["interface"]["readiness"]
    if readiness != {
        "method": readiness_contract["method"],
        "path": readiness_contract["path"],
        "expected_status": readiness_contract["expected_status"],
        "observed_status": readiness_contract["expected_status"],
        "observed_at": readiness["observed_at"],
    }:
        raise CatalogError("federated readiness differs from the exact model contract")
    readiness_at = _utc(readiness["observed_at"], "federated readiness observation time")

    semantic = _exact(
        value["semantic"],
        {
            "attempt_id",
            "observed_at",
            "request_contract_sha256",
            "request_asset_set_sha256",
            "validator",
            "responses",
            "distinct_requests",
            "distinct_responses",
            "gateway_path",
            "validator_result_digest",
        },
        "federated semantic qualification",
    )
    if semantic["attempt_id"] != "gateway-smoke":
        raise CatalogError("federated semantic qualification is not the gateway smoke")
    if (
        request_contract.state != "qualified"
        or semantic["request_contract_sha256"] != request_contract.digest
        or semantic["request_asset_set_sha256"] != request_contract.asset_set_digest
    ):
        raise CatalogError("federated semantic request/asset contract differs")
    readiness_digest = hashlib.sha256(canonical_bytes(readiness)).hexdigest()
    readiness_identity = _readiness_path_identity(
        record,
        evidence_kind="embedded-federated-readiness",
        evidence_digest=readiness_digest,
        service_uid=backend_subject["service_uid"],
        observed_generation=None,
        observation=readiness,
    )
    expected_gateway_path = _gateway_path(
        record,
        request_contract,
        gateway_identity,
        backend_subject,
        readiness_identity,
    )
    if semantic["gateway_path"] != expected_gateway_path:
        raise CatalogError("federated semantic receipt lacks the exact trusted gateway route")
    semantic_at = _utc(semantic["observed_at"], "federated semantic observation time")
    if any(
        observed > checked_at + MAX_EVENT_CLOCK_SKEW
        or store.now() - observed > MAX_READINESS_AGE
        or store.now() - observed < -MAX_EVENT_CLOCK_SKEW
        for observed in (readiness_at, semantic_at)
    ):
        raise CatalogError("federated semantic/readiness observation is stale or postdated")
    validator = _exact(
        semantic["validator"],
        {"contract", "source_path", "source_sha256", "fixture_path", "fixture_sha256"},
        "federated semantic validator",
    )
    expected_validator = {
        key: record_value["semantic_validator"][key]
        for key in (
            "contract",
            "source_path",
            "source_sha256",
            "fixture_path",
            "fixture_sha256",
        )
    }
    if validator != expected_validator:
        raise CatalogError("federated semantic validator differs from the pinned model contract")
    responses = _list(semantic["responses"], "federated semantic responses", nonempty=True)
    if len(responses) != 2:
        raise CatalogError("federated semantic qualification requires exactly two responses")
    request_hashes: list[str] = []
    response_hashes: set[str] = set()
    request_ids: list[str] = []
    for index, response in enumerate(responses):
        item = _exact(
            response,
            {"request_id", "request_sha256", "response_sha256", "semantic_valid"},
            f"federated semantic response {index}",
        )
        request_id = _text(item["request_id"], "federated semantic request ID")
        request_hash = strong_sha256(
            item["request_sha256"], "federated semantic request digest"
        )
        response_hash = strong_sha256(
            item["response_sha256"], "federated semantic response digest"
        )
        if item["semantic_valid"] is not True:
            raise CatalogError("federated semantic response did not pass validation")
        assert request_id is not None
        request_ids.append(request_id)
        request_hashes.append(request_hash)
        response_hashes.add(response_hash)
    if (
        semantic["distinct_requests"] is not True
        or semantic["distinct_responses"] is not True
        or len(set(request_ids)) != 2
        or len(set(request_hashes)) != 2
        or len(response_hashes) != 2
    ):
        raise CatalogError("federated qualification requires two distinct semantic calls")
    if (
        request_ids != list(request_contract.request_ids)
        or request_hashes != list(request_contract.request_sha256)
    ):
        raise CatalogError("federated semantic calls differ from canonical request fixtures")
    validator_result_digest = semantic["validator_result_digest"]
    strong_sha256(
        validator_result_digest, "federated semantic validator result digest"
    )
    _validate_semantic_result(
        store,
        validator_result_digest,
        record,
        request_contract,
        backend_evidence_digest,
        "gateway-smoke",
        validator,
        [item["request_sha256"] for item in responses],
        [item["response_sha256"] for item in responses],
        expected_gateway_path,
    )

    backend_subject = dict(backend_identity)
    claims = {
        "model_digest": record.digest,
        "backend_evidence_digest": backend_evidence_digest,
        "backend_subject_sha256": hashlib.sha256(
            canonical_bytes(backend_subject)
        ).hexdigest(),
        "artifact_subject_sha256": hashlib.sha256(
            canonical_bytes(artifact)
        ).hexdigest(),
        "runtime_identity_sha256": hashlib.sha256(
            canonical_bytes(runtime)
        ).hexdigest(),
        "readiness_contract_sha256": hashlib.sha256(
            canonical_bytes(
                {
                    "method": readiness["method"],
                    "path": readiness["path"],
                    "expected_status": readiness["expected_status"],
                }
            )
        ).hexdigest(),
        "readiness_observation_sha256": hashlib.sha256(
            canonical_bytes(readiness)
        ).hexdigest(),
        "validator_identity_sha256": hashlib.sha256(
            canonical_bytes(validator)
        ).hexdigest(),
        "request_contract_sha256": request_contract.digest,
        "request_asset_set_sha256": request_contract.asset_set_digest,
        "call_set_sha256": hashlib.sha256(canonical_bytes(responses)).hexdigest(),
        **_gateway_claims(expected_gateway_path),
        "validator_result_digest": validator_result_digest,
    }
    store.assert_claims("federated-qualifications", digest, claims)


def validate_federated_route_evidence(
    record: ModelRecord,
    qualification: dict[str, Any],
    evidence_root: Path | str,
    *,
    backend_identity: Mapping[str, Any],
    gateway_identity: Mapping[str, Any],
    semantic_request_contract: SemanticRequestContract,
    evidence_session_id: str,
    trusted_attestors: Mapping[str, str],
    validation_time: datetime | None = None,
) -> str:
    """Verify the signed route intersection for a qualified SM90 upstream."""

    store = EvidenceStore(
        evidence_root,
        session_id=evidence_session_id,
        trusted_attestors=trusted_attestors,
        validation_time=validation_time,
    )
    backend_digest = qualification["backend_evidence_digest"]
    backend_subject = _validate_backend_identity(
        store, backend_digest, record, backend_identity
    )
    _validate_federated_qualification(
        store,
        qualification["federated_qualification_digest"],
        record,
        semantic_request_contract,
        backend_identity,
        backend_subject,
        gateway_identity,
        backend_digest,
        qualification["artifact_manifest_digest"],
        qualification["artifact_uri"],
    )
    return store.valid_until()


def _scale_controller_subject(value: Any, label: str) -> dict[str, Any]:
    controller = _exact(
        value,
        {
            "class",
            "namespace",
            "deployment_name",
            "deployment_uid",
            "pod_name",
            "pod_uid",
            "pod_owner_deployment_uid",
            "service_account_name",
            "service_account_uid",
            "leader_lease_name",
            "leader_lease_uid",
            "leader_lease_resource_version",
            "leader_lease_holder_identity",
            "leader_lease_renew_time",
            "leader_lease_duration_seconds",
            "leader_role_namespace",
            "leader_role_name",
            "target_role_namespace",
            "target_role_name",
            "submitter_service_account_name",
            "submitter_service_account_uid",
            "submitter_deployment_name",
            "submitter_deployment_uid",
            "submitter_pod_name",
            "submitter_pod_uid",
            "submitter_pod_owner_deployment_uid",
            "submitter_database_role",
            "claim_owner_database_role",
            "submitter_database_secret",
            "claim_owner_database_secret",
            "database_grants_sha256",
            "activation_store_sha256",
            "activation_store_ddl_sha256",
            "auth_class",
            "intent_interface_sha256",
            "identity_sha256",
        },
        label,
    )
    expected = {
        "class": "fs2-model-activation-controller",
        "namespace": "fs2-system",
        "deployment_name": "fs2-serve-control-plane-activation",
        "service_account_name": "fs2-model-activation-controller",
        "leader_lease_name": "fs2-serve-activation-controller",
        "leader_role_namespace": "fs2-system",
        "leader_role_name": "fs2-serve-control-plane-activation-leader",
        "target_role_namespace": "fs2-models",
        "target_role_name": "fs2-serve-control-plane-activation-targets",
        "submitter_service_account_name": "fs2-serve-control-plane",
        "submitter_deployment_name": "fs2-serve-control-plane",
        "submitter_database_role": "fs2_activation_submitter",
        "claim_owner_database_role": "fs2_activation_claim_owner",
        "auth_class": (
            "postgres-role-grants-plus-projected-ksa-lease-operation-fence"
        ),
    }
    if any(controller[key] != item for key, item in expected.items()):
        raise CatalogError("scale receipt names a foreign activation controller")
    for key in (
        "deployment_uid",
        "pod_uid",
        "pod_owner_deployment_uid",
        "service_account_uid",
        "leader_lease_uid",
        "submitter_service_account_uid",
        "submitter_deployment_uid",
        "submitter_pod_uid",
        "submitter_pod_owner_deployment_uid",
    ):
        uid = _text(controller[key], f"{label} {key}")
        if uid is None or K8S_UID.fullmatch(uid) is None:
            raise CatalogError("scale receipt lacks an exact controller UID")
    subject = {key: controller[key] for key in expected}
    if (
        controller["pod_owner_deployment_uid"] != controller["deployment_uid"]
        or controller["submitter_pod_owner_deployment_uid"]
        != controller["submitter_deployment_uid"]
    ):
        raise CatalogError("scale receipt Pod ownership identity differs")
    for secret_key, expected_name in (
        ("submitter_database_secret", "fs2-activation-submitter-db"),
        ("claim_owner_database_secret", "fs2-activation-claim-owner-db"),
    ):
        secret = _exact(
            controller[secret_key],
            {"namespace", "name", "uid", "resource_version", "type", "key_set"},
            f"{label} {secret_key}",
        )
        secret_uid = _text(secret["uid"], f"{label} {secret_key} UID")
        _text(secret["resource_version"], f"{label} {secret_key} resourceVersion")
        if (
            secret["namespace"] != "fs2-system"
            or secret["name"] != expected_name
            or secret_uid is None
            or K8S_UID.fullmatch(secret_uid) is None
            or secret["type"] != "Opaque"
            or secret["key_set"] != ["dsn"]
        ):
            raise CatalogError("scale receipt database Secret identity differs")
    subject.update(
        {
            "deployment_uid": controller["deployment_uid"],
            "pod_name": _text(controller["pod_name"], f"{label} Pod name"),
            "pod_uid": controller["pod_uid"],
            "pod_owner_deployment_uid": controller["pod_owner_deployment_uid"],
            "service_account_uid": controller["service_account_uid"],
            "leader_lease_uid": controller["leader_lease_uid"],
            "leader_lease_resource_version": _text(
                controller["leader_lease_resource_version"],
                f"{label} leader Lease resourceVersion",
            ),
            "leader_lease_holder_identity": _text(
                controller["leader_lease_holder_identity"],
                f"{label} leader Lease holderIdentity",
            ),
            "leader_lease_renew_time": controller["leader_lease_renew_time"],
            "leader_lease_duration_seconds": controller[
                "leader_lease_duration_seconds"
            ],
            "submitter_service_account_uid": controller[
                "submitter_service_account_uid"
            ],
            "submitter_deployment_uid": controller["submitter_deployment_uid"],
            "submitter_pod_name": _text(
                controller["submitter_pod_name"], f"{label} submitter Pod name"
            ),
            "submitter_pod_uid": controller["submitter_pod_uid"],
            "submitter_pod_owner_deployment_uid": controller[
                "submitter_pod_owner_deployment_uid"
            ],
            "submitter_database_secret": controller["submitter_database_secret"],
            "claim_owner_database_secret": controller["claim_owner_database_secret"],
            "database_grants_sha256": strong_sha256(
                controller["database_grants_sha256"],
                f"{label} database grants",
            ),
            "intent_interface_sha256": strong_sha256(
                controller["intent_interface_sha256"], f"{label} intent interface"
            ),
            "activation_store_sha256": strong_sha256(
                controller["activation_store_sha256"], f"{label} activation store"
            ),
            "activation_store_ddl_sha256": strong_sha256(
                controller["activation_store_ddl_sha256"],
                f"{label} activation store DDL",
            ),
        }
    )
    if controller["leader_lease_duration_seconds"] != 30:
        raise CatalogError("scale controller leader Lease duration differs")
    _utc(controller["leader_lease_renew_time"], f"{label} leader Lease renewTime")
    identity = strong_sha256(controller["identity_sha256"], f"{label} identity")
    if identity != hashlib.sha256(canonical_bytes(subject)).hexdigest():
        raise CatalogError("scale controller identity digest differs from its exact subject")
    return dict(controller)


def _scale_intent_subject(
    value: Any,
    label: str,
    record: ModelRecord,
    activation: Mapping[str, Any],
    controller: Mapping[str, Any],
    *,
    action: str,
) -> tuple[dict[str, Any], datetime]:
    """Validate the durable PostgreSQL row and its live claim fence."""

    intent = _exact(
        value,
        {
            "schema",
            "intent_id",
            "operation_id",
            "operation_attempt",
            "fence_operation_id",
            "model_id",
            "model_revision",
            "binding_digest",
            "action",
            "subject_sha256",
            "store_contract_sha256",
            "submitter_service_account_uid",
            "submitter_database_role",
            "claim_owner_service_account_uid",
            "claim_owner_database_role",
            "controller_id",
            "previous_fencing_token",
            "fencing_token",
            "database_now",
            "claim_started_at",
            "leader_lease_uid",
            "leader_lease_resource_version",
            "leader_lease_holder_identity",
            "claim_lease_expires_at",
        },
        label,
    )
    if (
        intent["schema"] != "fs2-serve.nebius.ai/postgres-activation-intent/v3"
        or intent["action"] != action
        or intent["model_id"] != record.model_id
        or intent["binding_digest"] != activation["binding_digest"]
        or intent["model_revision"]
        != record.to_dict()["model"]["source"]["revision"]
        or intent["store_contract_sha256"] != controller["activation_store_sha256"]
    ):
        raise CatalogError("scale lifecycle intent differs from its canonical route subject")
    intent_id = _scale_operation(intent["intent_id"], f"{label} intent ID")
    fence_operation_id = _scale_operation(
        intent["fence_operation_id"], f"{label} fence operation ID"
    )
    if fence_operation_id != intent_id:
        raise CatalogError("scale lifecycle fence operation does not equal its intent ID")
    operation_attempt = intent["operation_attempt"]
    if (
        isinstance(operation_attempt, bool)
        or not isinstance(operation_attempt, int)
        or operation_attempt < 0
        or operation_attempt > 10
    ):
        raise CatalogError("scale lifecycle intent operation attempt is invalid")
    if action == "activate":
        operation_id = _scale_operation(intent["operation_id"], f"{label} operation ID")
        if operation_id != intent_id:
            raise CatalogError("activation intent ID must equal its durable operation ID")
    elif intent["operation_id"] is not None or operation_attempt != 0:
        raise CatalogError("deactivation intent must be operation-free at attempt zero")
    controller_id = _text(intent["controller_id"], f"{label} controller ID")
    if (
        controller_id != controller["leader_lease_holder_identity"]
        or intent["leader_lease_uid"] != controller["leader_lease_uid"]
        or intent["leader_lease_resource_version"]
        != controller["leader_lease_resource_version"]
        or intent["leader_lease_holder_identity"]
        != controller["leader_lease_holder_identity"]
        or intent["submitter_service_account_uid"]
        != controller["submitter_service_account_uid"]
        or intent["submitter_database_role"] != "fs2_activation_submitter"
        or intent["claim_owner_service_account_uid"]
        != controller["service_account_uid"]
        or intent["claim_owner_database_role"] != "fs2_activation_claim_owner"
    ):
        raise CatalogError(
            "scale lifecycle intent principal or Lease fence differs from its controller"
        )
    previous_token = intent["previous_fencing_token"]
    fencing_token = _positive_int(intent["fencing_token"], f"{label} fencing token")
    if (
        isinstance(previous_token, bool)
        or not isinstance(previous_token, int)
        or previous_token < 0
        or fencing_token != previous_token + 1
    ):
        raise CatalogError("scale lifecycle fencing token is not the next per-model value")
    expected_subject = {
        "intent_id": intent_id,
        "operation_id": intent["operation_id"],
        "operation_attempt": operation_attempt,
        "model_id": intent["model_id"],
        "model_revision": intent["model_revision"],
        "binding_digest": intent["binding_digest"],
        "action": intent["action"],
        "submitter_service_account_uid": intent["submitter_service_account_uid"],
        "store_contract_sha256": intent["store_contract_sha256"],
    }
    if intent["subject_sha256"] != hashlib.sha256(
        canonical_bytes(expected_subject)
    ).hexdigest():
        raise CatalogError("scale lifecycle intent idempotency subject digest differs")
    database_now = _utc(intent["database_now"], f"{label} database clock")
    claim_started = _utc(intent["claim_started_at"], f"{label} claim start")
    lease_expires = _utc(
        intent["claim_lease_expires_at"], f"{label} claim lease expiry"
    )
    leader_renewed = _utc(
        controller["leader_lease_renew_time"], f"{label} leader Lease renewTime"
    )
    leader_expires = leader_renewed + timedelta(
        seconds=controller["leader_lease_duration_seconds"]
    )
    if claim_started != database_now or not database_now < lease_expires <= leader_expires:
        raise CatalogError("scale lifecycle claim outlives its Kubernetes leader Lease")
    return dict(intent), lease_expires


def _scale_target_subject(
    value: Any,
    scale_contract: ScaleContract,
    label: str,
) -> dict[str, Any]:
    target = _exact(
        value,
        {
            "api_version",
            "kind",
            "namespace",
            "name",
            "uid",
            "resource_version",
            "observed_generation",
            "template_identity_sha256",
        },
        label,
    )
    expected = scale_contract.to_dict()["target"]
    if expected is None or any(
        target[key] != expected[key]
        for key in ("api_version", "kind", "namespace", "name", "template_identity_sha256")
    ):
        raise CatalogError("scale receipt target differs from the immutable model target")
    uid = _text(target["uid"], f"{label} UID")
    if uid is None or K8S_UID.fullmatch(uid) is None:
        raise CatalogError("scale receipt target lacks an exact Kubernetes UID")
    _text(target["resource_version"], f"{label} resource version")
    _positive_int(target["observed_generation"], f"{label} observed generation")
    return dict(target)


def _scale_operation(value: Any, label: str) -> str:
    operation_id = _text(value, label)
    assert operation_id is not None
    try:
        parsed = UUID(operation_id)
    except (ValueError, AttributeError) as exc:
        raise CatalogError("scale lifecycle operation ID is not a canonical UUID") from exc
    if str(parsed) != operation_id:
        raise CatalogError("scale lifecycle operation ID is not a canonical UUID")
    return operation_id


def _stable_scale_target(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project the immutable identity that survives replica mutations."""

    return {
        key: value[key]
        for key in (
            "api_version",
            "kind",
            "namespace",
            "name",
            "uid",
            "template_identity_sha256",
        )
    }


def _mounted_content_path(record: ModelRecord, content_uri: str) -> str:
    parsed = urlsplit(content_uri)
    if parsed.scheme == "pvc":
        prefix = "/qwen3-8b-weights"
        if record.model_id != "qwen3-8b" or not parsed.path.startswith(prefix + "/"):
            raise CatalogError("runtime startup PVC content path is not the exact Qwen claim")
        return "/mnt/fs2-provider-block" + parsed.path.removeprefix(prefix)
    if parsed.scheme in {"sfs", "nvme"}:
        return parsed.path
    raise CatalogError("runtime startup content URI is not a reviewed mounted backend")


def _validate_replica_ownership(
    value: Any,
    *,
    target: Mapping[str, Any],
    label: str,
) -> dict[str, Any]:
    ownership = _exact(
        value,
        {
            "schema",
            "api_server_identity_sha256",
            "target_uid",
            "target_resource_version",
            "managed_fields_resource_version",
            "managed_fields_observed_at",
            "replica_field_manager",
            "replica_field_path",
            "fields_v1_sha256",
            "gitops_manager",
            "gitops_owns_replicas",
            "foreign_replica_managers",
            "ownership_annotation_sha256",
        },
        label,
    )
    if ownership["schema"] != "fs2-serve.nebius.ai/replica-field-ownership-receipt/v1":
        raise CatalogError("replica ownership receipt schema differs")
    for key in (
        "api_server_identity_sha256",
        "fields_v1_sha256",
        "ownership_annotation_sha256",
    ):
        strong_sha256(ownership[key], f"{label} {key}")
    _utc(ownership["managed_fields_observed_at"], f"{label} observed time")
    if (
        ownership["target_uid"] != target["uid"]
        or ownership["target_resource_version"] != target["resource_version"]
        or ownership["managed_fields_resource_version"] != target["resource_version"]
        or ownership["replica_field_manager"] != "fs2-model-activation-controller"
        or ownership["replica_field_path"] != "f:spec/f:replicas"
        or ownership["gitops_manager"] != "argocd-application-controller"
        or ownership["gitops_owns_replicas"] is not False
        or ownership["foreign_replica_managers"] != []
    ):
        raise CatalogError("replica ownership does not prove activation-only managedFields")
    return dict(ownership)


def _validate_runtime_startup(
    value: Any,
    *,
    record: ModelRecord,
    target: Mapping[str, Any],
    content_uri: str,
    artifact_manifest_digest: str,
    ready_at: datetime,
) -> dict[str, Any] | None:
    required = record.model_id in {"qwen3-8b", "glm-5-2-fp8", "nv-reason-cxr-3b"}
    if not required:
        if value is not None:
            raise CatalogError("non-mounted runtime invented a startup-isolation receipt")
        return None
    startup = _exact(
        value,
        {
            "schema",
            "model_id",
            "artifact_manifest_digest",
            "artifact_uri",
            "mounted_content_path",
            "effective_argv",
            "served_model_alias",
            "pod",
            "network_policy",
            "network_probe",
            "timestamps",
        },
        "runtime mounted-content startup",
    )
    mounted_path = _mounted_content_path(record, content_uri)
    record_value = record.to_dict()
    expected_argv = [
        mounted_path if item == "{FS2_MODEL_CONTENT_PATH}" else item
        for item in record_value["runtime"]["command"]
    ]
    if (
        startup["schema"] != "fs2-serve.nebius.ai/runtime-startup-receipt/v1"
        or startup["model_id"] != record.model_id
        or startup["artifact_manifest_digest"] != artifact_manifest_digest
        or startup["artifact_uri"] != content_uri
        or startup["mounted_content_path"] != mounted_path
        or startup["effective_argv"] != expected_argv
        or startup["served_model_alias"] != record.model_id
        or record_value["model"]["source"]["repository"] in expected_argv
        or "--revision" in expected_argv
    ):
        raise CatalogError("runtime startup did not consume the exact mounted content address")
    pod = _exact(
        startup["pod"],
        {
            "api_version",
            "kind",
            "namespace",
            "name",
            "uid",
            "owner_uid",
            "service_account_uid",
            "container_name",
            "container_id",
            "runtime_image_digest",
        },
        "runtime startup Pod",
    )
    for key in ("uid", "owner_uid", "service_account_uid"):
        uid = _text(pod[key], f"runtime startup Pod {key}")
        if uid is None or K8S_UID.fullmatch(uid) is None:
            raise CatalogError("runtime startup lacks exact Pod/owner/KSA identities")
    if (
        pod["api_version"] != "v1"
        or pod["kind"] != "Pod"
        or pod["namespace"] != "fs2-models"
        or pod["owner_uid"] != target["uid"]
        or pod["container_name"] != "model"
        or not str(pod["container_id"]).startswith("containerd://")
        or pod["runtime_image_digest"] != record_value["runtime"]["image"]["digest"]
    ):
        raise CatalogError("runtime startup Pod differs from the exact activation target/runtime")
    policy = _exact(
        startup["network_policy"],
        {
            "api_version",
            "kind",
            "namespace",
            "name",
            "uid",
            "resource_version",
            "pod_selector",
            "policy_types",
            "egress",
            "observed_at",
        },
        "runtime startup NetworkPolicy",
    )
    policy_uid = _text(policy["uid"], "runtime startup NetworkPolicy UID")
    _text(policy["resource_version"], "runtime startup NetworkPolicy resourceVersion")
    policy_observed = _utc(policy["observed_at"], "runtime NetworkPolicy observation")
    if (
        policy["api_version"] != "networking.k8s.io/v1"
        or policy["kind"] != "NetworkPolicy"
        or policy["namespace"] != "fs2-models"
        or policy["name"] != f"{record.model_id}-runtime-deny-egress"
        or policy_uid is None
        or K8S_UID.fullmatch(policy_uid) is None
        or policy["pod_selector"]
        != {"matchLabels": {"fs2-serve.nebius.ai/model-id": record.model_id}}
        or policy["policy_types"] != ["Egress"]
        or policy["egress"] != []
    ):
        raise CatalogError("runtime startup lacks the exact API-observed deny-egress policy")
    probe = _exact(
        startup["network_probe"],
        {"dns", "https", "model_registry", "observed_at"},
        "runtime startup network probe",
    )
    probe_observed = _utc(probe["observed_at"], "runtime network probe observation")
    if probe != {
        "dns": "blocked",
        "https": "blocked",
        "model_registry": "blocked",
        "observed_at": probe["observed_at"],
    }:
        raise CatalogError("runtime startup network-denial canary did not fail closed")
    timestamps = _exact(
        startup["timestamps"],
        {"policy_observed_at", "process_started_at", "ready_at"},
        "runtime startup timestamps",
    )
    process_started = _utc(timestamps["process_started_at"], "runtime process start")
    startup_ready = _utc(timestamps["ready_at"], "runtime startup ready time")
    if (
        timestamps["policy_observed_at"] != policy["observed_at"]
        or not policy_observed <= process_started <= probe_observed <= startup_ready
        or startup_ready != ready_at
    ):
        raise CatalogError("runtime startup did not prove deny-egress before process start")
    return dict(startup)


def _validate_zero_to_ready(
    store: EvidenceStore,
    digest: str,
    record: ModelRecord,
    scale_contract: ScaleContract,
    runtime_digest: str,
    activation: Mapping[str, Any],
    content_uri: str,
    artifact_manifest_digest: str,
) -> tuple[dict[str, Any], datetime]:
    value = _exact(
        store.receipt(
            "zero-to-ready",
            digest,
            ZERO_TO_READY_RECEIPT_SCHEMA,
            record.model_id,
        ),
        {
            "schema",
            "receipt_digest",
            "status",
            "observed_at",
            "model_id",
            "model_digest",
            "scale_contract_digest",
            "runtime_tuple_digest",
            "intent",
            "controller",
            "target",
            "replicas",
            "replica_ownership",
            "runtime_startup",
            "timestamps",
            "readiness",
            "warmup",
            "preemption",
        },
        "zero-to-ready receipt",
    )
    if (
        value["status"] != "PASS"
        or value["model_id"] != record.model_id
        or value["model_digest"] != record.digest
        or value["scale_contract_digest"] != scale_contract.digest
        or value["runtime_tuple_digest"] != runtime_digest
    ):
        raise CatalogError("zero-to-ready receipt differs from the route subject")
    controller = _scale_controller_subject(value["controller"], "zero-to-ready controller")
    intent, claim_lease_expires = _scale_intent_subject(
        value["intent"],
        "zero-to-ready intent",
        record,
        activation,
        controller,
        action="activate",
    )
    target = _scale_target_subject(value["target"], scale_contract, "zero-to-ready target")
    if (
        controller != activation["controller_receipt_subject"]
        or controller["intent_interface_sha256"]
        != activation["controller_receipt_subject"]["intent_interface_sha256"]
        or _stable_scale_target(target)
        != _stable_scale_target(activation["target_receipt_subject"])
    ):
        raise CatalogError("zero-to-ready receipt differs from the live activation binding")
    policy = scale_contract.to_dict()["policy"]
    replicas = _exact(
        value["replicas"], {"previous", "desired", "observed"}, "zero-to-ready replicas"
    )
    if replicas != {
        "previous": policy["desired_floor"],
        "desired": policy["desired_max"],
        "observed": policy["desired_max"],
    } or policy["desired_floor"] != 0 or policy["desired_max"] != 1:
        raise CatalogError("zero-to-ready receipt does not prove the exact zero-to-one transition")
    timestamps = _exact(
        value["timestamps"],
        {"accepted_at", "mutation_at", "ready_at", "duration_seconds"},
        "zero-to-ready timestamps",
    )
    accepted = _utc(timestamps["accepted_at"], "zero-to-ready accepted time")
    mutated = _utc(timestamps["mutation_at"], "zero-to-ready mutation time")
    ready = _utc(timestamps["ready_at"], "zero-to-ready ready time")
    observed = _utc(value["observed_at"], "zero-to-ready observation time")
    if (
        not accepted <= mutated <= ready < claim_lease_expires
        or observed != ready
    ):
        raise CatalogError("zero-to-ready lifecycle timestamps are not ordered")
    duration = timestamps["duration_seconds"]
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or not math.isclose(
            float(duration),
            (ready - accepted).total_seconds(),
            rel_tol=0.0,
            abs_tol=MAX_DURATION_ERROR_SECONDS,
        )
    ):
        raise CatalogError("zero-to-ready duration differs from its ordered timestamps")
    if observed > store.attestation_issued_at("zero-to-ready", digest) + MAX_EVENT_CLOCK_SKEW:
        raise CatalogError("zero-to-ready receipt was signed before readiness completed")
    replica_ownership = _validate_replica_ownership(
        value["replica_ownership"],
        target=target,
        label="zero-to-ready replica ownership",
    )
    runtime_startup = _validate_runtime_startup(
        value["runtime_startup"],
        record=record,
        target=target,
        content_uri=content_uri,
        artifact_manifest_digest=artifact_manifest_digest,
        ready_at=ready,
    )
    contract = scale_contract.to_dict()
    readiness = _exact(
        value["readiness"],
        {"method", "path", "expected_status", "observed_status", "checked_at"},
        "zero-to-ready readiness",
    )
    expected_readiness = contract["readiness"]
    if expected_readiness is None or any(
        readiness[key] != expected_readiness[key]
        for key in ("method", "path", "expected_status")
    ) or readiness["observed_status"] != expected_readiness["expected_status"]:
        raise CatalogError("zero-to-ready readiness differs from the immutable probe")
    checked_at = _utc(readiness["checked_at"], "zero-to-ready readiness check")
    if checked_at != ready:
        raise CatalogError("zero-to-ready ready time differs from the probe observation")
    warmup = _exact(
        value["warmup"], {"required", "status", "checked_at"}, "zero-to-ready warmup"
    )
    if contract["warmup"] is None:
        if warmup != {"required": False, "status": "not-required", "checked_at": None}:
            raise CatalogError("zero-to-ready invented a warmup result")
    else:
        if warmup["required"] is not True or warmup["status"] != "PASS":
            raise CatalogError("zero-to-ready did not pass the required warmup")
        _utc(warmup["checked_at"], "zero-to-ready warmup check")
    preemption = _exact(
        value["preemption"],
        {"notice_observed", "new_admissions", "attempt_outcome"},
        "zero-to-ready preemption",
    )
    if preemption != {
        "notice_observed": False,
        "new_admissions": "allow",
        "attempt_outcome": "PASS",
    }:
        raise CatalogError("zero-to-ready receipt is not an uninterrupted PASS")
    store.assert_claims(
        "zero-to-ready",
        digest,
        {
            "model_digest": record.digest,
            "scale_contract_digest": scale_contract.digest,
            "runtime_tuple_digest": runtime_digest,
            "activation_intent_sha256": hashlib.sha256(
                canonical_bytes(intent)
            ).hexdigest(),
            "operation_id": intent["operation_id"],
            "operation_attempt": intent["operation_attempt"],
            "fence_operation_id": intent["fence_operation_id"],
            "intent_model_id": intent["model_id"],
            "binding_digest": intent["binding_digest"],
            "controller_id": intent["controller_id"],
            "previous_fencing_token": intent["previous_fencing_token"],
            "fencing_token": intent["fencing_token"],
            "database_now": intent["database_now"],
            "claim_started_at": intent["claim_started_at"],
            "intent_subject_sha256": intent["subject_sha256"],
            "activation_store_sha256": intent["store_contract_sha256"],
            "submitter_service_account_uid": intent[
                "submitter_service_account_uid"
            ],
            "claim_owner_service_account_uid": intent[
                "claim_owner_service_account_uid"
            ],
            "leader_lease_uid": intent["leader_lease_uid"],
            "leader_lease_resource_version": intent[
                "leader_lease_resource_version"
            ],
            "leader_lease_holder_identity": intent[
                "leader_lease_holder_identity"
            ],
            "claim_lease_expires_at": intent["claim_lease_expires_at"],
            "controller_identity_sha256": controller["identity_sha256"],
            "target_identity_sha256": hashlib.sha256(canonical_bytes(target)).hexdigest(),
            "replica_transition_sha256": hashlib.sha256(canonical_bytes(replicas)).hexdigest(),
            "replica_ownership_sha256": hashlib.sha256(
                canonical_bytes(replica_ownership)
            ).hexdigest(),
            "runtime_startup_sha256": (
                hashlib.sha256(canonical_bytes(runtime_startup)).hexdigest()
                if runtime_startup is not None
                else None
            ),
            "lifecycle_timestamps_sha256": hashlib.sha256(canonical_bytes(timestamps)).hexdigest(),
            "readiness_observation_sha256": hashlib.sha256(canonical_bytes(readiness)).hexdigest(),
        },
    )
    return value, ready


def _validate_return_to_zero(
    store: EvidenceStore,
    digest: str,
    record: ModelRecord,
    scale_contract: ScaleContract,
    runtime_digest: str,
    activation: Mapping[str, Any],
    *,
    artifact_manifest_digest: str,
    zero_to_ready: Mapping[str, Any],
    ready_at: datetime,
) -> None:
    value = _exact(
        store.receipt(
            "return-to-zero",
            digest,
            RETURN_TO_ZERO_RECEIPT_SCHEMA,
            record.model_id,
        ),
        {
            "schema",
            "receipt_digest",
            "status",
            "observed_at",
            "model_id",
            "model_digest",
            "scale_contract_digest",
            "runtime_tuple_digest",
            "intent",
            "controller",
            "target",
            "replicas",
            "replica_ownership",
            "timestamps",
            "drain",
            "cleanup",
        },
        "return-to-zero receipt",
    )
    if (
        value["status"] != "PASS"
        or value["model_id"] != record.model_id
        or value["model_digest"] != record.digest
        or value["scale_contract_digest"] != scale_contract.digest
        or value["runtime_tuple_digest"] != runtime_digest
    ):
        raise CatalogError("return-to-zero receipt differs from the route subject")
    controller = _scale_controller_subject(value["controller"], "return-to-zero controller")
    intent, claim_lease_expires = _scale_intent_subject(
        value["intent"],
        "return-to-zero intent",
        record,
        activation,
        controller,
        action="deactivate",
    )
    if intent["intent_id"] == zero_to_ready["intent"]["intent_id"]:
        raise CatalogError("scale-up and scale-down must use distinct durable intents")
    zero_intent = zero_to_ready["intent"]
    if (
        intent["previous_fencing_token"] != zero_intent["fencing_token"]
        or intent["fencing_token"] != zero_intent["fencing_token"] + 1
    ):
        raise CatalogError("return-to-zero fencing token did not advance monotonically per model")
    target = _scale_target_subject(value["target"], scale_contract, "return-to-zero target")
    zero_target = zero_to_ready["target"]
    if (
        controller != activation["controller_receipt_subject"]
        or target != activation["target_receipt_subject"]
        or _stable_scale_target(target) != _stable_scale_target(zero_target)
    ):
        raise CatalogError("return-to-zero receipt substituted controller or target identity")
    if (
        target["resource_version"] == zero_target["resource_version"]
        or target["observed_generation"] <= zero_target["observed_generation"]
    ):
        raise CatalogError("return-to-zero target version did not advance after mutation")
    replica_ownership = _validate_replica_ownership(
        value["replica_ownership"],
        target=target,
        label="return-to-zero replica ownership",
    )
    policy = scale_contract.to_dict()["policy"]
    replicas = _exact(
        value["replicas"], {"previous", "desired", "observed"}, "return-to-zero replicas"
    )
    if replicas != {
        "previous": policy["desired_max"],
        "desired": policy["desired_floor"],
        "observed": policy["desired_floor"],
    }:
        raise CatalogError("return-to-zero receipt does not prove the exact one-to-zero transition")
    timestamps = _exact(
        value["timestamps"],
        {
            "last_activity_at",
            "cooldown_elapsed_at",
            "drain_started_at",
            "mutation_at",
            "zero_observed_at",
            "duration_seconds",
        },
        "return-to-zero timestamps",
    )
    last_activity = _utc(timestamps["last_activity_at"], "return-to-zero last activity")
    cooldown = _utc(timestamps["cooldown_elapsed_at"], "return-to-zero cooldown")
    drain_started = _utc(timestamps["drain_started_at"], "return-to-zero drain start")
    mutated = _utc(timestamps["mutation_at"], "return-to-zero mutation time")
    zero = _utc(timestamps["zero_observed_at"], "return-to-zero observation time")
    observed = _utc(value["observed_at"], "return-to-zero observed_at")
    if (
        not ready_at <= last_activity <= cooldown <= drain_started <= mutated <= zero < claim_lease_expires
        or observed != zero
    ):
        raise CatalogError("return-to-zero lifecycle timestamps are not ordered")
    if (cooldown - last_activity).total_seconds() < policy["cooldown_seconds"]:
        raise CatalogError("return-to-zero did not observe the immutable cooldown")
    duration = timestamps["duration_seconds"]
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or not math.isclose(
            float(duration),
            (zero - drain_started).total_seconds(),
            rel_tol=0.0,
            abs_tol=MAX_DURATION_ERROR_SECONDS,
        )
    ):
        raise CatalogError("return-to-zero duration differs from its ordered timestamps")
    if observed > store.attestation_issued_at("return-to-zero", digest) + MAX_EVENT_CLOCK_SKEW:
        raise CatalogError("return-to-zero receipt was signed before cleanup completed")
    drain = _exact(
        value["drain"],
        {
            "new_admissions_stopped",
            "active_assignments_before",
            "active_assignments_after",
            "preemption_notice_sha256",
            "interrupted_attempt_ids",
        },
        "return-to-zero drain",
    )
    before = drain["active_assignments_before"]
    if isinstance(before, bool) or not isinstance(before, int) or before < 0:
        raise CatalogError("return-to-zero active assignment count is invalid")
    interrupted = _list(drain["interrupted_attempt_ids"], "return-to-zero interruptions")
    if interrupted != sorted(set(interrupted)):
        raise CatalogError("return-to-zero interrupted attempt IDs are not canonical")
    notice = drain["preemption_notice_sha256"]
    if notice is not None:
        strong_sha256(notice, "return-to-zero preemption notice")
    if (
        drain["new_admissions_stopped"] is not True
        or drain["active_assignments_after"] != 0
        or (notice is None and interrupted)
    ):
        raise CatalogError("return-to-zero drain is incomplete or contradictory")
    cleanup = _exact(
        value["cleanup"],
        {
            "expected_resource_uids",
            "resources",
            "foreign_uids_touched",
            "gpu_clients_after",
            "temporary_paths_absent",
            "retained_artifact_digests",
        },
        "return-to-zero cleanup",
    )
    expected_resources = _list(
        cleanup["expected_resource_uids"], "return-to-zero expected resources", nonempty=True
    )
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(expected_resources):
        resource = _exact(
            raw,
            {"api_version", "kind", "namespace", "name", "uid"},
            f"return-to-zero expected resource[{index}]",
        )
        uid = _text(resource["uid"], "return-to-zero expected resource UID")
        if (
            resource["namespace"] != "fs2-models"
            or uid is None
            or K8S_UID.fullmatch(uid) is None
        ):
            raise CatalogError("return-to-zero expected resource is not UID-fenced")
        normalized.append(dict(resource))
    def sort_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            item["api_version"],
            item["kind"],
            item["namespace"],
            item["name"],
            item["uid"],
        )

    if normalized != sorted(normalized, key=sort_key) or len({item["uid"] for item in normalized}) != len(normalized):
        raise CatalogError("return-to-zero expected UID set is not canonical")
    if set(item["kind"] for item in normalized) != set(
        policy["cleanup"]["expected_resource_kinds"]
    ):
        raise CatalogError("return-to-zero UID set differs from the immutable cleanup policy")
    target_identity = {
        key: target[key] for key in ("api_version", "kind", "namespace", "name", "uid")
    }
    if target_identity not in normalized:
        raise CatalogError("return-to-zero cleanup omits the exact scale target UID")
    resources = _list(cleanup["resources"], "return-to-zero cleanup resources", nonempty=True)
    observed_resources: list[dict[str, Any]] = []
    for index, raw in enumerate(resources):
        resource = _exact(
            raw,
            {"api_version", "kind", "namespace", "name", "uid", "precondition_uid", "final_state"},
            f"return-to-zero cleanup resource[{index}]",
        )
        if resource["uid"] != resource["precondition_uid"]:
            raise CatalogError("return-to-zero cleanup lacks an exact UID precondition")
        identity = {
            key: resource[key]
            for key in ("api_version", "kind", "namespace", "name", "uid")
        }
        expected_state = "retained-scaled-zero" if identity == target_identity else "absent"
        if resource["final_state"] != expected_state:
            raise CatalogError("return-to-zero cleanup resource final state is incorrect")
        observed_resources.append(identity)
    if observed_resources != normalized:
        raise CatalogError("return-to-zero cleanup result differs from the expected UID set")
    retained = _list(
        cleanup["retained_artifact_digests"],
        "return-to-zero retained artifact digests",
        nonempty=True,
    )
    if retained != sorted(set(retained)) or artifact_manifest_digest not in retained:
        raise CatalogError("return-to-zero did not preserve the bound artifact")
    for retained_digest in retained:
        strong_sha256(retained_digest, "return-to-zero retained artifact digest")
    if (
        cleanup["foreign_uids_touched"] is not False
        or cleanup["gpu_clients_after"] != 0
        or cleanup["temporary_paths_absent"] is not True
    ):
        raise CatalogError("return-to-zero cleanup did not prove fenced reclamation")
    store.assert_claims(
        "return-to-zero",
        digest,
        {
            "model_digest": record.digest,
            "scale_contract_digest": scale_contract.digest,
            "runtime_tuple_digest": runtime_digest,
            "activation_intent_sha256": hashlib.sha256(
                canonical_bytes(intent)
            ).hexdigest(),
            "operation_id": intent["operation_id"],
            "operation_attempt": intent["operation_attempt"],
            "fence_operation_id": intent["fence_operation_id"],
            "intent_model_id": intent["model_id"],
            "binding_digest": intent["binding_digest"],
            "controller_id": intent["controller_id"],
            "previous_fencing_token": intent["previous_fencing_token"],
            "fencing_token": intent["fencing_token"],
            "database_now": intent["database_now"],
            "claim_started_at": intent["claim_started_at"],
            "intent_subject_sha256": intent["subject_sha256"],
            "activation_store_sha256": intent["store_contract_sha256"],
            "submitter_service_account_uid": intent[
                "submitter_service_account_uid"
            ],
            "claim_owner_service_account_uid": intent[
                "claim_owner_service_account_uid"
            ],
            "leader_lease_uid": intent["leader_lease_uid"],
            "leader_lease_resource_version": intent[
                "leader_lease_resource_version"
            ],
            "leader_lease_holder_identity": intent[
                "leader_lease_holder_identity"
            ],
            "claim_lease_expires_at": intent["claim_lease_expires_at"],
            "controller_identity_sha256": controller["identity_sha256"],
            "target_identity_sha256": hashlib.sha256(canonical_bytes(target)).hexdigest(),
            "replica_transition_sha256": hashlib.sha256(canonical_bytes(replicas)).hexdigest(),
            "replica_ownership_sha256": hashlib.sha256(
                canonical_bytes(replica_ownership)
            ).hexdigest(),
            "lifecycle_timestamps_sha256": hashlib.sha256(canonical_bytes(timestamps)).hexdigest(),
            "drain_sha256": hashlib.sha256(canonical_bytes(drain)).hexdigest(),
            "expected_resource_uid_set_sha256": hashlib.sha256(canonical_bytes(normalized)).hexdigest(),
            "resource_result_set_sha256": hashlib.sha256(canonical_bytes(resources)).hexdigest(),
            "retained_artifact_set_sha256": hashlib.sha256(canonical_bytes(retained)).hexdigest(),
        },
    )


def _validate_scale_lifecycle(
    store: EvidenceStore,
    record: ModelRecord,
    scale_contract: ScaleContract,
    activation: Mapping[str, Any],
    runtime_digest: str,
    artifact_manifest_digest: str,
    content_uri: str,
) -> None:
    if scale_contract.activation_mode != "replica-scale":
        raise CatalogError("local route activation requires a replica-scale contract")
    zero_digest = activation["zero_to_ready_receipt_digest"]
    return_digest = activation["return_to_zero_receipt_digest"]
    zero_subject, ready_at = _validate_zero_to_ready(
        store,
        zero_digest,
        record,
        scale_contract,
        runtime_digest,
        activation,
        content_uri,
        artifact_manifest_digest,
    )
    _validate_return_to_zero(
        store,
        return_digest,
        record,
        scale_contract,
        runtime_digest,
        activation,
        artifact_manifest_digest=artifact_manifest_digest,
        zero_to_ready=zero_subject,
        ready_at=ready_at,
    )


def validate_route_evidence(
    catalog: Catalog,
    record: ModelRecord,
    plan: AcquisitionPlan,
    qualification: dict[str, Any],
    evidence_root: Path | str,
    *,
    backend_identity: Mapping[str, Any],
    gateway_identity: Mapping[str, Any],
    evidence_session_id: str,
    trusted_attestors: Mapping[str, str],
    activation: Mapping[str, Any],
    validation_time: datetime | None = None,
) -> str:
    """Verify every live promotion receipt and all cross-receipt identities."""

    store = EvidenceStore(
        evidence_root,
        session_id=evidence_session_id,
        trusted_attestors=trusted_attestors,
        validation_time=validation_time,
    )
    backend_subject = _validate_backend_identity(
        store,
        qualification["backend_evidence_digest"],
        record,
        backend_identity,
    )
    manifest_digest = qualification["artifact_manifest_digest"]
    manifest = store.artifact(manifest_digest, record.model_id)
    record_value = record.to_dict()
    request_contract = catalog.semantic_request_contract(record.model_id)
    if request_contract.state != "qualified":
        raise CatalogError("live route lacks a qualified canonical semantic request contract")
    if manifest.model_id != record.model_id:
        raise CatalogError("live artifact manifest belongs to another model")
    if manifest.license_id != record_value["model"]["source"]["license"]["id"] or manifest.license_state != "verified":
        raise CatalogError("live artifact does not carry the verified model license")
    expected_entitlement = record_value["model"]["source"]["entitlement"]["state"]
    if manifest.entitlement_state != expected_entitlement:
        raise CatalogError("live artifact entitlement differs from the catalog")
    if manifest.source_revision != record_value["model"]["source"]["revision"]:
        raise CatalogError("live artifact revision differs from the immutable model revision")
    artifact_contract = record_value["cache"]["artifact"]
    if artifact_contract.get("qualification_gate") == (
        "fs2-serve/exact-hf-weight-per-file-sha256-manifest/v1"
    ):
        historical = artifact_contract["historical_inventory"]
        expected_identity = artifact_contract["expected_identity"]
        if (
            manifest.kind != "weights"
            or any(not item.sha256 for item in manifest.files)
            or manifest.digest != expected_identity["manifest_digest"]
            or manifest.content_digest != expected_identity["content_digest"]
            or manifest.expanded_bytes != expected_identity["expanded_bytes"]
            or len(manifest.files) != expected_identity["file_count"]
            or manifest.digest
            in {
                historical["identity_sha256"],
                "76b8845141df43882a142f9085ff233a0b5bf27b55f19ae385dd9ac88dab6394",
            }
        ):
            raise CatalogError(
                "Qwen conventional activation requires the exact full per-file SHA-256 weight manifest"
            )
    storage_mode = qualification["storage_mode"]
    if storage_mode not in {
        "provider-block-pvc",
        "sfs-pvc",
        "local-nvme",
        "nimcache-pvc",
    }:
        raise CatalogError("local route lacks an explicit supported storage mode")
    if (storage_mode == "nimcache-pvc") != (
        plan.method == "ngc-target-node-nimcache"
    ):
        raise CatalogError("route storage mode differs from the acquisition/runtime kind")
    placement_scheme = (
        "nvme"
        if storage_mode == "local-nvme"
        else "pvc"
        if storage_mode == "provider-block-pvc"
        else "sfs"
    )
    content_uri = canonical_content_uri(
        qualification["artifact_uri"],
        model_id=record.model_id,
        content_digest=manifest.content_digest,
        scheme=placement_scheme,
    )
    store.assert_claims(
        "artifacts",
        manifest.digest,
        {
            "artifact_manifest_digest": manifest.digest,
            "artifact_content_digest": manifest.content_digest,
            "artifact_kind": manifest.kind,
            "model_revision": manifest.source_revision,
        },
    )
    acquisition_digest = qualification["acquisition_receipt_digest"]
    acquisition = _validate_acquisition(
        store, acquisition_digest, record, manifest, plan
    )
    prerequisite_digest = qualification["prerequisite_receipt_digest"]
    prerequisite_subject = _validate_prerequisites(
        store, prerequisite_digest, record, catalog, plan
    )
    claimed_placement_digest = qualification["placement_receipt_digest"]
    if storage_mode == "provider-block-pvc":
        if claimed_placement_digest is None:
            raise CatalogError("provider block route lacks a PVC lifecycle receipt")
        placement_digest = claimed_placement_digest
        placement = acquisition
        if acquisition["content_uri"] != content_uri:
            raise CatalogError("provider block lifecycle differs from acquired content")
    elif storage_mode == "nimcache-pvc":
        if claimed_placement_digest is None:
            raise CatalogError("NIM route lacks a NIMCache placement receipt")
        placement_digest = claimed_placement_digest
        placement = _validate_nim_cache_readiness(
            store,
            placement_digest,
            record,
            manifest,
            content_uri,
            prerequisite_subject=prerequisite_subject,
            prerequisite_digest=prerequisite_digest,
        )
    elif storage_mode == "local-nvme":
        if claimed_placement_digest is None:
            raise CatalogError("node-local route lacks a localizer placement receipt")
        placement_digest = claimed_placement_digest
        placement = _validate_staging(
            store, placement_digest, record, manifest, content_uri
        )
    else:
        if claimed_placement_digest is not None:
            raise CatalogError("SFS route cannot substitute a node-local placement receipt")
        if acquisition["content_uri"] != content_uri:
            raise CatalogError("SFS route differs from the acquired content address")
        placement_digest = acquisition_digest
        placement = acquisition
    runtime_digest = qualification["runtime_tuple_digest"]
    # The provider lifecycle receipt closes only after the runtime semantic and
    # return-to-zero observations, so making the runtime tuple name that receipt
    # would create a circular signed-subject graph.  The tuple instead binds the
    # immutable acquisition/PVC content receipt; the separately reopened
    # lifecycle receipt joins that tuple's semantic and scale subjects below.
    runtime_placement_digest = (
        acquisition_digest
        if storage_mode == "provider-block-pvc"
        else placement_digest
    )
    runtime = _validate_runtime_tuple(
        store,
        runtime_digest,
        record,
        manifest,
        runtime_placement_digest,
        content_uri,
        acquisition if storage_mode == "provider-block-pvc" else placement,
    )
    if storage_mode == "provider-block-pvc":
        placement = _validate_provider_block_pvc(
            store,
            placement_digest,
            record,
            manifest,
            content_uri,
            acquisition_receipt_digest=acquisition_digest,
            semantic_receipt_digest=qualification["semantic_evidence_digest"],
            return_to_zero_receipt_digest=activation[
                "return_to_zero_receipt_digest"
            ],
            runtime_tuple_digest=runtime_digest,
            runtime_tuple=runtime,
        )
    mechanism = runtime["runtime"]["startup_mechanism"]
    if mechanism == "conventional":
        expected_manifest = record_value["cache"]["artifact"]["manifest_digest"]
        expected_kind = record_value["cache"]["artifact"]["kind"]
    else:
        experiment = next(
            (
                item
                for item in record_value["startup"]["experiments"]
                if item["mechanism"] == mechanism and item["state"] == "qualified"
            ),
            None,
        )
        if experiment is None:
            raise CatalogError("live acceleration lacks a qualified static experiment")
        expected_manifest = experiment["artifact_manifest_digest"]
        expected_kind = experiment["artifact_kind"]
    if manifest.digest != expected_manifest or manifest.kind != expected_kind:
        raise CatalogError("live artifact manifest subject differs from the selected startup mechanism")
    qualification_placement_digest = (
        acquisition_digest
        if storage_mode == "provider-block-pvc"
        else placement_digest
    )
    _validate_qualification(
        store,
        qualification["prepared_qualification_digest"],
        record,
        request_contract,
        "prepared-node",
        runtime_digest,
        qualification_placement_digest,
        mechanism,
        runtime,
    )
    _validate_qualification(
        store,
        qualification["new_node_qualification_digest"],
        record,
        request_contract,
        "new-node",
        runtime_digest,
        qualification_placement_digest,
        mechanism,
        runtime,
    )
    readiness_subject = _validate_readiness(
        store,
        qualification["readiness_evidence_digest"],
        record,
        runtime_digest,
        backend_identity,
    )
    if readiness_subject["service_uid"] != backend_subject["service_uid"]:
        raise CatalogError("readiness and backend evidence identify different Services")
    readiness_digest = qualification["readiness_evidence_digest"]
    readiness_identity = _readiness_path_identity(
        record,
        evidence_kind="signed-readiness-receipt",
        evidence_digest=readiness_digest,
        service_uid=readiness_subject["service_uid"],
        observed_generation=readiness_subject["observed_generation"],
        observation=readiness_subject,
    )
    gateway_path = _gateway_path(
        record,
        request_contract,
        gateway_identity,
        backend_subject,
        readiness_identity,
    )
    semantic_digest = qualification["semantic_evidence_digest"]
    _validate_semantic(
        store,
        semantic_digest,
        record,
        request_contract,
        runtime_digest,
        "gateway-smoke",
        gateway_path=gateway_path,
    )
    canary_digest = qualification["target_node_canary_digest"]
    if plan.method == "ngc-target-node-nimcache":
        if canary_digest is None:
            raise CatalogError("NGC promotion requires a target-node pull/runtime canary")
        _validate_target_node_canary(
            store,
            canary_digest,
            record,
            plan,
            runtime,
            acquisition_digest,
            prerequisite_digest,
            semantic_digest,
            qualification["readiness_evidence_digest"],
            placement_digest,
            hashlib.sha256(
                canonical_bytes(
                    prerequisite_subject["observation"][
                        "ngc_credential_materialization"
                    ]
                )
            ).hexdigest(),
        )
    elif canary_digest is not None:
        raise CatalogError("public artifact route cannot claim an NGC target-node canary")
    _validate_scale_lifecycle(
        store,
        record,
        catalog.scale_contract(record.model_id),
        activation,
        runtime_digest,
        manifest.digest,
        content_uri,
    )
    return store.valid_until()
