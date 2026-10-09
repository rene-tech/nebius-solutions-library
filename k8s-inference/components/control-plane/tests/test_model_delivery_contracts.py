"""Reference delivery fidelity through real admission, worker and result handling."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from test_admission_workers import service, wait_status
from test_cosmos_native_runtime import MP4
from test_dynamic_routes import _cosmos_revision
from test_model_deployment_publication import status_view

from fs2_serve.memory_store import MemoryStore
from fs2_serve.model_delivery_contracts import admission_model
from fs2_serve.model_deployment_publication import project_dynamic_publications
from fs2_serve.models import AdmissionRequest, OperationStatus, Principal, Scope, TokenCreate
from fs2_serve.registry import Registry
from fs2_serve.runtime import RuntimeClient

MODEL = "cosmos-transfer2-5-2b"


def transfer_model(registry):
    base = registry.get("qwen3-8b")
    return replace(base, max_attempts=3, gateway=replace(
        base.gateway, model_id=MODEL, protocols=("native",), policy_operations=("transfer-video",),
        # The production Transfer App is an already-hot single replica. This
        # HTTP/store test has no Kubernetes activation-controller fixture.
        binding=replace(base.binding, endpoints={"native": "/v1/transfer"},
                        activation=replace(base.binding.activation, enabled=False)),
    ))


def test_transfer_delivery_limit_is_scoped_and_reservation_matches(registry):
    model = transfer_model(registry)
    bound = admission_model(model, protocol="native", operation="transfer-video")
    assert bound.max_attempts == 1 and model.max_attempts == 3
    assert bound.gpu_seconds_reservation * 3 == model.gpu_seconds_reservation
    assert bound.gateway is model.gateway
    for protocol, operation in [("openai-chat", "transfer-video"), ("native", "other")]:
        assert admission_model(model, protocol=protocol, operation=operation) is model
    sibling = registry.get("qwen3-8b")
    assert admission_model(sibling, protocol="openai-chat", operation="chat") is sibling


def test_transfer_delivery_limit_uses_canonical_source_for_dynamic_apps(registry):
    model = transfer_model(registry)
    alias = replace(model, gateway=replace(model.gateway, model_id="custom-transfer-app"),
                    dynamic_policy=SimpleNamespace(publication=SimpleNamespace(source_model_ref=MODEL)))
    bound = admission_model(alias, protocol="native", operation="transfer-video")
    assert bound.id == "custom-transfer-app" and bound.max_attempts == 1
    assert bound.dynamic_policy is alias.dynamic_policy
    other = replace(model, dynamic_policy=SimpleNamespace(
        publication=SimpleNamespace(source_model_ref="another-runtime")))
    assert admission_model(other, protocol="native", operation="transfer-video") is other


def test_delivery_limit_is_frozen_in_dynamic_dispatch_snapshot(registry):
    revision = _cosmos_revision(registry)
    snapshot = project_dynamic_publications([revision], {(revision.namespace, revision.name): status_view(revision)})
    assert registry.set_dynamic_publications(snapshot, valid_until=datetime.now(UTC) + timedelta(minutes=1))
    source = registry.get("cosmos3-nano")
    publication = source.dynamic_policy.publication.model_copy(update={"canonical_model_ref": MODEL})
    model = replace(source, max_attempts=3, dynamic_policy=replace(source.dynamic_policy, publication=publication))
    bounded = admission_model(model, protocol="native", operation="transfer-video")
    from fs2_serve.dynamic_routes import DynamicDispatchSnapshot
    persisted = DynamicDispatchSnapshot.model_validate_json(Registry.dispatch_snapshot(bounded))
    historical = DynamicDispatchSnapshot.model_validate_json(Registry.dispatch_snapshot(model))
    assert persisted.max_attempts == 1 and historical.max_attempts == 3
    assert persisted.publication == historical.publication
    assert persisted.max_gpu_seconds_per_attempt == historical.max_gpu_seconds_per_attempt


async def exercise_delivery(registry, store, failure):
    model = transfer_model(registry)
    selected = Registry(replace(registry.catalog, models={MODEL: model.gateway}), {MODEL: model})
    token_id = uuid4()
    scopes = {Scope.INFERENCE_INVOKE, Scope.MCP_INVOKE, Scope.USE_NONCLINICAL}
    await store.issue_token(token_id=token_id, prefix="fs2_pat_transfer_delivery", pepper_key_id="pepper-v1",
        digest="transfer-delivery", request=TokenCreate(principal_id="delivery-test", tenant_id="delivery-test",
        scopes=scopes, models={MODEL}, gpu_seconds_budget=1000, max_concurrency=1), created_by="test")
    principal = Principal(token_id=token_id, token_prefix="fs2_pat_transfer_delivery",
        principal_id="delivery-test", tenant_id="delivery-test", scopes=frozenset(s.value for s in scopes),
        models=frozenset({MODEL}), gpu_seconds_budget=1000, max_concurrency=1)
    calls = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"ready": True})
        calls.append(request.content)
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic lost response", request=request)
        if failure == "http503":
            return httpx.Response(503, json={"detail": "synthetic failed generation"})
        if failure == "success":
            return httpx.Response(200, content=MP4, headers={"content-type": "video/mp4"})
        return httpx.Response(200, content=b"invalid MP4", headers={"content-type": "video/mp4"})

    request = AdmissionRequest(model_id=MODEL, operation="transfer-video", protocol="native",
        idempotency_key="reference-delivery-one-attempt-"+failure, request_body=b'{"prompt":"synthetic"}')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runtime = RuntimeClient(activation_timeout_seconds=2, runtime_timeout_seconds=2,
                                max_response_bytes=8192, client=client)
        worker = service(selected, store, runtime)
        operation = await worker.admit(principal, request)
        assert operation.max_attempts == 1
        assert operation.reserved_gpu_seconds == model.gpu_seconds_reservation / 3
        if failure == "lease_expiry":
            claimed = await store.claim_operation("lost-delivery-worker", lease_seconds=0.01)
            assert claimed.id == operation.id and claimed.max_attempts == 1
            await asyncio.sleep(0.03)
            assert await store.reap_stale_operations() == 1
            final = await store.get_operation(operation.id)
            assert final.status == OperationStatus.EXPIRED
            assert final.attempt == 1 and final.error_code == "lease_recovery_exhausted"
            assert await store.claim_operation("replacement-worker", lease_seconds=1) is None
            replay = await worker.admit(principal, request)
            assert replay.id == operation.id and replay.status == OperationStatus.EXPIRED
            assert calls == []
            return
        await worker.start()
        try:
            expected = OperationStatus.SUCCEEDED if failure == "success" else OperationStatus.FAILED
            try:
                final = await wait_status(store, operation.id, {expected}, timeout=5)
            except TimeoutError:
                current = await store.get_operation(operation.id)
                raise AssertionError({"state": current.status, "attempt": current.attempt,
                                      "error": current.error_code, "calls": len(calls),
                                      "worker": worker.health()}) from None
            assert final.attempt == 1 and final.max_attempts == 1
            replay = await worker.admit(principal, request)
            assert replay.id == operation.id and replay.status == expected
            assert calls == [request.request_body]
        finally:
            await worker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http503", "invalid_media", "timeout", "success", "lease_expiry"])
async def test_transfer_terminal_delivery_and_replay(registry, cipher, hasher, failure):
    await exercise_delivery(registry, MemoryStore(cipher, hasher), failure)
