"""Resolve a GROMACS companion's destination from its durable submitting user."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse

from ..store import Store
from .capability import ScientificWorkloadCapabilityAuthority
from .workload_routes import WorkloadBatchRepository, authorize_workload_capability


def gromacs_storage_router(
    *, authority: ScientificWorkloadCapabilityAuthority, batches: WorkloadBatchRepository, store: Store, storage: Any
) -> APIRouter:
    router = APIRouter(prefix="/internal/scientific-workloads/gromacs", tags=["scientific-workloads-internal"])

    @router.get("/storage")
    async def destination(authorization: Annotated[str | None, Header()] = None) -> JSONResponse:
        capability, _, _ = await authorize_workload_capability(authority, batches, authorization)
        if (capability.model_id, capability.stage_id, capability.collector_id) != (
            "gromacs",
            "workflow",
            "gromacs-workflow-v1",
        ):
            raise HTTPException(403, "this workload has no customer checkpoint export")
        if storage is None:
            raise HTTPException(503, "customer storage is not configured")
        operation = await store.get_operation(capability.operation_id, tenant_id=capability.tenant_id)
        token = await store.get_token(operation.token_id)
        if (
            operation.model_id != capability.model_id
            or operation.status.terminal
            or token.revoked_at is not None
            or (token.expires_at is not None and token.expires_at <= datetime.now(UTC))
            or (token.tenant_id, token.principal_id) != (operation.tenant_id, operation.principal_id)
        ):
            raise HTTPException(409, "the submitting user's invocation is no longer active")
        if (await storage.policy(operation.tenant_id)).mode == "disabled":
            raise HTTPException(409, "customer storage is disabled")
        view = await storage.view(operation.tenant_id, operation.principal_id)
        if view.state != "ready":
            raise HTTPException(409, "customer storage is not ready for checkpoint export")
        credentials = await storage.repository.disclose(operation.tenant_id, operation.principal_id)
        return JSONResponse(
            {**credentials.model_dump(), "attempt_number": capability.attempt_number},
            headers={"Cache-Control": "no-store"},
        )

    return router
