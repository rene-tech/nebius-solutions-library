"""Feature-gated admin validation and render preview for ModelDeployment.

No type in this module can write Kubernetes. Mutation-shaped routes are
intentionally mounted with the preview service and return 501 until the next
tranche supplies a separately reviewed writer, admission webhook, PostgreSQL
revision store, and controller.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from .access import AdminAccessService
from .access_models import OperatorPrincipal, OperatorRole
from .admin import AdminProblemError
from .admin_models import AdminEnvelope
from .model_deployment import (
    DNS_LABEL_PATTERN,
    DNS_SUBDOMAIN_PATTERN,
    InfrastructureEnvelope,
    ModelDeploymentSpec,
    ModelRenderer,
    RenderContext,
    RenderPlan,
    ValidationDecision,
    ValidationDisposition,
    spec_digest,
    validate_model_deployment,
)
from .models import IdempotencyKey, StrictModel

ETAG_PATTERN = r"^sha256:[a-f0-9]{64}$"
PREVIEW_TTL = timedelta(minutes=15)


class ModelDeploymentPreviewProblemError(RuntimeError):
    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


class ModelDeploymentCurrent(StrictModel):
    name: str = Field(min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)
    namespace: str = Field(min_length=1, max_length=63, pattern=DNS_LABEL_PATTERN)
    revision: int = Field(ge=1)
    etag: str = Field(pattern=ETAG_PATTERN)
    spec: ModelDeploymentSpec


class ModelDeploymentPreviewProposal(StrictModel):
    name: str = Field(min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)
    namespace: str = Field(default="fs2-models", min_length=1, max_length=63, pattern=DNS_LABEL_PATTERN)
    base_etag: str | None = Field(default=None, pattern=ETAG_PATTERN)
    spec: ModelDeploymentSpec


class ModelDeploymentValidationPreview(StrictModel):
    schema_version: Literal["fs2-serve.nebius.ai/model-deployment-validation-preview/v1"] = (
        "fs2-serve.nebius.ai/model-deployment-validation-preview/v1"
    )
    name: str
    namespace: str
    current_revision: int | None = None
    current_etag: str | None = Field(default=None, pattern=ETAG_PATTERN)
    decision: ValidationDecision
    mutation_supported: Literal[False] = False


class ModelDeploymentRenderPreview(StrictModel):
    schema_version: Literal["fs2-serve.nebius.ai/model-deployment-render-preview/v1"] = (
        "fs2-serve.nebius.ai/model-deployment-render-preview/v1"
    )
    preview_id: UUID
    name: str
    namespace: str
    base_etag: str | None = Field(default=None, pattern=ETAG_PATTERN)
    proposed_etag: str = Field(pattern=ETAG_PATTERN)
    decision: ValidationDecision
    render: RenderPlan | None = None
    created_at: datetime
    expires_at: datetime
    mutation_supported: Literal[False] = False
    blocked_actions: list[Literal["apply", "adopt", "delete"]] = ["apply", "adopt", "delete"]


class BlockedMutationRequest(StrictModel):
    preview_id: UUID
    base_etag: str | None = Field(default=None, pattern=ETAG_PATTERN)
    idempotency_key: IdempotencyKey


class ModelDeploymentPreviewState(Protocol):
    async def current(self, *, namespace: str, name: str) -> ModelDeploymentCurrent | None: ...


class InMemoryModelDeploymentPreviewState:
    def __init__(self, records: list[ModelDeploymentCurrent] | None = None) -> None:
        self._records = {(item.namespace, item.name): item for item in records or []}

    async def current(self, *, namespace: str, name: str) -> ModelDeploymentCurrent | None:
        item = self._records.get((namespace, name))
        return item.model_copy(deep=True) if item is not None else None


class ModelDeploymentPreviewAudit(Protocol):
    async def record(
        self,
        *,
        actor: str,
        tenant_id: str,
        action: str,
        subject: str,
        detail: dict[str, str | int | bool | None],
    ) -> None: ...


class InMemoryModelDeploymentPreviewAudit:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def record(
        self,
        *,
        actor: str,
        tenant_id: str,
        action: str,
        subject: str,
        detail: dict[str, str | int | bool | None],
    ) -> None:
        self.events.append(
            {
                "actor": actor,
                "tenant_id": tenant_id,
                "action": action,
                "subject": subject,
                "detail": detail.copy(),
            }
        )


class ModelDeploymentPreviewService:
    """Read-only validation/renderer orchestration with optimistic previews."""

    def __init__(
        self,
        *,
        envelope: InfrastructureEnvelope,
        renderer: ModelRenderer,
        state: ModelDeploymentPreviewState,
        prometheus_server_address: str,
        audit: ModelDeploymentPreviewAudit | None = None,
        namespace: str = "fs2-models",
    ) -> None:
        self.envelope = envelope
        self.renderer = renderer
        self.state = state
        self.prometheus_server_address = prometheus_server_address
        self.audit = audit
        self.namespace = namespace

    async def _current(
        self,
        proposal: ModelDeploymentPreviewProposal,
        actor: OperatorPrincipal,
        *,
        require_etag: bool,
    ) -> ModelDeploymentCurrent | None:
        if proposal.namespace != self.namespace:
            raise ModelDeploymentPreviewProblemError(422, "namespace_outside_policy", "namespace is outside policy")
        if actor.tenant_id is not None and actor.tenant_id != proposal.spec.tenant_id:
            raise ModelDeploymentPreviewProblemError(403, "tenant_forbidden", "tenant is outside operator policy")
        current = await self.state.current(namespace=proposal.namespace, name=proposal.name)
        if current is not None and current.spec.tenant_id != proposal.spec.tenant_id:
            raise ModelDeploymentPreviewProblemError(
                409, "model_identity_conflict", "model name belongs to another tenant"
            )
        if require_etag:
            if current is None and proposal.base_etag is not None:
                raise ModelDeploymentPreviewProblemError(
                    409, "stale_model_etag", "model does not exist at base ETag"
                )
            if current is not None and proposal.base_etag != current.etag:
                raise ModelDeploymentPreviewProblemError(
                    409, "stale_model_etag", "model changed after the preview base"
                )
        return current

    async def validate(
        self,
        proposal: ModelDeploymentPreviewProposal,
        actor: OperatorPrincipal,
    ) -> ModelDeploymentValidationPreview:
        current = await self._current(proposal, actor, require_etag=False)
        decision = validate_model_deployment(
            proposal.spec,
            self.envelope,
            current=current.spec if current is not None else None,
        )
        return ModelDeploymentValidationPreview(
            name=proposal.name,
            namespace=proposal.namespace,
            current_revision=current.revision if current is not None else None,
            current_etag=current.etag if current is not None else None,
            decision=decision,
        )

    async def plan(
        self,
        proposal: ModelDeploymentPreviewProposal,
        actor: OperatorPrincipal,
    ) -> ModelDeploymentRenderPreview:
        current = await self._current(proposal, actor, require_etag=True)
        decision = validate_model_deployment(
            proposal.spec,
            self.envelope,
            current=current.spec if current is not None else None,
        )
        if decision.disposition is ValidationDisposition.REJECTED:
            raise ModelDeploymentPreviewProblemError(
                422,
                "model_deployment_rejected",
                "model deployment failed live-policy or qualification validation",
            )
        render = None
        if decision.disposition is ValidationDisposition.ACCEPTED:
            assert decision.admitted_pool_ref is not None
            pool = self.envelope.pools[decision.admitted_pool_ref]
            try:
                render = self.renderer.render(
                    proposal.spec,
                    RenderContext(
                        name=proposal.name,
                        namespace=proposal.namespace,
                        uid=None,
                        generation=(current.revision + 1 if current is not None else 1),
                        pool=pool,
                        prometheus_server_address=self.prometheus_server_address,
                        preview=True,
                    ),
                )
            except ValueError:
                raise ModelDeploymentPreviewProblemError(
                    422,
                    "model_deployment_render_failed",
                    "qualified renderer could not produce a bounded preview",
                ) from None
        now = datetime.now(UTC)
        preview = ModelDeploymentRenderPreview(
            preview_id=uuid4(),
            name=proposal.name,
            namespace=proposal.namespace,
            base_etag=proposal.base_etag,
            proposed_etag=spec_digest(proposal.spec),
            decision=decision,
            render=render,
            created_at=now,
            expires_at=now + PREVIEW_TTL,
        )
        if self.audit is not None:
            await self.audit.record(
                actor=actor.subject,
                tenant_id=proposal.spec.tenant_id,
                action="model_deployment.preview",
                subject=f"{proposal.namespace}/{proposal.name}",
                detail={
                    "preview_id": str(preview.preview_id),
                    "proposed_etag": preview.proposed_etag,
                    "disposition": decision.disposition.value,
                    "mutation_supported": False,
                },
            )
        return preview


def model_deployment_preview_router(
    *,
    service: ModelDeploymentPreviewService,
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

    async def authorize(request: Request, role: OperatorRole, action: str) -> OperatorPrincipal:
        value = identity(request)
        await access.authorize_global(value, role, action=action)
        return value

    def translate(exc: ModelDeploymentPreviewProblemError) -> AdminProblemError:
        return AdminProblemError(exc.status_code, exc.code, exc.detail)

    @router.post(
        "/admin/api/v1/model-deployments:validate-preview",
        response_model=AdminEnvelope[ModelDeploymentValidationPreview],
        responses=problem_responses,
    )
    async def validate_preview(
        request: Request,
        proposal: ModelDeploymentPreviewProposal,
    ) -> AdminEnvelope[ModelDeploymentValidationPreview]:
        actor = await authorize(request, OperatorRole.VIEWER, "model_deployment.preview.validate")
        try:
            return envelope(await service.validate(proposal, actor))
        except ModelDeploymentPreviewProblemError as exc:
            raise translate(exc) from None

    @router.post(
        "/admin/api/v1/model-deployments:plan-preview",
        response_model=AdminEnvelope[ModelDeploymentRenderPreview],
        responses=problem_responses,
    )
    async def plan_preview(
        request: Request,
        proposal: ModelDeploymentPreviewProposal,
    ) -> AdminEnvelope[ModelDeploymentRenderPreview]:
        actor = await authorize(request, OperatorRole.OPERATOR, "model_deployment.preview.plan")
        try:
            return envelope(await service.plan(proposal, actor))
        except ModelDeploymentPreviewProblemError as exc:
            raise translate(exc) from None

    async def writer_disabled(request: Request, action: str) -> None:
        await authorize(request, OperatorRole.ADMIN, f"model_deployment.{action}")
        raise AdminProblemError(
            501,
            "model_deployment_writer_disabled",
            "dynamic model mutation remains feature-gated; only validation and render preview are available",
        )

    @router.post("/admin/api/v1/model-deployments:apply", responses=problem_responses)
    async def apply_disabled(request: Request, body: BlockedMutationRequest) -> None:
        del body
        await writer_disabled(request, "apply")

    @router.post("/admin/api/v1/model-deployments/{name}:adopt", responses=problem_responses)
    async def adopt_disabled(request: Request, name: str, body: BlockedMutationRequest) -> None:
        del name, body
        await writer_disabled(request, "adopt")

    @router.delete("/admin/api/v1/model-deployments/{name}", responses=problem_responses)
    async def delete_disabled(request: Request, name: str) -> None:
        del name
        await writer_disabled(request, "delete")

    return router
