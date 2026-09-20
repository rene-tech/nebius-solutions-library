"""Operator API for durable campaigns. Uses the existing operator session/RBAC."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from .access_models import OperatorPrincipal, OperatorRole
from .performance import (
    CampaignCreate,
    ClaimRequest,
    PerformanceRepository,
    TrialLease,
    TrialResult,
    summarize_profiles,
)


def performance_router(pool: Any, operator: Any, access: Any, envelope: Any) -> APIRouter:
    router = APIRouter(prefix="/admin/api/v1/performance", tags=["performance"])
    operator_dep = Depends(operator)

    def repository() -> PerformanceRepository:
        if pool is None:
            raise HTTPException(503, "durable performance registry requires PostgreSQL")
        return PerformanceRepository(pool)

    @router.get("/campaigns")
    async def campaigns(
        limit: int = Query(default=50, ge=1, le=200),
        identity: OperatorPrincipal = operator_dep,
    ) -> Any:
        await access.authorize_global(identity, OperatorRole.VIEWER, action="benchmark.list")
        return envelope({"items": await repository().list(limit), "mode": "advisory"})

    @router.post("/campaigns", status_code=201)
    async def create(payload: CampaignCreate, identity: OperatorPrincipal = operator_dep) -> Any:
        await access.authorize_global(identity, OperatorRole.ADMIN, action="benchmark.create")
        return envelope(await repository().create(payload, identity.subject))

    @router.get("/campaigns/{campaign_id}")
    async def detail(campaign_id: UUID, identity: OperatorPrincipal = operator_dep) -> Any:
        await access.authorize_global(identity, OperatorRole.VIEWER, action="benchmark.read")
        campaign = await repository().detail(campaign_id)
        return envelope({**campaign, "profiles": summarize_profiles(campaign["trials"])})

    @router.post("/campaigns/{campaign_id}/claim")
    async def claim(
        campaign_id: UUID,
        payload: ClaimRequest,
        identity: OperatorPrincipal = operator_dep,
    ) -> Any:
        await access.authorize_global(identity, OperatorRole.ADMIN, action="benchmark.claim")
        return envelope({"trial": await repository().claim(campaign_id, payload)})

    @router.post("/trials/{trial_id}/heartbeat")
    async def heartbeat(
        trial_id: UUID,
        payload: TrialLease,
        identity: OperatorPrincipal = operator_dep,
    ) -> Any:
        await access.authorize_global(identity, OperatorRole.ADMIN, action="benchmark.heartbeat")
        await repository().heartbeat(trial_id, payload)
        return envelope({"renewed": True})

    @router.post("/trials/{trial_id}/result")
    async def finish(
        trial_id: UUID,
        payload: TrialResult,
        identity: OperatorPrincipal = operator_dep,
    ) -> Any:
        await access.authorize_global(identity, OperatorRole.ADMIN, action="benchmark.finish")
        await repository().finish(trial_id, payload)
        return envelope({"committed": True})

    return router
