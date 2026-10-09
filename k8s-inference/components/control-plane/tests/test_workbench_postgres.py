import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
import pytest

from fs2_serve.store import ConflictError
from fs2_serve.workbench_models import WorkbenchBinding, WorkbenchCommand
from fs2_serve.workbench_repository import PostgresWorkbenchRepository


@pytest.fixture
async def repository():
    url = os.environ.get("FS2_TEST_DATABASE_URL")
    if not url:
        pytest.skip("FS2_TEST_DATABASE_URL not set")
    pool = await asyncpg.create_pool(url, min_size=1, max_size=4)
    try:
        yield PostgresWorkbenchRepository(pool)
    finally:
        await pool.close()


def binding():
    suffix = uuid4().hex
    return WorkbenchBinding(
        id=uuid4(),
        tenant_id="test-" + suffix,
        principal_ids=["researcher"],
        name="Customer",
        endpoint_id="aiendpoint-" + suffix,
        project_id="project-test",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_concurrent_registration_and_commands_are_durable_and_idempotent(repository):
    value = binding()
    first, second = await asyncio.gather(repository.register(value), repository.register(value))
    assert first.id == second.id
    request = WorkbenchCommand(
        kind="upgrade",
        idempotency_key=uuid4().hex,
        expected_revision=1,
        target_release="fixture",
        confirm_interruption=True,
    )
    one, two = await asyncio.gather(repository.command(first, request, "qa"), repository.command(first, request, "qa"))
    assert one.id == two.id and one.state == "queued"
    with pytest.raises(ConflictError, match="already active"):
        await repository.command(first, request.model_copy(update={"idempotency_key": uuid4().hex}), "qa")
    with pytest.raises(ConflictError, match="different request"):
        await repository.command(first, request.model_copy(update={"target_release": "changed"}), "qa")


@pytest.mark.asyncio
async def test_database_hold_is_authoritative_even_for_an_old_binding(repository):
    first = await repository.register(binding())
    await repository.pool.execute("UPDATE fs2_workbenches SET protected=true WHERE id=$1", first.id)
    with pytest.raises(ConflictError, match="protected"):
        await repository.command(
            first, WorkbenchCommand(kind="upgrade", idempotency_key=uuid4().hex, expected_revision=1), "qa"
        )


@pytest.mark.asyncio
async def test_failed_inventory_transaction_keeps_previous_observation(repository):
    project = "project-" + uuid4().hex
    row = {"resource_id": "aiendpoint-" + uuid4().hex, "project_id": project, "kind": "endpoint"}
    await repository.observe(project, [row])
    with pytest.raises(asyncpg.CheckViolationError):
        await repository.observe(project, [{**row, "kind": "invalid"}])
    values = [item for item in await repository.observations() if item["project_id"] == project]
    assert len(values) == 1 and values[0]["kind"] == "endpoint"
