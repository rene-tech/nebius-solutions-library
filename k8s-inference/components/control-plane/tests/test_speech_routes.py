"""Compatibility transport tests; real GPU/public acceptance is separate."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from test_model_input_contracts import selected

from fs2_serve.models import OperationStatus, Principal, Scope
from fs2_serve.scientific_run_result import ArtifactRef
from fs2_serve.speech_routes import COMPATIBILITY_MAX_BYTES, speech_router

MODEL = "nemotron-speech-en-0-6b"


@pytest.fixture
def speech(registry):
    principal = Principal(token_id=uuid4(), token_prefix="fst_test", tenant_id="speech-a",
                          principal_id="person-a", scopes=frozenset({str(Scope.INFERENCE_INVOKE)}),
                          models=frozenset({MODEL}))
    model = selected(registry, MODEL)
    routes = Mock()
    routes.get.return_value = model

    def authorize(_model, identity, **kwargs):
        identity.require(Scope.INFERENCE_INVOKE, model_id=kwargs["requested_model_id"])

    routes.authorize_principal.side_effect = authorize
    operation = SimpleNamespace(id=uuid4(), status=OperationStatus.SUCCEEDED)
    admission = SimpleNamespace(admit=AsyncMock(return_value=operation), wait=AsyncMock(return_value=operation))
    upload = SimpleNamespace(operation_id=uuid4(), upload_id=uuid4())
    artifact = ArtifactRef(artifact_id=str(uuid4()), sha256=hashlib.sha256(b"audio").hexdigest(),
                           size_bytes=5, media_type="audio/wav", compression="none")
    uploads = SimpleNamespace(begin=AsyncMock(return_value=upload), store_content=AsyncMock(),
                              finalize=AsyncMock(return_value=artifact))
    store = SimpleNamespace(get_operation=AsyncMock(return_value=SimpleNamespace(status=OperationStatus.QUEUED)),
                            get_operation_result=AsyncMock(return_value=SimpleNamespace(
                                result={"text": "A complete transcript.", "audio_seconds": 8.0})))

    async def identity():
        return principal

    async def response(current):
        return JSONResponse({"operation_id": str(current.id), "status": current.status}, status_code=202,
                            headers={"location": f"/v1/operations/{current.id}"})

    app = FastAPI()

    @app.exception_handler(PermissionError)
    async def denied(request, error):
        return JSONResponse({"error": "permission_denied"}, status_code=403)

    app.include_router(speech_router(principal=identity, registry=routes, admission=admission, store=store,
                                    uploads=uploads, wait_seconds=30, operation_response=response))
    return SimpleNamespace(client=TestClient(app), uploads=uploads, store=store, admission=admission,
                           operation=operation, artifact=artifact, routes=routes)


def submit(speech, *, data=None, content=b"audio", media_type="audio/wav", headers=None):
    return speech.client.post("/v1/audio/transcriptions", data={"model": MODEL, **(data or {})},
                              files={"file": ("recording.wav", content, media_type)}, headers=headers)


@pytest.mark.parametrize("response_format", ["json", "verbose_json", "text"])
def test_file_uses_authorized_artifact_and_native_operation(speech, response_format):
    response = submit(speech, data={"response_format": response_format, "language": "en"},
                      headers={"idempotency-key": "speech-recording-1"})
    assert response.status_code == 200, response.text
    if response_format == "text":
        assert response.text == "A complete transcript."
    elif response_format == "json":
        assert response.json() == {"text": "A complete transcript."}
    else:
        assert response.json()["audio_seconds"] == 8.0
    assert response.headers["x-fs2-operation-id"] == str(speech.operation.id)
    request = speech.admission.admit.call_args.args[1]
    assert request.protocol == "native" and request.operation == "transcribe"
    assert request.idempotency_key == "speech-recording-1"
    payload = json.loads(request.request_body)
    assert payload["audio"] == speech.artifact.model_dump(mode="json")
    assert payload["options"] == {"model": "nemotron-speech-en-0.6b", "language": "en"}
    assert speech.uploads.begin.call_args.kwargs["principal"].tenant_id == "speech-a"
    assert speech.uploads.store_content.call_args.kwargs["content"] == b"audio"


@pytest.mark.parametrize(("data", "code"), [
    ({"language": "fr"}, 422), ({"language": "auto"}, 422), ({"prompt": "secretly ignore me"}, 422),
    ({"response_format": "srt"}, 422), ({"model": "not-granted"}, 403),
])
def test_invalid_or_forbidden_request_never_reserves_upload(speech, data, code):
    response = submit(speech, data=data)
    assert response.status_code == code, response.text
    speech.uploads.begin.assert_not_called()
    speech.admission.admit.assert_not_called()


@pytest.mark.parametrize(("content", "media_type", "headers", "code"), [
    (b"", "audio/wav", None, 422), (b"audio", "text/plain", None, 415),
    (b"audio", "audio/wav", {"idempotency-key": "x"}, 400),
    (b"x" * (COMPATIBILITY_MAX_BYTES + 1), "audio/wav", None, 413),
], ids=["empty", "wrong-media", "short-key", "oversized"])
def test_upload_validation_is_bounded(speech, content, media_type, headers, code):
    assert submit(speech, content=content, media_type=media_type, headers=headers).status_code == code
    speech.uploads.begin.assert_not_called()


def test_cold_transcription_has_a_durable_pollable_operation(speech):
    speech.admission.wait.return_value = SimpleNamespace(id=speech.operation.id, status=OperationStatus.QUEUED)
    response = submit(speech)
    assert response.status_code == 202 and response.json()["status"] == "queued"
    assert response.headers["location"] == f"/v1/operations/{speech.operation.id}"
    speech.store.get_operation_result.assert_not_called()


def test_completed_upload_is_not_overwritten_on_idempotent_retry(speech):
    speech.store.get_operation.return_value = SimpleNamespace(status=OperationStatus.SUCCEEDED)
    assert submit(speech).status_code == 200
    speech.uploads.store_content.assert_not_called()
    speech.uploads.finalize.assert_awaited_once()
