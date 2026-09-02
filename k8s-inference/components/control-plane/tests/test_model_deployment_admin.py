from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from test_admin_access_api import (
    BOOTSTRAP_AUTH,
    _client,
    _create_principal,
    _principal_cookie,
    _runtime,
)
from test_model_deployment import model_spec

from fs2_serve.access_models import OperatorRole
from fs2_serve.api import ADMIN_SESSION_COOKIE, create_app
from fs2_serve.crypto import KeyedHasher, PayloadCipher
from fs2_serve.memory_store import MemoryStore
from fs2_serve.model_deployment_admin import (
    ModelDeploymentReadService,
    StoreModelDeploymentRepository,
)
from fs2_serve.model_deployment_records import (
    ModelDeploymentAdoptionState,
    ModelDeploymentAdoptionStatus,
    ModelDeploymentAppendRequest,
    ModelDeploymentObservedStatus,
    ModelDeploymentRevisionAction,
    ModelDeploymentRuntimePhase,
    ModelDeploymentStatusAvailability,
    ModelDeploymentStatusObservation,
)
from fs2_serve.store import ConflictError

CONTROL_ROOT = Path(__file__).resolve().parents[1]
ACTOR_ID = UUID("11111111-1111-1111-1111-111111111111")


def append_request(
    *,
    key: str,
    action: ModelDeploymentRevisionAction = ModelDeploymentRevisionAction.CREATE,
    expected_etag: str | None = None,
    tenant_id: str = "tenant-a",
    name: str = "qwen-live",
    max_replicas: int = 4,
) -> ModelDeploymentAppendRequest:
    spec = model_spec()
    spec = spec.model_copy(
        update={
            "tenant_id": tenant_id,
            "availability": spec.availability.model_copy(update={"max_replicas": max_replicas}),
        }
    )
    return ModelDeploymentAppendRequest(
        namespace="fs2-models",
        name=name,
        expected_etag=expected_etag,
        spec=spec,
        action=action,
        actor_id=ACTOR_ID,
        actor="operator@example.test",
        idempotency_key=key,
    )


def observation(
    *,
    revision: int,
    etag: str,
    tenant_id: str = "tenant-a",
    name: str = "qwen-live",
) -> ModelDeploymentStatusObservation:
    now = datetime.now(UTC)
    return ModelDeploymentStatusObservation(
        observation_id=uuid4(),
        source_uid="modeldeployment-uid-1",
        source_resource_version=str(revision),
        namespace="fs2-models",
        name=name,
        tenant_id=tenant_id,
        revision=revision,
        status=ModelDeploymentObservedStatus(
            observed_generation=revision,
            phase=ModelDeploymentRuntimePhase.COLD,
            spec_digest=etag,
            adoption=ModelDeploymentAdoptionStatus(state=ModelDeploymentAdoptionState.NONE),
            last_reconcile_time=now,
        ),
        observed_at=now,
    )


@pytest.mark.asyncio
async def test_revision_repository_is_atomic_idempotent_rotation_safe_and_audited(
    cipher: PayloadCipher,
) -> None:
    keys = {"ledger-old": b"o" * 32, "ledger-new": b"n" * 32}
    store = MemoryStore(cipher, KeyedHasher(active_key_id="ledger-old", keys=keys))
    create = append_request(key="create-qwen-0001")

    first = await store.model_deployment_append_revision(create)
    store.hasher = KeyedHasher(active_key_id="ledger-new", keys=keys)
    replay = await store.model_deployment_append_revision(create)

    assert first.value == replay.value
    assert first.value.revision == 1 and not first.reused and replay.reused
    assert len(store.audit) == 1
    assert store.audit[0].action == "model_deployment.revision.create"
    assert store.audit[0].detail["revision"] == 1
    assert create.idempotency_key not in repr(store.model_deployment_idempotency)

    conflicting = append_request(key=create.idempotency_key, max_replicas=3)
    with pytest.raises(ConflictError, match="bound to another request"):
        await store.model_deployment_append_revision(conflicting)
    assert len(store.model_deployment_revisions[("fs2-models", "qwen-live")]) == 1
    assert len(store.audit) == 1

    update = append_request(
        key="update-qwen-0002",
        action=ModelDeploymentRevisionAction.UPDATE,
        expected_etag=first.value.etag,
        max_replicas=3,
    )
    second = await store.model_deployment_append_revision(update)
    assert second.value.revision == 2 and second.value.previous_revision == 1
    assert len(store.audit) == 2

    stale = append_request(
        key="stale-qwen-0003",
        action=ModelDeploymentRevisionAction.UPDATE,
        expected_etag=first.value.etag,
        max_replicas=2,
    )
    with pytest.raises(ConflictError, match="ETag is stale"):
        await store.model_deployment_append_revision(stale)
    assert len(store.audit) == 2


@pytest.mark.asyncio
async def test_history_tenant_pagination_and_status_never_invent_current_observation(
    cipher: PayloadCipher,
    hasher: KeyedHasher,
) -> None:
    store = MemoryStore(cipher, hasher)
    first = await store.model_deployment_append_revision(append_request(key="create-qwen-0001"))
    first_observation = observation(revision=1, etag=first.value.etag)
    assert await store.model_deployment_append_status(first_observation) == first_observation
    assert await store.model_deployment_append_status(first_observation) == first_observation

    second = await store.model_deployment_append_revision(
        append_request(
            key="update-qwen-0002",
            action=ModelDeploymentRevisionAction.UPDATE,
            expected_etag=first.value.etag,
            max_replicas=3,
        )
    )
    await store.model_deployment_append_revision(
        append_request(
            key="create-cosmos-0003",
            tenant_id="tenant-b",
            name="cosmos-live",
        )
    )
    repository = StoreModelDeploymentRepository(store)
    service = ModelDeploymentReadService(repository)

    page = await service.list(
        namespace="fs2-models",
        tenant_id=None,
        after_name=None,
        limit=1,
    )
    assert [item.name for item in page.items] == ["cosmos-live"]
    assert page.next_after == "cosmos-live"
    tenant_page = await service.list(
        namespace="fs2-models",
        tenant_id="tenant-a",
        after_name=None,
        limit=10,
    )
    assert [item.name for item in tenant_page.items] == ["qwen-live"]

    history = await service.history(
        namespace="fs2-models",
        name="qwen-live",
        tenant_id="tenant-a",
        before_revision=None,
        limit=1,
    )
    assert [item.revision for item in history.items] == [2]
    assert history.next_before_revision == 2

    stale_status = await service.status(
        namespace="fs2-models",
        name="qwen-live",
        tenant_id="tenant-a",
    )
    assert stale_status.state is ModelDeploymentStatusAvailability.STALE
    assert stale_status.observation == first_observation

    second_observation = observation(revision=2, etag=second.value.etag)
    await store.model_deployment_append_status(second_observation)
    delayed = second_observation.model_copy(
        update={
            "observation_id": uuid4(),
            "observed_at": second_observation.observed_at - timedelta(seconds=1),
            "status": second_observation.status.model_copy(
                update={
                    "last_reconcile_time": second_observation.status.last_reconcile_time
                    - timedelta(seconds=1)
                }
            ),
        }
    )
    with pytest.raises(ConflictError, match="older than current status"):
        await store.model_deployment_append_status(delayed)
    with pytest.raises(ConflictError, match="older than current status"):
        await store.model_deployment_append_status(
            first_observation.model_copy(update={"observation_id": uuid4()})
        )
    current_status = await service.status(
        namespace="fs2-models",
        name="qwen-live",
        tenant_id="tenant-a",
    )
    assert current_status.state is ModelDeploymentStatusAvailability.OBSERVED
    assert current_status.observation == second_observation
    assert len(store.model_deployment_status_events[("fs2-models", "qwen-live")]) == 2


def test_authenticated_read_routes_are_tenant_scoped_and_writers_stay_unmounted(
    registry: object,
    cipher: object,
    hasher: object,
) -> None:
    default_runtime = _runtime(registry, cipher, hasher)
    with TestClient(create_app(default_runtime), base_url="https://inference.test.invalid") as client:
        assert client.get("/admin/api/v1/model-deployments").status_code == 404

    runtime = _runtime(registry, cipher, hasher)
    assert isinstance(runtime.store, MemoryStore)
    first = asyncio.run(
        runtime.store.model_deployment_append_revision(append_request(key="create-qwen-0001"))
    )
    asyncio.run(
        runtime.store.model_deployment_append_revision(
            append_request(
                key="update-qwen-0002",
                action=ModelDeploymentRevisionAction.UPDATE,
                expected_etag=first.value.etag,
                max_replicas=3,
            )
        )
    )
    asyncio.run(
        runtime.store.model_deployment_append_revision(
            append_request(
                key="create-cosmos-0003",
                tenant_id="tenant-b",
                name="cosmos-live",
            )
        )
    )
    runtime.model_deployment_read = ModelDeploymentReadService(
        StoreModelDeploymentRepository(runtime.store)
    )
    tenant_principal_id = _create_principal(
        runtime,
        role=OperatorRole.VIEWER,
        tenant_id="tenant-a",
        subject="tenant-a-model-viewer",
    )
    tenant_cookie = _principal_cookie(runtime, tenant_principal_id)
    tenant_headers = {"cookie": f"{ADMIN_SESSION_COOKIE}={tenant_cookie}"}

    with _client(runtime) as client:
        unauthenticated = client.get("/admin/api/v1/model-deployments")
        assert client.post("/admin/api/v1/session", headers=BOOTSTRAP_AUTH).status_code == 200
        global_page = client.get("/admin/api/v1/model-deployments", params={"limit": 1})
        global_history = client.get("/admin/api/v1/model-deployments/qwen-live/history")
        unavailable_status = client.get("/admin/api/v1/model-deployments/qwen-live/status")
        mutation = client.post("/admin/api/v1/model-deployments:apply", json={})
        client.cookies.clear()
        tenant_page = client.get("/admin/api/v1/model-deployments", headers=tenant_headers)
        hidden_other_tenant = client.get(
            "/admin/api/v1/model-deployments/cosmos-live",
            headers=tenant_headers,
        )
        forbidden_filter = client.get(
            "/admin/api/v1/model-deployments",
            params={"tenant_id": "tenant-b"},
            headers=tenant_headers,
        )

    assert unauthenticated.status_code == 401
    assert global_page.status_code == 200
    assert len(global_page.json()["data"]["items"]) == 1
    assert global_page.json()["data"]["next_after"] == "cosmos-live"
    assert [item["revision"] for item in global_history.json()["data"]["items"]] == [2, 1]
    assert unavailable_status.status_code == 200
    assert unavailable_status.json()["data"]["state"] == "unavailable"
    assert unavailable_status.json()["data"]["observation"] is None
    assert mutation.status_code == 404
    assert [item["name"] for item in tenant_page.json()["data"]["items"]] == ["qwen-live"]
    assert hidden_other_tenant.status_code == 404
    assert forbidden_filter.status_code == 403


def test_migration_stores_only_hmac_receipts_and_runtime_has_narrow_dml() -> None:
    migration = (CONTROL_ROOT / "migrations/0012_model_deployments.sql").read_text()
    lowered = migration.lower()
    assert "key_hmac char(64)" in lowered and "request_hmac char(64)" in lowered
    assert "idempotency_key text" not in lowered
    assert "raw idempotency keys are never persisted" in lowered

    postgres_source = (CONTROL_ROOT / "src/fs2_serve/postgres.py").read_text()
    assert "GRANT SELECT,INSERT ON fs2_model_deployment_revisions" in postgres_source
    assert "GRANT SELECT,INSERT,UPDATE ON fs2_model_deployments" in postgres_source
    assert "DELETE ON fs2_model_deployments" not in postgres_source
