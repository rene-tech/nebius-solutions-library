"""A long scientific run is not an expired short-lived inference payload."""

from datetime import UTC, datetime, timedelta

import pytest
from test_scientific_batch_production import principal

from fs2_serve.memory_store import MemoryStore
from fs2_serve.models import AdmissionRequest, OperationStatus


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["outbox", "materialized"])
@pytest.mark.parametrize("status", [OperationStatus.QUEUED, OperationStatus.ACTIVATING, OperationStatus.RUNNING])
async def test_active_scientific_payload_survives_past_fourteen_days_then_terminal_purges(
    cipher, hasher, owner, status
):
    store = MemoryStore(cipher, hasher)
    identity = await principal(store)
    operation = await store.append_operation(
        principal=identity,
        admission=AdmissionRequest(
            model_id="protein-design",
            operation="design",
            protocol="scientific-batch-v1",
            idempotency_key="longrun-retention-0001",
            request_body=b'{"scientific":true}',
        ),
        model_revision="test",
        reserved_gpu_seconds=0,
        max_attempts=1,
        scientific_admission_factory=lambda operation: {"operation_id": str(operation.id)},
    )
    if owner == "materialized":
        await store.complete_scientific_admission(operation.id)
    row = store.operations[operation.id]
    row.view = row.view.model_copy(update={
        "status": status,
        "payload_expires_at": datetime.now(UTC) - timedelta(days=15),
    })
    assert await store.purge_expired_payloads() == 0
    assert row.view.status is status
    assert row.request is not None
    row.view = row.view.model_copy(update={"status": OperationStatus.SUCCEEDED})
    assert await store.purge_expired_payloads() == 1
    assert row.view.status is OperationStatus.SUCCEEDED
    assert row.request is None


@pytest.mark.asyncio
async def test_generic_payload_expiry_is_unchanged(cipher, hasher):
    store = MemoryStore(cipher, hasher)
    identity = await principal(store)
    operation = await store.append_operation(
        principal=identity,
        admission=AdmissionRequest(
            model_id="protein-design", operation="predict", protocol="native",
            idempotency_key="generic-retention-0001", request_body=b"{}",
        ),
        model_revision="test", reserved_gpu_seconds=0, max_attempts=1,
    )
    row = store.operations[operation.id]
    row.view = row.view.model_copy(update={"payload_expires_at": datetime.now(UTC) - timedelta(days=15)})
    assert await store.purge_expired_payloads() == 1
    assert row.view.status is OperationStatus.EXPIRED
    assert row.view.error_code == "payload_expired"
