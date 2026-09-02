"""Authenticated, read-only admin seams for durable ModelDeployment state.

The router in this module exposes no mutation verb and has no Kubernetes
adapter.  It is mounted only when a read service is explicitly injected into
``AppRuntime``; production defaults therefore remain fail closed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Depends, Path, Query, Request

from .access import AdminAccessService
from .access_models import OperatorPrincipal, OperatorRole
from .admin import AdminProblemError
from .admin_models import AdminEnvelope
from .model_deployment import DNS_LABEL_PATTERN, DNS_SUBDOMAIN_PATTERN
from .model_deployment_records import (
    ModelDeploymentAppendRequest,
    ModelDeploymentAppendResult,
    ModelDeploymentHistory,
    ModelDeploymentList,
    ModelDeploymentRevision,
    ModelDeploymentStatusAvailability,
    ModelDeploymentStatusObservation,
    ModelDeploymentStatusView,
)


class ModelDeploymentReadProblemError(RuntimeError):
    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


class ModelDeploymentPersistenceStore(Protocol):
    async def model_deployment_append_revision(
        self,
        request: ModelDeploymentAppendRequest,
    ) -> ModelDeploymentAppendResult: ...

    async def model_deployment_list(
        self,
        *,
        namespace: str,
        tenant_id: str | None,
        after_name: str | None,
        limit: int,
    ) -> list[ModelDeploymentRevision]: ...

    async def model_deployment_current(
        self,
        *,
        namespace: str,
        name: str,
        tenant_id: str | None,
    ) -> ModelDeploymentRevision | None: ...

    async def model_deployment_history(
        self,
        *,
        namespace: str,
        name: str,
        tenant_id: str | None,
        before_revision: int | None,
        limit: int,
    ) -> list[ModelDeploymentRevision]: ...

    async def model_deployment_status(
        self,
        *,
        namespace: str,
        name: str,
        tenant_id: str | None,
    ) -> ModelDeploymentStatusObservation | None: ...

    async def model_deployment_append_status(
        self,
        observation: ModelDeploymentStatusObservation,
    ) -> ModelDeploymentStatusObservation: ...


class StoreModelDeploymentRepository:
    """Narrow durable adapter over the shared persistence contract.

    The append methods are internal seams for a later reviewed admission
    workflow. ``ModelDeploymentReadService`` deliberately does not expose
    either method and the HTTP router contains no mutation route.
    """

    def __init__(self, store: ModelDeploymentPersistenceStore) -> None:
        self.store = store

    async def append_revision(
        self,
        request: ModelDeploymentAppendRequest,
    ) -> ModelDeploymentAppendResult:
        return await self.store.model_deployment_append_revision(request)

    async def list_current(
        self,
        *,
        namespace: str,
        tenant_id: str | None,
        after_name: str | None,
        limit: int,
    ) -> list[ModelDeploymentRevision]:
        return await self.store.model_deployment_list(
            namespace=namespace,
            tenant_id=tenant_id,
            after_name=after_name,
            limit=limit,
        )

    async def current(
        self,
        *,
        namespace: str,
        name: str,
        tenant_id: str | None,
    ) -> ModelDeploymentRevision | None:
        return await self.store.model_deployment_current(
            namespace=namespace,
            name=name,
            tenant_id=tenant_id,
        )

    async def history(
        self,
        *,
        namespace: str,
        name: str,
        tenant_id: str | None,
        before_revision: int | None,
        limit: int,
    ) -> list[ModelDeploymentRevision]:
        return await self.store.model_deployment_history(
            namespace=namespace,
            name=name,
            tenant_id=tenant_id,
            before_revision=before_revision,
            limit=limit,
        )

    async def status(
        self,
        *,
        namespace: str,
        name: str,
        tenant_id: str | None,
    ) -> ModelDeploymentStatusObservation | None:
        return await self.store.model_deployment_status(
            namespace=namespace,
            name=name,
            tenant_id=tenant_id,
        )

    async def append_status(
        self,
        observation: ModelDeploymentStatusObservation,
    ) -> ModelDeploymentStatusObservation:
        return await self.store.model_deployment_append_status(observation)


class ModelDeploymentReadService:
    def __init__(self, repository: StoreModelDeploymentRepository) -> None:
        self.repository = repository

    @staticmethod
    def _missing() -> ModelDeploymentReadProblemError:
        return ModelDeploymentReadProblemError(
            404,
            "model_deployment_not_found",
            "model deployment was not found",
        )

    async def list(
        self,
        *,
        namespace: str,
        tenant_id: str | None,
        after_name: str | None,
        limit: int,
    ) -> ModelDeploymentList:
        rows = await self.repository.list_current(
            namespace=namespace,
            tenant_id=tenant_id,
            after_name=after_name,
            limit=limit + 1,
        )
        visible = rows[:limit]
        return ModelDeploymentList(
            items=visible,
            next_after=visible[-1].name if len(rows) > limit else None,
        )

    async def get(
        self,
        *,
        namespace: str,
        name: str,
        tenant_id: str | None,
    ) -> ModelDeploymentRevision:
        value = await self.repository.current(namespace=namespace, name=name, tenant_id=tenant_id)
        if value is None:
            raise self._missing()
        return value

    async def history(
        self,
        *,
        namespace: str,
        name: str,
        tenant_id: str | None,
        before_revision: int | None,
        limit: int,
    ) -> ModelDeploymentHistory:
        current = await self.repository.current(namespace=namespace, name=name, tenant_id=tenant_id)
        if current is None:
            raise self._missing()
        rows = await self.repository.history(
            namespace=namespace,
            name=name,
            tenant_id=tenant_id,
            before_revision=before_revision,
            limit=limit + 1,
        )
        visible = rows[:limit]
        return ModelDeploymentHistory(
            items=visible,
            next_before_revision=visible[-1].revision if len(rows) > limit else None,
        )

    async def status(
        self,
        *,
        namespace: str,
        name: str,
        tenant_id: str | None,
    ) -> ModelDeploymentStatusView:
        current = await self.repository.current(namespace=namespace, name=name, tenant_id=tenant_id)
        if current is None:
            raise self._missing()
        observation = await self.repository.status(namespace=namespace, name=name, tenant_id=tenant_id)
        if observation is None:
            return ModelDeploymentStatusView(
                namespace=namespace,
                name=name,
                revision=current.revision,
                etag=current.etag,
                state=ModelDeploymentStatusAvailability.UNAVAILABLE,
                reason="the controller has not published a durable observation",
            )
        if observation.revision != current.revision or observation.status.spec_digest != current.etag:
            return ModelDeploymentStatusView(
                namespace=namespace,
                name=name,
                revision=current.revision,
                etag=current.etag,
                state=ModelDeploymentStatusAvailability.STALE,
                observation=observation,
                reason="the latest controller observation does not match the current desired revision",
            )
        return ModelDeploymentStatusView(
            namespace=namespace,
            name=name,
            revision=current.revision,
            etag=current.etag,
            state=ModelDeploymentStatusAvailability.OBSERVED,
            observation=observation,
        )


def model_deployment_read_router(
    *,
    service: ModelDeploymentReadService,
    access: AdminAccessService,
    operator_dependency: Callable[..., Any],
    envelope: Callable[[Any], AdminEnvelope[Any]],
    problem_responses: dict[int | str, dict[str, Any]],
) -> APIRouter:
    router = APIRouter(dependencies=[Depends(operator_dependency)])

    def identity(request: Request) -> OperatorPrincipal:
        value = getattr(request.state, "operator_principal", None)
        if not isinstance(value, OperatorPrincipal):
            raise AdminProblemError(401, "operator_session_required", "operator session is required")
        return value

    async def tenant(request: Request, requested: str | None, action: str) -> str | None:
        return await access.authorize(
            identity(request),
            OperatorRole.VIEWER,
            action=action,
            tenant_id=requested,
        )

    def translate(exc: ModelDeploymentReadProblemError) -> AdminProblemError:
        return AdminProblemError(exc.status_code, exc.code, exc.detail)

    @router.get(
        "/admin/api/v1/model-deployments",
        response_model=AdminEnvelope[ModelDeploymentList],
        responses=problem_responses,
    )
    async def list_model_deployments(
        request: Request,
        namespace: Annotated[
            str,
            Query(min_length=1, max_length=63, pattern=DNS_LABEL_PATTERN),
        ] = "fs2-models",
        tenant_id: Annotated[
            str | None,
            Query(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"),
        ] = None,
        after: Annotated[
            str | None,
            Query(min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN),
        ] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
    ) -> AdminEnvelope[ModelDeploymentList]:
        authorized_tenant = await tenant(request, tenant_id, "model_deployment.list")
        return envelope(
            await service.list(
                namespace=namespace,
                tenant_id=authorized_tenant,
                after_name=after,
                limit=limit,
            )
        )

    @router.get(
        "/admin/api/v1/model-deployments/{name}",
        response_model=AdminEnvelope[ModelDeploymentRevision],
        responses=problem_responses,
    )
    async def get_model_deployment(
        request: Request,
        name: Annotated[str, Path(min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)],
        namespace: Annotated[
            str,
            Query(min_length=1, max_length=63, pattern=DNS_LABEL_PATTERN),
        ] = "fs2-models",
        tenant_id: Annotated[
            str | None,
            Query(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"),
        ] = None,
    ) -> AdminEnvelope[ModelDeploymentRevision]:
        authorized_tenant = await tenant(request, tenant_id, "model_deployment.read")
        try:
            return envelope(
                await service.get(
                    namespace=namespace,
                    name=name,
                    tenant_id=authorized_tenant,
                )
            )
        except ModelDeploymentReadProblemError as exc:
            raise translate(exc) from None

    @router.get(
        "/admin/api/v1/model-deployments/{name}/history",
        response_model=AdminEnvelope[ModelDeploymentHistory],
        responses=problem_responses,
    )
    async def model_deployment_history(
        request: Request,
        name: Annotated[str, Path(min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)],
        namespace: Annotated[
            str,
            Query(min_length=1, max_length=63, pattern=DNS_LABEL_PATTERN),
        ] = "fs2-models",
        tenant_id: Annotated[
            str | None,
            Query(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"),
        ] = None,
        before_revision: Annotated[int | None, Query(ge=2)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
    ) -> AdminEnvelope[ModelDeploymentHistory]:
        authorized_tenant = await tenant(request, tenant_id, "model_deployment.history")
        try:
            return envelope(
                await service.history(
                    namespace=namespace,
                    name=name,
                    tenant_id=authorized_tenant,
                    before_revision=before_revision,
                    limit=limit,
                )
            )
        except ModelDeploymentReadProblemError as exc:
            raise translate(exc) from None

    @router.get(
        "/admin/api/v1/model-deployments/{name}/status",
        response_model=AdminEnvelope[ModelDeploymentStatusView],
        responses=problem_responses,
    )
    async def model_deployment_status(
        request: Request,
        name: Annotated[str, Path(min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)],
        namespace: Annotated[
            str,
            Query(min_length=1, max_length=63, pattern=DNS_LABEL_PATTERN),
        ] = "fs2-models",
        tenant_id: Annotated[
            str | None,
            Query(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"),
        ] = None,
    ) -> AdminEnvelope[ModelDeploymentStatusView]:
        authorized_tenant = await tenant(request, tenant_id, "model_deployment.status")
        try:
            return envelope(
                await service.status(
                    namespace=namespace,
                    name=name,
                    tenant_id=authorized_tenant,
                )
            )
        except ModelDeploymentReadProblemError as exc:
            raise translate(exc) from None

    return router
