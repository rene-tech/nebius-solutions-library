from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from test_model_deployment import digest
from test_model_deployment_admin import append_request

from fs2_serve.crypto import KeyedHasher, PayloadCipher
from fs2_serve.memory_store import MemoryStore
from fs2_serve.model_deployment_admin import StoreModelDeploymentRepository
from fs2_serve.model_deployment_bridge import ModelDeploymentRuntimeBridge
from fs2_serve.model_deployment_mutation import DesiredWriteError, DesiredWriteReceipt
from fs2_serve.model_deployment_records import ModelDeploymentAppendRequest
from fs2_serve.registry import Registry


class FakeKubernetes:
    def __init__(self) -> None:
        self.models: list[dict[str, object]] = []
        self.fail_list = False
        self.fail_apply = False
        self.applied: list[object] = []

    async def list_models(self) -> list[dict[str, object]]:
        if self.fail_list:
            raise DesiredWriteError("private list error")
        return self.models

    async def apply(self, revision: object) -> DesiredWriteReceipt:
        self.applied.append(revision)
        if self.fail_apply:
            raise DesiredWriteError("private apply error")
        value = revision
        return DesiredWriteReceipt(
            namespace=value.namespace,
            name=value.name,
            uid="uid-1",
            resource_version="1",
            generation=value.revision,
            spec_digest=value.etag,
        )


def _ready_cr(revision: object) -> dict[str, object]:
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return {
        "apiVersion": "inference.fs2.nebius.ai/v1alpha1",
        "kind": "ModelDeployment",
        "metadata": {
            "namespace": revision.namespace,
            "name": revision.name,
            "uid": "model-uid-1",
            "resourceVersion": "9",
            "generation": revision.revision,
            "annotations": {
                "inference.fs2.nebius.ai/desired-revision": str(revision.revision),
                "inference.fs2.nebius.ai/spec-digest": revision.etag,
            },
        },
        "spec": revision.spec.model_dump(mode="json", by_alias=True),
        "status": {
            "observedGeneration": revision.revision,
            "phase": "Ready",
            "specDigest": revision.etag,
            "renderDigest": digest("d"),
            "activeRevision": revision.spec.artifact.revision,
            "admittedPoolRef": "h100-preemptible",
            "replicas": {"desired": 1, "ready": 1, "available": 1},
            "endpoint": {
                "namespace": revision.namespace,
                "serviceName": "qwen-event",
                "servicePort": 8000,
                "uid": "service-uid-1",
                "digest": digest("e"),
            },
            "retryCount": 0,
            "lastReconcileTime": now,
            "conditions": [
                {
                    "type": "Ready",
                    "status": "True",
                    "observedGeneration": revision.revision,
                    "reason": "RuntimeObservedReady",
                    "message": "runtime is ready",
                    "lastTransitionTime": now,
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_bridge_persists_status_and_publishes_exact_ready_service(registry: Registry) -> None:
    store = MemoryStore(
        PayloadCipher(active_key_id="payload", keys={"payload": b"p" * 32}),
        KeyedHasher(active_key_id="ledger", keys={"ledger": b"h" * 32}),
    )
    request = append_request(key="create-qwen-0001")
    base = registry.get("qwen3-8b", require_enabled=False)
    assert base.gateway.model_revision is not None
    assert base.binding.artifact_manifest_digest is not None
    assert base.binding.backend_runtime_image_digest is not None
    spec = request.spec.model_copy(
        update={
            "model_ref": "qwen3-8b",
            "artifact": request.spec.artifact.model_copy(
                update={
                    "revision": base.gateway.model_revision,
                    "manifest_digest": f"sha256:{base.binding.artifact_manifest_digest}",
                }
            ),
            "runtime": request.spec.runtime.model_copy(
                update={"image": f"registry.example/fs2/runtime@{base.binding.backend_runtime_image_digest}"}
            ),
        }
    )
    request = ModelDeploymentAppendRequest(
        **{
            **request.model_dump(),
            "spec": spec,
            "actor_id": UUID("11111111-1111-1111-1111-111111111111"),
        }
    )
    revision = (await store.model_deployment_append_revision(request)).value
    kubernetes = FakeKubernetes()
    kubernetes.models = [_ready_cr(revision)]
    bridge = ModelDeploymentRuntimeBridge(
        repository=StoreModelDeploymentRepository(store),
        writer=kubernetes,
        source=kubernetes,
        registry=registry,
        interval_seconds=5,
        route_ttl_seconds=30,
    )

    assert await bridge.refresh(force=True)
    route = registry.get("qwen3-8b")
    assert route.binding.backend_service_name == "qwen-event"
    assert route.binding.ready
    persisted = await store.model_deployment_status(
        namespace=revision.namespace,
        name=revision.name,
        tenant_id=revision.tenant_id,
    )
    assert persisted is not None
    assert persisted.status.endpoint is not None
    assert persisted.status.endpoint.uid == "service-uid-1"
    assert kubernetes.applied == []
    assert bridge.health()["publication"] == {
        "state": "ready",
        "error": None,
        "rejected_count": 0,
        "rejections": [],
        "rejections_truncated": False,
    }

    # Stable Kubernetes evidence is idempotent in the append-only status log.
    assert await bridge.refresh(force=True)
    assert len(store.model_deployment_status_events[(revision.namespace, revision.name)]) == 1
    assert registry.get("qwen3-8b").binding.backend_service_name == "qwen-event"


@pytest.mark.asyncio
async def test_bridge_reapplies_drift_and_withdraws_when_kubernetes_is_unavailable(registry: Registry) -> None:
    store = MemoryStore(
        PayloadCipher(active_key_id="payload", keys={"payload": b"p" * 32}),
        KeyedHasher(active_key_id="ledger", keys={"ledger": b"h" * 32}),
    )
    request = append_request(key="create-qwen-0001")
    spec = request.spec.model_copy(update={"model_ref": "qwen3-8b"})
    request = request.model_copy(update={"spec": spec})
    revision = (await store.model_deployment_append_revision(request)).value
    kubernetes = FakeKubernetes()
    bridge = ModelDeploymentRuntimeBridge(
        repository=StoreModelDeploymentRepository(store),
        writer=kubernetes,
        source=kubernetes,
        registry=registry,
        interval_seconds=5,
        route_ttl_seconds=30,
    )

    assert await bridge.refresh(force=True)
    assert kubernetes.applied == [revision]
    with pytest.raises(RuntimeError, match="not routable"):
        registry.get("qwen3-8b")

    kubernetes.fail_list = True
    assert not await bridge.refresh(force=True)
    assert bridge.health()["error"] == "kubernetes-list-unavailable"
    with pytest.raises(RuntimeError, match="not routable"):
        registry.get("qwen3-8b")


@pytest.mark.asyncio
async def test_bridge_never_publishes_ready_status_from_a_drifted_spec(registry: Registry) -> None:
    store = MemoryStore(
        PayloadCipher(active_key_id="payload", keys={"payload": b"p" * 32}),
        KeyedHasher(active_key_id="ledger", keys={"ledger": b"h" * 32}),
    )
    request = append_request(key="create-qwen-0001")
    request = request.model_copy(update={"spec": request.spec.model_copy(update={"model_ref": "qwen3-8b"})})
    revision = (await store.model_deployment_append_revision(request)).value
    drifted = _ready_cr(revision)
    drifted_spec = dict(drifted["spec"])
    drifted_spec["modelRef"] = "another-model"
    drifted["spec"] = drifted_spec
    kubernetes = FakeKubernetes()
    kubernetes.models = [drifted]
    kubernetes.fail_apply = True
    bridge = ModelDeploymentRuntimeBridge(
        repository=StoreModelDeploymentRepository(store),
        writer=kubernetes,
        source=kubernetes,
        registry=registry,
        interval_seconds=5,
        route_ttl_seconds=30,
    )

    assert await bridge.refresh(force=True)
    assert bridge.health()["error"] == "desired-projection-pending"
    assert bridge.health()["route_inventory_fresh"] is True
    with pytest.raises(RuntimeError, match="not routable"):
        registry.get("qwen3-8b")
    assert not store.model_deployment_status_events.get((revision.namespace, revision.name))


@pytest.mark.asyncio
async def test_bridge_does_not_refresh_a_route_from_stale_ready_status(registry: Registry) -> None:
    store = MemoryStore(
        PayloadCipher(active_key_id="payload", keys={"payload": b"p" * 32}),
        KeyedHasher(active_key_id="ledger", keys={"ledger": b"h" * 32}),
    )
    request = append_request(key="create-qwen-stale-0001")
    base = registry.get("qwen3-8b", require_enabled=False)
    assert base.gateway.model_revision is not None
    assert base.binding.artifact_manifest_digest is not None
    assert base.binding.backend_runtime_image_digest is not None
    spec = request.spec.model_copy(
        update={
            "model_ref": "qwen3-8b",
            "artifact": request.spec.artifact.model_copy(
                update={
                    "revision": base.gateway.model_revision,
                    "manifest_digest": f"sha256:{base.binding.artifact_manifest_digest}",
                }
            ),
            "runtime": request.spec.runtime.model_copy(
                update={"image": f"registry.example/fs2/runtime@{base.binding.backend_runtime_image_digest}"}
            ),
        }
    )
    revision = (
        await store.model_deployment_append_revision(
            request.model_copy(update={"spec": spec, "actor_id": UUID("11111111-1111-1111-1111-111111111111")})
        )
    ).value
    stale = _ready_cr(revision)
    stale_at = (datetime.now(UTC) - timedelta(seconds=31)).isoformat().replace("+00:00", "Z")
    stale["status"]["lastReconcileTime"] = stale_at
    stale["status"]["conditions"][0]["lastTransitionTime"] = stale_at
    kubernetes = FakeKubernetes()
    kubernetes.models = [stale]
    bridge = ModelDeploymentRuntimeBridge(
        repository=StoreModelDeploymentRepository(store),
        writer=kubernetes,
        source=kubernetes,
        registry=registry,
        interval_seconds=5,
        route_ttl_seconds=30,
    )

    assert await bridge.refresh(force=True)
    assert bridge.health()["route_inventory_fresh"] is True
    with pytest.raises(RuntimeError, match="not routable"):
        registry.get("qwen3-8b")
    assert not store.model_deployment_status_events.get((revision.namespace, revision.name))


@pytest.mark.asyncio
async def test_pending_repair_does_not_block_a_healthy_model_route_inventory(registry: Registry) -> None:
    store = MemoryStore(
        PayloadCipher(active_key_id="payload", keys={"payload": b"p" * 32}),
        KeyedHasher(active_key_id="ledger", keys={"ledger": b"h" * 32}),
    )
    healthy_request = append_request(key="create-qwen-healthy-0001")
    base = registry.get("qwen3-8b", require_enabled=False)
    assert base.gateway.model_revision is not None
    assert base.binding.artifact_manifest_digest is not None
    assert base.binding.backend_runtime_image_digest is not None
    healthy_spec = healthy_request.spec.model_copy(
        update={
            "model_ref": "qwen3-8b",
            "artifact": healthy_request.spec.artifact.model_copy(
                update={
                    "revision": base.gateway.model_revision,
                    "manifest_digest": f"sha256:{base.binding.artifact_manifest_digest}",
                }
            ),
            "runtime": healthy_request.spec.runtime.model_copy(
                update={"image": f"registry.example/fs2/runtime@{base.binding.backend_runtime_image_digest}"}
            ),
        }
    )
    healthy = (
        await store.model_deployment_append_revision(healthy_request.model_copy(update={"spec": healthy_spec}))
    ).value
    pending_request = append_request(key="create-pending-0001", name="pending-live")
    pending_model_id = next(item.id for item in registry.list() if item.id != "qwen3-8b")
    pending_spec = pending_request.spec.model_copy(update={"model_ref": pending_model_id})
    pending = (
        await store.model_deployment_append_revision(pending_request.model_copy(update={"spec": pending_spec}))
    ).value
    pending_cr = _ready_cr(pending)
    pending_cr["spec"] = dict(pending_cr["spec"])
    pending_cr["spec"]["modelRef"] = "drifted-model"
    kubernetes = FakeKubernetes()
    kubernetes.models = [_ready_cr(healthy), pending_cr]
    kubernetes.fail_apply = True
    bridge = ModelDeploymentRuntimeBridge(
        repository=StoreModelDeploymentRepository(store),
        writer=kubernetes,
        source=kubernetes,
        registry=registry,
        interval_seconds=5,
        route_ttl_seconds=30,
    )

    assert await bridge.refresh(force=True)
    assert await bridge.refresh()
    assert bridge.health()["error"] == "desired-projection-pending"
    assert bridge.health()["route_inventory_fresh"] is True
    assert registry.get("qwen3-8b").binding.backend_service_name == "qwen-event"
