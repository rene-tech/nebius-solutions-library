"""Archive-backed retirement preserves history and cannot resurrect paid Apps."""

# ruff: noqa: F811 -- imported fixture is injected by pytest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from test_model_deployment_admin import append_request
from test_model_deployment_bridge import _ready_cr
from test_postgres_integration import (
    postgres_store,  # noqa: F401
    scientific_runtime_pool,  # noqa: F401
)

from fs2_serve.apps import default_app_id
from fs2_serve.apps_models import AppRecord
from fs2_serve.apps_repository import PostgresAppsRepository
from fs2_serve.auth import TokenService
from fs2_serve.model_deployment import DesiredState
from fs2_serve.model_deployment_admin import StoreModelDeploymentRepository
from fs2_serve.model_deployment_mutation import DesiredWriteError, HttpKubernetesDesiredWriter
from fs2_serve.model_deployment_records import ModelDeploymentRevisionAction
from fs2_serve.model_retirement import ModelRetirementRequest, retire_model
from fs2_serve.models import AdmissionRequest, Scope, TokenCreate
from fs2_serve.store import ConflictError


async def drained(store):
    create = append_request(key="retirement-create-" + uuid4().hex)
    first = (await store.model_deployment_append_revision(create)).value
    update = append_request(
        key="retirement-drain-" + uuid4().hex, action=ModelDeploymentRevisionAction.UPDATE, expected_etag=first.etag
    )
    spec = update.spec.model_copy(
        update={"lifecycle": update.spec.lifecycle.model_copy(update={"desired_state": DesiredState.DRAINING})}
    )
    last = (await store.model_deployment_append_revision(update.model_copy(update={"spec": spec}))).value
    return create, last


class ObservedCold:
    def __init__(self):
        self.checked = []

    async def check_retirement(self, revision):
        self.checked.append(revision)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_retirement_refuses_active_operations_even_with_cold_observation(postgres_store, pepper):
    _, revision = await drained(postgres_store)
    tokens = TokenService(postgres_store, pepper)
    issued = await tokens.issue(
        TokenCreate(
            principal_id="retirement-test",
            tenant_id="tenant-a",
            scopes={Scope.INFERENCE_INVOKE},
            models={revision.spec.public_model_id},
            max_concurrency=1,
            request_budget=2,
            gpu_seconds_budget=100,
            name="retirement fixture",
        ),
        created_by="test-admin",
    )
    principal = await tokens.verify(issued.token)
    await postgres_store.append_operation(
        principal=principal,
        admission=AdmissionRequest(
            model_id=revision.spec.public_model_id,
            operation="chat",
            protocol="openai-chat",
            idempotency_key="retirement-active-operation",
            request_body=b"{}",
        ),
        model_revision="fixture",
        reserved_gpu_seconds=1,
        max_attempts=1,
    )
    writer = ObservedCold()
    service = SimpleNamespace(
        repository=StoreModelDeploymentRepository(postgres_store), namespace=revision.namespace, writer=writer
    )
    with pytest.raises(ConflictError, match="active operations"):
        await retire_model(
            service,
            revision.name,
            ModelRetirementRequest(expected_etag=revision.etag, archive_sha256="a" * 64),
            "test-admin",
        )
    assert writer.checked == []
    assert await service.repository.current(namespace=revision.namespace, name=revision.name, tenant_id=None)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_retirement_hides_active_app_preserves_history_and_blocks_replay(postgres_store):
    create, revision = await drained(postgres_store)
    apps = PostgresAppsRepository(postgres_store.pool)
    now = datetime.now(UTC)
    app = AppRecord(
        app_id=default_app_id(revision.spec.public_model_id),
        display_name="Temporary reference App",
        model_ref=revision.spec.model_ref,
        public_model_id=revision.spec.public_model_id,
        execution_mode="serving",
        namespace=revision.namespace,
        deployment_name=revision.name,
        created_at=now,
        updated_at=now,
    )
    await apps.seed(app)
    await postgres_store.model_deployment_append_revision(
        append_request(key="retirement-unrelated", name="unrelated-model")
    )
    repo = StoreModelDeploymentRepository(postgres_store)
    writer = ObservedCold()
    service = SimpleNamespace(repository=repo, namespace=revision.namespace, writer=writer)
    request = ModelRetirementRequest(expected_etag=revision.etag, archive_sha256="b" * 64)
    first = await retire_model(service, revision.name, request, "test-admin")
    again = await retire_model(service, revision.name, request, "test-admin")
    assert first["retired_at"] == again["retired_at"] and len(writer.checked) == 1
    assert await repo.current(namespace=revision.namespace, name=revision.name, tenant_id=None) is None
    assert [
        item.name
        for item in await repo.list_current(namespace=revision.namespace, tenant_id=None, after_name=None, limit=100)
    ] == ["unrelated-model"]
    assert await repo.retired(revision.namespace) == [revision]
    assert await apps.get(app.app_id) is None
    assert app.app_id not in {item.app_id for item in await apps.list_records()}
    assert app.app_id not in {item.app_id for item in await apps.list_discoverable_records()}
    assert revision.spec.public_model_id in await apps.retired_public_model_ids()
    history = await repo.history(
        namespace=revision.namespace, name=revision.name, tenant_id=None, before_revision=None, limit=100
    )
    assert [item.revision for item in history] == [2, 1]
    with pytest.raises(ConflictError, match="retired"):
        await postgres_store.model_deployment_append_revision(create)
    with pytest.raises(ConflictError, match="different archive"):
        await retire_model(
            service, revision.name, request.model_copy(update={"archive_sha256": "c" * 64}), "test-admin"
        )
    assert (
        await postgres_store.pool.fetchval(
            "SELECT count(*) FROM fs2_audit_events WHERE action='model_deployment.retire'"
        )
        == 1
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_retirement_rejects_enabled_changed_or_unobserved_models(postgres_store):
    create = append_request(key="enabled-retirement-test")
    first = (await postgres_store.model_deployment_append_revision(create)).value
    service = SimpleNamespace(
        repository=StoreModelDeploymentRepository(postgres_store), namespace=first.namespace, writer=ObservedCold()
    )
    request = ModelRetirementRequest(expected_etag=first.etag, archive_sha256="d" * 64)
    with pytest.raises(ConflictError, match="drain"):
        await retire_model(service, first.name, request, "test-admin")
    with pytest.raises(ConflictError, match="changed"):
        await retire_model(
            service, first.name, request.model_copy(update={"expected_etag": "sha256:" + "e" * 64}), "test-admin"
        )
    assert await service.repository.current(namespace=first.namespace, name=first.name, tenant_id=None) == first
    assert service.writer.checked == []


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_retirement_uses_runtime_role_and_failed_observation_leaves_no_tombstone(
    postgres_store, scientific_runtime_pool
):
    _, revision = await drained(postgres_store)

    class NotCold:
        async def check_retirement(self, revision):
            raise DesiredWriteError("fresh observation required")

    runtime_store = SimpleNamespace(pool=scientific_runtime_pool)
    service = SimpleNamespace(
        repository=SimpleNamespace(store=runtime_store), namespace=revision.namespace, writer=NotCold()
    )
    request = ModelRetirementRequest(expected_etag=revision.etag, archive_sha256="f" * 64)
    with pytest.raises(DesiredWriteError, match="fresh"):
        await retire_model(service, revision.name, request, "runtime-admin")
    assert await postgres_store.pool.fetchval(
        "SELECT retired_at IS NULL FROM fs2_model_deployments WHERE name=$1", revision.name
    )
    service.writer = ObservedCold()
    result = await retire_model(service, revision.name, request, "runtime-admin")
    assert result["history_retained"]
    assert (
        await postgres_store.pool.fetchval("SELECT retired_by FROM fs2_model_deployments WHERE name=$1", revision.name)
        == "runtime-admin"
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_kubernetes_retirement_requires_cold_zero_and_uid_preconditions(postgres_store, tmp_path):
    _, revision = await drained(postgres_store)
    cr = _ready_cr(revision)
    cr["status"].update(
        phase="Cold", replicas={"desired": 0, "ready": 0, "available": 0}, publication={"mcp": False, "openAI": False}
    )
    token = tmp_path / "token"
    token.write_text("test-kubernetes-token-not-live")
    requests = []
    present = True
    pod_items = []

    def handler(request):
        nonlocal present
        requests.append(request)
        if request.url.path.endswith("/pods"):
            assert (
                request.url.params["labelSelector"] == "fs2-serve.nebius.ai/model-id=" + revision.spec.public_model_id
            )
            return httpx.Response(200, json={"items": pod_items})
        if request.method == "DELETE":
            import json

            assert json.loads(request.content)["preconditions"] == {"uid": "model-uid-1", "resourceVersion": "9"}
            present = False
            return httpx.Response(200, json={})
        return httpx.Response(200, json=cr) if present else httpx.Response(404, json={})

    async with httpx.AsyncClient(base_url="https://kubernetes.test", transport=httpx.MockTransport(handler)) as client:
        writer = HttpKubernetesDesiredWriter(
            base_url="https://kubernetes.test",
            token_file=token,
            ca_file=tmp_path / "unused",
            namespace=revision.namespace,
            client=client,
        )
        cr["status"]["replicas"]["ready"] = 1
        with pytest.raises(DesiredWriteError, match="zero-replica"):
            await writer.check_retirement(revision)
        cr["status"]["replicas"]["ready"] = 0
        cr["status"]["lastReconcileTime"] = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        with pytest.raises(DesiredWriteError, match="fresh"):
            await writer.check_retirement(revision)
        cr["status"]["lastReconcileTime"] = datetime.now(UTC).isoformat()
        pod_items.append({"status": {"phase": "Running"}})
        with pytest.raises(DesiredWriteError, match="nonterminal Pods"):
            await writer.check_retirement(revision)
        pod_items.clear()
        await writer.check_retirement(revision)
        await writer.retire(revision)
        await writer.retire(revision)
        assert sum(request.method == "DELETE" for request in requests) == 1
