"""Checkpoint recovery keeps the authorized attempt's durable shard identity."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from fs2_serve.scientific_artifacts import (
    ArtifactAccess,
    ArtifactDirection,
    ArtifactDownload,
    ArtifactRecord,
    EphemeralHandle,
    artifact_storage_key,
)
from fs2_serve.scientific_batch.workload_routes import scientific_workload_artifact_router


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt_shard", [None, "gang", "replica-1"])
async def test_checkpoint_listing_and_download_use_authorized_attempt_shard(monkeypatch, attempt_shard):
    # Real capability/state authorization is exercised for all three execution
    # modes in test_scientific_batch_production. Here we isolate the artifact
    # boundary after authorization: JobSet None must not become literal "gang".
    now = datetime(2026, 10, 3, tzinfo=UTC)
    operation_id = uuid4()
    capability = SimpleNamespace(
        model_id="gromacs-mpi",
        stage_id="workflow",
        collector_id="gromacs-mpi-workflow-v1",
        operation_id=operation_id,
        tenant_id="tenant-a",
        shard_id=attempt_shard or "gang",
        artifacts=(),
    )
    authorized = AsyncMock(return_value=(capability, object(), SimpleNamespace(shard_id=attempt_shard)))
    monkeypatch.setattr("fs2_serve.scientific_batch.workload_routes.authorize_workload_capability", authorized)

    def record(shard, direction=ArtifactDirection.OUTPUT, *, seconds=0, checkpoint=True):
        attempt_id = uuid4()
        digest = "sha256:" + "a" * 64
        return ArtifactRecord(
            artifact_id=uuid4(),
            attempt_id=attempt_id,
            operation_id=operation_id,
            tenant_id="tenant-a",
            stage_id="workflow",
            shard_id=shard,
            direction=direction,
            digest=digest,
            size_bytes=10,
            media_type="application/vnd.fs2.gromacs-checkpoint+json" if checkpoint else "application/octet-stream",
            storage_key=artifact_storage_key(
                tenant_id="tenant-a",
                operation_id=operation_id,
                stage_id="workflow",
                shard_id=shard,
                attempt_id=attempt_id,
                direction=direction,
                digest=digest,
            ),
            access=ArtifactAccess(),
            retention_expires_at=now + timedelta(days=1),
            created_at=now + timedelta(seconds=seconds),
        )

    older = record(attempt_shard)
    current = record(attempt_shard, seconds=1)
    other = record("gang" if attempt_shard is None else None, seconds=2)
    input_record = record(attempt_shard, ArtifactDirection.INPUT, seconds=3)
    native_file = record(attempt_shard, seconds=4, checkpoint=False)
    records = [older, current, other, input_record, native_file]

    async def download(artifact_id, *, tenant_id):
        assert tenant_id == "tenant-a"
        return ArtifactDownload(
            artifact=next(item for item in records if item.artifact_id == artifact_id),
            handle=EphemeralHandle(
                method="GET", url="https://objects.test/checkpoint", expires_at=now + timedelta(minutes=5)
            ),
        )

    artifacts = SimpleNamespace(
        list_artifacts=AsyncMock(return_value=records), download=AsyncMock(side_effect=download)
    )
    app = FastAPI()
    app.include_router(scientific_workload_artifact_router(authority=object(), artifacts=artifacts, batches=object()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        latest = await client.get("/internal/scientific-workloads/checkpoints/latest")
        assert latest.status_code == 200
        assert latest.json()["checkpoint"]["artifact_id"] == str(current.artifact_id)
        for allowed in (older, current, native_file):
            response = await client.get(f"/internal/scientific-workloads/artifacts/{allowed.artifact_id}:download")
            assert response.status_code == 200
        for excluded in (other, input_record):
            response = await client.get(f"/internal/scientific-workloads/artifacts/{excluded.artifact_id}:download")
            assert response.status_code == 403
        artifacts.list_artifacts.assert_called_with(operation_id, tenant_id="tenant-a", stage_id="workflow")
        assert artifacts.download.await_count == 3
