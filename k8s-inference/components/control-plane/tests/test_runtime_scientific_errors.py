"""Recognized molecular search failures stay actionable, bounded and non-transient."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from test_admission_workers import service, wait_status
from test_dynamic_routes import _cosmos_revision
from test_model_deployment_publication import status_view
from test_runtime_and_schema import FixedTrustedMetadata, claimed
from test_runtime_debug import Chunks, DebugSink

from fs2_serve.memory_store import MemoryStore
from fs2_serve.model_deployment_publication import project_dynamic_publications
from fs2_serve.models import AdmissionRequest, OperationStatus, Principal, RuntimeIdentity, Scope, TokenCreate
from fs2_serve.runtime import RuntimeClient, sanitize_error_detail

PRIVATE_MESSAGE = "SYNTHETIC_MODEL_TRACE_NEVER_PUBLIC /models/private-input"
MOLMIM = {"detail": {"code": "GENERATION_EXHAUSTED", "message": PRIVATE_MESSAGE, "counts": {
    "requested_molecules": 8, "distinct_feasible_molecules": 6, "attempted_model_decodes": 32,
    "invalid_decodes": 1, "below_similarity": 10, "unchanged_decodes": 10,
    "duplicate_decodes": 5, "optimizer_steps": 4,
}}}
GENMOL = {"detail": {"code": "generation_exhausted", "message": PRIVATE_MESSAGE, "metrics": {
    "requested_molecules": 16, "returned_molecules": 0, "accepted_molecules": 15,
    "sampling_attempts": 8, "candidate_requests": 23, "sampled_candidates": 23,
    "upstream_unreturned_candidates": 0, "invalid_candidates": 8, "duplicate_candidates": 0,
    "nonfinite_score_candidates": 0, "max_sampling_attempts": 8, "max_candidate_requests": 128,
    "minimum_mask_tokens": 15, "unique_requested": True,
}}}
INVALID = {"detail": {"code": "INVALID_MOLECULE", "message": PRIVATE_MESSAGE}}


def model_for(registry, model_id="molmim"):
    base = registry.get("qwen3-8b")
    binding = replace(base.binding, model_id=model_id, protocols=("native",), operations=("generate",),
                      endpoints={"native": "/generate"}, activation=replace(base.binding.activation, enabled=False))
    return replace(base, gateway=replace(base.gateway, model_id=model_id, protocols=("native",),
                                        policy_operations=("generate",), binding=binding))


async def invoke(registry, payload, *, model_id="molmim", status=422, model=None, maximum=32768,
                 content_type="application/json", debug=True, stream=None, protocol="native"):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    model = model or model_for(registry, model_id)
    operation = claimed(registry).model_copy(update={"model_id": model.id, "protocol": protocol})
    identity = RuntimeIdentity(pod_uid="trusted-model-pod", gpu_count=1)
    metadata, sink = FixedTrustedMetadata(identity), DebugSink() if debug else None

    def handler(_):
        return httpx.Response(status, content=None if stream else body, stream=stream,
                              headers={"content-type": content_type, "x-fs2-gpu-count": "99"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runtime = RuntimeClient(activation_timeout_seconds=2, runtime_timeout_seconds=2,
                                max_response_bytes=maximum, client=client, metadata_provider=metadata, debug_store=sink)
        result = await runtime.invoke(model, operation, b'{"fixture":"synthetic request"}')
    assert result.runtime == identity and metadata.calls == [(operation.id, model.id)]
    assert result.body == b"" and result.usage is None and result.semantic_outcome == "not_evaluated"
    return result, sink


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id,status,payload,code,fragment", [
    ("molmim", 422, MOLMIM, "generation_exhausted", "found 6 of 8"),
    ("genmol", 503, GENMOL, "generation_exhausted", "accepted 15 of 16"),
    ("molmim", 422, INVALID, "invalid_molecule", "Supply valid SMILES"),
])
async def test_recognized_failure_retains_original_debug_but_public_static_counts_only(
    registry, model_id, status, payload, code, fragment,
):
    result, sink = await invoke(registry, payload, model_id=model_id, status=status)
    assert result.status_code == status and result.failure_code == code
    assert fragment in result.failure_detail and PRIVATE_MESSAGE not in result.failure_detail
    assert sanitize_error_detail(result.failure_detail) == result.failure_detail
    assert len(result.failure_detail) <= 1024
    capture = sink.exchanges[0].response_body
    assert capture.complete and json.loads(capture.data) == payload


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id,status,payload", [
    ("qwen3-8b", 422, MOLMIM), ("molmim", 503, MOLMIM), ("genmol", 422, GENMOL),
    ("molmim", 422, GENMOL), ("genmol", 503, MOLMIM),
    ("genmol", 503, {"detail": "model is loading"}),
    ("molmim", 422, {"detail": [{"msg": PRIVATE_MESSAGE, "input": PRIVATE_MESSAGE}]}),
    ("molmim", 422, {"detail": {"code": "OTHER", "message": PRIVATE_MESSAGE}}),
    ("molmim", 422, b'{"detail":'), ("molmim", 422, b"\xff"),
])
async def test_unrecognized_failures_remain_payload_free(registry, model_id, status, payload):
    result, _ = await invoke(registry, payload, model_id=model_id, status=status)
    assert result.status_code == status and result.failure_code == "upstream_http_error"
    assert result.failure_detail is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [True, -1, 10**30, "16", "PRIVATE_MODEL_INPUT", 1.5, None])
@pytest.mark.parametrize("model_id,section,payload,status", [
    ("molmim", "counts", MOLMIM, 422), ("genmol", "metrics", GENMOL, 503),
])
async def test_untrusted_malformed_counts_never_become_public(registry, bad, model_id, section, payload, status):
    payload = deepcopy(payload)
    payload["detail"][section]["requested_molecules"] = bad
    result, _ = await invoke(registry, payload, model_id=model_id, status=status)
    assert result.failure_code == "upstream_http_error" and result.failure_detail is None


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id,section,payload,status,field,value", [
    ("molmim", "counts", MOLMIM, 422, "distinct_feasible_molecules", 8),
    ("molmim", "counts", MOLMIM, 422, "attempted_model_decodes", 31),
    ("genmol", "metrics", GENMOL, 503, "returned_molecules", 15),
    ("genmol", "metrics", GENMOL, 503, "sampled_candidates", 22),
    ("genmol", "metrics", GENMOL, 503, "sampling_attempts", 9),
])
async def test_inconsistent_exhaustion_is_not_a_recognized_contract(
    registry, model_id, section, payload, status, field, value,
):
    payload = deepcopy(payload)
    payload["detail"][section][field] = value
    result, _ = await invoke(registry, payload, model_id=model_id, status=status)
    assert result.failure_code == "upstream_http_error" and result.failure_detail is None


@pytest.mark.asyncio
async def test_alias_uses_canonical_source_and_projection_works_without_debug(registry):
    revision = _cosmos_revision(registry)
    snapshot = project_dynamic_publications([revision], {(revision.namespace, revision.name): status_view(revision)})
    assert registry.set_dynamic_publications(snapshot, valid_until=datetime.now(UTC) + timedelta(minutes=1))
    model = registry.get("cosmos3-nano")
    publication = model.dynamic_policy.publication.model_copy(update={"canonical_model_ref": "molmim"})
    model = replace(model, dynamic_policy=replace(model.dynamic_policy, publication=publication))
    result, sink = await invoke(registry, MOLMIM, model=model, debug=False)
    assert result.failure_code == "generation_exhausted" and "found 6 of 8" in result.failure_detail
    assert sink is None


@pytest.mark.asyncio
@pytest.mark.parametrize("debug", [True, False])
async def test_oversized_projection_is_not_parsed_and_existing_debug_bound_is_preserved(registry, debug):
    body = json.dumps({**MOLMIM, "padding": "x" * 17000}).encode()
    result, sink = await invoke(registry, body, debug=debug)
    assert result.failure_code == "upstream_http_error" and result.failure_detail is None
    if debug:
        assert sink.exchanges[0].response_body.complete
        assert sink.exchanges[0].response_body.observed_bytes == len(body)
    result, sink = await invoke(registry, body, debug=debug, maximum=100)
    assert result.failure_code == "upstream_http_error" and result.failure_detail is None
    if debug:
        assert not sink.exchanges[0].response_body.complete


@pytest.mark.asyncio
async def test_interrupted_error_read_preserves_upstream_status_and_partial_debug(registry):
    stream = Chunks([b'{"detail":'], httpx.ReadTimeout("fixture interrupted"))
    result, sink = await invoke(registry, b"", stream=stream)
    assert result.status_code == 422 and result.failure_code == "upstream_http_error"
    assert sink.exchanges[0].error_type == "ReadTimeout"
    assert not sink.exchanges[0].response_body.complete and stream.iterations == 1


@pytest.mark.asyncio
async def test_cancellation_during_error_read_is_not_converted_to_terminal_failure(registry):
    stream = Chunks([], asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await invoke(registry, b"", stream=stream)


def test_detail_sanitizer_accepts_only_exact_static_templates():
    _, safe = RuntimeClient._scientific_error("molmim", 422, json.dumps(MOLMIM).encode())
    assert sanitize_error_detail(safe) == safe
    for bad in [safe + PRIVATE_MESSAGE, PRIVATE_MESSAGE, safe.replace("6 of 8", "1234 of 8"),
                safe.replace("6 of 8", "6\n of 8"), safe.replace("6 of 8", "6.0 of 8")]:
        assert sanitize_error_detail(bad) == "runtime operation failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id,status,payload,code,expected_attempts", [
    ("molmim", 422, MOLMIM, "generation_exhausted", 1),
    ("molmim", 422, INVALID, "invalid_molecule", 1),
    ("genmol", 503, GENMOL, "generation_exhausted", 1),
    ("genmol", 503, {"detail": "model is loading"}, "upstream_http_error", 2),
])
async def test_real_runtime_worker_store_retains_actionable_failure_without_replaying_search(
    registry, cipher, hasher, monkeypatch, model_id, status, payload, code, expected_attempts,
):
    model = model_for(registry, model_id)
    monkeypatch.setattr(registry, "get", lambda *args, **kwargs: model)
    monkeypatch.setattr(registry, "get_revision", lambda *args, **kwargs: model)
    monkeypatch.setattr(registry, "list", lambda *args, **kwargs: [model])
    store = MemoryStore(cipher, hasher)
    principal = Principal(token_id=uuid4(), token_prefix="fs2_pat_fixture", principal_id="scientist",
                          tenant_id="science-test", scopes=frozenset({"inference.invoke"}),
                          models=frozenset({model_id}), max_concurrency=1)
    await store.issue_token(token_id=principal.token_id, prefix=principal.token_prefix, pepper_key_id="pepper-v1",
        digest="fixture", request=TokenCreate(principal_id=principal.principal_id, tenant_id=principal.tenant_id,
            scopes={Scope.INFERENCE_INVOKE}, models={model_id}, max_concurrency=1), created_by="test")
    calls, sink = [], DebugSink()

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"ready": True})
        calls.append(request)
        return httpx.Response(status, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runtime = RuntimeClient(activation_timeout_seconds=2, runtime_timeout_seconds=2,
                                max_response_bytes=32768, client=client, debug_store=sink)
        worker = service(registry, store, runtime)
        operation = await worker.admit(principal, AdmissionRequest(model_id=model_id, operation="generate",
            protocol="native", idempotency_key="scientific-error-test-0001", request_body=b"{}"))
        await worker.start()
        try:
            final = await wait_status(store, operation.id, {OperationStatus.FAILED}, timeout=5)
            assert final.attempt == expected_attempts == len(calls)
            assert final.http_status == status and final.error_code == code
            assert PRIVATE_MESSAGE not in final.model_dump_json()
            if code == "upstream_http_error":
                assert final.error_detail is None
            else:
                expected = RuntimeClient._scientific_error(model_id, status, json.dumps(payload).encode())
                assert final.error_detail == expected[1]
            assert len(sink.exchanges) == expected_attempts
            assert all(json.loads(exchange.response_body.data) == payload for exchange in sink.exchanges)
        finally:
            await worker.close()
