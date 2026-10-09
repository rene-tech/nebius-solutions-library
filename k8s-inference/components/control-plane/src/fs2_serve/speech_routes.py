"""Small-file STT compatibility over the same durable native audio operations.

Large recordings use the existing upload/finalize/native-invoke endpoints. This
adapter never forwards multipart bytes to the model or an LLM/MCP tool.
"""

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from jsonschema import Draft202012Validator
from starlette.datastructures import UploadFile

from .admission import AdmissionService
from .model_input_contracts import contract_for
from .models import (
    MAX_IDEMPOTENCY_KEY_LENGTH,
    MIN_IDEMPOTENCY_KEY_LENGTH,
    AdmissionRequest,
    OperationStatus,
    Principal,
    Scope,
)
from .registry import Registry
from .scientific_input_uploads import ScientificInputUploadRequest, ScientificInputUploadService
from .store import Store

COMPATIBILITY_MAX_BYTES = 8 * 1024 * 1024
SPEECH_MODELS = frozenset({"nemotron-speech-en-0-6b", "nemotron-speech-multilingual-0-6b"})


def speech_router(
    *,
    principal: Callable[..., Awaitable[Principal]],
    registry: Registry,
    admission: AdmissionService,
    store: Store,
    uploads: ScientificInputUploadService | None,
    wait_seconds: float,
    operation_response: Callable[[Any], Awaitable[Response]],
) -> APIRouter:
    router = APIRouter(tags=["Speech"])

    @router.post("/v1/audio/transcriptions")
    async def transcribe(request: Request, identity: Annotated[Principal, Depends(principal)]) -> Response:
        """Transcribe a complete small recording; long files use artifact jobs.

        Fields: file, model, optional language and response_format
        (json/verbose_json/text). A cold/queued request returns a durable 202;
        clients must poll Location, never resubmit with a fresh idempotency key.
        """
        identity.require(Scope.INFERENCE_INVOKE)
        if uploads is None:
            raise HTTPException(503, "audio upload storage is not configured")
        if not request.headers.get("content-type", "").startswith("multipart/form-data;"):
            raise HTTPException(415, "multipart/form-data with file and model is required")
        key = request.headers.get("idempotency-key") or f"speech-{uuid4()}"
        if not MIN_IDEMPOTENCY_KEY_LENGTH <= len(key) <= MAX_IDEMPOTENCY_KEY_LENGTH:
            raise HTTPException(400, "Idempotency-Key length is invalid")
        async with request.form(max_files=1, max_fields=4, max_part_size=4096) as form:
            allowed = {"file", "model", "language", "response_format"}
            if set(form) - allowed or len(form.multi_items()) != len(form):
                raise HTTPException(422, "unknown or duplicate transcription fields")
            file, model_id = form.get("file"), form.get("model")
            language, response_format = form.get("language"), form.get("response_format", "json")
            if not isinstance(file, UploadFile) or not isinstance(model_id, str):
                raise HTTPException(422, "file and model are required")
            if language is not None and not isinstance(language, str):
                raise HTTPException(422, "language must be a locale string")
            if response_format not in ("json", "verbose_json", "text"):
                raise HTTPException(422, "response_format must be json, verbose_json or text")
            model = registry.get(model_id)
            contract = contract_for(model, "native")
            if contract.model_ref not in SPEECH_MODELS:
                raise HTTPException(422, "model is not a speech transcription App")
            registry.authorize_principal(model, identity, requested_model_id=model_id, surface="mcp")
            registry.authorize(model, identity.scopes)
            options = {"model": contract.input_schema["properties"]["options"]["properties"]["model"]["const"]}
            if language:
                options["language"] = language
            if not Draft202012Validator(contract.input_schema["properties"]["options"]).is_valid(options):
                raise HTTPException(422, "invalid speech options; use an enabled locale for the selected App")
            # Resolve the selected worker's model-wide defaults in its native
            # contract; do not silently accept unsupported decoding parameters.
            media_type = (file.content_type or "").split(";", 1)[0].lower()
            accepted = contract.input_schema["properties"]["audio"]["properties"]["media_type"]["enum"]
            if media_type not in accepted:
                raise HTTPException(415, "unsupported audio media type; send the actual file content type")
            content = await file.read(COMPATIBILITY_MAX_BYTES + 1)
            if not content:
                raise HTTPException(422, "audio file is empty")
            if len(content) > COMPATIBILITY_MAX_BYTES:
                raise HTTPException(
                    413, "compatibility limit is 8 MiB; use model artifact upload and native transcription",
                )
        # Namespace the upload key without shortening the inference key's
        # identity. Duplicate file/form calls reuse the same upload operation.
        upload_key = "speech-upload-" + hashlib.sha256(key.encode()).hexdigest()
        upload = await uploads.begin(
            principal=identity,
            request=ScientificInputUploadRequest(
                model_id=model_id, sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content), media_type=media_type,
            ),
            idempotency_key=upload_key,
        )
        upload_operation = await store.get_operation(upload.operation_id, tenant_id=identity.tenant_id)
        if upload_operation.status is not OperationStatus.SUCCEEDED:
            await uploads.store_content(
                principal=identity, operation_id=upload.operation_id, upload_id=upload.upload_id,
                content=content, declared_media_type=media_type, declared_size_bytes=len(content),
            )
        artifact = await uploads.finalize(
            principal=identity, operation_id=upload.operation_id, upload_id=upload.upload_id,
        )
        payload = {"audio": artifact.model_dump(mode="json"), "options": options}
        operation = await admission.admit(identity, AdmissionRequest(
            model_id=model_id, operation="transcribe", protocol="native", idempotency_key=key,
            request_body=json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
            request_content_type="application/json", traceparent=request.headers.get("traceparent"),
        ))
        request.state.model_id, request.state.operation_id = model_id, operation.id
        current = await admission.wait(operation.id, tenant_id=identity.tenant_id, seconds=wait_seconds)
        if current.status is not OperationStatus.SUCCEEDED:
            return await operation_response(current)
        result = await store.get_operation_result(operation.id, tenant_id=identity.tenant_id)
        headers = {"x-fs2-operation-id": str(operation.id), "location": f"/v1/operations/{operation.id}"}
        if response_format == "text":
            return PlainTextResponse(result.result["text"], headers=headers)
        body = result.result if response_format == "verbose_json" else {"text": result.result["text"]}
        return JSONResponse(body, headers=headers)

    return router
