"""PAIDF native contracts preserve exact model requests and original responses."""

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from test_artifact_inputs import _Artifacts, _reference
from test_cosmos_native_runtime import MP4, model_for

from fs2_serve.artifact_inputs import ArtifactInputMaterializer
from fs2_serve.model_input_contracts import _paidf_chat
from fs2_serve.runtime import _PAIDF_CHAT_MODELS, RuntimeClient, RuntimeProtocolError


def test_public_schema_resource_is_derived_from_reviewed_runtime_contract():
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location(
        "paidf_contract_test", root / "models/general-media/paidf-chat/adapter/contracts.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    resource = json.loads(
        (root / "components/control-plane/src/fs2_serve/model_input_schemas/paidf-chat.json").read_text()
    )
    assert module.MODELS == _PAIDF_CHAT_MODELS
    assert resource == {key: module.contract(key) for key in module.MODELS}


@pytest.mark.asyncio
async def test_full_video_artifact_materializes_at_native_boundary_byte_identically(registry, monkeypatch):
    app_id = "qwen3-6-27b-fp8"
    artifact = _reference(MP4, media_type="video/mp4")
    schema, _ = _paidf_chat(app_id)
    body = {
        "model": _PAIDF_CHAT_MODELS[app_id][0],
        "messages": [
            {"role": "user", "content": [{"type": "video_url", "video_url": {"url": artifact.model_dump(mode="json")}}]}
        ],
        "temperature": 0.3,
        "top_p": 0.95,
        "frequency_penalty": 1.05,
        "max_tokens": 4096,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    Draft202012Validator(schema).validate(body)
    monkeypatch.setattr("fs2_serve.artifact_inputs.contract_for", lambda *_: SimpleNamespace(input_schema=schema))
    materializer = ArtifactInputMaterializer(_Artifacts(MP4, artifact))
    actual = json.loads(
        await materializer.materialize(
            model_for(registry, app_id), "native", tenant_id="tenant-a", request_body=json.dumps(body).encode()
        )
    )
    url = actual["messages"][0]["content"][0]["video_url"]["url"]
    assert url.startswith("data:video/mp4;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == MP4
    actual["messages"] = body["messages"]
    assert actual == body


def envelope(stream=False):
    model, revision = _PAIDF_CHAT_MODELS["qwen2-5-14b-instruct"]
    request = {"model": model, "messages": [{"role": "user", "content": "fixture"}], "stream": stream}
    result = {
        "model": model,
        "choices": [{"message": {"content": "fixture"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2},
    }
    raw = json.dumps(result)
    if stream:
        raw = (
            "data: "
            + json.dumps({"model": model, "choices": [{"delta": {"content": "fixture"}, "finish_reason": "stop"}]})
            + "\n\ndata: [DONE]\n\n"
        )
    value = {
        "schema": "scientific-reference-chat/v1",
        "model": model,
        "model_revision": revision,
        "stream": stream,
        "content_type": "text/event-stream" if stream else "application/json",
        "response_body": raw,
        "response_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "request_sha256": hashlib.sha256(
            json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest(),
        "usage": None if stream else result["usage"],
    }
    return request, value


@pytest.mark.parametrize("stream", [False, True])
def test_native_wrapper_validates_request_response_model_revision_and_original_bytes(stream):
    request, value = envelope(stream)
    RuntimeClient._paidf_chat_valid(
        "qwen2-5-14b-instruct", json.dumps(value).encode(), "application/json", json.dumps(request).encode()
    )
    for key, replacement in [
        ("model", "replacement"),
        ("model_revision", "unqualified"),
        ("response_sha256", "0" * 64),
        ("stream", not stream),
        ("usage", {"prompt_tokens": 99}),
    ]:
        with pytest.raises(RuntimeProtocolError):
            RuntimeClient._paidf_chat_valid(
                "qwen2-5-14b-instruct",
                json.dumps(dict(value, **{key: replacement})).encode(),
                "application/json",
                json.dumps(request).encode(),
            )
    if not stream:
        usage = RuntimeClient._reported_usage("openai-chat", json.dumps(value).encode())
        assert usage.input_tokens == 4 and usage.output_tokens == 2
