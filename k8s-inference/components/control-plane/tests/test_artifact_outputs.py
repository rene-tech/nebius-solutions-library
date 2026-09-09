"""Large runtime responses become compact tenant-owned artifact results."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from fs2_serve.artifact_outputs import RESULT_SCHEMA, ServingOutputArtifactizer
from fs2_serve.models import ClaimedOperation, OperationStatus, RuntimeIdentity, RuntimeResult
from fs2_serve.scientific_run_result import ArtifactRef


class _ArtifactRecord:
    def __init__(self, reference: ArtifactRef) -> None:
        self.reference = reference

    def to_public_ref(self) -> ArtifactRef:
        return self.reference


class _Artifacts:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.content = b""
        self.upload = None
        self.opened = None
        self.closed = None

    async def open_attempt(self, request):
        self.events.append("open")
        self.opened = request
        return request

    async def begin_upload(self, request):
        self.events.append("begin")
        self.upload = request
        return request

    async def store_trusted_upload_content(self, request, *, content: bytes):
        self.events.append("store")
        self.content = content
        return request

    async def finalize_upload(self, request):
        self.events.append("finalize")
        return _ArtifactRecord(
            ArtifactRef(
                artifact_id=str(uuid4()),
                sha256=hashlib.sha256(self.content).hexdigest(),
                size_bytes=len(self.content),
                media_type="application/octet-stream",
                compression="none",
            )
        )

    async def close_attempt(self, request):
        self.events.append("close")
        self.closed = request
        return request


def _operation() -> ClaimedOperation:
    now = datetime.now(UTC)
    return ClaimedOperation(
        id=uuid4(),
        tenant_id="tenant-a",
        principal_id="researcher-a",
        token_id=uuid4(),
        model_id="diffdock",
        model_revision="1" * 40,
        protocol="native",
        operation="infer",
        idempotency_key="artifact-output-test-01",
        status=OperationStatus.RUNNING,
        accepted_at=now,
        available_at=now,
        ready_at=now,
        started_at=now,
        attempt=1,
        max_attempts=1,
        fencing_token=1,
        request_content_type="application/json",
        worker_id="worker-1",
    )


@pytest.mark.asyncio
async def test_large_json_result_is_externalized_to_small_pointer():
    artifacts = _Artifacts()
    original = json.dumps({"structure": "ATOM\n" * 20_000}).encode()
    result = RuntimeResult(
        status_code=200,
        body=original,
        content_type="application/json",
        elapsed_seconds=1,
        runtime=RuntimeIdentity(),
        semantic_outcome="protocol_valid",
    )

    externalized = await ServingOutputArtifactizer(artifacts, threshold_bytes=1024).externalize(  # type: ignore[arg-type]
        _operation(), result
    )

    envelope = json.loads(externalized.body)
    assert envelope["schema"] == RESULT_SCHEMA
    assert envelope["artifact"]["size_bytes"] == len(original)
    assert envelope["content_type"] == "application/json"
    assert len(externalized.body) < 1024
    assert externalized.content_type == "application/json"
    assert artifacts.content == original
    assert artifacts.events == ["open", "begin", "store", "finalize", "close"]
    assert artifacts.opened.admission.accelerator_count == 0
    assert artifacts.opened.admission.admitted_at == artifacts.opened.started_at
    assert artifacts.closed.admission == artifacts.opened.admission


@pytest.mark.asyncio
async def test_small_json_result_stays_inline():
    artifacts = _Artifacts()
    result = RuntimeResult(
        status_code=200,
        body=b'{"ok":true}',
        content_type="application/json",
        elapsed_seconds=1,
        runtime=RuntimeIdentity(),
        semantic_outcome="protocol_valid",
    )

    kept = await ServingOutputArtifactizer(artifacts, threshold_bytes=1024).externalize(_operation(), result)  # type: ignore[arg-type]

    assert kept is result
    assert not artifacts.events
