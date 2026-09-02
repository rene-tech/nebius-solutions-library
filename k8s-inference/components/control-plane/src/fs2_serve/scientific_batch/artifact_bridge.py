"""Projection bridge to the artifact-service-owned repository and models."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Protocol, cast
from uuid import UUID

from ..scientific_artifacts import ArtifactNotFoundError, ArtifactRepository, TerminalResultManifest
from ..store import ConflictError, Store
from .models import AttemptOutcome, BatchEvent, BatchEventKind, BatchStatus, ScientificBatchState
from .profile_catalog import ScientificProfileCatalog, ScientificProfileError

_ERROR = re.compile(r"[^A-Z0-9_]+")


def _raw_digest(value: str | None) -> str | None:
    return None if value is None else value.removeprefix("sha256:")


def _public_artifact(record: Any) -> dict[str, Any]:
    return cast(dict[str, Any], record.to_public_ref().model_dump(mode="json", exclude_none=True))


class ScientificBatchResultRepository(Protocol):
    async def get(self, operation_id: UUID, *, tenant_id: str) -> ScientificBatchState: ...

    async def list_events(
        self, operation_id: UUID, *, tenant_id: str, after_sequence: int = 0, limit: int = 1000
    ) -> list[BatchEvent]: ...


class ArtifactServiceBridge:
    """Authorize artifact reads and assemble the canonical public run result."""

    def __init__(
        self,
        *,
        artifacts: ArtifactRepository,
        batches: ScientificBatchResultRepository,
        profiles: ScientificProfileCatalog,
        store: Store,
    ) -> None:
        self.artifacts = artifacts
        self.batches = batches
        self.profiles = profiles
        self.store = store

    async def validate_input(self, pointer: Mapping[str, Any], *, tenant_id: str) -> None:
        try:
            artifact_id = UUID(str(pointer["artifact_id"]))
        except (KeyError, ValueError):
            raise ArtifactNotFoundError("input artifact does not exist") from None
        artifact = await self.artifacts.get_artifact(artifact_id, tenant_id=tenant_id)
        actual = _public_artifact(artifact)
        expected = dict(pointer)
        if expected.get("compression") == "none":
            expected.pop("compression")
        if actual != expected:
            raise ArtifactNotFoundError("input artifact metadata does not match")

    async def artifact_response(self, artifact_id: UUID, *, tenant_id: str) -> Mapping[str, Any]:
        artifact = await self.artifacts.get_artifact(artifact_id, tenant_id=tenant_id)
        # This endpoint deliberately returns the canonical pointer only. Signed
        # locations remain an optional access-time concern of the artifact
        # service and are never persisted in controller state.
        return _public_artifact(artifact)

    @staticmethod
    def _manifest_artifact(manifest: TerminalResultManifest, *, output: bool) -> Any | None:
        artifacts = list(manifest.output_artifacts if output else manifest.input_artifacts)
        if output and manifest.validation.evidence_artifact is not None:
            artifacts = [
                item for item in artifacts if item.artifact_id != manifest.validation.evidence_artifact.artifact_id
            ]
        candidates = [item for item in artifacts if item.media_type == "application/vnd.fs2.scientific-manifest+json"]
        if len(candidates) != 1:
            return None
        return candidates[0]

    async def result_response(self, operation_id: UUID, *, tenant_id: str) -> Mapping[str, Any]:
        operation = await self.store.get_operation(operation_id, tenant_id=tenant_id)
        state = await self.batches.get(operation_id, tenant_id=tenant_id)
        if not state.status.terminal:
            raise ConflictError("scientific batch is not terminal")
        manifest = await self.artifacts.get_terminal_result(operation_id, tenant_id=tenant_id)
        if manifest.status.value != state.status.value:
            raise ScientificProfileError("artifact result and controller terminal status differ")
        profile = self.profiles.get(state.model_id, runnable=False)
        identity = profile.value["execution_identity"]
        access = profile.value["access"]
        semantic = profile.value["semantic_validation"]
        if not isinstance(identity, Mapping) or not isinstance(access, Mapping) or not isinstance(semantic, Mapping):
            raise ScientificProfileError("scientific profile result identity is invalid")

        events = await self.batches.list_events(operation_id, tenant_id=tenant_id, limit=1000)
        attempts: list[dict[str, Any]] = []
        for stage in state.stages:
            for attempt in stage.attempts:
                related = [
                    event
                    for event in events
                    if event.draft.kind is BatchEventKind.LIFECYCLE and event.draft.attempt_id == attempt.attempt_id
                ]
                if not related:
                    raise ScientificProfileError("scientific attempt lifecycle evidence is absent")
                status = {
                    AttemptOutcome.ACTIVE: "cancelled",
                    AttemptOutcome.SUCCEEDED: "succeeded",
                    AttemptOutcome.FAILED: "failed",
                    AttemptOutcome.PREEMPTED: "preempted",
                    AttemptOutcome.CANCELLED: "cancelled",
                }[attempt.outcome]
                attempts.append(
                    {
                        "attempt_id": str(attempt.attempt_id),
                        "stage_id": attempt.stage_id,
                        "status": status,
                        "started_at": related[0].occurred_at.isoformat(),
                        "completed_at": related[-1].occurred_at.isoformat(),
                        "kueue_workload_uid": None,
                        "k8s_job_uid": attempt.workload.uid,
                        "pod_uids": [],
                        "node_uids": [],
                        "gpu_uuids": [],
                        "checkpoint_input": None,
                        "checkpoint_output": None,
                    }
                )
        if len(attempts) > self.profiles.max_result_attempts:
            raise ScientificProfileError("public result attempt bound is exceeded")

        scheduling = state.scheduling.stages[-1]
        input_manifest = self._manifest_artifact(manifest, output=False)
        output_manifest = self._manifest_artifact(manifest, output=True)
        if input_manifest is None:
            raise ScientificProfileError("terminal result has no unique public input manifest")
        if state.status is BatchStatus.SUCCEEDED and output_manifest is None:
            raise ScientificProfileError("successful result has no unique public output manifest")
        evidence = manifest.validation.evidence_artifact
        validation_status = manifest.validation.status.value.replace("_", "-")
        code = state.failure_code or "SCIENTIFIC_RUN_FAILED"
        public_error = None
        if state.status is not BatchStatus.SUCCEEDED:
            normalized = _ERROR.sub("_", code.upper()).strip("_") or "SCIENTIFIC_RUN_FAILED"
            public_error = {"code": normalized[:64], "message": "scientific run did not succeed", "retryable": False}
        result = {
            "schema": "fs2-serve.nebius.ai/scientific-run-result/v1",
            "operation_id": str(operation_id),
            "batch_id": str(state.batch_id),
            "workload_id": str(state.workload_id),
            "terminal_status": state.status.value,
            "submitted_at": operation.accepted_at.isoformat(),
            "completed_at": manifest.completed_at.isoformat(),
            "execution_identity": {
                "model_id": state.model_id,
                "model_revision": identity["model_revision"],
                "runtime_image_digest": identity["runtime_image_digest"],
                "runtime_recipe_sha256": _raw_digest(identity["runtime_recipe_sha256"]),
                "workload_recipe_sha256": _raw_digest(identity["workload_recipe_sha256"]),
                "model_artifact_manifest_digest": _raw_digest(identity["artifact_manifest_digest"]),
                "execution_identity_sha256": _raw_digest(identity["execution_identity_sha256"]),
            },
            "access_admission": {
                "profile": access["profile"],
                "state": access["state"],
                "receipt_digest": _raw_digest(access["receipt_digest"]),
            },
            "scheduling_snapshot": {
                "policy_revision": state.scheduling.policy_revision,
                "service_class": state.scheduling.service_class.value,
                "tenant_queue": state.scheduling.tenant_queue,
                "model_lane": state.scheduling.model_lane,
                "resolved_cluster_queue": scheduling.resolved_cluster_queue,
                "resolved_local_queue": scheduling.resolved_local_queue,
                "workload_priority_class": scheduling.workload_priority_class,
                "workload_priority_value": scheduling.workload_priority_value,
                "resolved_pool_preference": list(scheduling.resolved_pool_preference),
                "admitted_resource_flavor": scheduling.admitted_resource_flavor,
                "accelerator_resource_name": scheduling.accelerator_resource_name,
                "accelerator_count": scheduling.accelerator_count,
                "max_queue_seconds": scheduling.max_queue_seconds,
                "max_execution_seconds": scheduling.max_execution_seconds,
                "checkpoint_mode": scheduling.checkpoint_mode.value,
                "preemption_mode": scheduling.preemption_mode.value,
            },
            "input_manifest": _public_artifact(input_manifest),
            "output_manifest": None if output_manifest is None else _public_artifact(output_manifest),
            "attempts": attempts,
            "semantic_validation": {
                "validator_id": semantic["validator_id"],
                "status": validation_status,
                "receipt_digest": None if evidence is None else _raw_digest(evidence.digest),
            },
            "error": public_error,
        }
        self.profiles.validate_result(result)
        return result
