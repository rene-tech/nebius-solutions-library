from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from uuid import uuid4

import pytest
from test_model_deployment import model_spec
from test_model_deployment_publication import status_view

from fs2_serve.admission import AdmissionService
from fs2_serve.crypto import KeyedHasher, PayloadCipher
from fs2_serve.memory_store import MemoryStore
from fs2_serve.model_deployment import DesiredState, LifecycleSpec, TenantPolicySpec, Visibility, spec_digest
from fs2_serve.model_deployment_publication import project_dynamic_publications
from fs2_serve.model_deployment_records import (
    ModelDeploymentAppendRequest,
    ModelDeploymentRevision,
    ModelDeploymentRevisionAction,
)
from fs2_serve.models import AdmissionRequest, Principal, Scope, TokenCreate
from fs2_serve.registry import Registry
from fs2_serve.runtime import StubRuntimeClient
from fs2_serve.store import ConflictError
from fs2_serve.telemetry import Metrics


def _revision(
    registry: Registry,
    *,
    visibility: Visibility = Visibility.TENANT,
    model_ref: str = "qwen3-8b",
    name: str = "qwen-event",
    aliases: list[str] | None = None,
    mcp_tool_name: str = "qwen_3_8b",
) -> ModelDeploymentRevision:
    base = registry.get(model_ref, require_enabled=False)
    assert base.gateway.model_revision is not None
    assert base.binding.artifact_manifest_digest is not None
    assert base.binding.backend_runtime_image_digest is not None
    spec = model_spec().model_copy(
        update={
            "model_ref": model_ref,
            "artifact": model_spec().artifact.model_copy(
                update={
                    "revision": base.gateway.model_revision,
                    "manifest_digest": f"sha256:{base.binding.artifact_manifest_digest}",
                }
            ),
            "runtime": model_spec().runtime.model_copy(
                update={"image": f"registry.example/fs2/runtime@{base.binding.backend_runtime_image_digest}"}
            ),
            "exposure": model_spec().exposure.model_copy(
                update={
                    "open_ai_aliases": ["event-qwen"] if aliases is None else aliases,
                    "mcp_tool_name": mcp_tool_name,
                }
            ),
            "policy": TenantPolicySpec(
                visibility=visibility,
                policy_ref="tenant-default.v1",
                allowed_principal_ids=["private-user"] if visibility is Visibility.PRIVATE else [],
            ),
        }
    )
    return ModelDeploymentRevision(
        namespace="fs2-models",
        name=name,
        tenant_id=spec.tenant_id,
        revision=1,
        etag=spec_digest(spec),
        spec=spec,
        action=ModelDeploymentRevisionAction.CREATE,
        created_at=datetime.now(UTC),
        created_by="operator@example.test",
    )


def _with_qwen_clone(registry: Registry) -> Registry:
    source = registry.get("qwen3-8b", require_enabled=False)
    clone_id = "qwen3-8b-copy"
    assert source.gateway.binding is not None
    clone_gateway = replace(
        source.gateway,
        model_id=clone_id,
        display_name="Qwen3 8B copy",
        binding=replace(source.gateway.binding, model_id=clone_id),
    )
    catalog_models = dict(registry.catalog.models)
    catalog_models[clone_id] = clone_gateway
    operational = {item.id: item for item in registry.list(enabled_only=False)}
    operational[clone_id] = replace(source, gateway=clone_gateway)
    return Registry(
        replace(
            registry.catalog,
            models=MappingProxyType(dict(sorted(catalog_models.items()))),
        ),
        operational,
    )


def _principal(*, tenant: str = "tenant-a", principal_id: str = "event-user") -> Principal:
    return Principal(
        token_id=uuid4(),
        token_prefix="fs2_test",
        principal_id=principal_id,
        tenant_id=tenant,
        scopes=frozenset({"catalog.read", "inference.invoke", "mcp.invoke", "use.nonclinical"}),
        models=frozenset({"*"}),
        max_concurrency=8,
    )


def test_registry_atomically_overlays_observed_route_alias_and_tenant_policy(registry: Registry) -> None:
    revision = _revision(registry)
    snapshot = project_dynamic_publications(
        [revision],
        {(revision.namespace, revision.name): status_view(revision)},
    )

    assert registry.set_dynamic_publications(snapshot, valid_until=datetime.now(UTC) + timedelta(minutes=1))
    routed = registry.get("qwen3-8b")
    assert routed.binding.backend_service_name == "qwen-event"
    assert routed.binding.backend_port == 8000
    assert routed.model_revision == f"dynamic:{revision.etag}"
    assert registry.get("event-qwen") is routed
    assert [item.id for item in registry.allowed_for_principal(_principal(), surface="openai")] == ["qwen3-8b"]
    assert registry.allowed_for_principal(_principal(tenant="tenant-b"), surface="openai") == []


def test_private_dynamic_route_requires_exact_principal(registry: Registry) -> None:
    revision = _revision(registry, visibility=Visibility.PRIVATE)
    snapshot = project_dynamic_publications(
        [revision],
        {(revision.namespace, revision.name): status_view(revision)},
    )
    assert registry.set_dynamic_publications(snapshot, valid_until=datetime.now(UTC) + timedelta(minutes=1))

    assert registry.allowed_for_principal(_principal(), surface="openai") == []
    permitted = _principal(principal_id="private-user")
    assert [item.id for item in registry.allowed_for_principal(permitted, surface="openai")] == ["qwen3-8b"]
    with pytest.raises(PermissionError, match="dynamic tenant policy"):
        registry.authorize_principal(
            registry.get("qwen3-8b"),
            _principal(),
            requested_model_id="qwen3-8b",
            surface="openai",
        )


def test_protocol_exposure_is_enforced_per_authenticated_surface(registry: Registry) -> None:
    revision = _revision(registry)
    mcp_only_spec = revision.spec.model_copy(
        update={
            "exposure": revision.spec.exposure.model_copy(update={"open_ai": False, "open_ai_aliases": [], "mcp": True})
        }
    )
    revision = revision.model_copy(update={"spec": mcp_only_spec, "etag": spec_digest(mcp_only_spec)})
    snapshot = project_dynamic_publications(
        [revision],
        {(revision.namespace, revision.name): status_view(revision)},
    )
    assert registry.set_dynamic_publications(snapshot, valid_until=datetime.now(UTC) + timedelta(minutes=1))

    principal = _principal()
    assert registry.allowed_for_principal(principal, surface="openai") == []
    assert [item.id for item in registry.allowed_for_principal(principal, surface="mcp")] == ["qwen3-8b"]
    with pytest.raises(PermissionError, match="dynamic tenant policy"):
        registry.authorize_principal(
            registry.get("qwen3-8b"),
            principal,
            requested_model_id="qwen3-8b",
            surface="openai",
        )


def test_disabled_dynamic_owner_withdraws_legacy_route_and_expiry_fails_closed(registry: Registry) -> None:
    revision = _revision(registry)
    ready = project_dynamic_publications(
        [revision],
        {(revision.namespace, revision.name): status_view(revision)},
    )
    assert registry.set_dynamic_publications(ready, valid_until=datetime.now(UTC) + timedelta(minutes=1))

    disabled_spec = revision.spec.model_copy(
        update={
            "lifecycle": LifecycleSpec(desired_state=DesiredState.DISABLED),
            "availability": revision.spec.availability.model_copy(update={"min_replicas": 0}),
        }
    )
    disabled = revision.model_copy(
        update={
            "revision": 2,
            "previous_revision": 1,
            "action": ModelDeploymentRevisionAction.UPDATE,
            "spec": disabled_spec,
            "etag": spec_digest(disabled_spec),
        }
    )
    withdrawn = project_dynamic_publications([disabled], {})
    assert registry.set_dynamic_publications(withdrawn, valid_until=datetime.now(UTC) + timedelta(minutes=1))
    with pytest.raises(RuntimeError, match="not routable"):
        registry.get("qwen3-8b")
    assert (
        registry.get_revision("qwen3-8b", routed_revision := f"dynamic:{revision.etag}").model_revision
        == routed_revision
    )
    with pytest.raises(KeyError, match="unknown model revision"):
        registry.get_revision("qwen3-8b", routed_revision, allow_dynamic=False)
    assert "qwen3-8b" not in {item.id for item in registry.list(enabled_only=True)}

    assert registry.set_dynamic_publications(ready, valid_until=datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(RuntimeError, match="not routable"):
        registry.get("qwen3-8b")


def test_invalid_dynamic_snapshot_does_not_withdraw_unmanaged_static_routes(registry: Registry) -> None:
    revision = _revision(registry)
    unknown_spec = revision.spec.model_copy(update={"model_ref": "unknown-live-model"})
    unknown = revision.model_copy(update={"spec": unknown_spec, "etag": spec_digest(unknown_spec)})
    snapshot = project_dynamic_publications(
        [unknown],
        {(unknown.namespace, unknown.name): status_view(unknown)},
    )

    assert registry.set_dynamic_publications(snapshot, valid_until=datetime.now(UTC) + timedelta(minutes=1))
    assert registry.get("qwen3-8b").enabled
    assert registry.validation_health()["healthy"] is True
    assert registry.dynamic_publication_health() == {
        "state": "partial",
        "error": None,
        "rejected_count": 1,
        "rejections": [
            {
                "namespace": unknown.namespace,
                "name": unknown.name,
                "model_ref": "unknown-live-model",
                "reason": "canonical-binding-invalid",
            }
        ],
        "rejections_truncated": False,
    }


def test_dynamic_readiness_cannot_promote_an_unqualified_catalog_model(registry: Registry) -> None:
    revision = _revision(registry)
    unqualified_spec = revision.spec.model_copy(update={"model_ref": "boltz2"})
    unqualified = revision.model_copy(update={"spec": unqualified_spec, "etag": spec_digest(unqualified_spec)})
    snapshot = project_dynamic_publications(
        [unqualified],
        {(unqualified.namespace, unqualified.name): status_view(unqualified)},
    )

    assert registry.set_dynamic_publications(
        snapshot,
        valid_until=datetime.now(UTC) + timedelta(minutes=1),
    )
    with pytest.raises(RuntimeError, match="not routable"):
        registry.get("boltz2")
    assert registry.get("qwen3-8b").enabled
    assert registry.dynamic_publication_health()["state"] == "partial"
    assert registry.dynamic_publication_health()["rejections"] == [
        {
            "namespace": unqualified.namespace,
            "name": unqualified.name,
            "model_ref": "boltz2",
            "reason": "canonical-binding-invalid",
        }
    ]


def test_one_invalid_binding_does_not_withdraw_unrelated_ready_dynamic_model(
    registry: Registry,
) -> None:
    good = _revision(
        registry,
        name="qwen-good",
        aliases=["good-qwen"],
        mcp_tool_name="good_qwen",
    )
    bad_base = _revision(
        registry,
        name="boltz-bad",
        aliases=["bad-boltz"],
        mcp_tool_name="bad_boltz",
    )
    bad_spec = bad_base.spec.model_copy(update={"model_ref": "boltz2"})
    bad = bad_base.model_copy(update={"spec": bad_spec, "etag": spec_digest(bad_spec)})
    snapshot = project_dynamic_publications(
        [good, bad],
        {
            (good.namespace, good.name): status_view(good),
            (bad.namespace, bad.name): status_view(bad),
        },
    )

    assert registry.set_dynamic_publications(
        snapshot,
        valid_until=datetime.now(UTC) + timedelta(minutes=1),
    )
    assert registry.get("good-qwen").model_revision == f"dynamic:{good.etag}"
    with pytest.raises(RuntimeError, match="not routable"):
        registry.get("boltz2")
    health = registry.dynamic_publication_health()
    assert health["state"] == "partial"
    assert health["rejected_count"] == 1


def test_one_static_identity_collision_does_not_withdraw_unrelated_dynamic_model(
    registry: Registry,
) -> None:
    registry = _with_qwen_clone(registry)
    good = _revision(
        registry,
        name="qwen-good",
        aliases=["good-qwen"],
        mcp_tool_name="good_qwen",
    )
    colliding = _revision(
        registry,
        model_ref="qwen3-8b-copy",
        name="qwen-collision",
        aliases=["boltz2"],
        mcp_tool_name="collision_qwen",
    )
    snapshot = project_dynamic_publications(
        [good, colliding],
        {
            (good.namespace, good.name): status_view(good),
            (colliding.namespace, colliding.name): status_view(colliding),
        },
    )

    assert registry.set_dynamic_publications(
        snapshot,
        valid_until=datetime.now(UTC) + timedelta(minutes=1),
    )
    assert registry.get("good-qwen").model_revision == f"dynamic:{good.etag}"
    with pytest.raises(RuntimeError, match="not routable"):
        registry.get("qwen3-8b-copy")
    assert registry.dynamic_publication_health()["rejections"] == [
        {
            "namespace": colliding.namespace,
            "name": colliding.name,
            "model_ref": "qwen3-8b-copy",
            "reason": "openai-identity-conflict",
        }
    ]


def test_ambiguous_global_inventory_failure_withdraws_all_managed_routes(
    registry: Registry,
) -> None:
    revision = _revision(registry)
    snapshot = project_dynamic_publications(
        [revision],
        {(revision.namespace, revision.name): status_view(revision)},
    )
    assert registry.set_dynamic_publications(
        snapshot,
        valid_until=datetime.now(UTC) + timedelta(minutes=1),
    )
    assert registry.get("event-qwen").enabled

    corrupt = snapshot.model_copy(update={"digest": f"sha256:{'0' * 64}"})
    assert not registry.set_dynamic_publications(
        corrupt,
        valid_until=datetime.now(UTC) + timedelta(minutes=1),
    )
    with pytest.raises(RuntimeError, match="not routable"):
        registry.get("qwen3-8b")
    assert registry.dynamic_publication_health() == {
        "state": "invalid",
        "error": "inventory-invalid",
        "rejected_count": 0,
        "rejections": [],
        "rejections_truncated": False,
    }


@pytest.mark.asyncio
async def test_alias_admission_is_canonicalized_for_keda_durable_demand(registry: Registry) -> None:
    revision = _revision(registry)
    snapshot = project_dynamic_publications(
        [revision],
        {(revision.namespace, revision.name): status_view(revision)},
    )
    assert registry.set_dynamic_publications(snapshot, valid_until=datetime.now(UTC) + timedelta(minutes=1))
    store = MemoryStore(
        PayloadCipher(active_key_id="payload", keys={"payload": b"p" * 32}),
        KeyedHasher(active_key_id="ledger", keys={"ledger": b"h" * 32}),
    )
    metrics = Metrics(registry.list())
    service = AdmissionService(
        registry=registry,
        store=store,
        runtime=StubRuntimeClient(),
        metrics=metrics,
        worker_concurrency=1,
        poll_seconds=0.01,
        lease_seconds=30,
        maintenance_interval_seconds=1,
        shutdown_grace_seconds=1,
    )

    principal = _principal()
    await store.model_deployment_append_revision(
        ModelDeploymentAppendRequest(
            namespace=revision.namespace,
            name=revision.name,
            expected_etag=None,
            spec=revision.spec,
            action=ModelDeploymentRevisionAction.CREATE,
            actor_id=uuid4(),
            actor="operator@example.test",
            idempotency_key="dynamic-model-create-0001",
        )
    )
    await store.issue_token(
        token_id=principal.token_id,
        prefix=principal.token_prefix,
        pepper_key_id="pepper-v1",
        digest="test-digest",
        request=TokenCreate(
            principal_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            scopes={Scope.CATALOG_READ, Scope.INFERENCE_INVOKE, Scope.MCP_INVOKE, Scope.USE_NONCLINICAL},
            models={"*"},
            max_concurrency=principal.max_concurrency,
        ),
        created_by="test",
    )
    operation = await service.admit(
        principal,
        AdmissionRequest(
            model_id="event-qwen",
            operation="chat",
            protocol="openai-chat",
            idempotency_key="alias-keda-demand-0001",
            request_body=b"{}",
        ),
    )
    assert operation.model_id == "qwen3-8b"
    assert operation.deadline_at is not None
    remaining = (operation.deadline_at - datetime.now(UTC)).total_seconds()
    assert 7195 <= remaining <= 7200
    stored = store.operations[operation.id]
    assert stored.dispatch_snapshot is not None
    assert stored.request is not None
    dispatch = store.cipher.decrypt(
        stored.request,
        aad=store.cipher.aad(operation.id, operation.tenant_id, operation.model_id, "request"),
    )
    assert json.loads(dispatch) == {"model": "qwen3-8b"}
    metrics.set_queue(await store.queue_counts())
    rendered = metrics.render().decode("utf-8")
    assert 'fs2_serve_operations{model="qwen3-8b",state="queued"} 1.0' in rendered
    assert 'model="event-qwen"' not in rendered

    claimed = await store.claim_operation("worker-after-route-drain", lease_seconds=30)
    assert claimed is not None
    assert claimed.dispatch_snapshot == stored.dispatch_snapshot
    disabled_spec = revision.spec.model_copy(
        update={
            "lifecycle": LifecycleSpec(desired_state=DesiredState.DRAINING),
            "availability": revision.spec.availability.model_copy(update={"min_replicas": 0}),
        }
    )
    disabled = revision.model_copy(
        update={
            "revision": 2,
            "previous_revision": 1,
            "action": ModelDeploymentRevisionAction.UPDATE,
            "spec": disabled_spec,
            "etag": spec_digest(disabled_spec),
        }
    )
    await store.model_deployment_append_revision(
        ModelDeploymentAppendRequest(
            namespace=disabled.namespace,
            name=disabled.name,
            expected_etag=revision.etag,
            spec=disabled.spec,
            action=ModelDeploymentRevisionAction.UPDATE,
            actor_id=uuid4(),
            actor="operator@example.test",
            idempotency_key="dynamic-model-drain-0001",
        )
    )
    with pytest.raises(ConflictError, match="no longer accepts admissions"):
        await service.admit(
            principal,
            AdmissionRequest(
                model_id="event-qwen",
                operation="chat",
                protocol="openai-chat",
                idempotency_key="post-drain-admission-0001",
                request_body=b"{}",
            ),
        )
    assert registry.set_dynamic_publications(
        project_dynamic_publications([disabled], {}),
        valid_until=datetime.now(UTC) + timedelta(minutes=1),
    )
    registry._retired_routes.clear()  # simulate another/restarted gateway replica
    restored = await service._current_model(claimed)
    assert restored.model_revision == operation.model_revision
    assert restored.binding.backend_service_name == "qwen-event"
