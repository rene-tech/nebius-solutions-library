"""Observational classifiers using the normal durable admission and scale-zero lane.

The old unbilled preview is retained only until an App is registered. Registered
Apps never bypass admission, budget accounting or a disabled publication.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .admission import AdmissionService
from .mindguard import (
    MindGuardModel,
    MindGuardTranscriptAssessment,
    assess_mindguard_transcript,
)
from .mindguard_contracts import MindGuardAssessRequest
from .models import AdmissionRequest, OperationStatus, Principal, Scope
from .registry import Registry
from .request_telemetry import ensure_request_id, observe_request_metadata
from .store import Store


class MindGuardObservedUsage(MindGuardModel):
    accounting_mode: Literal["observational_unbilled"] = "observational_unbilled"
    durable_operation_id: None = None
    completed_classifier_requests: int
    input_tokens: int | None
    output_tokens: int | None
    measured_runtime_latency_ms: float
    gpu_seconds: None = None


class MindGuardAssessResponse(MindGuardTranscriptAssessment):
    request_id: UUID
    usage: MindGuardObservedUsage


def mindguard_router(
    *,
    principal: Callable[..., Awaitable[Principal]],
    endpoints: Mapping[str, str | None],
    client: httpx.AsyncClient | None = None,
    admission: AdmissionService | None = None,
    store: Store | None = None,
    registry: Registry | None = None,
) -> APIRouter:
    """Mount using the normal PAT dependency; endpoints must come from operator settings.

    The optional client supports connection pooling when owned by application lifespan.
    Without one, each observation request owns and closes its HTTP client. Existing
    principal/model checks run even when the selected endpoint is unavailable.
    """
    configured = dict(endpoints)
    for model_id, endpoint in configured.items():
        if model_id not in {"mindguard-4b", "mindguard-8b"}:
            raise ValueError("unknown public MindGuard classifier")
        if endpoint:
            parsed = urlsplit(endpoint)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or not parsed.hostname.endswith(".svc.cluster.local")
                or parsed.path != "/v1"
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("MindGuard endpoint must be an internal runtime service base URL ending /v1")
    router = APIRouter(tags=["MindGuard safety classifiers"])
    identity_dependency = Depends(principal)

    @router.post("/v1/mindguard/assess", response_model=None)
    async def assess(
        body: MindGuardAssessRequest,
        request: Request,
        identity: Principal = identity_dependency,
    ) -> MindGuardAssessResponse | JSONResponse:
        try:
            identity.require(Scope.INFERENCE_INVOKE, body.model)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        request.state.model_id = body.model
        request.state.principal = identity
        observe_request_metadata(principal=identity, model_id=body.model)
        # The old preview stays available during the staged catalog migration.
        # Once its canonical App exists, never bypass a failed/disabled route.
        registered = False
        if admission is not None and store is not None and registry is not None:
            try:
                registry.get(body.model, require_enabled=False)
                registered = True
            except KeyError:
                pass
        if registered and admission is not None and store is not None:
            try:
                wait = float(request.headers.get("x-fs2-wait-seconds", "30"))
                if not 0 <= wait <= 30:
                    raise ValueError
            except ValueError:
                raise HTTPException(422, "x-fs2-wait-seconds must be between 0 and 30") from None
            operation = await admission.admit(
                identity,
                AdmissionRequest(
                    model_id=body.model,
                    operation="assess-transcript",
                    protocol="native",
                    request_body=body.model_dump_json().encode(),
                    idempotency_key=request.headers.get("idempotency-key") or "mindguard-" + str(uuid4()),
                    deadline_at=datetime.now(UTC) + timedelta(hours=2),
                ),
            )
            request.state.operation_id = operation.id
            operation = await admission.wait(operation.id, tenant_id=identity.tenant_id, seconds=wait)
            headers = {
                "x-fs2-operation-id": str(operation.id),
                "x-fs2-idempotent-replay": str(operation.reused).lower(),
                "cache-control": "no-store",
            }
            if operation.status is OperationStatus.SUCCEEDED:
                stored_result = await store.get_operation_result(operation.id, tenant_id=identity.tenant_id)
                return JSONResponse(
                    {
                        **stored_result.result,
                        "request_id": str(ensure_request_id(request.scope)),
                        "usage": {
                            "accounting_mode": "durable_operation",
                            "durable_operation_id": str(operation.id),
                            "input_tokens": operation.input_tokens,
                            "output_tokens": operation.output_tokens,
                        },
                    },
                    headers=headers,
                )
            if operation.status.terminal:
                return JSONResponse(
                    {
                        "error": {"code": operation.error_code or "classification_failed"},
                        "operation": operation.model_dump(mode="json"),
                    },
                    status_code=operation.http_status or 502,
                    headers=headers,
                )
            return JSONResponse(
                operation.model_dump(mode="json"),
                status_code=202,
                headers={**headers, "location": f"/v1/operations/{operation.id}", "retry-after": "1"},
            )
        if identity.request_budget is not None or identity.gpu_seconds_budget is not None:
            raise HTTPException(
                503,
                {
                    "code": "mindguard_metered_admission_required",
                    "message": "Budget-constrained keys need durable admission; this observation is unbilled.",
                },
            )
        if client is not None:
            result = await assess_mindguard_transcript(
                body.messages,
                model_id=body.model,
                endpoint=configured.get(body.model),
                client=client,
                language=body.language,
            )
        else:
            async with httpx.AsyncClient() as request_client:
                result = await assess_mindguard_transcript(
                    body.messages,
                    model_id=body.model,
                    endpoint=configured.get(body.model),
                    client=request_client,
                    language=body.language,
                )
        complete_usage = all(
            row.status == "completed" and row.input_tokens is not None and row.output_tokens is not None
            for row in result.assessments
        )
        return MindGuardAssessResponse(
            **result.model_dump(),
            request_id=ensure_request_id(request.scope),
            usage=MindGuardObservedUsage(
                completed_classifier_requests=result.evaluated_user_turns,
                input_tokens=sum(row.input_tokens or 0 for row in result.assessments) if complete_usage else None,
                output_tokens=sum(row.output_tokens or 0 for row in result.assessments) if complete_usage else None,
                measured_runtime_latency_ms=sum(row.latency_ms or 0 for row in result.assessments),
            ),
        )

    return router
