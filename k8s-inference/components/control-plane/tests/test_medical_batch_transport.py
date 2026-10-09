"""Mock HTTP transport only; no worker, capacity or clinical qualification."""

import asyncio
import json
from dataclasses import replace
from types import MappingProxyType

import httpx
import jsonschema
import pytest
from conftest import CONTROL_ROOT
from test_federation import _federated_model, _operation, _route_document, _secret_fixture, _write_routes

from fs2_serve.admission import AdmissionService
from fs2_serve.federation import FederationConfigError, FederationRouter, FederationTransportError
from fs2_serve.runtime import RuntimeBusyError, RuntimeClient, RuntimeNoReplayError
from fs2_serve.runtime_scheduling import RuntimeScheduling
from fs2_serve.speech_models import MEDICAL_NEMOTRON

CHECKPOINT = "2a2b1cae8e96d62e83a82351f7d483df01fc28d1d64793ce45a5de514a6c3b5f"


def setup_transport(tmp_path, registry, handler, *, scheduling=True):
    secret, digest = _secret_fixture(tmp_path)
    source = _federated_model(registry, digest)
    model = replace(source, max_attempts=1, gateway=replace(
        source.gateway, model_id=MEDICAL_NEMOTRON, qualification=MappingProxyType({
            "native_serverless": {"checkpoint_sha256": CHECKPOINT,
                                  "endpoint_origin": "https://upstream.unit.invalid"}}),
        binding=replace(source.binding, backend_class="federated-serverless", endpoints={"native": "/generate"})))
    document = _route_document(model)
    document["routes"][model.id]["speech"] = {
        "stream_path": "/v1/audio/stream", "max_session_seconds": 7500,
        "file_deadline_seconds": 7500, "file_retry": "never",
    }
    jsonschema.validate(document, json.loads((CONTROL_ROOT / "contracts/federation-routes.schema.json").read_text()))
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False)
    router = FederationRouter.load(_write_routes(secret, document), [model], secret_root=secret,
                                   client_factory=lambda route: client)
    runtime = RuntimeClient(activation_timeout_seconds=2, runtime_timeout_seconds=2, max_response_bytes=8192,
                            federation=router, speech_scheduling=(
                                RuntimeScheduling(b"test-group-key-" * 4, "test-gateway-token-" * 3)
                                if scheduling else None))
    operation = _operation(model).model_copy(update={"protocol": "native", "max_attempts": 1})
    return model, operation, router, runtime, document, secret


def success():
    return {"text": "Synthetic test transcript.", "audio_seconds": 1.0,
            "model_revision": "sha256:" + CHECKPOINT,
            "runtime_identity": {"checkpoint_sha256": CHECKPOINT}}


async def test_one_file_post_pins_host_sni_and_opaque_tenant_group(tmp_path, registry):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=success())
    model, operation, router, runtime, _, _ = setup_transport(tmp_path, registry, handler)
    try:
        result = await runtime.invoke(model, operation, b'{"synthetic":"audio"}')
        assert result.status_code == 200 and json.loads(result.body)["text"] == success()["text"]
        assert len(calls) == 1 and calls[0].url.path == "/generate"
        assert calls[0].headers["host"] == "upstream.unit.invalid"
        assert calls[0].extensions["sni_hostname"] == "upstream.unit.invalid"
        assert calls[0].headers["authorization"] == "Bearer federation-test-value-one"
        group = calls[0].headers["x-fs2-scheduling-group"]
        assert len(group) == 64 and operation.tenant_id not in group
        assert group == runtime.speech_scheduling.headers(operation)["x-fs2-scheduling-group"]
        other = operation.model_copy(update={"tenant_id": operation.tenant_id + "-other"})
        assert runtime.speech_scheduling.headers(other)["x-fs2-scheduling-group"] != group
        assert not hasattr(router, "speech_websocket") and not hasattr(router, "speech_connection")
    finally:
        await runtime.close()


@pytest.mark.parametrize("failure", ["503", "timeout", "checkpoint", "malformed", "oversize"])
async def test_uncertain_or_invalid_file_result_never_replayed(tmp_path, registry, failure):
    calls = []
    def handler(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic lost response")
        if failure == "503":
            return httpx.Response(503, json={"detail": "synthetic failure"})
        if failure == "malformed":
            return httpx.Response(200, text="not JSON", headers={"content-type": "application/json"})
        body = success()
        if failure == "checkpoint":
            body["runtime_identity"]["checkpoint_sha256"] = "a" * 64
        else:
            body["text"] = "x" * 9000
        return httpx.Response(200, json=body)
    model, operation, _, runtime, _, _ = setup_transport(tmp_path, registry, handler)
    try:
        with pytest.raises(RuntimeNoReplayError):
            await runtime.invoke(model, operation, b'{}')
        assert len(calls) == 1
        assert not await AdmissionService._retry(object(), model,
            operation.model_copy(update={"max_attempts": 3}), RuntimeNoReplayError("uncertain"))
    finally:
        await runtime.close()


async def test_missing_scheduling_or_multiattempt_claim_sends_nothing(tmp_path, registry):
    calls = []
    model, operation, _, runtime, _, _ = setup_transport(
        tmp_path, registry, lambda request: calls.append(request), scheduling=False)
    try:
        # The public error deliberately normalizes both preflight failures;
        # the actual safety proof is that neither path reaches the transport.
        with pytest.raises(RuntimeNoReplayError, match="automatic replay disabled"):
            await runtime.invoke(model, operation, b'{}')
        with pytest.raises(RuntimeNoReplayError, match="automatic replay disabled"):
            await runtime.invoke(model, operation.model_copy(update={"max_attempts": 2}), b'{}')
        assert not calls
    finally:
        await runtime.close()


async def test_exact_preacceptance_busy_and_redirect_are_not_automatic_retries(tmp_path, registry):
    calls = []
    statuses = [429, 302]
    def handler(request):
        calls.append(request)
        return httpx.Response(statuses.pop(0), json={"detail": "runtime_busy"},
                              headers={"location": "https://untrusted.example.invalid/other"})
    model, operation, _, runtime, _, _ = setup_transport(tmp_path, registry, handler)
    try:
        with pytest.raises(RuntimeBusyError):
            await runtime.invoke(model, operation, b'{}')
        assert len(calls) == 1
        result = await runtime.invoke(model, operation, b'{}')
        assert result.status_code == 302 and len(calls) == 2
        assert all(call.url.host == "8.8.8.8" for call in calls)
    finally:
        await runtime.close()


async def test_changed_destination_and_binding_fail_closed(tmp_path, registry):
    model, _, router, runtime, document, secret = setup_transport(tmp_path, registry, lambda request: None)
    try:
        document["routes"][model.id]["destination"]["host"] = "different.example.invalid"
        with pytest.raises(FederationConfigError, match="destination"):
            FederationRouter.load(_write_routes(secret, document), [model], secret_root=secret)
        changed = replace(model, gateway=replace(model.gateway,
            binding=replace(model.binding, backend_endpoint_identity_sha256="b" * 64)))
        with pytest.raises(FederationTransportError, match="stale"):
            router.validate_speech_route(changed)
    finally:
        await runtime.close()


async def test_cancellation_propagates_without_retry(tmp_path, registry):
    calls = []
    def handler(request):
        calls.append(request)
        raise asyncio.CancelledError()
    model, operation, _, runtime, _, _ = setup_transport(tmp_path, registry, handler)
    try:
        with pytest.raises(asyncio.CancelledError):
            await runtime.invoke(model, operation, b'{}')
        assert len(calls) == 1
    finally:
        await runtime.close()
