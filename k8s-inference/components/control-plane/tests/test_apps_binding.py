"""Regression for Apps seeded before managed ModelDeployment bootstrap."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_admin_access_api import BOOTSTRAP_AUTH, _client, _runtime
from test_apps import _context, _record
from test_dynamic_routes import _revision
from test_model_deployment import envelope, renderer
from test_model_deployment_mutation import FakeWriter
from test_users_apps_postgres import database as database

from fs2_serve.admin import AdminReadService
from fs2_serve.apps import AppsService, default_app_id
from fs2_serve.apps_repository import AppConflictError, MemoryAppsRepository, PostgresAppsRepository
from fs2_serve.memory_store import MemoryStore
from fs2_serve.model_deployment import AppDeploymentIdentity, spec_digest
from fs2_serve.model_deployment_admin import StoreModelDeploymentRepository
from fs2_serve.model_deployment_mutation import ModelDeploymentMutationService
from fs2_serve.model_deployment_records import ModelDeploymentAppendRequest, ModelDeploymentRevisionAction


def append_request(revision):
    return ModelDeploymentAppendRequest(
        namespace=revision.namespace,
        name=revision.name,
        expected_etag=None,
        spec=revision.spec,
        action=ModelDeploymentRevisionAction.CREATE,
        actor_id=uuid4(),
        actor="test-bootstrap",
        idempotency_key=f"late-bootstrap-{uuid4()}",
    )


def unbound_record(model="qwen3-8b"):
    return _record(public_id=model, name=None).model_copy(update={"model_ref": model})


@pytest.mark.asyncio
async def test_existing_default_app_is_backfilled_on_read_without_reseed_or_metadata_reset(registry, cipher, hasher):
    store = MemoryStore(cipher, hasher)
    repository = MemoryAppsRepository()
    deployments = StoreModelDeploymentRepository(store)
    service = AppsService(
        repository=repository,
        registry=registry,
        admin=AdminReadService(store=store, registry=registry),
        deployments=SimpleNamespace(repository=deployments),
    )
    await service.seed_defaults()
    original = await repository.get(default_app_id("qwen3-8b"))
    assert original.deployment_name is None
    edited = await repository.update(
        original.model_copy(
            update={
                "display_name": "Customer title",
                "academic_required": True,
            }
        ),
        expected_revision=1,
    )
    before = await service.settings(original.app_id, _context())
    assert before.serving is None and not before.capabilities.live_settings
    revision = _revision(registry)
    await deployments.append_revision(append_request(revision))
    settings = await service.settings(original.app_id, _context())
    assert settings.serving.name == revision.name
    assert settings.capabilities.live_settings and settings.capabilities.duplicate
    bound = await repository.get(original.app_id)
    assert bound.app_id == edited.app_id and bound.display_name == edited.display_name
    assert bound.academic_required and bound.created_at == edited.created_at
    assert bound.revision == edited.revision + 1
    # A listing can hold an older record while another API replica attaches it.
    summary = await service.summary(original, _context(), None)
    assert summary.deployment_name == revision.name and summary.capabilities.live_settings
    assert summary.display_name == "Customer title"
    await service.seed_defaults()
    assert (await repository.get(original.app_id)).revision == bound.revision


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["source", "public", "namespace"])
async def test_default_binding_cannot_adopt_a_foreign_identity(registry, cipher, hasher, mismatch):
    repository = MemoryAppsRepository()
    record = await repository.seed(unbound_record())
    store = MemoryStore(cipher, hasher)
    service = AppsService(
        repository=repository,
        registry=registry,
        admin=AdminReadService(store=store, registry=registry),
        deployments=SimpleNamespace(repository=StoreModelDeploymentRepository(store)),
    )
    revision = _revision(registry)
    if mismatch == "namespace":
        revision = revision.model_copy(update={"namespace": "other"})
    else:
        spec = revision.spec
        if mismatch == "source":
            spec = spec.model_copy(update={"model_ref": "other-model"})
        else:
            app_id = uuid4()
            identity = AppDeploymentIdentity(app_id=app_id, public_model_id=f"app-{app_id.hex}")
            spec = spec.model_copy(update={"app": identity})
        revision = revision.model_copy(update={"spec": spec, "etag": spec_digest(spec)})
    assert await service._attach_default_deployment(record, revision=revision) == record
    assert (await repository.get(record.app_id)).deployment_name is None


def test_real_apps_settings_endpoint_recovers_bootstrap_after_api_start(registry, cipher, hasher):
    runtime = _runtime(registry, cipher, hasher)
    runtime.model_deployment_mutation = ModelDeploymentMutationService(
        repository=StoreModelDeploymentRepository(runtime.store),
        writer=FakeWriter(),
        envelope=envelope(),
        renderer=renderer(),
        prometheus_server_address="http://prometheus.fs2-observability.svc:9090",
    )
    with _client(runtime) as client:
        assert client.post("/admin/api/v1/session", headers=BOOTSTRAP_AUTH).status_code == 200
        path = f"/admin/api/v1/apps/{default_app_id('qwen3-8b')}/settings"
        before = client.get(path).json()["data"]
        assert before["serving"] is None and not before["capabilities"]["live_settings"]
        revision = _revision(registry)
        client.portal.call(runtime.model_deployment_mutation.repository.append_revision, append_request(revision))
        response = client.get(path)
        assert response.status_code == 200, response.text
        after = response.json()["data"]
        assert after["serving"]["name"] == revision.name
        assert after["capabilities"]["live_settings"] and after["app_id"] == before["app_id"]
        assert after["app_revision"] == before["app_revision"] + 1
        listing = client.get("/admin/api/v1/apps").json()["data"]["items"]
        listed = next(item for item in listing if item["app_id"] == before["app_id"])
        assert listed["deployment_name"] == revision.name and listed["capabilities"]["live_settings"]
        assert client.get(path).json()["data"]["app_revision"] == after["app_revision"]


@pytest.mark.postgres
async def test_actual_postgres_attach_is_atomic_preserves_edits_and_fences_rebinding(database):  # noqa: F811
    repository = PostgresAppsRepository(database.pool)
    original = await repository.seed(unbound_record())
    edited = await repository.update(
        original.model_copy(
            update={
                "display_name": "Retained operator title",
                "academic_required": True,
            }
        ),
        expected_revision=1,
    )
    binding = original.model_copy(update={"updated_at": datetime.now(UTC)})
    results = await asyncio.gather(*(repository.attach_deployment(binding, name="late-bootstrap") for _ in range(6)))
    assert all(item.revision == 3 and item.deployment_name == "late-bootstrap" for item in results)
    current = await repository.get(original.app_id)
    assert current.display_name == edited.display_name and current.academic_required
    assert current.app_id == original.app_id and current.created_at == original.created_at
    with pytest.raises(AppConflictError):
        await repository.attach_deployment(binding, name="other-deployment")
    with pytest.raises(AppConflictError):
        await repository.attach_deployment(
            binding.model_copy(update={"model_ref": "other-source"}), name="late-bootstrap"
        )
    assert (await repository.get(original.app_id)).revision == 3
    assert await database.pool.fetchval("SELECT count(*) FROM fs2_apps") == 1
    assert await database.pool.fetchval("SELECT count(*) FROM fs2_operations") == 0


@pytest.mark.postgres
async def test_actual_postgres_apps_read_binds_later_durable_bootstrap(database, registry):  # noqa: F811
    repository = PostgresAppsRepository(database.pool)
    namespace = f"models-{uuid4().hex}"
    original = await repository.seed(unbound_record().model_copy(update={"namespace": namespace}))
    deployments = StoreModelDeploymentRepository(database)
    service = AppsService(
        repository=repository,
        registry=registry,
        admin=AdminReadService(store=database, registry=registry),
        deployments=SimpleNamespace(repository=deployments),
    )
    assert (await service.settings(original.app_id, _context())).serving is None
    revision = _revision(registry).model_copy(update={"name": f"late-{uuid4().hex}", "namespace": namespace})
    await deployments.append_revision(append_request(revision))
    results = await asyncio.gather(*(service.settings(original.app_id, _context()) for _ in range(4)))
    assert all(item.serving.name == revision.name and item.capabilities.live_settings for item in results)
    assert (await repository.get(original.app_id)).revision == 2
