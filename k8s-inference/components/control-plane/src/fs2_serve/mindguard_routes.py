"""Authenticated observational classifier endpoint using existing request telemetry.

This preview is explicitly unbilled; it does not invent a second durable meter.
Budget-constrained keys require the ordinary admission path before being supported.
The enclosing workshop run persists full assessments under its existing ownership.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, model_validator

from .mindguard import (
    MindGuardMessage,
    MindGuardModel,
    MindGuardModelId,
    MindGuardTranscriptAssessment,
    assess_mindguard_transcript,
)
from .models import Principal, Scope
from .request_telemetry import ensure_request_id, observe_request_metadata


class MindGuardAssessRequest(MindGuardModel):
    model: MindGuardModelId
    messages: list[MindGuardMessage] = Field(min_length=1, max_length=128)
    language: Literal["en"] = "en"

    @model_validator(mode="after")
    def bounded_transcript(self) -> MindGuardAssessRequest:
        if not any(message.role == "user" for message in self.messages):
            raise ValueError("a transcript must contain a user turn")
        if sum(len(message.content) for message in self.messages) > 200_000:
            raise ValueError("transcript exceeds the observation request size limit")
        return self


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

    @router.post("/v1/mindguard/assess", response_model=MindGuardAssessResponse)
    async def assess(
        body: MindGuardAssessRequest,
        request: Request,
        identity: Principal = identity_dependency,
    ) -> MindGuardAssessResponse:
        try:
            identity.require(Scope.INFERENCE_INVOKE, body.model)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        request.state.model_id = body.model
        request.state.principal = identity
        observe_request_metadata(principal=identity, model_id=body.model)
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
