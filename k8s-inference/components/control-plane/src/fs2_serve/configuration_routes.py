"""Authenticated admin routes for declarative configuration workflows."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from .access import AdminAccessService
from .access_models import OperatorPrincipal, OperatorRole
from .admin import AdminProblemError
from .admin_models import AdminEnvelope
from .configuration import ConfigurationProblemError, ConfigurationService
from .configuration_models import (
    ConfigurationDiff,
    ConfigurationPlan,
    ConfigurationProposal,
    ConfigurationRevision,
    ConfigurationValidation,
    ReconcileRequest,
    ReconciliationStatus,
    RollbackPlan,
    RollbackRequest,
)


def configuration_router(
    *,
    service: ConfigurationService,
    access: AdminAccessService,
    operator_dependency: Callable[..., Any],
    envelope: Callable[[Any], AdminEnvelope[Any]],
    problem_responses: dict[int | str, dict[str, Any]],
) -> APIRouter:
    """Build routes only after the real session dependency is available."""

    router = APIRouter(dependencies=[Depends(operator_dependency)])

    def identity(request: Request) -> OperatorPrincipal:
        value = getattr(request.state, "operator_principal", None)
        if not isinstance(value, OperatorPrincipal):
            raise AdminProblemError(401, "operator_session_required", "operator session is required")
        return value

    async def authorize(request: Request, role: OperatorRole, action: str) -> OperatorPrincipal:
        value = identity(request)
        await access.authorize_global(value, role, action=action)
        return value

    def translate(exc: ConfigurationProblemError) -> AdminProblemError:
        return AdminProblemError(exc.status_code, exc.code, exc.detail)

    @router.get(
        "/admin/api/v1/configuration",
        response_model=AdminEnvelope[ConfigurationRevision],
        responses=problem_responses,
    )
    async def read_configuration(request: Request) -> AdminEnvelope[ConfigurationRevision]:
        await authorize(request, OperatorRole.VIEWER, "configuration.read")
        try:
            return envelope(await service.read())
        except ConfigurationProblemError as exc:
            raise translate(exc) from None

    @router.post(
        "/admin/api/v1/configuration:diff",
        response_model=AdminEnvelope[ConfigurationDiff],
        responses=problem_responses,
    )
    async def diff_configuration(
        request: Request,
        proposal: ConfigurationProposal,
    ) -> AdminEnvelope[ConfigurationDiff]:
        await authorize(request, OperatorRole.VIEWER, "configuration.diff")
        try:
            return envelope(await service.diff(proposal))
        except ConfigurationProblemError as exc:
            raise translate(exc) from None

    @router.post(
        "/admin/api/v1/configuration:validate",
        response_model=AdminEnvelope[ConfigurationValidation],
        responses=problem_responses,
    )
    async def validate_configuration(
        request: Request,
        proposal: ConfigurationProposal,
    ) -> AdminEnvelope[ConfigurationValidation]:
        actor = await authorize(request, OperatorRole.VIEWER, "configuration.validate")
        try:
            return envelope(await service.validate(proposal, actor))
        except ConfigurationProblemError as exc:
            raise translate(exc) from None

    @router.post(
        "/admin/api/v1/configuration:plan",
        response_model=AdminEnvelope[ConfigurationPlan],
        responses=problem_responses,
    )
    async def plan_configuration(
        request: Request,
        proposal: ConfigurationProposal,
    ) -> AdminEnvelope[ConfigurationPlan]:
        actor = await authorize(request, OperatorRole.OPERATOR, "configuration.plan")
        try:
            return envelope(await service.plan(proposal, actor))
        except ConfigurationProblemError as exc:
            raise translate(exc) from None

    @router.post(
        "/admin/api/v1/configuration:reconcile",
        response_model=AdminEnvelope[ReconciliationStatus],
        responses=problem_responses,
    )
    async def reconcile_configuration(
        request: Request,
        body: ReconcileRequest,
    ) -> AdminEnvelope[ReconciliationStatus]:
        actor = await authorize(request, OperatorRole.OPERATOR, "configuration.reconcile")
        try:
            value = await service.reconcile(plan_id=body.plan_id, base_etag=body.base_etag, actor=actor)
            return envelope(value)
        except ConfigurationProblemError as exc:
            raise translate(exc) from None

    @router.get(
        "/admin/api/v1/configuration/reconciliations/{reconciliation_id}",
        response_model=AdminEnvelope[ReconciliationStatus],
        responses=problem_responses,
    )
    async def reconciliation_status(
        request: Request,
        reconciliation_id: UUID,
    ) -> AdminEnvelope[ReconciliationStatus]:
        await authorize(request, OperatorRole.VIEWER, "configuration.status")
        try:
            return envelope(await service.status(reconciliation_id))
        except ConfigurationProblemError as exc:
            raise translate(exc) from None

    @router.post(
        "/admin/api/v1/configuration:rollback",
        response_model=AdminEnvelope[RollbackPlan],
        responses=problem_responses,
    )
    async def rollback_configuration(
        request: Request,
        body: RollbackRequest,
    ) -> AdminEnvelope[RollbackPlan]:
        actor = await authorize(request, OperatorRole.ADMIN, "configuration.rollback")
        try:
            return envelope(await service.rollback(body, actor))
        except ConfigurationProblemError as exc:
            raise translate(exc) from None

    return router
