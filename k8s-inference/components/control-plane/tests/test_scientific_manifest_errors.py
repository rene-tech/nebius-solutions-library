"""Owned, verified input errors remain actionable without admitting GPU work."""

from __future__ import annotations

import gzip
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from starlette.requests import Request
from test_api_mcp import build_runtime
from test_scientific_batch_production import profile_catalog

from fs2_serve.api import create_app
from fs2_serve.scientific_artifacts import ArtifactNotFoundError
from fs2_serve.scientific_batch.artifact_bridge import ArtifactServiceBridge, SignedArtifactContentReader
from fs2_serve.scientific_batch.profile_catalog import ScientificRequestError


def bridge_for(payload: bytes):
    artifact_id = uuid4()
    pointer = {
        "artifact_id": str(artifact_id),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "media_type": "application/vnd.fs2.scientific-manifest+json",
    }
    record = SimpleNamespace(
        artifact_id=artifact_id,
        media_type=pointer["media_type"],
        size_bytes=len(payload),
        digest="sha256:" + pointer["sha256"],
        to_public_ref=lambda: SimpleNamespace(model_dump=lambda **kwargs: dict(pointer)),
    )
    bridge = ArtifactServiceBridge(
        artifacts=SimpleNamespace(get_artifact=AsyncMock(return_value=record)),
        batches=SimpleNamespace(),
        profiles=profile_catalog(),
        store=SimpleNamespace(),
        content_reader=SimpleNamespace(read=AsyncMock(return_value=payload)),
    )
    return bridge, pointer


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [b"not json", b"\xff", b'{"schema":"wrong"}', json.dumps({
    "schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
    "manifest_id": "bad-semantic-type",
    "entries": [{"name": "source.tar.zst", "semantic_type": "lerobot-bundle", "artifact": {
        "artifact_id": str(uuid4()), "sha256": "a" * 64, "size_bytes": 1,
        "media_type": "application/x-tar", "compression": "zstd",
    }}],
}).encode()])
async def test_verified_malformed_manifest_is_422_not_missing(payload, registry, cipher, hasher):
    bridge, pointer = bridge_for(payload)
    with pytest.raises(ScientificRequestError) as caught:
        await bridge.validate_input(pointer, tenant_id="tenant-a")
    assert caught.value.public_detail
    assert "corrected" in caught.value.public_detail
    app = create_app(build_runtime(registry, cipher, hasher))
    response = await app.exception_handlers[ScientificRequestError](Request({"type": "http"}), caught.value)
    assert response.status_code == 422
    assert json.loads(response.body)["error"]["type"] == "scientific_request_invalid"
    # No manifest-entry lookup, controller, or durable admission is reached.
    bridge.artifacts.get_artifact.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["authorization", "hash", "transport"])
async def test_unowned_or_unverified_manifest_does_not_disclose_input_contract(failure):
    bridge, pointer = bridge_for(b"not json")
    expected = ArtifactNotFoundError
    if failure == "authorization":
        bridge.artifacts.get_artifact.side_effect = ArtifactNotFoundError("not authorized")
    elif failure == "hash":
        bridge.content_reader.read.return_value = b"altered"
    else:
        expected = TimeoutError
        bridge.content_reader.read.side_effect = TimeoutError()
    with pytest.raises(expected):
        await bridge.validate_input(pointer, tenant_id="tenant-b")
    if failure == "authorization":
        bridge.content_reader.read.assert_not_awaited()


def test_published_manifest_schema_is_canonical_and_cannot_mutate_validation():
    catalog = profile_catalog()
    published = catalog.artifact_manifest_schema()
    assert published["properties"]["entries"]["items"]["properties"]["semantic_type"]["pattern"].endswith("*$")
    published.clear()
    assert catalog.artifact_manifest_schema()["properties"]["entries"]
    with pytest.raises(ScientificRequestError):
        catalog.validate_artifact_manifest({})


@pytest.mark.asyncio
@pytest.mark.parametrize("encoded", [False, True])
async def test_signed_reader_preserves_stored_compressed_bytes(encoded):
    plain = b"a tar archive may be served with Content-Encoding gzip" * 100
    payload = gzip.compress(plain) if encoded else plain
    artifact_id = uuid4()

    class StoredStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for offset in range(0, len(payload), 17):
                yield payload[offset:offset + 17]

    def handler(request):
        return httpx.Response(
            200, stream=StoredStream(),
            headers={"Content-Encoding": "gzip"} if encoded else {},
        )

    service = SimpleNamespace(download=AsyncMock(return_value=SimpleNamespace(
        artifact=SimpleNamespace(size_bytes=len(payload)),
        handle=SimpleNamespace(url="https://artifact.example/stored", headers={}),
    )))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reader = SignedArtifactContentReader(service, client)
        actual = await reader.read(artifact_id, tenant_id="tenant-a", maximum_bytes=len(payload))
    assert actual == payload
    assert hashlib.sha256(actual).digest() == hashlib.sha256(payload).digest()
    service.download.assert_awaited_once_with(artifact_id, tenant_id="tenant-a")
