"""Projection bridge to the artifact-service-owned repository and models."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any, Protocol, cast
from uuid import UUID

import httpx

from ..scientific_artifacts import (
    ArtifactDirection,
    ArtifactNotFoundError,
    ArtifactRecord,
    ArtifactRepository,
    ExecutionProvenance,
    ScientificArtifactControllerPort,
    SemanticValidation,
    SemanticValidationStatus,
    TerminalResultDraft,
    TerminalResultManifest,
    TerminalResultStatus,
)
from ..store import ConflictError, Store
from .models import (
    ArtifactAccessContext,
    ArtifactCommit,
    AttemptOutcome,
    BatchEvent,
    BatchEventKind,
    BatchStatus,
    ScientificBatchState,
    ScientificInputAdmission,
    ScientificInputArtifact,
    VerifiedInputManifest,
)
from .profile_catalog import ScientificProfileCatalog, ScientificProfileError

_ERROR = re.compile(r"[^A-Z0-9_]+")
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024


def _raw_digest(value: str | None) -> str | None:
    return None if value is None else value.removeprefix("sha256:")


def _public_artifact(record: Any) -> dict[str, Any]:
    return cast(dict[str, Any], record.to_public_ref().model_dump(mode="json", exclude_none=True))


class ScientificBatchResultRepository(Protocol):
    async def get(self, operation_id: UUID, *, tenant_id: str) -> ScientificBatchState: ...

    async def list_events(
        self, operation_id: UUID, *, tenant_id: str, after_sequence: int = 0, limit: int = 1000
    ) -> list[BatchEvent]: ...

    async def list_artifact_commits(self, operation_id: UUID, *, tenant_id: str) -> tuple[ArtifactCommit, ...]: ...


class ArtifactContentReader(Protocol):
    async def read(self, artifact_id: UUID, *, tenant_id: str, maximum_bytes: int) -> bytes: ...


class SignedArtifactContentReader:
    """Bounded internal reader over artifact-service-issued ephemeral handles."""

    def __init__(self, service: ScientificArtifactControllerPort, client: httpx.AsyncClient | None = None) -> None:
        self.service = service
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=60, follow_redirects=False)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def read(self, artifact_id: UUID, *, tenant_id: str, maximum_bytes: int) -> bytes:
        download = await self.service.download(artifact_id, tenant_id=tenant_id)
        if download.artifact.size_bytes > maximum_bytes:
            raise ArtifactNotFoundError("input artifact exceeds the controller manifest bound")
        content = bytearray()
        async with self.client.stream(
            "GET",
            download.handle.url,
            headers=dict(download.handle.headers),
            follow_redirects=False,
        ) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > maximum_bytes:
                    raise ArtifactNotFoundError("input artifact exceeds the controller manifest bound")
        payload = bytes(content)
        if len(payload) != download.artifact.size_bytes:
            raise ArtifactNotFoundError("input artifact size differs from verified metadata")
        return payload


class ArtifactServiceBridge:
    """Authorize artifact reads and assemble the canonical public run result."""

    def __init__(
        self,
        *,
        artifacts: ArtifactRepository,
        batches: ScientificBatchResultRepository,
        profiles: ScientificProfileCatalog,
        store: Store,
        content_reader: ArtifactContentReader | None = None,
        service: ScientificArtifactControllerPort | None = None,
    ) -> None:
        self.artifacts = artifacts
        self.batches = batches
        self.profiles = profiles
        self.store = store
        self.content_reader = content_reader
        self.service = service

    async def validate_input(self, pointer: Mapping[str, Any], *, tenant_id: str) -> ScientificInputAdmission:
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
        if artifact.media_type != "application/vnd.fs2.scientific-manifest+json":
            raise ArtifactNotFoundError("input_manifest must point to a scientific artifact manifest")
        if self.content_reader is None:
            raise ScientificProfileError("scientific artifact manifest reader is unavailable")
        try:
            payload = await self.content_reader.read(
                artifact_id,
                tenant_id=tenant_id,
                maximum_bytes=_MAX_MANIFEST_BYTES,
            )
            if len(payload) != artifact.size_bytes or hashlib.sha256(
                payload
            ).hexdigest() != artifact.digest.removeprefix("sha256:"):
                raise ArtifactNotFoundError("input manifest bytes differ from verified metadata")
            manifest = self.profiles.validate_artifact_manifest(json.loads(payload))
        except (UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise ArtifactNotFoundError("input manifest bytes are invalid") from error
        entries: list[ScientificInputArtifact] = []
        for raw_entry in cast(list[Mapping[str, Any]], manifest["entries"]):
            ref = cast(Mapping[str, Any], raw_entry["artifact"])
            try:
                entry_id = UUID(str(ref["artifact_id"]))
            except ValueError:
                raise ArtifactNotFoundError("input manifest entry artifact ID is not canonical") from None
            entry = await self.artifacts.get_artifact(entry_id, tenant_id=tenant_id)
            expected_ref = dict(ref)
            if expected_ref.get("compression") == "none":
                expected_ref.pop("compression")
            if _public_artifact(entry) != expected_ref or entry.access != artifact.access:
                raise ArtifactNotFoundError("input manifest entry metadata or access admission differs")
            entries.append(
                ScientificInputArtifact(
                    logical_artifact_id=str(raw_entry["name"]),
                    semantic_type=str(raw_entry["semantic_type"]),
                    artifact_id=entry.artifact_id,
                    digest=entry.digest,
                    size_bytes=entry.size_bytes,
                    media_type=entry.media_type,
                    compression=None if entry.compression is None else entry.compression.value,
                )
            )
        access = ArtifactAccessContext(
            profile=artifact.access.profile.value,
            receipt_digest=artifact.access.receipt_digest,
            tenant_id=tenant_id,
        )
        return ScientificInputAdmission(
            manifest=VerifiedInputManifest(
                manifest_id=str(manifest["manifest_id"]),
                manifest_artifact_id=artifact.artifact_id,
                manifest_digest=artifact.digest,
                entries=tuple(entries),
            ),
            access_context=access,
        )

    async def artifact_response(self, artifact_id: UUID, *, tenant_id: str) -> Mapping[str, Any]:
        artifact = await self.artifacts.get_artifact(artifact_id, tenant_id=tenant_id)
        # This endpoint deliberately returns the canonical pointer only. Signed
        # locations remain an optional access-time concern of the artifact
        # service and are never persisted in controller state.
        return _public_artifact(artifact)

    @staticmethod
    def _profile_digest(value: object, label: str) -> str:
        if not isinstance(value, str):
            raise ScientificProfileError(f"scientific profile {label} is invalid")
        digest = value if value.startswith("sha256:") else f"sha256:{value}"
        if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
            raise ScientificProfileError(f"scientific profile {label} is invalid")
        return digest

    async def publish_terminal(self, state: ScientificBatchState) -> None:
        """Idempotently publish the artifact-service-owned terminal record."""

        if not state.status.terminal or state.result_published:
            raise ScientificProfileError("scientific batch is not awaiting terminal publication")
        if self.service is None:
            raise ScientificProfileError("scientific artifact result publisher is unavailable")
        operation = await self.store.get_operation(state.operation_id, tenant_id=state.tenant_id)
        profile = self.profiles.get(state.model_id, runnable=False)
        identity = profile.value.get("execution_identity")
        semantic = profile.value.get("semantic_validation")
        if not isinstance(identity, Mapping) or not isinstance(semantic, Mapping):
            raise ScientificProfileError("scientific profile terminal identity is invalid")
        events = await self.batches.list_events(state.operation_id, tenant_id=state.tenant_id, limit=1000)
        terminal_kind = {
            BatchStatus.SUCCEEDED: BatchEventKind.BATCH_SUCCEEDED,
            BatchStatus.FAILED: BatchEventKind.BATCH_FAILED,
            BatchStatus.CANCELLED: BatchEventKind.BATCH_CANCELLED,
        }[state.status]
        terminal_events = [event for event in events if event.draft.kind is terminal_kind]
        if len(terminal_events) != 1:
            raise ScientificProfileError("scientific batch terminal event is absent or ambiguous")
        completed_at = terminal_events[0].occurred_at
        lifecycle = [event for event in events if event.draft.kind is BatchEventKind.LIFECYCLE]
        started_at = lifecycle[0].occurred_at if lifecycle else operation.accepted_at

        output_artifacts: tuple[ArtifactRecord, ...] = ()
        evidence: ArtifactRecord | None = None
        validator_id = semantic.get("validator_id")
        if not isinstance(validator_id, str):
            raise ScientificProfileError("scientific profile validator identity is invalid")
        validation_status = (
            SemanticValidationStatus.PASSED
            if state.status is BatchStatus.SUCCEEDED
            else SemanticValidationStatus.FAILED
            if state.status is BatchStatus.FAILED
            else SemanticValidationStatus.NOT_RUN
        )
        if state.status is BatchStatus.SUCCEEDED:
            dependent = {dependency for stage in state.plan.stages for dependency in stage.depends_on}
            sinks = {stage.stage_id for stage in state.plan.stages if stage.stage_id not in dependent}
            commits = await self.batches.list_artifact_commits(state.operation_id, tenant_id=state.tenant_id)
            sink_commits = [commit for commit in commits if commit.stage_id in sinks and commit.semantic_valid]
            if len(sinks) != 1 or len(sink_commits) != 1:
                raise ScientificProfileError("successful scientific batch has no unique terminal artifact commit")
            commit = sink_commits[0]
            output_manifest = await self.artifacts.get_artifact(
                commit.manifest_artifact_id,
                tenant_id=state.tenant_id,
            )
            evidence = await self.artifacts.get_artifact(
                commit.validation_artifact_id,
                tenant_id=state.tenant_id,
            )
            if (
                output_manifest.operation_id != state.operation_id
                or output_manifest.attempt != operation.attempt
                or output_manifest.direction is not ArtifactDirection.OUTPUT
                or output_manifest.media_type != "application/vnd.fs2.scientific-manifest+json"
                or output_manifest.digest != commit.manifest_digest
                or evidence.operation_id != state.operation_id
                or evidence.attempt != operation.attempt
                or evidence.direction is not ArtifactDirection.OUTPUT
                or evidence.digest != commit.validation_digest
                or commit.validator_id != validator_id
            ):
                raise ScientificProfileError("terminal artifact commit differs from the frozen execution")
            output_artifacts = (output_manifest, evidence)

        workload_uids = [
            attempt.workload.uid
            for stage in state.stages
            for attempt in stage.attempts
            if attempt.workload.uid is not None
        ]
        pod_uids = tuple(
            dict.fromkeys(
                pod_uid for stage in state.stages for attempt in stage.attempts for pod_uid in attempt.pod_uids
            )
        )
        await self.service.commit_terminal_result(
            TerminalResultDraft(
                operation_id=state.operation_id,
                tenant_id=state.tenant_id,
                attempt=operation.attempt,
                status=TerminalResultStatus(state.status.value),
                # The submitted input manifest is an immutable artifact from
                # the caller's preparation Operation, so it is projected from
                # frozen controller state instead of being falsely re-owned.
                input_artifacts=(),
                output_artifacts=output_artifacts,
                provenance=ExecutionProvenance(
                    model_id=state.model_id,
                    model_revision=str(identity["model_revision"]),
                    runtime_image_digest=self._profile_digest(identity["runtime_image_digest"], "runtime image digest"),
                    workload_spec_digest=self._profile_digest(
                        identity["workload_recipe_sha256"], "workload recipe digest"
                    ),
                    scheduling_snapshot_digest=state.scheduling.digest,
                    job_uid=workload_uids[-1] if workload_uids else None,
                    pod_uids=pod_uids,
                    started_at=started_at,
                    completed_at=completed_at,
                ),
                validation=SemanticValidation(
                    validator_id=validator_id,
                    validator_revision=self._profile_digest(
                        identity["workload_recipe_sha256"], "validator-bound workload recipe digest"
                    ),
                    status=validation_status,
                    evidence_artifact=evidence,
                ),
                completed_at=completed_at,
            )
        )

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
        if not state.status.terminal or not state.result_published:
            raise ConflictError("scientific batch result is not committed")
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
                admission = attempt.scheduling_admission
                if status in {"succeeded", "preempted"} and admission is None:
                    raise ScientificProfileError("terminal scientific attempt lacks exact Kueue admission evidence")
                attempts.append(
                    {
                        "attempt_id": str(attempt.attempt_id),
                        "stage_id": attempt.stage_id,
                        "shard_id": attempt.shard_id,
                        "attempt_number": attempt.attempt_number,
                        "status": status,
                        "started_at": related[0].occurred_at.isoformat(),
                        "completed_at": related[-1].occurred_at.isoformat(),
                        "scheduling_admission": (
                            None
                            if admission is None
                            else {
                                "resolved_pool_id": admission.resolved_pool_id,
                                "admitted_resource_flavor": admission.admitted_resource_flavor,
                                "accelerator_resource_name": admission.accelerator_resource_name,
                                "accelerator_count": admission.accelerator_count,
                                "admitted_at": admission.admitted_at.isoformat(),
                            }
                        ),
                        "kueue_workload_uid": attempt.kueue_workload_uid,
                        "k8s_job_uid": attempt.workload.uid,
                        "pod_uids": list(attempt.pod_uids),
                        "node_uids": [],
                        "gpu_uuids": [],
                        "checkpoint_input": None,
                        "checkpoint_output": None,
                    }
                )
        if len(attempts) > self.profiles.max_result_attempts:
            raise ScientificProfileError("public result attempt bound is exceeded")

        if state.input_manifest is None:
            raise ScientificProfileError("terminal result has no frozen input manifest")
        input_manifest = await self.artifacts.get_artifact(
            state.input_manifest.manifest_artifact_id,
            tenant_id=tenant_id,
        )
        output_manifest = self._manifest_artifact(manifest, output=True)
        if (
            input_manifest.digest != state.input_manifest.manifest_digest
            or input_manifest.media_type != "application/vnd.fs2.scientific-manifest+json"
        ):
            raise ScientificProfileError("terminal result input manifest differs from frozen admission")
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
                "captured_at": state.scheduling.captured_at.isoformat(),
                "service_class": state.scheduling.service_class.value,
                "tenant_queue": state.scheduling.tenant_queue,
                "model_lane": state.scheduling.model_lane,
                "stages": [
                    {
                        "stage_id": scheduling.stage_id,
                        "resource_class": state.plan.stage(scheduling.stage_id).resource_class.value,
                        "resolved_cluster_queue": scheduling.resolved_cluster_queue,
                        "resolved_local_queue": scheduling.resolved_local_queue,
                        "workload_priority_class": scheduling.workload_priority_class,
                        "workload_priority_value": scheduling.workload_priority_value,
                        "resolved_pool_preference": (
                            list(scheduling.resolved_pool_preference)
                            if state.plan.stage(scheduling.stage_id).resource_class.value == "gpu"
                            else []
                        ),
                        "accelerator_resource_name": (
                            scheduling.accelerator_resource_name
                            if state.plan.stage(scheduling.stage_id).resource_class.value == "gpu"
                            else None
                        ),
                        "accelerator_count": scheduling.accelerator_count,
                        "max_queue_seconds": scheduling.max_queue_seconds,
                        "max_execution_seconds": scheduling.max_execution_seconds,
                        "checkpoint_mode": scheduling.checkpoint_mode.value,
                        "preemption_mode": scheduling.preemption_mode.value,
                    }
                    for scheduling in state.scheduling.stages
                ],
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
