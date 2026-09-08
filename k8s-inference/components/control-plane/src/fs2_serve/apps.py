"""Apps coordinate existing deployment, scientific-policy and run services.

This layer owns identity and metadata, not a second inference controller.
Default registration never resets operator-edited settings. A catalog App may
gain its exact managed deployment after bootstrap. Scientific details enrich durable gateway
operations and are never counted as additional requests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from .access_models import OperatorPrincipal
from .admin import AdminProblemError, AdminReadService
from .admin_models import AdminContext, AdminEnvelope, AdminOperationItem
from .apps_models import (
    AppCapabilities,
    AppChoice,
    AppCreate,
    AppList,
    AppObservabilityTarget,
    AppRecord,
    AppRun,
    AppRunList,
    AppSettings,
    AppSettingsUpdate,
    AppSummary,
    AppUsage,
)
from .apps_repository import AppConflictError, AppsRepository
from .apps_scientific import ScientificAppsInventory
from .model_deployment import (
    MODEL_DEPLOYMENT_LABEL,
    MODEL_ID_LABEL,
    AdoptionSpec,
    AppDeploymentIdentity,
    DesiredState,
    ModelDeploymentSpec,
    bounded_label_value,
    spec_digest,
)
from .model_deployment_mutation import (
    ModelDeploymentApplyRequest,
    ModelDeploymentMutationProblemError,
    ModelDeploymentMutationService,
)
from .model_deployment_preview import ModelDeploymentPreviewProposal
from .model_deployment_records import ModelDeploymentRevision
from .models import OperationStatus
from .registry import Registry
from .request_telemetry import PostgresRequestTelemetryStore
from .scientific_admin import ScientificAdminReadService
from .scientific_admin_models import ScientificModelPolicyUpdate

_UPLOAD_PROTOCOL = "scientific-artifact-upload-v1"


def default_app_id(model_id: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"fs2-serve/app/default/{model_id}")


class AppsService:
    def __init__(
        self,
        *,
        repository: AppsRepository,
        registry: Registry,
        admin: AdminReadService,
        deployments: ModelDeploymentMutationService | None = None,
        scientific: ScientificAdminReadService | None = None,
        namespace: str = "fs2-models",
        scientific_namespace: Callable[[str], str] | None = None,
        scientific_apps: ScientificAppsInventory | None = None,
        scientific_profiles: Any | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repository = repository
        self.registry = registry
        self.admin = admin
        self.deployments = deployments
        self.scientific = scientific
        self.namespace = namespace
        self.scientific_namespace = scientific_namespace
        self.scientific_apps = scientific_apps
        self.scientific_profiles = scientific_profiles
        self.clock = clock
        pool = getattr(repository, "pool", None)
        self.request_telemetry = PostgresRequestTelemetryStore(pool) if pool is not None else None

    async def seed_defaults(self) -> None:
        """Register the complete current inventory without overwriting metadata."""
        now = self.clock()
        existing_routes = {item.public_model_id: item for item in await self.repository.list_records()}
        revisions: dict[str, ModelDeploymentRevision] = {}
        if self.deployments is not None:
            after: str | None = None
            while True:
                page = await self.deployments.repository.list_current(
                    namespace=self.namespace,
                    tenant_id=None,
                    after_name=after,
                    limit=200,
                )
                for item in page:
                    revisions[item.spec.public_model_id] = item
                if len(page) < 200:
                    break
                after = page[-1].name
        scientific_models = {}
        if self.scientific is not None and self.scientific.models is not None:
            snapshot = await self.scientific.models.list_models(tenant_id=None)
            scientific_models = {item.model_id: item for item in snapshot.data.items}
        catalog = {model.id: model for model in self.registry.list()}
        for model_id in sorted(catalog.keys() | scientific_models.keys() | revisions.keys()):
            if model_id in existing_routes:
                if model_id in revisions:
                    await self._attach_default_deployment(existing_routes[model_id], revision=revisions[model_id])
                continue
            revision = revisions.get(model_id)
            science = scientific_models.get(model_id)
            model = catalog.get(model_id)
            identity = revision.spec.app if revision else None
            await self.repository.seed(
                AppRecord(
                    app_id=identity.app_id if identity else default_app_id(model_id),
                    display_name=(
                        science.display_name if science else model.gateway.display_name if model else model_id
                    ),
                    model_ref=revision.spec.model_ref if revision else model_id,
                    public_model_id=model_id,
                    execution_mode="scientific" if science is not None else "serving",
                    namespace=(
                        self.scientific_namespace(model_id)
                        if science is not None and self.scientific_namespace is not None
                        else revision.namespace
                        if revision
                        else self.namespace
                    ),
                    deployment_name=revision.name if revision else None,
                    academic_required=bool(science is not None and science.access.profile == "academic"),
                    created_at=now,
                    updated_at=now,
                )
            )

    async def require(self, app_id: UUID | str) -> AppRecord:
        try:
            parsed = UUID(str(app_id))
        except ValueError:
            raise AdminProblemError(404, "app_not_found", "app was not found") from None
        record = await self.repository.get(parsed)
        if record is None:
            raise AdminProblemError(404, "app_not_found", "app was not found")
        return await self._attach_default_deployment(record)

    async def _attach_default_deployment(
        self, record: AppRecord, *, revision: ModelDeploymentRevision | None = None,
    ) -> AppRecord:
        """Resolve catalog-before-bootstrap ordering on normal App reads.

        Only a canonical, previously unbound serving App is eligible. Existing
        deployments, independent clones and scientific identities never move.
        """
        if (
            self.deployments is None or record.deployment_name is not None
            or record.execution_mode != "serving" or record.app_id != default_app_id(record.public_model_id)
            or record.model_ref != record.public_model_id
        ):
            return record
        if revision is None:
            candidates: list[ModelDeploymentRevision] = []
            after = None
            while True:
                page = await self.deployments.repository.list_current(
                    namespace=record.namespace, tenant_id=None, after_name=after, limit=200,
                )
                candidates.extend(item for item in page if item.spec.public_model_id == record.public_model_id)
                if len(page) < 200:
                    break
                after = page[-1].name
            if len(candidates) != 1:
                return record
            revision = candidates[0]
        if (
            revision.namespace != record.namespace or revision.spec.model_ref != record.model_ref
            or revision.spec.public_model_id != record.public_model_id
            or (revision.spec.app is not None and revision.spec.app.app_id != record.app_id)
        ):
            return record
        return await self.repository.attach_deployment(
            record.model_copy(update={"updated_at": self.clock()}), name=revision.name,
        )

    async def choices(self) -> list[AppChoice]:
        return [
            AppChoice(
                app_id=str(item.app_id),
                display_name=item.display_name,
                public_model_id=item.public_model_id,
                academic_required=item.academic_required,
            )
            for item in await self.repository.list_records()
        ]

    def _capabilities(self, record: AppRecord) -> AppCapabilities:
        serving = record.execution_mode == "serving"
        configured = self.deployments is not None and record.deployment_name is not None
        return AppCapabilities(
            duplicate=(serving and configured) or (not serving and self.scientific_apps is not None),
            reusable_workers=serving and configured,
            live_settings=configured if serving else self.scientific is not None,
        )

    async def _revision(self, record: AppRecord) -> ModelDeploymentRevision | None:
        if self.deployments is None or record.deployment_name is None:
            return None
        return await self.deployments.repository.current(
            namespace=record.namespace,
            name=record.deployment_name,
            tenant_id=None,
        )

    async def settings(self, app_id: UUID, context: AdminContext, tenant_id: str | None = None) -> AppSettings:
        record = await self.require(app_id)
        revision = await self._revision(record)
        policy = None
        if record.execution_mode == "scientific" and self.scientific is not None:
            policies = await self.scientific.policy_list(context, tenant_id=tenant_id)
            policy = next((item for item in policies.data.items if item.model_id == record.public_model_id), None)
        return AppSettings(
            app_id=record.app_id,
            execution_mode=record.execution_mode,
            app_revision=record.revision,
            display_name=record.display_name,
            academic_required=record.academic_required,
            serving=revision,
            scientific=policy,
            capabilities=self._capabilities(record),
            unsupported_reason=(
                "This scientific app runs Jobs; max_active_runs limits dispatch, not reusable hot workers."
                if record.execution_mode == "scientific"
                else "No managed ModelDeployment is registered for this app."
                if revision is None
                else None
            ),
        )

    async def summary(self, record: AppRecord, context: AdminContext, tenant_id: str | None) -> AppSummary:
        record = await self._attach_default_deployment(record)
        settings = await self.settings(record.app_id, context, tenant_id)
        enabled = True
        state, reason = "unavailable", None
        if settings.scientific is not None:
            enabled = not settings.scientific.effective.paused
            state = settings.scientific.effective.state
            reason = settings.scientific.effective.reason
        elif settings.serving is not None and self.deployments is not None:
            enabled = settings.serving.spec.lifecycle.desired_state is DesiredState.ENABLED
            observed = await self.deployments.repository.status(
                namespace=record.namespace,
                name=settings.serving.name,
                tenant_id=None,
            )
            state = str(observed.status.phase) if observed else "Desired"
            reason = None if observed else "Waiting for controller observation."
            if observed is not None and observed.status.spec_digest != settings.serving.etag:
                state, reason = "Desired", "Waiting for the controller to observe the latest settings."
        else:
            model = next((item for item in self.registry.list() if item.id == record.public_model_id), None)
            enabled = model.enabled if model else False
            state = "available" if model and model.enabled else "unavailable"
            reason = settings.unsupported_reason
        usage = await self.usage(record.app_id, context, tenant_id)
        lifetime_reader = getattr(self.repository, "last_used", None)
        last_used = await lifetime_reader(record.public_model_id, tenant_id) if lifetime_reader else usage.last_used_at
        return AppSummary(
            **record.model_dump(),
            enabled=enabled,
            status=state,
            status_reason=reason,
            capabilities=settings.capabilities,
            logical_run_count=usage.logical_runs,
            last_used_at=last_used,
        )

    async def list(self, context: AdminContext, tenant_id: str | None = None) -> AppList:
        # Bound fanout independently of the number of registered apps.
        gate = asyncio.Semaphore(4)

        async def one(record: AppRecord) -> AppSummary:
            async with gate:
                return await self.summary(record, context, tenant_id)

        return AppList(items=await asyncio.gather(*(one(item) for item in await self.repository.list_records())))

    async def create(self, body: AppCreate, actor: OperatorPrincipal, context: AdminContext) -> AppSummary:
        records = await self.repository.list_records()
        source = (
            await self.require(body.source_app_id)
            if body.source_app_id
            else next((item for item in records if item.model_ref == body.model_ref), None)
        )
        if source is None or source.model_ref != body.model_ref:
            raise AdminProblemError(404, "app_source_not_found", "a managed source app was not found")
        if not self._capabilities(source).duplicate:
            raise AdminProblemError(422, "app_duplicate_unavailable", "this app has no independent deployment adapter")
        if source.execution_mode == "scientific":
            return await self._create_scientific(source, body, actor, context)
        current = await self._revision(source)
        assert current is not None and self.deployments is not None
        app_id = uuid4()
        public_id = f"app-{app_id.hex}"
        spec_value = current.spec.model_dump(mode="json", by_alias=True)
        spec_value["app"] = AppDeploymentIdentity(app_id=app_id, public_model_id=public_id).model_dump(
            mode="json", by_alias=True
        )
        spec_value["adoption"] = AdoptionSpec().model_dump(mode="json", by_alias=True)
        spec_value["availability"]["minReplicas"] = 0
        spec_value["availability"]["warmWindows"] = []
        spec_value["exposure"]["openAIAliases"] = []
        if spec_value["exposure"]["mcp"]:
            spec_value["exposure"]["mcpToolName"] = f"app_{app_id.hex}"
        spec = ModelDeploymentSpec.model_validate(spec_value)
        # Validate before creating durable metadata; runtime projection may be
        # pending after durable admission and remains visible in the app.
        try:
            self.deployments._validate(spec, None, name=public_id)
        except ModelDeploymentMutationProblemError as exc:
            raise AdminProblemError(exc.status_code, exc.code, exc.detail) from None
        now = self.clock()
        record = await self.repository.seed(
            AppRecord(
                app_id=app_id,
                display_name=body.display_name,
                model_ref=source.model_ref,
                public_model_id=public_id,
                execution_mode=source.execution_mode,
                namespace=current.namespace,
                deployment_name=public_id,
                academic_required=source.academic_required,
                created_at=now,
                updated_at=now,
            )
        )
        await self._apply(record, spec, None, actor, f"app-create-{app_id}")
        return await self.summary(record, context, actor.tenant_id)

    async def _create_scientific(
        self,
        source: AppRecord,
        body: AppCreate,
        actor: OperatorPrincipal,
        context: AdminContext,
    ) -> AppSummary:
        if self.scientific_apps is None or self.scientific_profiles is None or self.scientific is None:
            raise AdminProblemError(422, "app_duplicate_unavailable", "scientific app adapter is unavailable")
        self.scientific_profiles.get(source.model_ref)
        original = await self.settings(source.app_id, context, actor.tenant_id)
        app_id = uuid4()
        now = self.clock()
        record = await self.repository.seed(
            AppRecord(
                app_id=app_id,
                display_name=body.display_name,
                model_ref=source.model_ref,
                public_model_id=f"app-{app_id.hex}",
                execution_mode="scientific",
                namespace=source.namespace,
                academic_required=source.academic_required,
                created_at=now,
                updated_at=now,
            )
        )
        await self.scientific_apps.refresh()
        if original.scientific is not None:
            policy = original.scientific.desired
            await self.scientific.set_policy(
                context,
                record.public_model_id,
                tenant_id=actor.tenant_id,
                update=ScientificModelPolicyUpdate(
                    expected_revision=0,
                    paused=policy.paused,
                    max_active_runs=policy.max_active_runs,
                    startup_policies=policy.startup_policies,
                ),
                actor=actor.subject,
            )
        return await self.summary(record, context, actor.tenant_id)

    async def _apply(
        self,
        record: AppRecord,
        spec: ModelDeploymentSpec,
        base_etag: str | None,
        actor: OperatorPrincipal,
        idempotency_key: str,
    ) -> None:
        if self.deployments is None or record.deployment_name is None:
            raise AdminProblemError(422, "app_settings_unavailable", "app has no managed deployment")
        try:
            await self.deployments.apply(
                ModelDeploymentApplyRequest(
                    preview_id=uuid4(),
                    proposed_etag=spec_digest(spec),
                    idempotency_key=idempotency_key,
                    proposal=ModelDeploymentPreviewProposal(
                        namespace=record.namespace,
                        name=record.deployment_name,
                        base_etag=base_etag,
                        spec=spec,
                    ),
                ),
                actor,
            )
        except ModelDeploymentMutationProblemError as exc:
            raise AdminProblemError(exc.status_code, exc.code, exc.detail) from None

    async def update_settings(
        self,
        app_id: UUID,
        body: AppSettingsUpdate,
        actor: OperatorPrincipal,
        context: AdminContext,
    ) -> AppSettings:
        record = await self.require(app_id)
        if record.revision != body.expected_app_revision:
            raise AdminProblemError(409, "app_revision_conflict", "app changed; reload before saving")
        if body.serving_spec is not None and body.scientific_policy is not None:
            raise AdminProblemError(422, "app_settings_invalid", "select this app's single execution adapter")
        if body.serving_spec is not None:
            if record.execution_mode != "serving":
                raise AdminProblemError(422, "app_settings_invalid", "scientific Jobs have no reusable-worker setting")
            if (
                body.serving_spec.model_ref != record.model_ref
                or body.serving_spec.public_model_id != record.public_model_id
            ):
                raise AdminProblemError(
                    422, "app_identity_immutable", "app and qualified model identities are immutable"
                )
            await self._apply(record, body.serving_spec, body.serving_base_etag, actor, f"app-save-{uuid4()}")
        if body.scientific_policy is not None:
            if record.execution_mode != "scientific" or self.scientific is None:
                raise AdminProblemError(
                    422, "app_settings_invalid", "serving app does not use scientific dispatch policy"
                )
            await self.scientific.set_policy(
                context,
                record.public_model_id,
                tenant_id=actor.tenant_id,
                update=body.scientific_policy,
                actor=actor.subject,
            )
        updates: dict[str, datetime | str | bool] = {"updated_at": self.clock()}
        if body.display_name is not None:
            updates["display_name"] = body.display_name
        if body.academic_required is not None:
            updates["academic_required"] = body.academic_required
        try:
            await self.repository.update(
                record.model_copy(update=updates), expected_revision=body.expected_app_revision
            )
        except AppConflictError as exc:
            raise AdminProblemError(409, "app_revision_conflict", str(exc)) from None
        if self.scientific_apps is not None:
            await self.scientific_apps.refresh()
        return await self.settings(app_id, context, actor.tenant_id)

    async def _run(self, record: AppRecord, operation: AdminOperationItem, context: AdminContext) -> AppRun:
        if operation.model_id != record.public_model_id or operation.protocol == _UPLOAD_PROTOCOL:
            raise AdminProblemError(404, "app_run_not_found", "operation does not belong to this app")
        detail = None
        if record.execution_mode == "scientific" and self.scientific is not None:
            detail = (await self.scientific.run_detail(context, operation.id, tenant_id=operation.tenant_id)).data
        transport = (
            await self.request_telemetry.for_operation(operation.id, operation.tenant_id)
            if self.request_telemetry is not None
            else []
        )
        return AppRun(app_id=record.app_id, operation=operation, scientific=detail, observed_transport=transport)

    async def runs(
        self,
        app_id: UUID,
        context: AdminContext,
        *,
        tenant_id: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
        principal_id: str | None = None,
        status: OperationStatus | None = None,
    ) -> AdminEnvelope[AppRunList]:
        record = await self.require(app_id)
        page = await self.admin.operation_list(
            context,
            limit=limit,
            cursor=cursor,
            tenant_id=tenant_id,
            model_id=record.public_model_id,
            principal_id=principal_id,
            api_key_prefix=None,
            status=status,
            error_code=None,
            exclude_protocols=(_UPLOAD_PROTOCOL,),
        )
        # List rows stay cheap; full scientific DAG/artifacts are fetched only
        # on detail. The operation remains the unique durable list identity.
        return AdminEnvelope(
            meta=page.meta,
            data=AppRunList(
                items=[AppRun(app_id=app_id, operation=item) for item in page.data.items],
                next_cursor=page.data.next_cursor,
            ),
        )

    async def run_detail(
        self,
        app_id: UUID,
        operation_id: UUID,
        context: AdminContext,
        tenant_id: str | None = None,
    ) -> AdminEnvelope[AppRun]:
        record = await self.require(app_id)
        detail = await self.admin.operation_detail(context, operation_id, tenant_id=tenant_id)
        return AdminEnvelope(meta=detail.meta, data=await self._run(record, detail.data.operation, context))

    async def usage(self, app_id: UUID, context: AdminContext, tenant_id: str | None = None) -> AppUsage:
        record = await self.require(app_id)
        reader = getattr(self.repository, "usage", None)
        if reader is not None:
            values = await reader(record.public_model_id, context, tenant_id)
            transport = (
                await self.request_telemetry.usage(record.public_model_id, context.from_at, context.to_at, tenant_id)
                if self.request_telemetry is not None
                else None
            )
            return AppUsage(
                app_id=app_id,
                **values,
                observed_transport=transport,
                request_bytes=transport.request_bytes if transport else None,
                response_bytes=transport.response_bytes if transport else None,
                notes=[
                    "One inference operation is one logical run; artifact uploads, polling and replays are excluded.",
                    "Estimated GPU time is not scheduler occupancy or device utilization.",
                    "Scientific GPU time sums exclusive complete attempt ledgers; shared serving idle is unallocated.",
                    "Historical request/response byte sizes were not recorded and are unknown.",
                ],
            )
        # Test/embedded stores reuse exactly the existing operation projection.
        operations = []
        cursor = None
        while True:
            page = await self.admin.operation_list(
                context,
                limit=200,
                cursor=cursor,
                tenant_id=tenant_id,
                model_id=record.public_model_id,
                principal_id=None,
                api_key_prefix=None,
                status=None,
                error_code=None,
                exclude_protocols=(_UPLOAD_PROTOCOL,),
            )
            operations.extend(page.data.items)
            cursor = page.data.next_cursor
            if cursor is None:
                break
        return AppUsage(
            app_id=app_id,
            logical_runs=len(operations),
            succeeded_runs=sum(item.status is OperationStatus.SUCCEEDED for item in operations),
            failed_runs=sum(
                item.status
                in {
                    OperationStatus.FAILED,
                    OperationStatus.CANCELLED,
                    OperationStatus.EXPIRED,
                    OperationStatus.PREEMPTED,
                }
                for item in operations
            ),
            active_runs=sum(
                item.status in {OperationStatus.QUEUED, OperationStatus.ACTIVATING, OperationStatus.RUNNING}
                for item in operations
            ),
            first_used_at=min((item.accepted_at for item in operations), default=None),
            last_used_at=max((item.accepted_at for item in operations), default=None),
        )

    async def resolve_observability(self, app_id: str | UUID) -> AppObservabilityTarget:
        record = await self.require(app_id)
        if record.execution_mode == "scientific":
            from .scientific_batch.kubernetes import MODEL_LABEL

            labels = {MODEL_LABEL: record.public_model_id}
        elif record.deployment_name is not None:
            labels = {MODEL_DEPLOYMENT_LABEL: bounded_label_value(record.deployment_name)}
        else:
            labels = {MODEL_ID_LABEL: bounded_label_value(record.public_model_id)}
        return AppObservabilityTarget(
            app_id=str(record.app_id),
            model_id=record.public_model_id,
            namespace=record.namespace,
            pod_labels=labels,
            deployment_name=record.deployment_name,
            execution_mode=record.execution_mode,
        )
