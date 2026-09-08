"""Ready app Deployments may have Kubernetes-valid names longer than DNS labels."""

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from test_dynamic_routes import _revision
from test_model_deployment_bridge import FakeKubernetes, _ready_cr

from fs2_serve.memory_store import MemoryStore
from fs2_serve.model_deployment import AppDeploymentIdentity, spec_digest, startup_retention_promql
from fs2_serve.model_deployment_admin import StoreModelDeploymentRepository
from fs2_serve.model_deployment_bridge import ModelDeploymentRuntimeBridge, _normalize_keys
from fs2_serve.model_deployment_records import (
    ModelDeploymentAppendRequest,
    ModelDeploymentEndpointStatus,
    ModelDeploymentObservedStatus,
    ModelDeploymentPlacementStatus,
    ModelDeploymentResourceStatus,
    ModelDeploymentRevisionAction,
)

APP_ID = UUID("96c9e1e5-0d82-4532-a2e0-0218cd2a99e0")
PUBLIC_ID = "app-" + APP_ID.hex
SERVICE = PUBLIC_ID + "-8d61f100652a"
DEPLOYMENT = SERVICE + "-hot-h100-reserved-8x"
DIGEST = "sha256:" + "a" * 64


def placement(name):
    return ModelDeploymentPlacementStatus(
        deployment_name=name, pool_ref="h100-reserved-8x", role="hot", desired=1, ready=1, available=1
    )


def resource(name):
    return ModelDeploymentResourceStatus(
        identity=f"apps/v1/Deployment/fs2-models/{name}",
        api_version="apps/v1",
        kind="Deployment",
        namespace="fs2-models",
        name=name,
        uid="runtime-uid",
        generation=1,
        digest=DIGEST,
    )


@pytest.mark.asyncio
async def test_live_70_character_ready_app_status_is_persisted_and_published_without_renaming(registry, cipher, hasher):
    source = _revision(registry)
    spec = source.spec.model_copy(
        update={
            "app": AppDeploymentIdentity(app_id=APP_ID, public_model_id=PUBLIC_ID),
            "exposure": source.spec.exposure.model_copy(
                update={"mcp_tool_name": "app_" + APP_ID.hex, "open_ai_aliases": []}
            ),
        }
    )
    store = MemoryStore(cipher, hasher)
    revision = (
        await store.model_deployment_append_revision(
            ModelDeploymentAppendRequest(
                namespace=source.namespace,
                name=PUBLIC_ID,
                expected_etag=None,
                spec=spec,
                action=ModelDeploymentRevisionAction.CREATE,
                actor_id=uuid4(),
                actor="operator",
                idempotency_key="long-app-object-create",
            )
        )
    ).value
    raw = _ready_cr(revision)
    raw["status"]["placements"] = [
        {
            "deploymentName": DEPLOYMENT,
            "poolRef": "h100-reserved-8x",
            "role": "hot",
            "desired": 1,
            "ready": 1,
            "available": 1,
        }
    ]
    raw["status"]["resources"] = [
        {
            "identity": f"apps/v1/Deployment/fs2-models/{DEPLOYMENT}",
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "namespace": "fs2-models",
            "name": DEPLOYMENT,
            "uid": "runtime-uid",
            "generation": 1,
            "digest": DIGEST,
        }
    ]
    raw["status"]["endpoint"]["serviceName"] = SERVICE
    assert len(DEPLOYMENT) == 70 and len(SERVICE) < 63
    parsed = ModelDeploymentObservedStatus.model_validate(_normalize_keys(raw["status"]))
    assert parsed.placements[0].deployment_name == DEPLOYMENT
    kubernetes = FakeKubernetes()
    kubernetes.models = [raw]
    bridge = ModelDeploymentRuntimeBridge(
        repository=StoreModelDeploymentRepository(store),
        writer=kubernetes,
        source=kubernetes,
        registry=registry,
        interval_seconds=5,
        route_ttl_seconds=30,
    )
    assert await bridge.refresh(force=True)
    published = registry.get(PUBLIC_ID)
    assert published.binding.backend_service_name == SERVICE
    assert published.dynamic_policy.publication.source_model_ref == "qwen3-8b"
    assert published.dynamic_policy.etag == spec_digest(spec)
    assert registry.get("qwen3-8b").enabled
    assert kubernetes.applied == []  # No rename, restart or desired-state rewrite.
    assert await bridge.refresh(force=True)  # Repeated bridge observation remains published.
    assert registry.get(PUBLIC_ID).binding.backend_service_name == SERVICE


@pytest.mark.parametrize("name", ["a" * 253, DEPLOYMENT, "a" * 80 + ".pool-runtime"])
def test_kubernetes_object_name_bounds_and_startup_retention_accept_valid_long_names(name):
    assert placement(name).deployment_name == resource(name).name == name
    query = startup_retention_promql(namespace="fs2-models", deployment=name, timeout_seconds=900, target_queue_depth=1)
    assert f'deployment="{name}"' in query


@pytest.mark.parametrize("name", ["a" * 254, "Uppercase", "bad_name", "-bad", "bad-", "a..b", "a.-b", "a/b", 'a"b', ""])
def test_kubernetes_resource_names_still_reject_malformed_or_oversized_values(name):
    for validator in (placement, resource):
        with pytest.raises(ValidationError):
            validator(name)
    with pytest.raises(ValueError):
        startup_retention_promql(namespace="fs2-models", deployment=name, timeout_seconds=900, target_queue_depth=1)


def test_service_endpoint_dns_label_limit_is_not_relaxed():
    with pytest.raises(ValidationError):
        ModelDeploymentEndpointStatus(
            namespace="fs2-models", service_name=DEPLOYMENT, service_port=8000, uid="service-uid", digest=DIGEST
        )
