from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_users import user_env  # noqa: F401 -- pytest fixture registration

from fs2_serve.access_models import OperatorRole
from fs2_serve.models import TokenCreate
from fs2_serve.store import ConflictError
from fs2_serve.workbench_inventory import WorkbenchInventoryWorker, endpoint_observation
from fs2_serve.workbench_models import WorkbenchCommand, WorkbenchRegistration
from fs2_serve.workbench_repository import MemoryWorkbenchRepository
from fs2_serve.workbenches import WorkbenchService, observed_state


@pytest.fixture
async def fleet(request):
    env = request.getfixturevalue("user_env")
    await env.tokens.issue(
        TokenCreate(tenant_id="tenant-a", principal_id="alice", models={"*"}, scopes={"inference.invoke"}),
        created_by="qa",
    )
    repository = MemoryWorkbenchRepository()
    repository.storage_values = [
        {
            "tenant_id": "tenant-a",
            "owner_key": "",
            "bucket_name": "fs2-tenant-a-1",
            "bucket_id": "bucket-1",
            "quota_bytes": 5_000_000_000,
        }
    ]
    repository.observation_values = [
        {
            "kind": "endpoint",
            "project_id": "project-test",
            "resource_id": "aiendpoint-test",
            "is_workbench": True,
            "name": "client",
            "state": "RUNNING",
            "buckets": ["fs2-tenant-a-1"],
            "observed_at": datetime.now(UTC),
        }
    ]
    return env, WorkbenchService(repository, env.users)


def registration(**kwargs):
    return WorkbenchRegistration(
        tenant_id="tenant-a",
        principal_ids=["alice"],
        name="Client",
        endpoint_id="aiendpoint-test",
        project_id="project-test",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_adoption_reuses_endpoint_without_cloud_mutation(fleet):
    env, service = fleet
    first = await service.register(env.operator, registration())
    second = await service.register(env.operator, registration())
    assert first.id == second.id
    assert len(service.repository.binding_values) == 1
    assert first.bucket_name == "fs2-tenant-a-1"


@pytest.mark.asyncio
async def test_protected_customer_cannot_be_upgraded_or_retired(fleet):
    env, service = fleet
    workbench = await service.register(env.operator, registration(protected=True, protection_reason="Owner hold"))
    service.executor_enabled = True
    for action in ("backup", "upgrade", "restore", "retire"):
        with pytest.raises(ConflictError, match="protected"):
            await service.command(
                env.operator,
                workbench.id,
                WorkbenchCommand(kind=action, idempotency_key="protected-test", expected_revision=1),
            )


@pytest.mark.asyncio
async def test_customer_bucket_cannot_be_adopted_by_another_owner(fleet):
    env, service = fleet
    service.repository.observation_values[0]["buckets"] = ["other-tenant"]
    with pytest.raises(ConflictError, match="workspace"):
        await service.register(env.operator, registration())
    assert not service.repository.binding_values


@pytest.mark.asyncio
async def test_register_requires_fresh_complete_observation(fleet):
    env, service = fleet
    service.repository.observation_values[0]["observed_at"] -= timedelta(hours=1)
    with pytest.raises(ConflictError, match="fresh"):
        await service.register(env.operator, registration())


@pytest.mark.asyncio
async def test_disabled_executor_does_not_accept_fake_operations(fleet):
    env, service = fleet
    workbench = await service.register(env.operator, registration())
    with pytest.raises(ConflictError, match="no operation was queued"):
        await service.command(
            env.operator,
            workbench.id,
            WorkbenchCommand(kind="upgrade", idempotency_key="upgrade-1", expected_revision=1),
        )


@pytest.mark.asyncio
async def test_customer_list_is_tenant_scoped_and_keeps_missing_data_unknown(fleet):
    env, service = fleet
    service.repository.storage_values.append({"tenant_id": "tenant-b", "owner_key": "", "bucket_id": "bucket-2"})
    viewer = env.operator.model_copy(update={"tenant_id": "tenant-a", "role": OperatorRole.VIEWER})
    result = await service.customers(viewer, env.context)
    assert [item["tenant_id"] for item in result["items"]] == ["tenant-a"]
    assert result["items"][0]["buckets"][0]["observation"] is None
    assert result["items"][0]["buckets"][0]["observation_state"] == "unavailable"


@pytest.mark.asyncio
async def test_failed_inventory_preserves_last_snapshot(fleet):
    _, service = fleet
    before = list(service.repository.observation_values)

    async def fail():
        raise TimeoutError("a provider error might contain secrets")

    worker = WorkbenchInventoryWorker(service.repository, SimpleNamespace(project_id="project-test", snapshot=fail))
    with pytest.raises(TimeoutError):
        await worker.refresh()
    assert service.repository.observation_values == before
    assert worker.last_error == "TimeoutError"


def test_endpoint_projection_never_returns_environment_or_secret_selectors():
    endpoint = SimpleNamespace(
        metadata=SimpleNamespace(id="aiendpoint-test", parent_id="project-test", name="client"),
        spec=SimpleNamespace(
            image="registry/lc@sha256:abc",
            platform="cpu-d3",
            preset="4vcpu-16gb",
            environment_variables={"TOKEN": "must-not-leak"},
            volumes=[SimpleNamespace(source="s3://customer-data")],
        ),
        status=SimpleNamespace(state="RUNNING", public_endpoints=["1.2.3.4:3080", "https://client.example.test"]),
    )
    value = endpoint_observation(endpoint)
    assert value.is_workbench and value.url == "https://client.example.test"
    assert "must-not-leak" not in value.model_dump_json()
    assert "environment" not in value.model_dump_json()


def test_unknown_and_stale_are_not_reported_as_running():
    assert observed_state(None) == "unavailable"
    assert observed_state({"observed_at": datetime.now(UTC) - timedelta(hours=1)}) == "stale"
