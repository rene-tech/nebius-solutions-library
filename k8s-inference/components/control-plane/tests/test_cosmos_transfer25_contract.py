"""Independent Transfer contract: real runtime client and artifact boundary."""

import base64
import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fs2_serve.artifact_inputs import ArtifactInputError, ArtifactInputMaterializer
from fs2_serve.artifact_outputs import ServingOutputArtifactizer
from fs2_serve.model_input_contracts import _cosmos_transfer25, _examples
from fs2_serve.runtime import RuntimeProtocolError
from fs2_serve.scientific_artifacts import MemoryArtifactRepository, ScientificArtifactService
from jsonschema import Draft202012Validator, ValidationError
from test_artifact_inputs import _Artifacts, _reference
from test_cosmos_native_runtime import MP4, invoke, model_for, png
from test_scientific_artifacts import FakeObjectStore

MODEL = "cosmos-transfer2.5-2b"


def test_public_contract_requires_owned_artifact_and_matches_adapter_bounds():
    schema = _cosmos_transfer25()
    validator = Draft202012Validator(schema)
    example = _examples(MODEL)[0]
    validator.validate(example)
    assert schema["properties"]["video"]["x-fs2-artifact-materialization"] == "base64"
    for field, value in [
        ("guidance", 7.5), ("guidance", 8), ("guidance", True),
        ("control_weight", 0), ("control_weight", 1.1),
        ("num_steps", 51), ("num_steps", 0), ("seed", -1),
        ("video", "https://example.org/video.mp4"), ("video", "/customer/video.mp4"),
        ("video", base64.b64encode(MP4).decode()), ("output_delivery", "inline"),
        ("endpoint", "http://127.0.0.1"), ("guardrails", False),
    ]:
        with pytest.raises(ValidationError):
            validator.validate({**example, field: value})


@pytest.mark.asyncio
async def test_transfer_mp4_dispatch_artifactization_and_hash_preserving_download(registry):
    operation, result, _ = await invoke(registry, MP4, content_type="video/mp4", model=model_for(registry, MODEL))
    assert result.semantic_outcome == "protocol_valid"
    repository = MemoryArtifactRepository()
    await repository.register_operation(operation.id, tenant_id=operation.tenant_id)
    artifacts = ScientificArtifactService(repository=repository,
        object_store=FakeObjectStore(clock=lambda: datetime.now(UTC)), allowed_media_types={"application/octet-stream"})
    published = await ServingOutputArtifactizer(artifacts).externalize(operation, result)
    envelope = json.loads(published.body)
    assert envelope["artifact"]["sha256"] == hashlib.sha256(MP4).hexdigest()
    stream = await artifacts.open_content(UUID(envelope["artifact"]["artifact_id"]), tenant_id=operation.tenant_id)
    assert b"".join([chunk async for chunk in stream.chunks]) == MP4


@pytest.mark.asyncio
@pytest.mark.parametrize("body,content_type", [
    (MP4[:-1], "video/mp4"), (b'{"ok":true}', "application/json"),
    (png(), "image/png"), (MP4, "application/octet-stream"),
])
async def test_transfer_rejects_non_mp4_and_malformed_success(registry, body, content_type):
    with pytest.raises(RuntimeProtocolError):
        await invoke(registry, body, content_type=content_type, model=model_for(registry, MODEL))


@pytest.mark.asyncio
async def test_video_materializes_only_at_dispatch_with_tenant_and_hash_check(registry, monkeypatch):
    from types import SimpleNamespace

    reference = _reference(MP4, media_type="video/mp4")
    artifacts = _Artifacts(MP4, reference)
    materializer = ArtifactInputMaterializer(artifacts)
    monkeypatch.setattr("fs2_serve.artifact_inputs.contract_for",
                        lambda *_: SimpleNamespace(input_schema=_cosmos_transfer25()))
    body = json.dumps({"video": reference.model_dump(mode="json"), "prompt": "Overcast"}).encode()
    output = await materializer.materialize(model_for(registry, MODEL), "native",
                                           tenant_id="tenant-a", request_body=body)
    assert base64.b64decode(json.loads(output)["video"]) == MP4
    assert artifacts.calls == [(UUID(reference.artifact_id), "tenant-a")]
    with pytest.raises(ArtifactInputError, match="not found"):
        await materializer.materialize(model_for(registry, MODEL), "native", tenant_id="other", request_body=body)
    altered = json.loads(body)
    altered["video"]["sha256"] = "0" * 64
    with pytest.raises(ArtifactInputError, match="does not match"):
        await materializer.materialize(model_for(registry, MODEL), "native", tenant_id="tenant-a",
                                       request_body=json.dumps(altered).encode())
