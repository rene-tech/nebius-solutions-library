"""Concurrent API/outbox materialization must not reject accepted operations."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import UUID

import httpx2 as httpx
import pytest
import test_scientific_batch_postgres_state as postgres_state_tests
from scientific_batch_fakes import FakeScientificBatchCluster
from test_scientific_batch_production import scientific_runtime

from fs2_serve.api import create_app
from fs2_serve.auth import PepperRing, TokenService
from fs2_serve.models import Scope, TokenCreate
from fs2_serve.scientific_batch.controller import ScientificBatchController
from fs2_serve.scientific_batch.models import BatchStatus
from fs2_serve.scientific_batch.postgres_repository import PostgresScientificBatchRepository
from fs2_serve.scientific_batch.protocols import BatchRepositoryConflictError

postgres_store = postgres_state_tests.store


async def prepare_runtime(registry, cipher, hasher, *, store=None):
    runtime, controller, repository, _, pointer = scientific_runtime(registry, cipher, hasher)
    service = runtime.scientific_batches
    assert service is not None
    if store is not None:
        runtime.store = store
        service.store = store
        runtime.tokens = TokenService(store, PepperRing(active_key_id="pepper-v1", keys={"pepper-v1": b"p" * 32}))
        repository = PostgresScientificBatchRepository(store.pool)
        controller = ScientificBatchController(
            repository=repository,
            cluster=FakeScientificBatchCluster(),
            controller_id="admission-race",
            namespace="fs2-models",
        )
        service.repository = repository
        service.controller = controller
    token = await runtime.tokens.issue(
        TokenCreate(
            principal_id="scientist-a",
            tenant_id="tenant-a",
            scopes={Scope.INFERENCE_INVOKE, Scope.OPERATIONS_READ},
            models={"protein-design"},
            max_concurrency=4,
        ),
        created_by="admission-race-test",
    )
    request = {
        "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
        "operation": "design",
        "service_class": "customer-batch",
        "input_manifest": pointer,
        "parameters": {},
    }
    return runtime, service, repository, controller, token, request


async def concurrent_original_submit(
    runtime, service, repository, controller, token, request, monkeypatch, *, postgres=None, mismatch=None
):
    """Pause the original creator while a real recovery worker wins admission.

    No sleep/retry determines the interleaving. The original HTTP request is
    still in flight, and the second task uses the durable outbox, not a client
    resubmission. PostgreSQL executes the actual terminal-operation conflict.
    """
    first_creator_waiting = asyncio.Event()
    recovery_finished = asyncio.Event()
    create = repository.create
    original_operation = []
    create_count = 0

    async def gated_create(**kwargs):
        nonlocal create_count
        create_count += 1
        if create_count == 1:
            original_operation.append(kwargs["operation_id"])
            first_creator_waiting.set()
            await asyncio.wait_for(recovery_finished.wait(), timeout=10)
            if postgres is None:
                # The in-memory fake does not check durable Operation status;
                # model the same repository conflict exercised for real below.
                raise BatchRepositoryConflictError("terminal operation cannot admit a scientific batch")
        return await create(**kwargs)

    monkeypatch.setattr(repository, "create", gated_create)

    async def recover():
        await asyncio.wait_for(first_creator_waiting.wait(), timeout=10)
        try:
            operation_id = original_operation[0]
            if postgres is not None:
                await postgres_state_tests.durable_input_artifact(
                    postgres,
                    operation_id,
                    artifact_id=UUID(request["input_manifest"]["artifact_id"]),
                    tenant_id="tenant-a",
                )
            assert await service.recover_pending_admissions() == 1
            if postgres is not None:
                await repository.request_cancel(operation_id, tenant_id="tenant-a", actor="test-racer")
                await controller.reconcile_once()
                assert (await repository.get(operation_id, tenant_id="tenant-a")).status is BatchStatus.CANCELLED
            else:
                current = repository.records[operation_id]
                changes = {"status": BatchStatus.CANCELLED}
                if mismatch == "runtime":
                    changes.update(
                        variant_id="different-runtime",
                        execution_plan=replace(current.execution_plan, variant_id="different-runtime"),
                    )
                elif mismatch == "scheduling":
                    changes["scheduling"] = replace(current.scheduling, policy_revision="f" * 64)
                elif mismatch == "input":
                    changes["input_manifest"] = replace(current.input_manifest, manifest_digest="sha256:" + "f" * 64)
                repository.records[operation_id] = replace(current, **changes)
                if mismatch == "missing":
                    del repository.records[operation_id]
        finally:
            recovery_finished.set()

    app = create_app(runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://inference.test.invalid",
            headers={"authorization": f"Bearer {token.token}"},
        ) as client,
    ):
        response, _ = await asyncio.wait_for(
            asyncio.gather(
                client.post(
                    "/v1/models/protein-design:submit",
                    headers={"idempotency-key": "original-racing-submit-0001"},
                    json=request,
                ),
                recover(),
            ),
            timeout=20,
        )
        if mismatch is None:
            replay = await client.post(
                "/v1/models/protein-design:submit",
                headers={"idempotency-key": "original-racing-submit-0001"},
                json=request,
            )
            assert replay.status_code == 202
            assert replay.json()["operation"]["id"] == str(original_operation[0])
            assert replay.json()["operation"]["reused"] is True
        # Changed scientific bytes under the same key remain an actual 409.
        changed = await client.post(
            "/v1/models/protein-design:submit",
            headers={"idempotency-key": "original-racing-submit-0001"},
            json={
                **request,
                "client_context": {
                    "display_name": "Different request",
                    "batch_id": "changed",
                    "correlation_id": "changed",
                },
            },
        )
        assert changed.status_code == 409
        anonymous = await client.post("/v1/models/protein-design:submit", headers={"authorization": ""}, json=request)
        assert anonymous.status_code == 401
    assert create_count == 2
    assert await service.store.get_scientific_admission(original_operation[0]) is None
    return response, original_operation[0]


@pytest.mark.asyncio
async def test_original_http_submit_accepts_matching_concurrent_materialization(registry, cipher, hasher, monkeypatch):
    parts = await prepare_runtime(registry, cipher, hasher)
    response, operation_id = await concurrent_original_submit(*parts, monkeypatch)
    assert response.status_code == 202, response.text
    assert response.json()["operation"]["id"] == str(operation_id)
    assert response.json()["operation"]["reused"] is False
    assert response.json()["batch"]["status"] == "cancelled"
    assert response.headers["location"] == f"/v1/operations/{operation_id}"


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["runtime", "scheduling", "input", "missing"])
async def test_materialization_conflict_without_exact_frozen_admission_still_rejects(
    registry,
    cipher,
    hasher,
    monkeypatch,
    mismatch,
):
    parts = await prepare_runtime(registry, cipher, hasher)
    response, _ = await concurrent_original_submit(*parts, monkeypatch, mismatch=mismatch)
    assert response.status_code == 409


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_real_postgres_original_submit_survives_recovery_finishing_first(
    postgres_store,
    registry,
    cipher,
    hasher,
    monkeypatch,
):
    parts = await prepare_runtime(registry, cipher, hasher, store=postgres_store)
    response, operation_id = await concurrent_original_submit(*parts, monkeypatch, postgres=postgres_store)
    assert response.status_code == 202, response.text
    assert response.json()["operation"]["id"] == str(operation_id)
    assert response.json()["operation"]["reused"] is False
    assert response.json()["operation"]["status"] == "cancelled"
    assert response.json()["batch"]["status"] == "cancelled"
    async with postgres_store.pool.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM fs2_operations WHERE id=$1", operation_id) == 1
        assert (
            await connection.fetchval("SELECT count(*) FROM fs2_scientific_batches WHERE operation_id=$1", operation_id)
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM fs2_operation_events WHERE operation_id=$1 AND event='accepted'", operation_id
            )
            == 1
        )
