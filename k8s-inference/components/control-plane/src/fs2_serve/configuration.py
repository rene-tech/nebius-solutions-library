"""Declarative configuration planning and reconciliation boundary.

The service never shells out to Terraform, calls a cloud API, or patches a
Kubernetes object directly. A renderer produces immutable artifacts and a
secret-free Terraform handoff only for fields with a concrete consumer in the
current root; typed future fields fail closed at planning time.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Never, Protocol
from uuid import UUID, uuid4

from fs2_serve_catalog.loader import Catalog

from .access_models import OperatorPrincipal, OperatorRole
from .configuration_models import (
    ConfigurationAuditReceipt,
    ConfigurationChange,
    ConfigurationDiff,
    ConfigurationOwner,
    ConfigurationPlan,
    ConfigurationPlanState,
    ConfigurationProposal,
    ConfigurationRevision,
    ConfigurationValidation,
    ConfigurationValidationIssue,
    ModelConfiguration,
    PlatformConfiguration,
    ReconciliationPhase,
    ReconciliationStatus,
    RenderedConfigurationArtifact,
    RollbackPlan,
    RollbackRequest,
    TerraformApplyReceipt,
    TerraformHandoff,
    ValidationSeverity,
)

PLAN_TTL = timedelta(minutes=15)
TERRAFORM_BOOTSTRAP_ACTOR = "terraform-bootstrap"
_MISSING = object()
_SECRET_KEY_PARTS = frozenset({"api_key", "apikey", "credential", "password", "secret", "token"})
_APPLICABLE_AUTOSCALING_FIELDS = frozenset(
    {
        "min_replicas",
        "max_replicas",
        "target_queue_depth",
        "polling_interval_seconds",
        "cooldown_seconds",
    }
)


class ConfigurationProblemError(RuntimeError):
    """Stable API-facing configuration failure."""

    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def configuration_etag(configuration: PlatformConfiguration) -> str:
    return canonical_sha256(configuration.model_dump(mode="json"))


def load_platform_configuration(path: Path) -> PlatformConfiguration:
    """Load one bounded duplicate-free desired-state document."""

    if path.is_symlink() or not path.is_file():
        raise ValueError("admin configuration file is unavailable or is a symlink")
    payload = path.read_bytes()
    if not 1 <= len(payload) <= 8 * 1024 * 1024:
        raise ValueError("admin configuration file is outside the accepted bound")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("admin configuration contains a duplicate key")
            result[key] = value
        return result

    def reject_constant(_: str) -> Never:
        raise ValueError("non-finite numbers are forbidden")

    try:
        value = json.loads(
            payload,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (RecursionError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError("admin configuration is not valid JSON") from None
    return PlatformConfiguration.model_validate(value)


def load_terraform_apply_receipt(path: Path) -> TerraformApplyReceipt:
    """Load the bounded immutable receipt mounted beside desired state."""

    if path.is_symlink() or not path.is_file():
        raise ValueError("Terraform apply receipt is unavailable or is a symlink")
    payload = path.read_bytes()
    if not 1 <= len(payload) <= 64 * 1024:
        raise ValueError("Terraform apply receipt is outside the accepted bound")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Terraform apply receipt contains a duplicate key")
            result[key] = value
        return result

    def reject_constant(_: str) -> Never:
        raise ValueError("non-finite numbers are forbidden")

    try:
        value = json.loads(
            payload,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (RecursionError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError("Terraform apply receipt is not valid JSON") from None
    return TerraformApplyReceipt.model_validate(value)


@dataclass(frozen=True)
class CatalogModelContract:
    """Exact catalog identities and qualified accelerator classes for one route."""

    model_id: str
    artifact_manifest_sha256: str | None
    acquisition_contract_sha256: str
    provenance_sha256: str
    semantic_health_contract_sha256: str
    runtime_image_digest: str
    model_revision: str
    supported_accelerator_classes: frozenset[str]


class CatalogConfigurationAdapter(Protocol):
    async def validate_model(
        self,
        model: ModelConfiguration,
        configuration: PlatformConfiguration,
    ) -> Sequence[ConfigurationValidationIssue]: ...


class StaticCatalogConfigurationAdapter:
    """Fail-closed adapter over already validated canonical catalog metadata."""

    def __init__(self, contracts: Mapping[str, CatalogModelContract]) -> None:
        self.contracts = dict(contracts)

    async def validate_model(
        self,
        model: ModelConfiguration,
        configuration: PlatformConfiguration,
    ) -> Sequence[ConfigurationValidationIssue]:
        base = f"$.models.{model.model_id}"
        contract = self.contracts.get(model.model_id)
        if contract is None:
            return (
                ConfigurationValidationIssue(
                    severity=ValidationSeverity.ERROR,
                    code="catalog_model_missing",
                    path=base,
                    message="model must complete catalog acquisition, provenance, semantic health, and promotion first",
                ),
            )
        issues: list[ConfigurationValidationIssue] = []
        identities = {
            "artifact_manifest_sha256": contract.artifact_manifest_sha256,
            "acquisition_contract_sha256": contract.acquisition_contract_sha256,
            "provenance_sha256": contract.provenance_sha256,
            "semantic_health_contract_sha256": contract.semantic_health_contract_sha256,
        }
        for name, expected in identities.items():
            if getattr(model.artifact, name) != expected:
                issues.append(
                    ConfigurationValidationIssue(
                        severity=ValidationSeverity.ERROR,
                        code="catalog_identity_mismatch",
                        path=f"{base}.artifact.{name}",
                        message="proposed immutable identity differs from the qualified catalog",
                    )
                )
        if model.artifact.image_digest != contract.runtime_image_digest:
            issues.append(
                ConfigurationValidationIssue(
                    severity=ValidationSeverity.ERROR,
                    code="catalog_runtime_mismatch",
                    path=f"{base}.artifact.image_digest",
                    message="runtime image digest differs from the canonical catalog",
                )
            )
        if model.artifact.model_revision != contract.model_revision:
            issues.append(
                ConfigurationValidationIssue(
                    severity=ValidationSeverity.ERROR,
                    code="catalog_revision_mismatch",
                    path=f"{base}.artifact.model_revision",
                    message="model revision differs from the canonical catalog",
                )
            )
        for pool_id in model.placement.pool_ids:
            pool = configuration.pools[pool_id]
            if pool.accelerator_class not in contract.supported_accelerator_classes:
                issues.append(
                    ConfigurationValidationIssue(
                        severity=ValidationSeverity.ERROR,
                        code="unsupported_accelerator_placement",
                        path=f"{base}.placement.pool_ids",
                        message="catalog qualification does not support the selected accelerator class",
                    )
                )
        return issues


def catalog_configuration_contracts(catalog: Catalog) -> dict[str, CatalogModelContract]:
    """Project exact model/acquisition/semantic identities from the canonical loader."""

    contracts: dict[str, CatalogModelContract] = {}
    for model_id, record in catalog.records.items():
        value = record.to_dict()
        acquisition = catalog.acquisition_plans.get(model_id)
        semantic = catalog.semantic_requests.get(model_id)
        if acquisition is None or semantic is None:
            continue
        gpu = value["resources"]["gpu"]
        alternatives = gpu.get("alternatives", [])
        classes = {str(gpu["class"])}
        classes.update(str(item["class"]) for item in alternatives if isinstance(item, dict) and item.get("class"))
        artifact = value["cache"]["artifact"]
        contracts[model_id] = CatalogModelContract(
            model_id=model_id,
            artifact_manifest_sha256=artifact.get("manifest_digest"),
            acquisition_contract_sha256=canonical_sha256(acquisition.to_dict()),
            provenance_sha256=record.digest,
            semantic_health_contract_sha256=semantic.digest,
            runtime_image_digest=str(value["runtime"]["image"]["digest"]),
            model_revision=str(value["model"]["source"]["revision"]),
            supported_accelerator_classes=frozenset(classes),
        )
    return contracts


@dataclass(frozen=True)
class RenderedConfiguration:
    artifacts: tuple[RenderedConfigurationArtifact, ...]
    terraform_variables: dict[str, Any]


class ConfigurationRenderer(Protocol):
    async def render(
        self,
        configuration: PlatformConfiguration,
        *,
        plan_id: UUID,
        base_revision: int,
        base_etag: str,
    ) -> RenderedConfiguration: ...


class DeclarativeConfigurationRenderer:
    """Render safe immutable receipts and a human-reviewable tfvars handoff."""

    async def render(
        self,
        configuration: PlatformConfiguration,
        *,
        plan_id: UUID,
        base_revision: int,
        base_etag: str,
    ) -> RenderedConfiguration:
        desired = configuration.model_dump(mode="json")
        etag = canonical_sha256(desired)
        artifacts = [
            RenderedConfigurationArtifact(
                kind="ModelConfiguration",
                name=model_id,
                sha256=canonical_sha256(model),
                source=f"catalog/models/{model_id}.json",
            )
            for model_id, model in sorted(desired["models"].items())
        ]
        artifacts.append(
            RenderedConfigurationArtifact(
                kind="PlatformConfiguration",
                name="fs2-serve",
                sha256=etag,
                source=f"admin-configuration-{plan_id}.tfvars.json",
            )
        )
        hot_models = sorted(
            model_id
            for model_id, model in configuration.models.items()
            if model.enabled and model.autoscaling.min_replicas > 0
        )
        cooldowns = sorted({model.autoscaling.cooldown_seconds for model in configuration.models.values()})
        variables: dict[str, Any] = {
            "admin_configuration": desired,
            "admin_configuration_sha256": etag,
            "admin_configuration_plan_id": str(plan_id),
            "admin_configuration_reconciliation_id": str(plan_id),
            "admin_configuration_base_revision": base_revision,
            "admin_configuration_base_etag": base_etag,
            "admin_configuration_bootstrap_baseline_accepted": False,
            "model_scaling_mode": "keda",
            "hot_model_ids": hot_models,
            "model_scaling_overrides": {
                model_id: {
                    "min_replicas": model.autoscaling.min_replicas,
                    "max_replicas": model.autoscaling.max_replicas,
                    "target_queue_depth": model.autoscaling.target_queue_depth,
                    "polling_interval_seconds": model.autoscaling.polling_interval_seconds,
                    "cooldown_seconds": model.autoscaling.cooldown_seconds,
                }
                for model_id, model in sorted(configuration.models.items())
            },
        }
        if len(cooldowns) == 1:
            variables["keda_cooldown_period_seconds"] = cooldowns[0]
        _reject_secret_keys(variables)
        return RenderedConfiguration(tuple(artifacts), variables)


class ConfigurationAuditSink(Protocol):
    async def record(self, receipt: ConfigurationAuditReceipt) -> None: ...


class InMemoryConfigurationAuditSink:
    def __init__(self) -> None:
        self.receipts: list[ConfigurationAuditReceipt] = []

    async def record(self, receipt: ConfigurationAuditReceipt) -> None:
        self.receipts.append(receipt.model_copy(deep=True))


class ConfigurationAuditStore(Protocol):
    async def append_audit_event(
        self,
        *,
        actor: str,
        tenant_id: str | None,
        token_id: UUID | None,
        action: str,
        target_type: str,
        target_id: str,
        outcome: str,
        detail: dict[str, str | int | float | bool | None] | None = None,
    ) -> None: ...


class StoreConfigurationAuditSink:
    """Write configuration receipts to the existing append-only admin audit."""

    def __init__(self, store: ConfigurationAuditStore) -> None:
        self.store = store

    async def record(self, receipt: ConfigurationAuditReceipt) -> None:
        await self.store.append_audit_event(
            actor=receipt.actor,
            tenant_id=None,
            token_id=None,
            action=f"configuration.{receipt.action}",
            target_type="platform_configuration",
            target_id=receipt.subject_sha256,
            outcome="succeeded",
            detail={"receipt_id": str(receipt.receipt_id)},
        )


class ConfigurationRepository(Protocol):
    """Durable repository boundary implemented by the PostgreSQL integration."""

    async def current(self) -> ConfigurationRevision: ...

    async def get_revision(self, revision: int) -> ConfigurationRevision: ...

    async def save_plan(self, plan: ConfigurationPlan) -> None: ...

    async def get_plan(self, plan_id: UUID) -> ConfigurationPlan: ...

    async def save_status(self, status: ReconciliationStatus) -> None: ...

    async def get_status(self, reconciliation_id: UUID) -> ReconciliationStatus: ...


class ConfigurationPersistenceStore(Protocol):
    async def configuration_current(self) -> ConfigurationRevision | None: ...

    async def configuration_get_revision(self, revision: int) -> ConfigurationRevision | None: ...

    async def configuration_ensure_initial(
        self,
        configuration: PlatformConfiguration,
        *,
        actor: str,
    ) -> ConfigurationRevision: ...

    async def configuration_accept_terraform_applied(
        self,
        configuration: PlatformConfiguration,
        receipt: TerraformApplyReceipt,
        *,
        actor: str,
    ) -> ConfigurationRevision: ...

    async def configuration_save_plan(self, plan: ConfigurationPlan) -> None: ...

    async def configuration_get_plan(self, plan_id: UUID) -> ConfigurationPlan | None: ...

    async def configuration_save_status(self, status: ReconciliationStatus) -> None: ...

    async def configuration_get_status(self, reconciliation_id: UUID) -> ReconciliationStatus | None: ...


class StoreConfigurationRepository:
    """Durable repository adapter over the production Store contract."""

    def __init__(self, store: ConfigurationPersistenceStore) -> None:
        self.store = store

    async def ensure_initial(
        self,
        configuration: PlatformConfiguration,
        *,
        actor: str,
    ) -> ConfigurationRevision:
        return await self.store.configuration_ensure_initial(configuration, actor=actor)

    async def accept_terraform_applied(
        self,
        configuration: PlatformConfiguration,
        receipt: TerraformApplyReceipt,
        *,
        actor: str,
    ) -> ConfigurationRevision:
        return await self.store.configuration_accept_terraform_applied(
            configuration,
            receipt,
            actor=actor,
        )

    async def current(self) -> ConfigurationRevision:
        value = await self.store.configuration_current()
        if value is None:
            raise ConfigurationProblemError(
                503,
                "configuration_uninitialized",
                "platform configuration is not initialized",
            )
        return value

    async def get_revision(self, revision: int) -> ConfigurationRevision:
        value = await self.store.configuration_get_revision(revision)
        if value is None:
            raise ConfigurationProblemError(
                404,
                "configuration_revision_not_found",
                "configuration revision was not found",
            )
        return value

    async def save_plan(self, plan: ConfigurationPlan) -> None:
        await self.store.configuration_save_plan(plan)

    async def get_plan(self, plan_id: UUID) -> ConfigurationPlan:
        value = await self.store.configuration_get_plan(plan_id)
        if value is None:
            raise ConfigurationProblemError(404, "configuration_plan_not_found", "configuration plan was not found")
        return value

    async def save_status(self, status: ReconciliationStatus) -> None:
        await self.store.configuration_save_status(status)

    async def get_status(self, reconciliation_id: UUID) -> ReconciliationStatus:
        value = await self.store.configuration_get_status(reconciliation_id)
        if value is None:
            raise ConfigurationProblemError(
                404,
                "configuration_reconciliation_not_found",
                "configuration reconciliation was not found",
            )
        return value


class InMemoryConfigurationRepository:
    """Deterministic test adapter; production startup must inject durable storage."""

    def __init__(
        self,
        initial: PlatformConfiguration,
        *,
        actor: str = "bootstrap",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.clock = clock or (lambda: datetime.now(UTC))
        now = _utc_now(self.clock)
        first = ConfigurationRevision(
            revision=1,
            etag=configuration_etag(initial),
            desired=initial.model_copy(deep=True),
            effective=initial.model_copy(deep=True),
            created_at=now,
            created_by=actor,
        )
        self._revisions = {1: first}
        self._plans: dict[UUID, ConfigurationPlan] = {}
        self._statuses: dict[UUID, ReconciliationStatus] = {}
        self._lock = asyncio.Lock()

    async def current(self) -> ConfigurationRevision:
        async with self._lock:
            return self._revisions[max(self._revisions)].model_copy(deep=True)

    async def get_revision(self, revision: int) -> ConfigurationRevision:
        async with self._lock:
            value = self._revisions.get(revision)
            if value is None:
                raise ConfigurationProblemError(
                    404,
                    "configuration_revision_not_found",
                    "configuration revision was not found",
                )
            return value.model_copy(deep=True)

    async def save_plan(self, plan: ConfigurationPlan) -> None:
        async with self._lock:
            self._plans[plan.plan_id] = plan.model_copy(deep=True)

    async def get_plan(self, plan_id: UUID) -> ConfigurationPlan:
        async with self._lock:
            value = self._plans.get(plan_id)
            if value is None:
                raise ConfigurationProblemError(
                    404,
                    "configuration_plan_not_found",
                    "configuration plan was not found",
                )
            return value.model_copy(deep=True)

    async def save_status(self, status: ReconciliationStatus) -> None:
        async with self._lock:
            self._statuses[status.reconciliation_id] = status.model_copy(deep=True)

    async def get_status(self, reconciliation_id: UUID) -> ReconciliationStatus:
        async with self._lock:
            value = self._statuses.get(reconciliation_id)
            if value is None:
                raise ConfigurationProblemError(
                    404,
                    "configuration_reconciliation_not_found",
                    "configuration reconciliation was not found",
                )
            return value.model_copy(deep=True)


class ConfigurationService:
    def __init__(
        self,
        *,
        repository: ConfigurationRepository,
        catalog: CatalogConfigurationAdapter,
        renderer: ConfigurationRenderer | None = None,
        audit: ConfigurationAuditSink | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.catalog = catalog
        self.renderer = renderer or DeclarativeConfigurationRenderer()
        self.audit = audit or InMemoryConfigurationAuditSink()
        self.clock = clock or (lambda: datetime.now(UTC))
        self._reconcile_lock = asyncio.Lock()

    async def read(self) -> ConfigurationRevision:
        return await self.repository.current()

    async def validate_bootstrap(self, desired: PlatformConfiguration) -> ConfigurationValidation:
        """Validate catalog identity for a Terraform-rendered baseline.

        The first baseline has no prior revision to diff. Production may call
        this only for the immutable ConfigMap rendered by the workloads root;
        static deployed-value equivalence remains a required live acceptance
        input and is not inferred from this catalog validation.
        """

        return await self._validate_desired(desired)

    async def diff(self, proposal: ConfigurationProposal) -> ConfigurationDiff:
        current = await self._require_base(proposal.base_etag)
        changes = configuration_changes(current.desired, proposal.desired)
        return ConfigurationDiff(
            base_revision=current.revision,
            base_etag=current.etag,
            proposed_etag=configuration_etag(proposal.desired),
            changes=changes,
            runtime_change_count=sum(item.owner == ConfigurationOwner.RUNTIME for item in changes),
            terraform_change_count=sum(item.owner == ConfigurationOwner.TERRAFORM for item in changes),
        )

    async def validate(
        self,
        proposal: ConfigurationProposal,
        actor: OperatorPrincipal,
    ) -> ConfigurationValidation:
        await self._require_base(proposal.base_etag)
        result = await self._validate_desired(proposal.desired)
        await self._audit("validate", actor, result.proposed_etag)
        return result

    async def plan(
        self,
        proposal: ConfigurationProposal,
        actor: OperatorPrincipal,
    ) -> ConfigurationPlan:
        _require_role(actor, OperatorRole.OPERATOR)
        return await self._plan(proposal, actor, audit_action="plan")

    async def reconcile(
        self,
        *,
        plan_id: UUID,
        base_etag: str,
        actor: OperatorPrincipal,
    ) -> ReconciliationStatus:
        _require_role(actor, OperatorRole.OPERATOR)
        async with self._reconcile_lock:
            plan = await self.repository.get_plan(plan_id)
            current = await self._require_base(base_etag)
            if plan.base_etag != base_etag or current.etag != plan.base_etag:
                raise ConfigurationProblemError(
                    409,
                    "configuration_changed",
                    "configuration changed after this plan",
                )
            now = _utc_now(self.clock)
            if now >= plan.expires_at:
                raise ConfigurationProblemError(
                    409,
                    "configuration_plan_expired",
                    "configuration plan has expired",
                )
            if plan.state != ConfigurationPlanState.VALID:
                raise ConfigurationProblemError(
                    422,
                    "configuration_plan_rejected",
                    "rejected configuration cannot reconcile",
                )
            reconciliation_id = plan.plan_id
            try:
                existing_status = await self.repository.get_status(reconciliation_id)
            except ConfigurationProblemError as exc:
                if exc.code != "configuration_reconciliation_not_found":
                    raise
                existing_status = None
            if existing_status is not None:
                if (
                    existing_status.plan_id != plan.plan_id
                    or existing_status.base_revision != plan.base_revision
                    or existing_status.target_etag != plan.proposed_etag
                    or existing_status.phase
                    not in {ReconciliationPhase.AWAITING_TERRAFORM, ReconciliationPhase.SUCCEEDED}
                ):
                    raise ConfigurationProblemError(
                        409,
                        "configuration_reconciliation_conflict",
                        "plan reconciliation identity has conflicting durable state",
                    )
                return existing_status
            if plan.terraform.required:
                status = ReconciliationStatus(
                    reconciliation_id=reconciliation_id,
                    plan_id=plan.plan_id,
                    phase=ReconciliationPhase.AWAITING_TERRAFORM,
                    base_revision=plan.base_revision,
                    target_etag=plan.proposed_etag,
                    previous_revision=plan.base_revision,
                    artifact_sha256=[item.sha256 for item in plan.artifacts],
                    terraform_variables_sha256=plan.terraform.variables_sha256,
                    started_at=now,
                )
                await self.repository.save_status(status)
                await self._audit("reconcile", actor, plan.proposed_etag)
                return status
            raise ConfigurationProblemError(
                500,
                "configuration_owner_invariant",
                "a valid configuration plan has no implemented non-Terraform owner",
            )

    async def status(self, reconciliation_id: UUID) -> ReconciliationStatus:
        return await self.repository.get_status(reconciliation_id)

    async def rollback(self, request: RollbackRequest, actor: OperatorPrincipal) -> RollbackPlan:
        _require_role(actor, OperatorRole.ADMIN)
        target = await self.repository.get_revision(request.target_revision)
        proposal = ConfigurationProposal(base_etag=request.base_etag, desired=target.desired)
        plan = await self._plan(proposal, actor, audit_action="rollback")
        return RollbackPlan(target_revision=target.revision, plan=plan)

    async def _plan(
        self,
        proposal: ConfigurationProposal,
        actor: OperatorPrincipal,
        *,
        audit_action: Literal["plan", "rollback"],
    ) -> ConfigurationPlan:
        diff = await self.diff(proposal)
        validation = await self._validate_desired(proposal.desired)
        issues = list(validation.issues)
        unsupported_paths = sorted(
            {
                change.path
                for change in diff.changes
                if not _terraform_change_is_applicable(change.path, proposal.desired)
            }
        )
        available_issue_slots = max(0, 1000 - len(issues))
        for path in unsupported_paths[:available_issue_slots]:
            issues.append(
                ConfigurationValidationIssue(
                    severity=ValidationSeverity.ERROR,
                    code="configuration_change_not_applicable",
                    path=path,
                    message=("field is typed for review but has no proven consumer in the current Terraform root"),
                )
            )
        if not diff.changes:
            issues.append(
                ConfigurationValidationIssue(
                    severity=ValidationSeverity.ERROR,
                    code="configuration_no_changes",
                    path="$",
                    message="proposed configuration is identical to the current revision",
                )
            )
        issues.sort(key=lambda item: (item.path, item.code, item.message))
        validation = ConfigurationValidation(
            valid=not any(item.severity == ValidationSeverity.ERROR for item in issues),
            proposed_etag=validation.proposed_etag,
            issues=issues,
        )
        plan_id = uuid4()
        rendered = (
            await self.renderer.render(
                proposal.desired,
                plan_id=plan_id,
                base_revision=diff.base_revision,
                base_etag=diff.base_etag,
            )
            if validation.valid
            else RenderedConfiguration((), {})
        )
        variables_sha256 = canonical_sha256(rendered.terraform_variables)
        tfvars_json = (
            json.dumps(
                rendered.terraform_variables,
                indent=2,
                sort_keys=True,
                ensure_ascii=True,
            )
            + "\n"
        )
        terraform_required = validation.valid and diff.terraform_change_count > 0
        now = _utc_now(self.clock)
        plan = ConfigurationPlan(
            plan_id=plan_id,
            state=ConfigurationPlanState.VALID if validation.valid else ConfigurationPlanState.REJECTED,
            base_revision=diff.base_revision,
            base_etag=diff.base_etag,
            proposed=proposal.desired,
            proposed_etag=diff.proposed_etag,
            validation=validation,
            diff=diff,
            artifacts=list(rendered.artifacts),
            terraform=TerraformHandoff(
                required=terraform_required,
                state="review-required" if terraform_required else "not-required",
                variables=rendered.terraform_variables,
                variables_sha256=variables_sha256,
                expected_source_etag=diff.base_etag,
                tfvars_filename=f"admin-configuration-{plan_id}.tfvars.json",
                tfvars_json=tfvars_json,
                tfvars_sha256=hashlib.sha256(tfvars_json.encode()).hexdigest(),
            ),
            created_at=now,
            expires_at=now + PLAN_TTL,
            created_by=actor.subject,
        )
        await self.repository.save_plan(plan)
        await self._audit(audit_action, actor, plan.proposed_etag)
        return plan

    async def _require_base(self, etag: str) -> ConfigurationRevision:
        current = await self.repository.current()
        if current.etag != etag:
            raise ConfigurationProblemError(
                409,
                "configuration_changed",
                "configuration changed; refresh and re-plan",
            )
        return current

    async def _validate_desired(self, desired: PlatformConfiguration) -> ConfigurationValidation:
        issues: list[ConfigurationValidationIssue] = []
        exposed_tools: dict[str, str] = {}
        for model_id, model in desired.models.items():
            for pool_id in model.placement.pool_ids:
                pool = desired.pools[pool_id]
                if (
                    model.placement.topology_policy in {"single-node", "nvlink-domain"}
                    and model.placement.accelerators > pool.accelerators_per_node
                ):
                    issues.append(
                        ConfigurationValidationIssue(
                            severity=ValidationSeverity.ERROR,
                            code="placement_exceeds_node",
                            path=f"$.models.{model_id}.placement.accelerators",
                            message="single-node placement exceeds accelerators available on a selected pool node",
                        )
                    )
                if pool.max_nodes == 0:
                    issues.append(
                        ConfigurationValidationIssue(
                            severity=ValidationSeverity.ERROR,
                            code="placement_pool_disabled",
                            path=f"$.models.{model_id}.placement.pool_ids",
                            message="selected accelerator pool has a zero maximum",
                        )
                    )
            if model.mcp.tool_name is not None:
                other = exposed_tools.setdefault(model.mcp.tool_name, model_id)
                if other != model_id:
                    issues.append(
                        ConfigurationValidationIssue(
                            severity=ValidationSeverity.ERROR,
                            code="duplicate_mcp_tool",
                            path=f"$.models.{model_id}.mcp.tool_name",
                            message="MCP tool names must be unique across exposed models",
                        )
                    )
            issues.extend(await self.catalog.validate_model(model, desired))
        etag = configuration_etag(desired)
        issues.sort(key=lambda item: (item.path, item.code, item.message))
        return ConfigurationValidation(
            valid=not any(item.severity == ValidationSeverity.ERROR for item in issues),
            proposed_etag=etag,
            issues=issues,
        )

    async def _audit(
        self,
        action: Literal["validate", "plan", "reconcile", "rollback"],
        actor: OperatorPrincipal,
        subject_sha256: str,
    ) -> None:
        await self.audit.record(
            ConfigurationAuditReceipt(
                receipt_id=uuid4(),
                action=action,
                actor=actor.subject,
                subject_sha256=subject_sha256,
                occurred_at=_utc_now(self.clock),
            )
        )


def _utc_now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeError("configuration clock must be timezone-aware")
    return value.astimezone(UTC)


def _require_role(actor: OperatorPrincipal, required: OperatorRole) -> None:
    try:
        actor.require(required)
    except PermissionError:
        raise ConfigurationProblemError(
            403,
            "configuration_write_forbidden",
            "operator role cannot perform this configuration action",
        ) from None


def _reject_secret_keys(value: object, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(part in normalized for part in _SECRET_KEY_PARTS):
                raise RuntimeError(f"secret-bearing key is forbidden in Terraform handoff at {path}")
            _reject_secret_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_keys(item, f"{path}[{index}]")


def _owner(path: str) -> ConfigurationOwner:
    del path
    # None of the current fields has a proven in-process hot-reload consumer.
    # Every real change therefore stops at the reviewed Terraform handoff. A
    # future runtime-owned field may be added only together with its concrete
    # reconciler and apply/rollback acceptance.
    return ConfigurationOwner.TERRAFORM


def _terraform_change_is_applicable(path: str, desired: PlatformConfiguration) -> bool:
    """Return true only for leaves consumed by model_scaling_overrides."""

    return any(
        path == f"$.models.{model_id}.autoscaling.{field}"
        for model_id in desired.models
        for field in _APPLICABLE_AUTOSCALING_FIELDS
    )


def _walk_changes(before: object, after: object, path: str, output: list[ConfigurationChange]) -> None:
    if isinstance(before, dict) or isinstance(after, dict):
        left = before if isinstance(before, dict) else {}
        right = after if isinstance(after, dict) else {}
        for key in sorted(set(left) | set(right)):
            _walk_changes(left.get(key, _MISSING), right.get(key, _MISSING), f"{path}.{key}", output)
        return
    if before == after:
        return
    output.append(
        ConfigurationChange(
            path=path,
            owner=_owner(path),
            before=None if before is _MISSING else copy.deepcopy(before),
            after=None if after is _MISSING else copy.deepcopy(after),
        )
    )


def configuration_changes(
    before: PlatformConfiguration,
    after: PlatformConfiguration,
) -> list[ConfigurationChange]:
    changes: list[ConfigurationChange] = []
    _walk_changes(before.model_dump(mode="json"), after.model_dump(mode="json"), "$", changes)
    return changes


def validate_terraform_apply_correlation(
    *,
    current: ConfigurationRevision,
    plan: ConfigurationPlan,
    status: ReconciliationStatus,
    configuration: PlatformConfiguration,
    receipt: TerraformApplyReceipt,
) -> Literal["apply", "replay"]:
    """Validate one durable plan/status/configuration receipt without mutation."""

    target_etag = configuration_etag(configuration)
    expected_variables = {
        "admin_configuration": configuration.model_dump(mode="json"),
        "admin_configuration_sha256": target_etag,
        "admin_configuration_plan_id": str(plan.plan_id),
        "admin_configuration_reconciliation_id": str(plan.plan_id),
        "admin_configuration_base_revision": plan.base_revision,
        "admin_configuration_base_etag": plan.base_etag,
        "admin_configuration_bootstrap_baseline_accepted": False,
    }
    if receipt.plan_id != plan.plan_id or receipt.reconciliation_id != plan.plan_id:
        raise ValueError("Terraform apply receipt identity differs from the durable plan")
    if (
        receipt.base_revision != plan.base_revision
        or receipt.base_etag != plan.base_etag
        or receipt.proposed_etag != plan.proposed_etag
        or receipt.configuration_sha256 != target_etag
    ):
        raise ValueError("Terraform apply receipt revision or ETag differs from the durable plan")
    if plan.state is not ConfigurationPlanState.VALID or not plan.terraform.required:
        raise ValueError("Terraform apply receipt references a non-applicable plan")
    try:
        rendered_tfvars = json.loads(plan.terraform.tfvars_json)
    except (RecursionError, ValueError):
        raise ValueError("Terraform handoff artifact is not valid JSON") from None
    if (
        canonical_sha256(plan.terraform.variables) != plan.terraform.variables_sha256
        or hashlib.sha256(plan.terraform.tfvars_json.encode()).hexdigest() != plan.terraform.tfvars_sha256
        or rendered_tfvars != plan.terraform.variables
        or plan.terraform.tfvars_filename != f"admin-configuration-{plan.plan_id}.tfvars.json"
    ):
        raise ValueError("Terraform handoff artifact differs from the durable plan")
    if any(not _terraform_change_is_applicable(change.path, plan.proposed) for change in plan.diff.changes):
        raise ValueError("Terraform apply receipt contains a change without a proven consumer")
    if plan.proposed != configuration or plan.proposed_etag != target_etag:
        raise ValueError("Terraform-applied configuration differs from the durable proposal")
    if any(plan.terraform.variables.get(key) != value for key, value in expected_variables.items()):
        raise ValueError("Terraform handoff variables differ from the durable receipt")
    if (
        status.reconciliation_id != receipt.reconciliation_id
        or status.plan_id != plan.plan_id
        or status.base_revision != plan.base_revision
        or status.target_etag != plan.proposed_etag
        or status.previous_revision != plan.base_revision
        or status.terraform_variables_sha256 != plan.terraform.variables_sha256
    ):
        raise ValueError("Terraform apply receipt differs from the durable awaiting event")

    if current.etag == target_etag:
        if (
            current.reconciliation_id != receipt.reconciliation_id
            or current.desired != configuration
            or current.effective != configuration
            or status.phase is not ReconciliationPhase.SUCCEEDED
            or status.applied_revision != current.revision
        ):
            raise ValueError("Terraform apply receipt replay is not byte-identical to committed state")
        return "replay"

    if current.revision != receipt.base_revision or current.etag != receipt.base_etag:
        raise ValueError("current configuration no longer matches the Terraform apply receipt base")
    if status.phase is not ReconciliationPhase.AWAITING_TERRAFORM:
        raise ValueError("Terraform apply receipt has no matching awaiting event")
    return "apply"
