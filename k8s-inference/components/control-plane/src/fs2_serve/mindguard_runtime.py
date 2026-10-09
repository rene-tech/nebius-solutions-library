"""MindGuard transcript execution inside the platform's ordinary operation lease.

The GPU runtime and checkpoints stay unchanged. One admitted operation evaluates
every user-turn prefix, with the existing observational parser and no enforcement.
The normal worker supplies cancellation, concurrency, leases and idle demand.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import httpx

from .mindguard import assess_mindguard_transcript
from .mindguard_contracts import MindGuardAssessRequest
from .models import ClaimedOperation, ReportedUsage, RuntimeResult

if TYPE_CHECKING:
    from .registry import OperationalModel
    from .runtime import RuntimeClient


async def invoke_mindguard(
    runtime: RuntimeClient,
    model: OperationalModel,
    operation: ClaimedOperation,
    request_body: bytes,
) -> RuntimeResult:
    from .runtime import _DEBUG_CAPTURE_EXTENSION, RuntimeProtocolError, _UpstreamCapture

    request = MindGuardAssessRequest.model_validate_json(request_body)
    source_model = model.dynamic_policy.publication.source_model_ref if model.dynamic_policy else model.id
    if request.model != source_model or source_model not in {"mindguard-4b", "mindguard-8b"}:
        raise RuntimeProtocolError("MindGuard payload and selected checkpoint differ")
    started = time.monotonic()

    class CapturedClient:
        def __init__(self) -> None:
            self.calls = 0

        async def post(self, url: str, **kwargs: Any) -> httpx.Response:
            self.calls += 1
            payload = json.dumps(kwargs.pop("json"), separators=(",", ":")).encode()
            headers = {
                **runtime._correlation_headers(operation),
                "content-type": "application/json",
                **kwargs.pop("headers", {}),
            }
            stream = runtime.client.stream("POST", url, content=payload, headers=headers, **kwargs)
            async with runtime._debug_stream(
                stream, operation, "/v1/chat/completions", payload, headers, self.calls
            ) as response:
                # Classifier output is at most fifteen tokens. Bound the
                # transport before asking the existing parser to inspect JSON.
                body = bytearray()
                capture = response.extensions.get(_DEBUG_CAPTURE_EXTENSION)
                if isinstance(capture, _UpstreamCapture):
                    capture.read_started = True
                async for part in response.aiter_bytes():
                    if isinstance(capture, _UpstreamCapture):
                        capture.observe(part)
                    body.extend(part)
                    if len(body) > min(65536, runtime.max_response_bytes):
                        raise RuntimeProtocolError("MindGuard response exceeds its bounded output")
                if isinstance(capture, _UpstreamCapture):
                    capture.finished()
                return httpx.Response(
                    response.status_code, content=bytes(body), headers=response.headers, request=response.request
                )

    result = await assess_mindguard_transcript(
        request.messages,
        model_id=request.model,
        endpoint=model.binding.service_origin.rstrip("/") + "/v1",
        client=CapturedClient(),
        language=request.language,
    )
    identity, lifecycle = await runtime._trusted_runtime_observation(operation, model)
    measured = all(row.input_tokens is not None and row.output_tokens is not None for row in result.assessments)
    completed = result.status == "completed"
    return RuntimeResult(
        status_code=200 if completed else 502,
        content_type="application/json",
        body=result.model_dump_json().encode(),
        elapsed_seconds=time.monotonic() - started,
        runtime=identity,
        lifecycle=lifecycle,
        semantic_outcome="protocol_valid" if completed else "invalid_classification",
        failure_code=None if completed else "mindguard_assessment_incomplete",
        failure_detail=None if completed else "The observational classifier did not evaluate every user turn.",
        usage=ReportedUsage(
            input_tokens=sum(row.input_tokens or 0 for row in result.assessments),
            output_tokens=sum(row.output_tokens or 0 for row in result.assessments),
        )
        if measured
        else None,
    )
