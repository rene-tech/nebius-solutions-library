"""Cosmos binary protocol regression: real dispatch, durable completion and bytes."""

import base64
import hashlib
import json
import struct
import zlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from test_admission_workers import service, wait_status
from test_dynamic_routes import _cosmos_revision, _principal
from test_model_deployment_publication import status_view
from test_runtime_and_schema import FixedTrustedMetadata, claimed
from test_runtime_debug import DebugSink
from test_scientific_artifacts import FakeObjectStore

from fs2_serve.artifact_outputs import ServingOutputArtifactizer
from fs2_serve.memory_store import MemoryStore
from fs2_serve.model_deployment import AppDeploymentIdentity, spec_digest
from fs2_serve.model_deployment_publication import project_dynamic_publications
from fs2_serve.model_deployment_records import ModelDeploymentAppendRequest, ModelDeploymentRevisionAction
from fs2_serve.models import AdmissionRequest, OperationStatus, RuntimeIdentity, Scope, TokenCreate
from fs2_serve.runtime import RuntimeClient, RuntimeProtocolError
from fs2_serve.scientific_artifacts import MemoryArtifactRepository, ScientificArtifactService

COSMOS = "cosmos3-nano"
# Real 16x16, one-frame H.264/MP4 fixture generated locally with ffmpeg 6.1.1:
# lavfi color=blue:s=16x16:r=1; libx264; remove SEI; frag_keyframe+empty_moov.
# Compressed fixture only: production/runtime/tests require no ffmpeg executable.
MP4 = zlib.decompress(base64.b64decode(
    "eJyVUrFuE0EQnbUTCaJIOYQjUgTFICNFglg+E1mECuHGRdoghGjO3jt85NZ32R2fDJRQoJR8Qrr8RQqKKEU+IFVSREkViS8w"
    "s3t38uYkIrF3s29ndmbn7cwCQCPAL0moYgFQAY0kHZK2lw5ckWy6ZL8UcZwCQCTSIYdbo3ptgJl/Nthtr7L+Bu4cFQq4Qunt"
    "0voj7pqc1TtO/7+8Ti7AvgseerSoC16+V8Zw57eBjSGPZLGThty3Pd+RHve8EY987cOaIhwFtKilwhxqU2jwbG+VSz+wKC6O"
    "ZVTP1zcK+xHhgULFLZ9fuiH/LIBDX4+wV3gsb5N/p9V86TbdVrsehf1Ju7NpRaxMpzS/IK8ue3u0ML2A5U+EZ9+Yo6ttpvU/"
    "Tz8zmBueLh3rHImnkjy7FkchKpsDMR5Yeo30r2WeCgexpa+L1J8Q1lEaLG5XvmF/zFG36oPwDerxxG4LtVJ6SRLZQRthpJDw2"
    "SHGOulj7plgczjVJ6D6uB2qT4v0hB657okjgqFd9/f0ELW9gZl9a8auej9/KI9YZqlhwLFEfQXleEQ4n8dNKpl9TRAbwof+zx"
    "+vH+xDxd07gu7JK7g3f07mrgik5vocCdmMjo1Q0GAmPzGXRW27fwHMy3//"
))


def png(width=1):
    def chunk(kind, content):
        return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\xff")) + chunk(b"IEND", b""))


def model_for(registry, model_id=COSMOS):
    base = registry.get("qwen3-8b")
    return replace(base, gateway=replace(base.gateway, model_id=model_id,
                   binding=replace(base.binding, endpoints={"native": "/generate"})))


async def invoke(registry, content, *, content_type, model=None, status=200, maximum=8192):
    model = model or model_for(registry)
    operation = claimed(registry).model_copy(update={"protocol": "native", "model_id": model.id})
    identity = RuntimeIdentity(pod_uid="trusted-cosmos-pod", gpu_count=1)
    metadata, sink = FixedTrustedMetadata(identity), DebugSink()

    def handler(request):
        assert request.content == b'{"prompt":"synthetic"}'
        return httpx.Response(status, content=content, headers={"content-type": content_type,
                              "x-fs2-pod-uid": "forged", "x-fs2-gpu-count": "99"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runtime = RuntimeClient(activation_timeout_seconds=2, runtime_timeout_seconds=2,
                                max_response_bytes=maximum, client=client, metadata_provider=metadata,
                                debug_store=sink)
        result = await runtime.invoke(model, operation, b'{"prompt":"synthetic"}')
    assert result.runtime == identity and metadata.calls == [(operation.id, model.id)]
    return operation, result, sink


@pytest.mark.asyncio
@pytest.mark.parametrize("body,content_type", [(png(), "image/png"), (MP4, "video/mp4")], ids=["png", "mp4"])
async def test_cosmos_dispatch_externalize_and_authorized_retrieval_preserve_bytes(registry, body, content_type):
    operation, result, sink = await invoke(registry, body, content_type=content_type)
    assert result.semantic_outcome == "protocol_valid" and result.usage is None
    assert result.content_type == content_type and result.body == body
    assert sink.exchanges[0].response_body.complete
    assert base64.b64decode(sink.exchanges[0].response_body.data) == body
    assert sink.exchanges[0].error_type is None
    repository = MemoryArtifactRepository()
    await repository.register_operation(operation.id, tenant_id=operation.tenant_id)
    objects = FakeObjectStore(clock=lambda: datetime.now(UTC))
    artifacts = ScientificArtifactService(repository=repository, object_store=objects,
                                         allowed_media_types={"application/octet-stream"})
    published = await ServingOutputArtifactizer(artifacts).externalize(operation, result)
    envelope = json.loads(published.body)
    assert envelope["content_type"] == content_type
    assert envelope["artifact"]["sha256"] == hashlib.sha256(body).hexdigest()
    assert envelope["artifact"]["size_bytes"] == len(body)
    assert published.runtime == result.runtime and published.usage == result.usage
    stream = await artifacts.open_content(UUID(envelope["artifact"]["artifact_id"]), tenant_id=operation.tenant_id)
    assert b"".join([chunk async for chunk in stream.chunks]) == body


@pytest.mark.asyncio
@pytest.mark.parametrize("body,content_type", [
    (b"", "image/png"), (png()[:-1], "image/png"), (png() + b"trailing", "image/png"),
    (png(0), "image/png"), (png()[:40] + b"X" + png()[41:], "image/png"),
    (b'{"error":"generation failed"}', "image/png"), (MP4, "image/png"),
    (b"", "video/mp4"), (MP4[:-1], "video/mp4"), (MP4[:36], "video/mp4"),
    (MP4.replace(b"vide", b"soun"), "video/mp4"), (b'{"error":"generation failed"}', "video/mp4"),
    (png(), "video/mp4"), (MP4, "application/json"), (MP4, "application/octet-stream"),
])
async def test_cosmos_invalid_truncated_and_wrong_type_are_protocol_failures(registry, body, content_type):
    with pytest.raises(RuntimeProtocolError):
        await invoke(registry, body, content_type=content_type)


@pytest.mark.asyncio
async def test_cosmos_response_bound_http_error_and_legacy_json_are_unchanged(registry):
    with pytest.raises(RuntimeProtocolError, match="exceeded configured maximum"):
        await invoke(registry, MP4, content_type="video/mp4", maximum=len(MP4) - 1)
    _, failed, _ = await invoke(registry, b'{"detail":"failed"}', content_type="application/json", status=503)
    assert failed.failure_code == "upstream_http_error" and failed.body == b"" and failed.usage is None
    _, legacy, _ = await invoke(registry, b'{"mime_type":"image/png","data_base64":"legacy"}',
                                content_type="application/json")
    assert legacy.semantic_outcome == "protocol_valid"
    with pytest.raises(RuntimeProtocolError):
        await invoke(registry, MP4, content_type="video/mp4", model=model_for(registry, "qwen3-8b"))
    _, other, _ = await invoke(registry, b'{"ok":true}', content_type="application/json",
                               model=model_for(registry, "qwen3-8b"))
    assert other.semantic_outcome == "protocol_valid" and other.usage is None


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", ["wan2-2-t2v-nim", "wan2-2-i2v-nim"])
async def test_wan_native_dispatch_accepts_valid_mp4_and_preserves_native_json(registry, model_id):
    model = model_for(registry, model_id)
    _, valid, _ = await invoke(registry, MP4, content_type="video/mp4", model=model)
    assert valid.semantic_outcome == "protocol_valid"
    assert valid.content_type == "video/mp4" and valid.body == MP4
    with pytest.raises(RuntimeProtocolError):
        await invoke(registry, MP4[:-1], content_type="video/mp4", model=model)
    _, legacy, _ = await invoke(
        registry, b'{"data":"legacy-native-result"}', content_type="application/json", model=model,
    )
    assert legacy.semantic_outcome == "protocol_valid"


@pytest.mark.asyncio
async def test_dynamic_published_alias_uses_cosmos_source_not_public_id(registry):
    revision = _cosmos_revision(registry)
    app_id = uuid4()
    public_id = f"app-{app_id.hex}"
    spec = revision.spec.model_copy(update={"app": AppDeploymentIdentity(app_id=app_id, public_model_id=public_id)})
    revision = revision.model_copy(update={"spec": spec, "etag": spec_digest(spec)})
    snapshot = project_dynamic_publications([revision], {(revision.namespace, revision.name): status_view(revision)})
    assert registry.set_dynamic_publications(snapshot, valid_until=datetime.now(UTC) + timedelta(minutes=1))
    model = registry.get(public_id)
    assert model.dynamic_policy.publication.source_model_ref == COSMOS
    assert model.id == public_id and model.id != COSMOS
    _, result, _ = await invoke(registry, MP4, content_type="video/mp4", model=model)
    assert result.semantic_outcome == "protocol_valid"


@pytest.mark.asyncio
@pytest.mark.parametrize("body,expected", [(MP4, OperationStatus.SUCCEEDED), (MP4[:-1], OperationStatus.FAILED)],
                         ids=["valid-mp4", "truncated-mp4"])
async def test_real_worker_completes_only_valid_cosmos_and_externalizes_before_terminal(
    registry, cipher, hasher, body, expected,
):
    revision = _cosmos_revision(registry)
    snapshot = project_dynamic_publications([revision], {(revision.namespace, revision.name): status_view(revision)})
    assert registry.set_dynamic_publications(snapshot, valid_until=datetime.now(UTC) + timedelta(minutes=1))
    store = MemoryStore(cipher, hasher)
    await store.model_deployment_append_revision(ModelDeploymentAppendRequest(
        namespace=revision.namespace, name=revision.name, expected_etag=None, spec=revision.spec,
        action=ModelDeploymentRevisionAction.CREATE, actor_id=uuid4(), actor="test",
        idempotency_key="cosmos-binary-revision-0001",
    ))
    principal = _principal()
    await store.issue_token(token_id=principal.token_id, prefix=principal.token_prefix, pepper_key_id="pepper-v1",
        digest="cosmos-binary-test", request=TokenCreate(principal_id=principal.principal_id,
            tenant_id=principal.tenant_id, scopes={Scope.INFERENCE_INVOKE, Scope.MCP_INVOKE, Scope.USE_NONCLINICAL},
            models={"*"}, max_concurrency=8), created_by="test")
    objects, repository = FakeObjectStore(clock=lambda: datetime.now(UTC)), MemoryArtifactRepository()
    artifacts = ScientificArtifactService(repository=repository, object_store=objects,
                                         allowed_media_types={"application/octet-stream"})

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"ready": True})
        return httpx.Response(200, content=body, headers={"content-type": "video/mp4"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runtime = RuntimeClient(activation_timeout_seconds=2, runtime_timeout_seconds=2,
                                max_response_bytes=8192, client=client)
        worker = service(registry, store, runtime)
        worker.artifact_outputs = ServingOutputArtifactizer(artifacts)
        operation = await worker.admit(principal, AdmissionRequest(model_id=COSMOS, operation="generate-media",
            protocol="native", idempotency_key="cosmos-binary-worker-0001", request_body=b"{}"))
        await repository.register_operation(operation.id, tenant_id=principal.tenant_id)
        await worker.start()
        try:
            final = await wait_status(store, operation.id, {expected}, timeout=5)
            if expected is OperationStatus.SUCCEEDED:
                retained = await store.get_operation_result(operation.id, tenant_id=principal.tenant_id)
                envelope = retained.result
                stream = await artifacts.open_content(UUID(envelope["artifact"]["artifact_id"]),
                                                      tenant_id=principal.tenant_id)
                assert b"".join([chunk async for chunk in stream.chunks]) == body
                assert final.semantic_outcome == "protocol_valid"
            else:
                assert final.error_code == "runtime_protocol_error" and not objects.written
        finally:
            await worker.close()
