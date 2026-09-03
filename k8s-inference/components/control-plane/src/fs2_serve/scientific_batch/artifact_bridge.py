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
    ArtifactAccess,
    ArtifactAccessProfile,
    ArtifactNotFoundError,
    ArtifactRepository,
    CloseStageAttempt,
    CommitStageResult,
    KueueAdmission,
    ManifestEntryDraft,
    OpenStageAttempt,
    RunResultDraft,
    ScientificArtifactControllerPort,
)
from ..scientific_artifacts import (
    AttemptStatus as ArtifactAttemptStatus,
)
from ..store import ConflictError, Store
from .models import (
    ArtifactAccessContext,
    AttemptArtifactCommit,
    AttemptOutcome,
    BatchEvent,
    BatchEventKind,
    BatchStatus,
    ScientificAttemptState,
    ScientificBatchState,
    ScientificInputAdmission,
    ScientificInputArtifact,
    VerifiedInputManifest,
    WorkloadResource,
)
from .profile_catalog import ScientificProfileCatalog, ScientificProfileError

_ERROR = re.compile(r"[^A-Z0-9_]+")
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024


def _raw_digest(value: str | None) -> str | None:
    return None if value is None else value.removeprefix("sha256:")


def _public_artifact(record: Any) -> dict[str, Any]:
    return cast(dict[str, Any], record.to_public_ref().model_dump(mode="json", exclude_none=True))


def _pointer_matches(record: Any, pointer: Mapping[str, Any]) -> bool:
    actual = _public_artifact(record)
    expected = dict(pointer)
    if expected.get("compression") == "none":
        expected.pop("compression")
    if actual.get("compression") == "none" and "compression" not in expected:
        actual.pop("compression")
    return actual == expected


class ScientificBatchResultRepository(Protocol):
    async def get(self, operation_id: UUID, *, tenant_id: str) -> ScientificBatchState: ...

    async def list_events(
        self, operation_id: UUID, *, tenant_id: str, after_sequence: int = 0, limit: int = 1000
    ) -> list[BatchEvent]: ...


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
        if not _pointer_matches(artifact, pointer):
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
            if not _pointer_matches(entry, ref) or entry.access != artifact.access:
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

    def _require_service(self) -> ScientificArtifactControllerPort:
        if self.service is None:
            raise ScientificProfileError("scientific artifact controller port is unavailable")
        return self.service

    async def open_attempt(self, resource: WorkloadResource, *, started_at: Any) -> None:
        await self._require_service().open_attempt(
            OpenStageAttempt(
                attempt_id=resource.attempt_id,
                operation_id=resource.operation_id,
                tenant_id=resource.tenant_id,
                stage_id=resource.stage_id,
                shard_id=resource.shard_id,
                attempt_number=resource.attempt_number,
                started_at=started_at,
            )
        )

    async def close_attempt(self, state: ScientificBatchState, attempt: ScientificAttemptState) -> None:
        if attempt.outcome is AttemptOutcome.ACTIVE or attempt.completed_at is None:
            raise ScientificProfileError("only a durable terminal attempt can close artifact publication")
        admission = attempt.scheduling_admission
        await self._require_service().close_attempt(
            CloseStageAttempt(
                attempt_id=attempt.attempt_id,
                operation_id=state.operation_id,
                tenant_id=state.tenant_id,
                status=ArtifactAttemptStatus(attempt.outcome.value),
                completed_at=attempt.completed_at,
                admission=(
                    None
                    if admission is None or admission.admitted_at is None
                    else KueueAdmission(
                        resolved_pool_id=admission.resolved_pool_id,
                        admitted_resource_flavor=admission.admitted_resource_flavor,
                        accelerator_resource_name=admission.accelerator_resource_name,
                        accelerator_count=admission.accelerator_count,
                        admitted_at=admission.admitted_at,
                    )
                ),
                kueue_workload_uid=attempt.kueue_workload_uid,
                k8s_job_uid=attempt.workload.uid,
                pod_uids=attempt.pod_uids,
            )
        )

    async def _attempt_output(
        self,
        state: ScientificBatchState,
        attempt: ScientificAttemptState,
    ) -> tuple[AttemptArtifactCommit, str]:
        if self.content_reader is None or state.execution_plan is None:
            raise ScientificProfileError("scientific collection reader or execution plan is unavailable")
        records = await self._require_service().list_artifacts(
            state.operation_id,
            tenant_id=state.tenant_id,
            stage_id=attempt.stage_id,
            attempt_id=attempt.attempt_id,
        )
        manifests = [item for item in records if item.media_type == "application/vnd.fs2.scientific-manifest+json"]
        evidence = [item for item in records if item.media_type == "application/vnd.fs2.scientific-validation+json"]
        if len(manifests) != 1 or len(evidence) != 1:
            raise ScientificProfileError("successful attempt lacks one canonical manifest and validation receipt")
        manifest_record, evidence_record = manifests[0], evidence[0]
        manifest = self.profiles.validate_artifact_manifest(
            json.loads(
                await self.content_reader.read(
                    manifest_record.artifact_id,
                    tenant_id=state.tenant_id,
                    maximum_bytes=_MAX_MANIFEST_BYTES,
                )
            )
        )
        validation = json.loads(
            await self.content_reader.read(
                evidence_record.artifact_id,
                tenant_id=state.tenant_id,
                maximum_bytes=_MAX_MANIFEST_BYTES,
            )
        )
        invocation = state.execution_plan.invocation(attempt.stage_id, attempt.shard_id)
        if not isinstance(validation, Mapping) or any(
            validation.get(key) != expected
            for key, expected in {
                "status": "passed",
                "collector_id": invocation.collector_id,
                "validator_id": invocation.validator_id,
                "stage_id": invocation.stage_id,
                "shard_id": invocation.shard_id,
                "logical_output_id": invocation.produces,
            }.items()
        ):
            raise ScientificProfileError("validation receipt differs from the frozen invocation")
        by_id = {item.artifact_id: item for item in records}
        entries = cast(list[Mapping[str, Any]], manifest["entries"])
        for entry in entries:
            artifact_id = UUID(str(cast(Mapping[str, Any], entry["artifact"])["artifact_id"]))
            record = by_id.get(artifact_id)
            if record is None or _public_artifact(record) != dict(cast(Mapping[str, Any], entry["artifact"])):
                raise ScientificProfileError("collected manifest references another or changed artifact")
        if invocation.handoff_name is None:
            handoff = manifest_record
            semantic_type = "scientific-artifact-manifest/v1"
        else:
            selected = [item for item in entries if item["name"] == invocation.handoff_name]
            if len(selected) != 1:
                raise ScientificProfileError("collected manifest omits the exact handoff entry")
            handoff = by_id[UUID(str(cast(Mapping[str, Any], selected[0]["artifact"])["artifact_id"]))]
            semantic_type = str(selected[0]["semantic_type"])
        return (
            AttemptArtifactCommit(
                operation_id=state.operation_id,
                stage_id=attempt.stage_id,
                attempt_ids=(attempt.attempt_id,),
                logical_artifact_id=invocation.produces,
                handoff_artifact_id=handoff.artifact_id,
                handoff_digest=handoff.digest,
                handoff_size_bytes=handoff.size_bytes,
                handoff_media_type=handoff.media_type,
                handoff_compression=None if handoff.compression is None else handoff.compression.value,
                manifest_artifact_id=manifest_record.artifact_id,
                validation_artifact_id=evidence_record.artifact_id,
                manifest_digest=manifest_record.digest,
                validation_digest=evidence_record.digest,
                committed_at=attempt.completed_at or state.scheduling.captured_at,
                validated_at=attempt.completed_at or state.scheduling.captured_at,
                semantic_valid=True,
                collector_id=invocation.collector_id,
                validator_id=invocation.validator_id,
            ),
            semantic_type,
        )

    @staticmethod
    def _validation_digest(commits: tuple[AttemptArtifactCommit, ...]) -> str:
        if len(commits) == 1:
            return commits[0].validation_digest
        payload = json.dumps(
            [item.validation_digest for item in commits], sort_keys=True, separators=(",", ":")
        ).encode()
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"

    async def ensure_stage_commit(self, state: ScientificBatchState, *, stage_id: str) -> None:
        service = self._require_service()
        if await service.stage_commit(state.operation_id, stage_id=stage_id, tenant_id=state.tenant_id) is not None:
            return
        stage = state.stage(stage_id)
        spec = state.plan.stage(stage_id)
        attempts = tuple(stage.latest_attempt(shard) for shard in spec.workload_units)
        if any(
            item is None or item.outcome is not AttemptOutcome.SUCCEEDED or not item.resource_released
            for item in attempts
        ):
            raise ScientificProfileError("stage artifacts cannot commit before every successful workload is released")
        outputs = tuple([await self._attempt_output(state, cast(ScientificAttemptState, item)) for item in attempts])
        commits = tuple(item[0] for item in outputs)
        completed_at = max(item.validated_at for item in commits)
        await service.commit_stage(
            CommitStageResult(
                operation_id=state.operation_id,
                tenant_id=state.tenant_id,
                stage_id=stage_id,
                attempt_ids=tuple(item.attempt_ids[0] for item in commits),
                entries=tuple(
                    ManifestEntryDraft(
                        name=commit.logical_artifact_id,
                        semantic_type=semantic_type,
                        artifact_id=commit.handoff_artifact_id,
                    )
                    for commit, semantic_type in outputs
                ),
                validation_digest=self._validation_digest(commits),
                semantic_valid=True,
                committed_at=completed_at,
                validated_at=completed_at,
            )
        )

    async def artifact_commits(
        self, state: ScientificBatchState, *, stage_id: str
    ) -> tuple[AttemptArtifactCommit, ...]:
        record = await self._require_service().stage_commit(
            state.operation_id,
            stage_id=stage_id,
            tenant_id=state.tenant_id,
        )
        if record is None:
            return ()
        stage = state.stage(stage_id)
        spec = state.plan.stage(stage_id)
        attempts = tuple(stage.latest_attempt(shard) for shard in spec.workload_units)
        if any(item is None for item in attempts):
            raise ScientificProfileError("stage commit has no matching controller attempts")
        outputs = tuple([await self._attempt_output(state, cast(ScientificAttemptState, item)) for item in attempts])
        commits = tuple(item[0] for item in outputs)
        entries = {item.name: item for item in record.manifest.entries}
        if (
            set(record.attempt_ids) != {item.attempt_ids[0] for item in commits}
            or not record.semantic_valid
            or record.validation_digest != self._validation_digest(commits)
            or set(entries) != {item.logical_artifact_id for item in commits}
            or any(
                entries[item.logical_artifact_id].artifact.artifact_id != str(item.handoff_artifact_id)
                or entries[item.logical_artifact_id].artifact.sha256 != item.handoff_digest.removeprefix("sha256:")
                for item in commits
            )
        ):
            raise ScientificProfileError("canonical stage commit differs from collected attempts")
        return commits

    @staticmethod
    def _scheduling_snapshot(state: ScientificBatchState) -> dict[str, Any]:
        return {
            "policy_revision": state.scheduling.policy_revision,
            "captured_at": state.scheduling.captured_at,
            "service_class": state.scheduling.service_class.value,
            "tenant_queue": state.scheduling.tenant_queue,
            "model_lane": state.scheduling.model_lane,
            "workload_namespace": state.scheduling.workload_namespace,
            "route_namespace": state.scheduling.route_namespace,
            "stages": [
                {
                    "stage_id": item.stage_id,
                    "resource_class": item.resource_class.value,
                    "resolved_cluster_queue": item.resolved_cluster_queue,
                    "resolved_local_queue": item.resolved_local_queue,
                    "workload_priority_class": item.workload_priority_class,
                    "workload_priority_value": item.workload_priority_value,
                    "resolved_pool_preference": list(item.resolved_pool_preference),
                    "accelerator_resource_name": item.accelerator_resource_name,
                    "accelerator_count": item.accelerator_count,
                    "max_queue_seconds": item.max_queue_seconds,
                    "max_execution_seconds": item.max_execution_seconds,
                    "checkpoint_mode": item.checkpoint_mode.value,
                    "preemption_mode": item.preemption_mode.value,
                }
                for item in state.scheduling.stages
            ],
        }

    async def publish_terminal(self, state: ScientificBatchState) -> None:
        """Idempotently publish the artifact-service-owned terminal result."""

        if not state.status.terminal or state.result_published or state.input_manifest is None:
            raise ScientificProfileError("scientific batch is not awaiting terminal publication")
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
        terminal = [event for event in events if event.draft.kind is terminal_kind]
        if len(terminal) != 1:
            raise ScientificProfileError("scientific batch terminal event is absent or ambiguous")
        output_manifest_id: UUID | None = None
        validator_id = semantic.get("validator_id")
        validation_receipt: str | None = None
        if not isinstance(validator_id, str):
            raise ScientificProfileError("scientific profile validator identity is invalid")
        if state.status is BatchStatus.SUCCEEDED:
            dependent = {dependency for stage in state.plan.stages for dependency in stage.depends_on}
            sinks = [stage.stage_id for stage in state.plan.stages if stage.stage_id not in dependent]
            if len(sinks) != 1:
                raise ScientificProfileError("successful scientific batch has no unique terminal stage")
            commits = await self.artifact_commits(state, stage_id=sinks[0])
            if len(commits) != 1:
                raise ScientificProfileError("terminal stage has no unique canonical output manifest")
            output_manifest_id = commits[0].manifest_artifact_id
            validator_id = commits[0].validator_id
            validation_receipt = commits[0].validation_digest
        access = ArtifactAccess(
            profile=ArtifactAccessProfile(state.access_context.profile),
            receipt_digest=state.access_context.receipt_digest,
        )
        error_code = None
        if state.status is BatchStatus.FAILED:
            error_code = _ERROR.sub("_", (state.failure_code or "SCIENTIFIC_RUN_FAILED").upper()).strip("_")
        await self._require_service().commit_run_result(
            RunResultDraft(
                operation_id=state.operation_id,
                tenant_id=state.tenant_id,
                terminal_status=cast(Any, state.status.value),
                submitted_at=operation.accepted_at,
                completed_at=terminal[0].occurred_at,
                execution_identity={
                    "model_id": state.model_id,
                    "model_revision": identity["model_revision"],
                    "runtime_image_digest": identity["runtime_image_digest"],
                    "runtime_recipe_sha256": _raw_digest(cast(str, identity["runtime_recipe_sha256"])),
                    "workload_recipe_sha256": _raw_digest(cast(str, identity["workload_recipe_sha256"])),
                    "model_artifact_manifest_digest": _raw_digest(cast(str, identity["artifact_manifest_digest"])),
                    "execution_identity_sha256": _raw_digest(cast(str, identity["execution_identity_sha256"])),
                },
                access=access,
                scheduling_snapshot=self._scheduling_snapshot(state),
                input_manifest_artifact_id=state.input_manifest.manifest_artifact_id,
                output_manifest_artifact_id=output_manifest_id,
                validator_id=validator_id,
                validation_status=(
                    "passed"
                    if state.status is BatchStatus.SUCCEEDED
                    else "failed"
                    if state.status is BatchStatus.FAILED
                    else "not-run"
                ),
                validation_receipt_digest=validation_receipt,
                error_code=error_code,
                error_message="scientific run did not succeed" if error_code is not None else None,
                error_retryable=False if error_code is not None else None,
            )
        )

    async def result_response(self, operation_id: UUID, *, tenant_id: str) -> Mapping[str, Any]:
        state = await self.batches.get(operation_id, tenant_id=tenant_id)
        if not state.status.terminal or not state.result_published:
            raise ConflictError("scientific batch result is not committed")
        record = await self._require_service().get_run_result(operation_id, tenant_id=tenant_id)
        result = record.result.to_document()
        if result["terminal_status"] != state.status.value:
            raise ScientificProfileError("artifact result and controller terminal status differ")
        self.profiles.validate_result(result)
        return result
