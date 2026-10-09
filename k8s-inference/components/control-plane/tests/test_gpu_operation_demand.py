"""CPU input uploads remain durable without activating GPU model replicas."""

from __future__ import annotations

import pytest
from prometheus_client.parser import text_string_to_metric_families
from test_postgres_integration import add_token, postgres_store  # noqa: F401

from fs2_serve.memory_store import MemoryStore
from fs2_serve.models import AdmissionRequest, OperationStatus, RuntimeIdentity
from fs2_serve.telemetry import Metrics


async def check_gpu_demand(store) -> None:
    principal = await add_token(store)
    metrics = Metrics([])

    async def append(key: str, protocol: str):
        return await store.append_operation(
            principal=principal,
            admission=AdmissionRequest(
                model_id="qwen3-8b",
                operation="upload" if protocol == "scientific-artifact-upload-v1" else "generate",
                protocol=protocol,
                idempotency_key="gpu-demand-" + key,
                request_body=b"{}",
            ),
            model_revision="test-revision",
            reserved_gpu_seconds=0 if protocol == "scientific-artifact-upload-v1" else 10,
            max_attempts=1,
        )

    def samples():
        return {
            (sample.labels["model"], sample.labels["state"]): sample.value
            for family in text_string_to_metric_families(metrics.render().decode())
            if family.name == "fs2_serve_operations"
            for sample in family.samples
        }

    upload = await append("cpu-upload", "scientific-artifact-upload-v1")
    assert (await store.get_operation(upload.id)).status == OperationStatus.QUEUED
    assert await store.queue_counts() == {}
    metrics.set_queue(await store.queue_counts())
    assert sum(samples().values()) == 0
    assert await store.claim_operation("worker", lease_seconds=30) is None

    first = await append("native", "native")
    await append("chat", "openai-chat")
    await append("batch", "scientific-batch-v1")
    assert await store.queue_counts() == {("qwen3-8b", "queued"): 3}
    claimed = await store.claim_operation("worker", lease_seconds=30)
    assert claimed is not None and claimed.id == first.id
    assert await store.queue_counts() == {("qwen3-8b", "queued"): 2, ("qwen3-8b", "activating"): 1}
    await store.mark_running(
        claimed.id, RuntimeIdentity(), worker_id="worker", fencing_token=claimed.fencing_token
    )
    second = await store.claim_operation("worker", lease_seconds=30)
    assert second is not None
    expected = {("qwen3-8b", "queued"): 1, ("qwen3-8b", "activating"): 1, ("qwen3-8b", "running"): 1}
    assert await store.queue_counts() == expected
    metrics.set_queue(await store.queue_counts())
    assert {key: value for key, value in samples().items() if value} == expected

    completed = await store.complete_scientific_artifact_upload(
        upload.id, tenant_id=principal.tenant_id, principal_id=principal.principal_id
    )
    assert completed.status == OperationStatus.SUCCEEDED
    assert completed.semantic_outcome == "verified"
    assert completed.estimated_gpu_seconds == 0
    assert (await store.get_operation(upload.id)).outcome == "artifact_uploaded"
    assert await store.queue_counts() == expected
    audit = await store.list_audit(tenant_id=principal.tenant_id)
    assert any(row.action == "scientific_artifact.upload.complete" and row.target_id == str(upload.id) for row in audit)
    accounting = await store.terminal_accounting()
    upload_totals = [row for row in accounting if row.protocol == "scientific-artifact-upload-v1"]
    assert len(upload_totals) == 1 and upload_totals[0].operations == 1
    assert upload_totals[0].outcome == "artifact_uploaded"
    metrics.set_terminal_accounting(accounting)
    assert any(
        sample.labels.get("protocol") == "scientific-artifact-upload-v1" and sample.value == 1
        for family in text_string_to_metric_families(metrics.render().decode())
        for sample in family.samples
    )


@pytest.mark.asyncio
async def test_memory_gpu_demand_excludes_upload_without_erasing_outcome(cipher, hasher):
    await check_gpu_demand(MemoryStore(cipher=cipher, hasher=hasher))


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_gpu_demand_excludes_upload_without_erasing_outcome(postgres_store):  # noqa: F811
    await check_gpu_demand(postgres_store)
