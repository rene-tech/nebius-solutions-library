"""Externalize large serving results before they re-enter an agent context."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from .models import ClaimedOperation, RuntimeResult
from .scientific_artifacts import (
    ArtifactAccess,
    ArtifactDirection,
    BeginArtifactUpload,
    CloseStageAttempt,
    FinalizeArtifactUpload,
    KueueAdmission,
    OpenStageAttempt,
    ScientificArtifactControllerPort,
)
from .scientific_artifacts import AttemptStatus as ArtifactAttemptStatus

RESULT_SCHEMA = "fs2-serve.nebius.ai/operation-artifact-result/v1"


def _identity(operation: ClaimedOperation, suffix: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"fs2-serve/serving-output/{operation.id}/{operation.attempt}/{suffix}")


class ServingOutputArtifactizer:
    """Store binary or large results and return a compact immutable pointer."""

    def __init__(self, artifacts: ScientificArtifactControllerPort, *, threshold_bytes: int = 64 * 1024) -> None:
        if threshold_bytes < 1:
            raise ValueError("result artifact threshold must be positive")
        self._artifacts = artifacts
        self._threshold_bytes = threshold_bytes

    @staticmethod
    def _media_type(value: str) -> str:
        media_type = value.split(";", 1)[0].strip().lower()
        # Unknown upstream types remain described in the small result envelope
        # while the object store receives its always-supported binary type.
        if not media_type or "/" not in media_type:
            return "application/octet-stream"
        return media_type

    def _should_externalize(self, result: RuntimeResult) -> bool:
        media_type = self._media_type(result.content_type)
        textual = media_type == "application/json" or media_type.startswith("text/")
        return not textual or len(result.body) >= self._threshold_bytes

    async def externalize(self, operation: ClaimedOperation, result: RuntimeResult) -> RuntimeResult:
        if not self._should_externalize(result):
            return result
        digest = hashlib.sha256(result.body).hexdigest()
        attempt_id = _identity(operation, "attempt")
        upload_id = _identity(operation, f"upload/{digest}")
        started_at = operation.started_at or operation.ready_at or operation.accepted_at
        # Result externalization is a CPU post-processing stage. The artifact
        # ledger requires successful attempts to carry an admission timestamp,
        # while a zero-accelerator admission deliberately carries no GPU pool,
        # flavor or resource identity.
        admission = KueueAdmission(accelerator_count=0, admitted_at=started_at)
        await self._artifacts.open_attempt(
            OpenStageAttempt(
                attempt_id=attempt_id,
                operation_id=operation.id,
                tenant_id=operation.tenant_id,
                stage_id="serving-output",
                attempt_number=operation.attempt,
                admission=admission,
                started_at=started_at,
            )
        )
        # application/octet-stream is part of every artifact deployment's
        # mandatory baseline. The exact upstream type remains in the envelope.
        media_type = "application/octet-stream"
        await self._artifacts.begin_upload(
            BeginArtifactUpload(
                upload_id=upload_id,
                attempt_id=attempt_id,
                operation_id=operation.id,
                tenant_id=operation.tenant_id,
                direction=ArtifactDirection.OUTPUT,
                expected_digest=f"sha256:{digest}",
                expected_size_bytes=len(result.body),
                media_type=media_type,
                access=ArtifactAccess(),
            )
        )
        finalize = FinalizeArtifactUpload(
            upload_id=upload_id,
            operation_id=operation.id,
            tenant_id=operation.tenant_id,
        )
        await self._artifacts.store_trusted_upload_content(finalize, content=result.body)
        artifact = await self._artifacts.finalize_upload(finalize)
        await self._artifacts.close_attempt(
            CloseStageAttempt(
                attempt_id=attempt_id,
                operation_id=operation.id,
                tenant_id=operation.tenant_id,
                status=ArtifactAttemptStatus.SUCCEEDED,
                completed_at=datetime.now(UTC),
                admission=admission,
            )
        )
        envelope = {
            "schema": RESULT_SCHEMA,
            "artifact": artifact.to_public_ref().model_dump(mode="json"),
            "content_type": result.content_type,
        }
        return result.model_copy(
            update={
                "body": json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode(),
                "content_type": "application/json",
            }
        )


__all__ = ["RESULT_SCHEMA", "ServingOutputArtifactizer"]
