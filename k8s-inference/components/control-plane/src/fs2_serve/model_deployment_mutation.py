"""Authenticated durable desired-state mutations for ModelDeployment.

PostgreSQL is the append-only operator intent and audit authority.  The
Kubernetes custom resource is an eventually reconciled projection.  A failed
API-server write therefore returns a persisted/pending result that can be
retried with the same idempotency key; it never rolls back or loses the desired
revision.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol
from urllib.parse import quote
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi import Path as ApiPath
from pydantic import Field, model_validator

from .access import AdminAccessService
from .access_models import OperatorPrincipal, OperatorRole
from .admin import AdminProblemError
from .admin_models import AdminEnvelope
from .fast_start import FastStartLevel, FastStartMode, FastStartSpec
from .fast_start_mechanisms import (
    DECLARED_MECHANISMS,
    SELECTABLE_MECHANISMS,
    FastStartMechanism,
    GpuResidentQualification,
    HostMemoryResidencyQualification,
)
from .model_deployment import (
    API_VERSION,
    DNS_LABEL_PATTERN,
    DNS_SUBDOMAIN_PATTERN,
    KIND,
    CacheTier,
    DesiredState,
    FastStartMechanismDecision,
    InfrastructureEnvelope,
    LifecycleSpec,
    ModelDeploymentSpec,
    ModelRenderer,
    RenderContext,
    ValidationDisposition,
    spec_digest,
    validate_model_deployment,
)
from .model_deployment_admin import StoreModelDeploymentRepository
from .model_deployment_preview import ETAG_PATTERN, ModelDeploymentPreviewProposal
from .model_deployment_records import (
    ModelDeploymentAppendRequest,
    ModelDeploymentRevision,
    ModelDeploymentRevisionAction,
    ModelDeploymentRuntimePhase,
)
from .models import IdempotencyKey, StrictModel
from .store import ConflictError

DESIRED_FIELD_MANAGER = "fs2-admin-model-desired"


class ModelDeploymentMutationProblemError(RuntimeError):
    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


class DesiredWriteError(RuntimeError):
    """A bounded Kubernetes projection error with no backend payload."""


class DesiredWriteReceipt(StrictModel):
    namespace: str
    name: str
    uid: str
    resource_version: str
    generation: int = Field(ge=1)
    spec_digest: str = Field(pattern=ETAG_PATTERN)


class ModelDeploymentDesiredWriter(Protocol):
    async def apply(self, revision: ModelDeploymentRevision) -> DesiredWriteReceipt: ...


class HttpKubernetesDesiredWriter:
    """Narrow Kubernetes client for the ModelDeployment desired state only."""

    def __init__(
        self,
        *,
        base_url: str,
        token_file: Path,
        ca_file: Path,
        namespace: str,
        timeout_seconds: float = 5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.token_file = token_file
        self.namespace = namespace
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            verify=str(ca_file),
            timeout=httpx.Timeout(timeout_seconds),
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise DesiredWriteError("Kubernetes credential is unavailable") from exc
        if len(token) < 16:
            raise DesiredWriteError("Kubernetes credential is unavailable")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if content_type is not None:
            headers["Content-Type"] = content_type
        return headers

    @staticmethod
    def _metadata(body: Mapping[str, Any], field: str) -> str:
        metadata = body.get("metadata")
        value = metadata.get(field) if isinstance(metadata, Mapping) else None
        if not isinstance(value, str) or not value:
            raise DesiredWriteError(f"Kubernetes response metadata.{field} is unavailable")
        return value

    def _path(self, name: str) -> str:
        group, version = API_VERSION.split("/", 1)
        return (
            f"/apis/{group}/{version}/namespaces/{quote(self.namespace, safe='')}/"
            f"modeldeployments/{quote(name, safe='')}"
        )

    def _collection_path(self) -> str:
        group, version = API_VERSION.split("/", 1)
        return f"/apis/{group}/{version}/namespaces/{quote(self.namespace, safe='')}/modeldeployments"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        allow_not_found: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        try:
            response = await self.client.request(
                method,
                path,
                headers=self._headers(kwargs.pop("content_type", None)),
                **kwargs,
            )
        except (OSError, httpx.HTTPError) as exc:
            raise DesiredWriteError("Kubernetes API request failed") from exc
        if response.status_code == 409:
            raise DesiredWriteError("Kubernetes desired-state ownership conflict")
        if response.status_code >= 400 and not (allow_not_found and response.status_code == 404):
            raise DesiredWriteError(f"Kubernetes API returned HTTP {response.status_code}")
        return response

    @staticmethod
    def _desired_revision(body: Mapping[str, Any]) -> int:
        metadata = body.get("metadata")
        annotations = metadata.get("annotations") if isinstance(metadata, Mapping) else None
        raw = annotations.get("inference.fs2.nebius.ai/desired-revision") if isinstance(annotations, Mapping) else None
        if not isinstance(raw, str) or not raw.isdigit() or int(raw) < 1:
            raise DesiredWriteError("Kubernetes desired-state revision annotation is unavailable")
        return int(raw)

    def _receipt(self, value: Mapping[str, Any], revision: ModelDeploymentRevision) -> DesiredWriteReceipt:
        observed_revision = self._desired_revision(value)
        if observed_revision > revision.revision:
            raise DesiredWriteError("Kubernetes already contains a newer desired revision")
        if observed_revision != revision.revision:
            raise DesiredWriteError("Kubernetes desired-state revision did not converge")
        metadata = value.get("metadata")
        generation = metadata.get("generation") if isinstance(metadata, Mapping) else None
        returned_spec = value.get("spec")
        try:
            observed_spec = ModelDeploymentSpec.model_validate(returned_spec)
        except ValueError as exc:
            raise DesiredWriteError("Kubernetes desired-state response spec is invalid") from exc
        if spec_digest(observed_spec) != revision.etag or not isinstance(generation, int) or generation < 1:
            raise DesiredWriteError("Kubernetes desired-state read-after-write verification failed")
        return DesiredWriteReceipt(
            namespace=revision.namespace,
            name=revision.name,
            uid=self._metadata(value, "uid"),
            resource_version=self._metadata(value, "resourceVersion"),
            generation=generation,
            spec_digest=revision.etag,
        )

    async def apply(self, revision: ModelDeploymentRevision) -> DesiredWriteReceipt:
        if revision.namespace != self.namespace:
            raise DesiredWriteError("desired model namespace is outside writer policy")
        body: dict[str, Any] = {
            "apiVersion": API_VERSION,
            "kind": KIND,
            "metadata": {
                "namespace": revision.namespace,
                "name": revision.name,
                "labels": {
                    "app.kubernetes.io/managed-by": "fs2-admin-model-desired",
                    "app.kubernetes.io/part-of": "fs2-serve",
                },
                "annotations": {
                    "inference.fs2.nebius.ai/desired-revision": str(revision.revision),
                    "inference.fs2.nebius.ai/spec-digest": revision.etag,
                },
            },
            "spec": revision.spec.model_dump(mode="json", by_alias=True),
        }
        path = self._path(revision.name)
        current_response = await self._request("GET", path, allow_not_found=True)
        if current_response.status_code == 404:
            await self._request(
                "POST",
                self._collection_path(),
                params={"fieldManager": DESIRED_FIELD_MANAGER, "fieldValidation": "Strict"},
                content_type="application/json",
                content=json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            )
        else:
            current = current_response.json()
            if not isinstance(current, Mapping):
                raise DesiredWriteError("Kubernetes desired-state response is invalid")
            current_revision = self._desired_revision(current)
            if current_revision > revision.revision:
                raise DesiredWriteError("Kubernetes already contains a newer desired revision")
            if current_revision == revision.revision:
                return self._receipt(current, revision)
            patch = {
                "metadata": {
                    "resourceVersion": self._metadata(current, "resourceVersion"),
                    "labels": body["metadata"]["labels"],
                    "annotations": body["metadata"]["annotations"],
                },
                "spec": body["spec"],
            }
            await self._request(
                "PATCH",
                path,
                params={"fieldManager": DESIRED_FIELD_MANAGER, "fieldValidation": "Strict"},
                content_type="application/merge-patch+json",
                content=json.dumps(patch, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            )

        # A separate read closes the create/update race.  If another replica
        # advanced the CR after this write, the older caller returns pending
        # instead of claiming that its revision is the live projection.
        reread = await self._request("GET", path)
        value = reread.json()
        if not isinstance(value, Mapping):
            raise DesiredWriteError("Kubernetes desired-state response is invalid")
        return self._receipt(value, revision)

    async def list_models(self) -> list[dict[str, Any]]:
        response = await self._request("GET", self._collection_path(), params={"limit": "1000"})
        value = response.json()
        items = value.get("items") if isinstance(value, Mapping) else None
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise DesiredWriteError("Kubernetes ModelDeployment list response is invalid")
        if len(items) > 1000:
            raise DesiredWriteError("Kubernetes ModelDeployment list exceeds the configured bound")
        return items


class ModelDeploymentApplyRequest(StrictModel):
    preview_id: UUID
    proposed_etag: str = Field(pattern=ETAG_PATTERN)
    proposal: ModelDeploymentPreviewProposal
    idempotency_key: IdempotencyKey

    @model_validator(mode="after")
    def preview_matches_proposal(self) -> ModelDeploymentApplyRequest:
        if self.proposed_etag != spec_digest(self.proposal.spec):
            raise ValueError("proposed ETag does not match the submitted desired spec")
        if self.proposal.base_etag is None and self.proposal.name == "":  # pragma: no cover - bounded by proposal
            raise ValueError("proposal identity is invalid")
        return self


class ModelDeploymentActionRequest(StrictModel):
    base_etag: str = Field(pattern=ETAG_PATTERN)
    idempotency_key: IdempotencyKey


class ModelDeploymentRollbackRequest(ModelDeploymentActionRequest):
    target_revision: int = Field(ge=1)


class ModelDeploymentReconcileRequest(StrictModel):
    expected_etag: str = Field(pattern=ETAG_PATTERN)


class ModelDeploymentMutationResult(StrictModel):
    revision: ModelDeploymentRevision
    idempotent_replay: bool
    projection: Literal["applied", "pending"]
    receipt: DesiredWriteReceipt | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def projection_state(self) -> ModelDeploymentMutationResult:
        if self.projection == "applied" and (self.receipt is None or self.reason is not None):
            raise ValueError("applied projection requires a receipt and no reason")
        if self.projection == "pending" and (self.receipt is not None or self.reason is None):
            raise ValueError("pending projection requires a reason and no receipt")
        return self


class ModelDeploymentActionCapability(StrictModel):
    enabled: bool
    reason: str | None = None


class ModelDeploymentPoolChoice(StrictModel):
    pool_ref: str = Field(min_length=1, max_length=128)
    accelerator_class: str = Field(min_length=1, max_length=128)
    capacity_type: str = Field(min_length=1, max_length=64)
    accelerators_per_node: int = Field(ge=1, le=64)
    maximum_replicas: int = Field(ge=1, le=10000)


class ModelDeploymentFastStartMechanismChoice(StrictModel):
    mechanism: FastStartMechanism
    pool_refs: list[str] = Field(min_length=1, max_length=128)
    required_cache_tier: CacheTier | None = None
    minimum_hot_replicas: int = Field(default=0, ge=0, le=10000)
    minimum_max_replicas: int = Field(default=1, ge=1, le=10000)

    @model_validator(mode="after")
    def exact_dependencies(self) -> ModelDeploymentFastStartMechanismChoice:
        if self.mechanism not in SELECTABLE_MECHANISMS:
            raise ValueError("configuration option mechanism is not operator-selectable")
        if len(set(self.pool_refs)) != len(self.pool_refs):
            raise ValueError("configuration option mechanism pools must be unique")
        if self.minimum_max_replicas < self.minimum_hot_replicas:
            raise ValueError("configuration option mechanism ceiling cannot be below its hot floor")
        return self


class ModelDeploymentConfigurationOption(StrictModel):
    model_ref: str = Field(min_length=1, max_length=128)
    suggested_name: str = Field(min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)
    namespace: str = Field(min_length=1, max_length=63, pattern=DNS_LABEL_PATTERN)
    default_spec: ModelDeploymentSpec
    pool_choices: list[ModelDeploymentPoolChoice] = Field(min_length=1, max_length=128)
    local_queue_choices: list[str] = Field(min_length=1, max_length=128)
    priority_class_choices: list[str] = Field(min_length=1, max_length=128)
    tenant_choices: list[str] = Field(min_length=1, max_length=1024)
    scale_to_zero_qualified: bool
    fast_start_mechanism_choices: list[ModelDeploymentFastStartMechanismChoice] = Field(
        min_length=1,
        max_length=len(SELECTABLE_MECHANISMS),
    )
    # Highest fast-start level backed by compatible benchmark evidence for the
    # default spec across every default pool; Off when no evidence exists.
    fast_start_qualified_level: FastStartLevel = FastStartLevel.OFF

    @model_validator(mode="after")
    def defaults_are_allowed(self) -> ModelDeploymentConfigurationOption:
        pool_refs = {choice.pool_ref for choice in self.pool_choices}
        default_pool_refs = set(self.default_spec.placement.pool_refs)
        if self.default_spec.model_ref != self.model_ref:
            raise ValueError("configuration option modelRef differs from its default spec")
        if len(pool_refs) != len(self.pool_choices):
            raise ValueError("configuration option pool choices must be unique")
        if not default_pool_refs or not default_pool_refs.issubset(pool_refs):
            raise ValueError("configuration option default pools must be a non-empty subset of allowed choices")
        if self.default_spec.queue.local_queue not in self.local_queue_choices:
            raise ValueError("configuration option default queue is not an allowed choice")
        if self.default_spec.queue.priority_class not in self.priority_class_choices:
            raise ValueError("configuration option default priority is not an allowed choice")
        if self.default_spec.tenant_id not in self.tenant_choices:
            raise ValueError("configuration option default tenant is not an allowed choice")
        if self.default_spec.availability.min_replicas == 0 and not self.scale_to_zero_qualified:
            raise ValueError("configuration option cannot default to unqualified scale-to-zero")
        mechanism_choices = {choice.mechanism: choice for choice in self.fast_start_mechanism_choices}
        if len(mechanism_choices) != len(self.fast_start_mechanism_choices):
            raise ValueError("configuration option mechanism choices must be unique")
        if FastStartMechanism.CONVENTIONAL not in mechanism_choices:
            raise ValueError("configuration option must retain the conventional mechanism")
        if any(not set(choice.pool_refs).issubset(pool_refs) for choice in mechanism_choices.values()):
            raise ValueError("configuration option mechanism pools must be allowed pool choices")
        default_mechanism = self.default_spec.cache.mechanism
        if default_mechanism is not None:
            choice = mechanism_choices.get(default_mechanism)
            if choice is None or not default_pool_refs.issubset(choice.pool_refs):
                raise ValueError("configuration option default mechanism is not an allowed choice")
        return self


class ModelDeploymentMutationCapabilities(StrictModel):
    schema_version: Literal["fs2-serve.nebius.ai/model-deployment-mutations/v1"] = (
        "fs2-serve.nebius.ai/model-deployment-mutations/v1"
    )
    declarative_apply: ModelDeploymentActionCapability
    drain: ModelDeploymentActionCapability
    rollback: ModelDeploymentActionCapability
    reconcile: ModelDeploymentActionCapability
    hard_delete: ModelDeploymentActionCapability
    configuration_revision: str = Field(pattern=ETAG_PATTERN)
    configuration_options: list[ModelDeploymentConfigurationOption] = Field(max_length=512)


_TENANT_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?$")
_DNS_SUBDOMAIN = re.compile(DNS_SUBDOMAIN_PATTERN)


def _dns_safe_reference(value: str) -> str:
    candidate = value.lower()
    if len(candidate) <= 253 and _DNS_SUBDOMAIN.fullmatch(candidate) is not None:
        return candidate
    stem = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "model"
    suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    stem = stem[: 63 - len(suffix) - 1].rstrip("-") or "model"
    return f"{stem}-{suffix}"


def _dns_safe_name(model_ref: str) -> str:
    return _dns_safe_reference(f"{model_ref}-live")


def _valid_dns_choice(value: str) -> bool:
    return 1 <= len(value) <= 253 and _DNS_SUBDOMAIN.fullmatch(value) is not None


def _valid_tenant_choice(value: str) -> bool:
    return 1 <= len(value) <= 120 and _TENANT_PATTERN.fullmatch(value) is not None


class ModelDeploymentMutationService:
    def __init__(
        self,
        *,
        repository: StoreModelDeploymentRepository,
        writer: ModelDeploymentDesiredWriter,
        envelope: InfrastructureEnvelope,
        renderer: ModelRenderer | None = None,
        prometheus_server_address: str | None = None,
        namespace: str = "fs2-models",
    ) -> None:
        self.repository = repository
        self.writer = writer
        self.envelope = envelope
        self.renderer = renderer
        self.prometheus_server_address = prometheus_server_address
        self.namespace = namespace

    def _render_is_proven(
        self,
        *,
        spec: ModelDeploymentSpec,
        mechanism: FastStartMechanismDecision,
        admitted_pool_ref: str | None,
        name: str,
        generation: int,
    ) -> bool:
        """Render the exact accepted mechanism with the installed template."""

        if (
            self.renderer is None
            or self.prometheus_server_address is None
            or admitted_pool_ref is None
            or not mechanism.renderable
        ):
            return False
        qualification = self.envelope.qualifications.get(spec.model_ref)
        pool = self.envelope.pools.get(admitted_pool_ref)
        if qualification is None or pool is None:
            return False
        try:
            self.renderer.render(
                spec,
                RenderContext(
                    name=name,
                    namespace=self.namespace,
                    uid=None,
                    generation=generation,
                    pool=pool,
                    eligible_pools=[self.envelope.pools[pool_ref] for pool_ref in spec.placement.pool_refs],
                    prometheus_server_address=self.prometheus_server_address,
                    fast_start_mechanism=mechanism,
                    model_express=qualification.model_express,
                    regional_cache=qualification.regional_cache,
                    host_memory_residency=qualification.host_memory_residency,
                    gpu_resident=qualification.gpu_resident,
                    residency_holder_image=self.envelope.residency_holder_image,
                    preview=True,
                ),
            )
        except ValueError:
            return False
        return True

    def _mechanism_choice_is_renderable(
        self,
        *,
        default_spec: ModelDeploymentSpec,
        choice: ModelDeploymentFastStartMechanismChoice,
        pool_refs: list[str],
        prove_full_node_pool: bool = False,
    ) -> bool:
        """Prove an advertised mechanism and pool set with the real renderer.

        A host-memory single-pool proof fills every accelerator slot on one
        node so holder plus runtime RAM is checked at worst-case Pod density.
        The subsequent full-set proof catches multi-pool render interactions.
        """

        minimum_replicas = max(default_spec.availability.min_replicas, choice.minimum_hot_replicas)
        available_replicas = sum(
            (self.envelope.pools[pool_ref].accelerators_per_node // default_spec.placement.accelerators_per_replica)
            * self.envelope.pools[pool_ref].max_nodes
            for pool_ref in pool_refs
        )
        maximum_replicas = max(default_spec.availability.max_replicas, choice.minimum_max_replicas)
        if len(pool_refs) == 1:
            # This artificial subset proves that the pool may be offered; it
            # must not inherit a multi-pool replica ceiling the pool cannot
            # satisfy on its own.
            maximum_replicas = min(maximum_replicas, available_replicas)
        if prove_full_node_pool:
            if len(pool_refs) != 1:
                return False
            pool = self.envelope.pools[pool_refs[0]]
            runtime_pods_per_node = pool.accelerators_per_node // default_spec.placement.accelerators_per_replica
            if runtime_pods_per_node < 1:
                return False
            maximum_replicas = runtime_pods_per_node
        if maximum_replicas < max(minimum_replicas, choice.minimum_max_replicas):
            return False
        candidate = default_spec.model_copy(
            update={
                "placement": default_spec.placement.model_copy(update={"pool_refs": pool_refs}),
                "availability": default_spec.availability.model_copy(
                    update={
                        "min_replicas": minimum_replicas,
                        "max_replicas": maximum_replicas,
                    }
                ),
                "cache": default_spec.cache.model_copy(
                    update={
                        "tier": choice.required_cache_tier or default_spec.cache.tier,
                        "mechanism": choice.mechanism,
                    }
                ),
            }
        )
        decision = validate_model_deployment(candidate, self.envelope)
        if decision.disposition is not ValidationDisposition.ACCEPTED:
            return False
        return self._render_is_proven(
            spec=candidate,
            mechanism=decision.fast_start_mechanism,
            admitted_pool_ref=decision.admitted_pool_ref,
            name=_dns_safe_name(candidate.model_ref),
            generation=1,
        )

    def configuration_options(self) -> list[ModelDeploymentConfigurationOption]:
        """Return only complete defaults accepted by the installed envelope."""

        options: list[ModelDeploymentConfigurationOption] = []
        valid_queues = sorted(value for value in self.envelope.local_queues if _valid_dns_choice(value))
        valid_priorities = sorted(value for value in self.envelope.priority_classes if _valid_dns_choice(value))
        valid_tenants = sorted(value for value in self.envelope.tenant_ids if _valid_tenant_choice(value))
        if not valid_queues or not valid_priorities or not valid_tenants:
            return options

        for model_ref, qualification in sorted(self.envelope.qualifications.items()):
            accelerators = qualification.max_accelerators_per_replica
            budget_replicas = min(10000, self.envelope.max_accelerators_per_model // accelerators)
            pool_choices: list[ModelDeploymentPoolChoice] = []
            for pool_ref, pool in sorted(self.envelope.pools.items()):
                if (
                    pool.accelerator_class not in qualification.accelerator_classes
                    or pool.accelerators_per_node < accelerators
                ):
                    continue
                maximum_replicas = min(
                    10000,
                    budget_replicas,
                    (pool.accelerators_per_node // accelerators) * pool.max_nodes,
                )
                if maximum_replicas < 1:
                    continue
                pool_choices.append(
                    ModelDeploymentPoolChoice(
                        pool_ref=pool_ref,
                        accelerator_class=pool.accelerator_class,
                        capacity_type=pool.capacity_type,
                        accelerators_per_node=pool.accelerators_per_node,
                        maximum_replicas=maximum_replicas,
                    )
                )
            pool_choices.sort(
                key=lambda choice: (
                    choice.capacity_type == "preemptible",
                    -choice.maximum_replicas,
                    choice.pool_ref,
                )
            )
            valid_images = sorted(
                value
                for value in qualification.runtime_images
                if 73 <= len(value) <= 768 and re.fullmatch(r"^[^\s@]+@sha256:[a-f0-9]{64}$", value) is not None
            )
            valid_templates = sorted(
                (name, value)
                for name, value in qualification.template_refs.items()
                if _valid_dns_choice(name) and re.fullmatch(r"^sha256:[a-f0-9]{64}$", value) is not None
            )
            artifact_pairs = sorted(qualification.artifact_revisions.items())
            if (
                not pool_choices
                or not valid_images
                or not valid_templates
                or not artifact_pairs
                or (not qualification.open_ai_qualified and qualification.mcp_tool_name is None)
            ):
                continue

            artifact_revision, artifact_digest = artifact_pairs[0]
            template_name, template_digest = valid_templates[0]
            default_maximum = min(4, budget_replicas, sum(choice.maximum_replicas for choice in pool_choices))
            default_minimum = 0 if qualification.scale_to_zero_qualified else 1
            selectable_pool_refs = [choice.pool_ref for choice in pool_choices]
            mechanism_choices = [
                ModelDeploymentFastStartMechanismChoice(
                    mechanism=FastStartMechanism.CONVENTIONAL,
                    pool_refs=selectable_pool_refs,
                )
            ]
            for mechanism in DECLARED_MECHANISMS:
                if mechanism not in SELECTABLE_MECHANISMS:
                    continue
                declaration = qualification.mechanism_declaration(mechanism)
                if declaration is None:
                    continue
                declared_pool_refs = [
                    pool_ref for pool_ref in selectable_pool_refs if pool_ref in declaration.pool_refs
                ]
                if isinstance(declaration, HostMemoryResidencyQualification):
                    if declaration.residency_mode == "runtime-sleep-offload":
                        continue
                    declared_pool_refs = [
                        pool_ref
                        for pool_ref in declared_pool_refs
                        if (allocatable_memory := self.envelope.pools[pool_ref].allocatable_memory_bytes) is not None
                        and declaration.reserved_bytes <= allocatable_memory
                    ]
                if not declared_pool_refs:
                    continue
                minimum_hot_replicas = 0
                minimum_max_replicas = 1
                required_cache_tier: CacheTier | None = None
                if mechanism in (
                    FastStartMechanism.REGIONAL_CACHE,
                    FastStartMechanism.HOST_MEMORY_RESIDENCY,
                ):
                    required_cache_tier = CacheTier.SHARED_FILESYSTEM
                    if qualification.template_cache_tiers[template_digest] is not required_cache_tier:
                        continue
                if isinstance(declaration, GpuResidentQualification):
                    minimum_hot_replicas = declaration.minimum_hot_replicas
                    minimum_max_replicas = declaration.minimum_hot_replicas + declaration.standby_replicas
                    available_replicas = sum(
                        choice.maximum_replicas for choice in pool_choices if choice.pool_ref in declared_pool_refs
                    )
                    if minimum_max_replicas > available_replicas:
                        continue
                mechanism_choices.append(
                    ModelDeploymentFastStartMechanismChoice(
                        mechanism=mechanism,
                        pool_refs=declared_pool_refs,
                        required_cache_tier=required_cache_tier,
                        minimum_hot_replicas=minimum_hot_replicas,
                        minimum_max_replicas=minimum_max_replicas,
                    )
                )
            try:
                default_spec = ModelDeploymentSpec.model_validate(
                    {
                        "modelRef": model_ref,
                        "tenantId": valid_tenants[0],
                        "lifecycle": {"desiredState": "Enabled"},
                        "artifact": {
                            "revision": artifact_revision,
                            "manifestDigest": artifact_digest,
                            "storageRef": None,
                        },
                        "runtime": {
                            "profile": qualification.runtime_profile,
                            "image": valid_images[0],
                            "templateRef": {
                                "name": template_name,
                                "digest": template_digest,
                            },
                        },
                        "placement": {
                            "poolRefs": [choice.pool_ref for choice in pool_choices],
                            "acceleratorsPerReplica": accelerators,
                            "topologyPolicy": "SingleNode",
                        },
                        "availability": {
                            "minReplicas": default_minimum,
                            "maxReplicas": max(default_minimum, default_maximum),
                            "idleSeconds": 300,
                            "targetQueueDepth": 1,
                            "pollingIntervalSeconds": 5,
                            "cooldownSeconds": 300,
                            "warmWindows": [],
                        },
                        "cache": {
                            "tier": qualification.template_cache_tiers[template_digest].value,
                            "snapshotPreference": "Never",
                            "snapshotRef": None,
                            # An unpinned draft keeps the historical spec digest.
                            # Fixed mode renders conventional loading; Automatic
                            # mode resolves one qualified, renderable mechanism.
                            "mechanism": None,
                        },
                        "queue": {
                            "localQueue": valid_queues[0],
                            "priorityClass": valid_priorities[0],
                            "maxQueueSeconds": 900,
                        },
                        "rollout": {
                            "strategy": "Rolling",
                            "maxUnavailable": 0,
                            "maxSurge": 1,
                            "progressDeadlineSeconds": 1800,
                        },
                        "exposure": {
                            "openAI": qualification.open_ai_qualified,
                            # The canonical model ID is already an OpenAI route.
                            # Repeating it as an alias collides during atomic
                            # registry binding and would withdraw the snapshot.
                            "openAIAliases": [],
                            # OpenAI and MCP are independent qualified surfaces.
                            # Match Terraform bootstrap by publishing both when
                            # the canonical model has an approved MCP tool.
                            "mcp": qualification.mcp_tool_name is not None,
                            "mcpToolName": qualification.mcp_tool_name,
                        },
                        "policy": {
                            "visibility": "Tenant",
                            "policyRef": "tenant-default.v1",
                            "allowedPrincipalIds": [],
                            "ratePolicyRef": None,
                        },
                        "adoption": {"mode": "None", "receiptRef": None},
                        # No startup-time class is requested until an operator
                        # picks one; qualification is published separately.
                        "fastStart": {"mode": "Fixed", "level": "Off", "fallbackPolicy": "AllowLowerLevel"},
                    }
                )
                renderable_mechanism_choices: list[ModelDeploymentFastStartMechanismChoice] = []
                for choice in mechanism_choices:
                    renderable_pool_refs = [
                        pool_ref
                        for pool_ref in choice.pool_refs
                        if self._mechanism_choice_is_renderable(
                            default_spec=default_spec,
                            choice=choice,
                            pool_refs=[pool_ref],
                            prove_full_node_pool=choice.mechanism is FastStartMechanism.HOST_MEMORY_RESIDENCY,
                        )
                    ]
                    if not renderable_pool_refs:
                        continue
                    choice = choice.model_copy(update={"pool_refs": renderable_pool_refs})
                    if not self._mechanism_choice_is_renderable(
                        default_spec=default_spec,
                        choice=choice,
                        pool_refs=renderable_pool_refs,
                    ):
                        continue
                    renderable_mechanism_choices.append(choice)
                conventional_choice = next(
                    (
                        choice
                        for choice in renderable_mechanism_choices
                        if choice.mechanism is FastStartMechanism.CONVENTIONAL
                    ),
                    None,
                )
                if conventional_choice is None or set(conventional_choice.pool_refs) != set(
                    default_spec.placement.pool_refs
                ):
                    continue
                decision = validate_model_deployment(default_spec, self.envelope)
                if decision.disposition is not ValidationDisposition.ACCEPTED or decision.fast_start is None:
                    continue
                options.append(
                    ModelDeploymentConfigurationOption(
                        model_ref=model_ref,
                        suggested_name=_dns_safe_name(model_ref),
                        namespace=self.namespace,
                        default_spec=default_spec,
                        pool_choices=pool_choices,
                        local_queue_choices=valid_queues,
                        priority_class_choices=valid_priorities,
                        tenant_choices=valid_tenants,
                        scale_to_zero_qualified=qualification.scale_to_zero_qualified,
                        fast_start_mechanism_choices=renderable_mechanism_choices,
                        fast_start_qualified_level=decision.fast_start.qualified_level,
                    )
                )
            except ValueError:
                # A malformed or incomplete installed tuple must never become
                # an editable draft that merely looks qualified.
                continue
        return options

    async def _project(self, revision: ModelDeploymentRevision, *, reused: bool) -> ModelDeploymentMutationResult:
        try:
            receipt = await self.writer.apply(revision)
        except DesiredWriteError:
            return ModelDeploymentMutationResult(
                revision=revision,
                idempotent_replay=reused,
                projection="pending",
                reason="desired revision is durable; Kubernetes projection is pending retry",
            )
        return ModelDeploymentMutationResult(
            revision=revision,
            idempotent_replay=reused,
            projection="applied",
            receipt=receipt,
        )

    def _validate(
        self,
        spec: ModelDeploymentSpec,
        current: ModelDeploymentRevision | None,
        *,
        name: str,
    ) -> None:
        decision = validate_model_deployment(
            spec,
            self.envelope,
            current=current.spec if current is not None else None,
        )
        if decision.disposition is ValidationDisposition.REJECTED:
            live_issues = [issue for issue in decision.issues if issue.owner == "live-control-plane"]
            details = "; ".join(f"{issue.code} at {issue.path}: {issue.message}" for issue in live_issues[:8])
            if len(live_issues) > 8:
                details = f"{details}; {len(live_issues) - 8} additional validation issue(s)"
            raise ModelDeploymentMutationProblemError(
                422,
                "model_deployment_rejected",
                f"model deployment failed live-policy or qualification validation: {details}",
            )
        if decision.disposition is ValidationDisposition.INFRASTRUCTURE_REQUIRED:
            inputs = ", ".join(decision.terraform_inputs)
            raise ModelDeploymentMutationProblemError(
                409,
                "model_deployment_infrastructure_required",
                f"Terraform-owned prerequisites are unavailable: {inputs}",
            )
        render_decisions = [decision]
        if spec.cache.mechanism is None and spec.fast_start.mode is FastStartMode.AUTOMATIC:
            # Automatic may choose a different qualified mechanism after demand
            # history changes. Prove every mechanism that live validation could
            # admit later, not only the missing-history choice made during this
            # request. A no-target proof spec isolates renderability from evidence
            # that can legitimately arrive after the desired revision is stored.
            proof_spec = spec.model_copy(update={"fast_start": FastStartSpec()})
            for mechanism in SELECTABLE_MECHANISMS:
                if mechanism is decision.fast_start_mechanism.mechanism:
                    continue
                candidate = validate_model_deployment(
                    proof_spec,
                    self.envelope,
                    current=current.spec if current is not None else None,
                    fast_start_mechanism=FastStartMechanismDecision(
                        mechanism=mechanism,
                        source="Automatic",
                        renderable=True,
                        reason="AuthoritativeRenderCandidate",
                    ),
                )
                if candidate.disposition is ValidationDisposition.ACCEPTED:
                    render_decisions.append(candidate)
        if any(
            not self._render_is_proven(
                spec=spec,
                mechanism=render_decision.fast_start_mechanism,
                admitted_pool_ref=render_decision.admitted_pool_ref,
                name=name,
                generation=current.revision + 1 if current is not None else 1,
            )
            for render_decision in render_decisions
        ):
            raise ModelDeploymentMutationProblemError(
                422,
                "model_deployment_render_failed",
                "qualified renderer could not produce the desired revision",
            )

    @staticmethod
    def _requires_cold_cutover(before: ModelDeploymentSpec, after: ModelDeploymentSpec) -> bool:
        return any(
            old != new
            for old, new in (
                (before.artifact, after.artifact),
                (before.runtime, after.runtime),
                (before.placement, after.placement),
                (before.cache, after.cache),
            )
        )

    async def _require_cold_cutover(
        self,
        *,
        current: ModelDeploymentRevision,
        proposed: ModelDeploymentSpec,
    ) -> None:
        if not self._requires_cold_cutover(current.spec, proposed):
            return
        observation = await self.repository.status(
            namespace=current.namespace,
            name=current.name,
            tenant_id=current.tenant_id,
        )
        replicas = observation.status.replicas if observation is not None else None
        safely_cold = bool(
            current.spec.lifecycle.desired_state is not DesiredState.ENABLED
            and observation is not None
            and observation.revision == current.revision
            and observation.status.spec_digest == current.etag
            and observation.status.phase is ModelDeploymentRuntimePhase.COLD
            and replicas is not None
            and replicas.desired == 0
            and replicas.ready == 0
            and replicas.available == 0
        )
        if not safely_cold:
            raise ModelDeploymentMutationProblemError(
                409,
                "cold_cutover_required",
                "drain the current revision and wait for observed zero replicas before changing runtime material",
            )

    async def apply(
        self,
        request: ModelDeploymentApplyRequest,
        actor: OperatorPrincipal,
    ) -> ModelDeploymentMutationResult:
        proposal = request.proposal
        if proposal.namespace != self.namespace:
            raise ModelDeploymentMutationProblemError(422, "namespace_outside_policy", "namespace is outside policy")
        current = await self.repository.current(
            namespace=proposal.namespace,
            name=proposal.name,
            tenant_id=actor.tenant_id,
        )
        if current is not None and current.tenant_id != proposal.spec.tenant_id:
            raise ModelDeploymentMutationProblemError(409, "model_identity_conflict", "model belongs to another tenant")
        if actor.tenant_id is not None and actor.tenant_id != proposal.spec.tenant_id:
            raise ModelDeploymentMutationProblemError(403, "tenant_forbidden", "tenant is outside operator policy")
        self._validate(proposal.spec, current, name=proposal.name)
        if current is not None:
            await self._require_cold_cutover(current=current, proposed=proposal.spec)
        action = (
            ModelDeploymentRevisionAction.CREATE
            if current is None or (proposal.base_etag is None and current.etag == request.proposed_etag)
            else ModelDeploymentRevisionAction.UPDATE
        )
        try:
            appended = await self.repository.append_revision(
                ModelDeploymentAppendRequest(
                    namespace=proposal.namespace,
                    name=proposal.name,
                    expected_etag=proposal.base_etag,
                    spec=proposal.spec,
                    action=action,
                    actor_id=actor.id,
                    actor=actor.subject,
                    idempotency_key=request.idempotency_key,
                )
            )
        except ConflictError as exc:
            raise ModelDeploymentMutationProblemError(409, "model_deployment_conflict", str(exc)) from None
        return await self._project(appended.value, reused=appended.reused)

    async def _current(self, *, name: str, actor: OperatorPrincipal) -> ModelDeploymentRevision:
        current = await self.repository.current(namespace=self.namespace, name=name, tenant_id=actor.tenant_id)
        if current is None:
            raise ModelDeploymentMutationProblemError(404, "model_deployment_not_found", "model was not found")
        return current

    async def drain(
        self,
        *,
        name: str,
        request: ModelDeploymentActionRequest,
        actor: OperatorPrincipal,
    ) -> ModelDeploymentMutationResult:
        current = await self._current(name=name, actor=actor)
        if request.base_etag != current.etag:
            raise ModelDeploymentMutationProblemError(409, "stale_model_etag", "model changed after selection")
        spec = current.spec.model_copy(
            update={
                "lifecycle": LifecycleSpec(desired_state=DesiredState.DRAINING),
                "availability": current.spec.availability.model_copy(update={"min_replicas": 0}),
            }
        )
        return await self._append_action(
            current=current,
            spec=spec,
            action=ModelDeploymentRevisionAction.UPDATE,
            idempotency_key=request.idempotency_key,
            actor=actor,
        )

    async def rollback(
        self,
        *,
        name: str,
        request: ModelDeploymentRollbackRequest,
        actor: OperatorPrincipal,
    ) -> ModelDeploymentMutationResult:
        current = await self._current(name=name, actor=actor)
        if request.base_etag != current.etag:
            raise ModelDeploymentMutationProblemError(409, "stale_model_etag", "model changed after selection")
        rows = await self.repository.history(
            namespace=self.namespace,
            name=name,
            tenant_id=actor.tenant_id,
            before_revision=None,
            limit=200,
        )
        target = next((item for item in rows if item.revision == request.target_revision), None)
        if target is None:
            raise ModelDeploymentMutationProblemError(404, "model_revision_not_found", "target revision was not found")
        self._validate(target.spec, current, name=current.name)
        await self._require_cold_cutover(current=current, proposed=target.spec)
        return await self._append_action(
            current=current,
            spec=target.spec,
            action=ModelDeploymentRevisionAction.ROLLBACK,
            idempotency_key=request.idempotency_key,
            actor=actor,
        )

    async def _append_action(
        self,
        *,
        current: ModelDeploymentRevision,
        spec: ModelDeploymentSpec,
        action: ModelDeploymentRevisionAction,
        idempotency_key: str,
        actor: OperatorPrincipal,
    ) -> ModelDeploymentMutationResult:
        try:
            appended = await self.repository.append_revision(
                ModelDeploymentAppendRequest(
                    namespace=current.namespace,
                    name=current.name,
                    expected_etag=current.etag,
                    spec=spec,
                    action=action,
                    actor_id=actor.id,
                    actor=actor.subject,
                    idempotency_key=idempotency_key,
                )
            )
        except ConflictError as exc:
            raise ModelDeploymentMutationProblemError(409, "model_deployment_conflict", str(exc)) from None
        return await self._project(appended.value, reused=appended.reused)

    async def reconcile(
        self,
        *,
        name: str,
        request: ModelDeploymentReconcileRequest,
        actor: OperatorPrincipal,
    ) -> ModelDeploymentMutationResult:
        current = await self._current(name=name, actor=actor)
        if request.expected_etag != current.etag:
            raise ModelDeploymentMutationProblemError(409, "stale_model_etag", "model changed after selection")
        return await self._project(current, reused=True)


def model_deployment_mutation_router(
    *,
    service: ModelDeploymentMutationService,
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

    async def authorize(request: Request, action: str) -> OperatorPrincipal:
        actor = identity(request)
        await access.authorize_global(actor, OperatorRole.OPERATOR, action=action)
        return actor

    def translate(exc: ModelDeploymentMutationProblemError) -> AdminProblemError:
        return AdminProblemError(exc.status_code, exc.code, exc.detail)

    @router.get(
        "/admin/api/v1/model-deployments:capabilities",
        response_model=AdminEnvelope[ModelDeploymentMutationCapabilities],
        responses=problem_responses,
    )
    async def capabilities(request: Request) -> AdminEnvelope[ModelDeploymentMutationCapabilities]:
        await access.authorize_global(identity(request), OperatorRole.VIEWER, action="model_deployment.capabilities")
        enabled = ModelDeploymentActionCapability(enabled=True)
        return envelope(
            ModelDeploymentMutationCapabilities(
                declarative_apply=enabled,
                drain=enabled,
                rollback=enabled,
                reconcile=enabled,
                hard_delete=ModelDeploymentActionCapability(
                    enabled=False,
                    reason="drain and retain revision history; hard deletion is not enabled",
                ),
                configuration_revision=service.envelope.revision,
                configuration_options=service.configuration_options(),
            )
        )

    @router.post(
        "/admin/api/v1/model-deployments:apply",
        response_model=AdminEnvelope[ModelDeploymentMutationResult],
        responses=problem_responses,
    )
    async def apply(
        request: Request,
        body: ModelDeploymentApplyRequest,
    ) -> AdminEnvelope[ModelDeploymentMutationResult]:
        actor = await authorize(request, "model_deployment.apply")
        try:
            return envelope(await service.apply(body, actor))
        except ModelDeploymentMutationProblemError as exc:
            raise translate(exc) from None

    @router.post(
        "/admin/api/v1/model-deployments/{name}:drain",
        response_model=AdminEnvelope[ModelDeploymentMutationResult],
        responses=problem_responses,
    )
    async def drain(
        request: Request,
        body: ModelDeploymentActionRequest,
        name: Annotated[str, ApiPath(min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)],
    ) -> AdminEnvelope[ModelDeploymentMutationResult]:
        actor = await authorize(request, "model_deployment.drain")
        try:
            return envelope(await service.drain(name=name, request=body, actor=actor))
        except ModelDeploymentMutationProblemError as exc:
            raise translate(exc) from None

    @router.post(
        "/admin/api/v1/model-deployments/{name}:rollback",
        response_model=AdminEnvelope[ModelDeploymentMutationResult],
        responses=problem_responses,
    )
    async def rollback(
        request: Request,
        body: ModelDeploymentRollbackRequest,
        name: Annotated[str, ApiPath(min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)],
    ) -> AdminEnvelope[ModelDeploymentMutationResult]:
        actor = await authorize(request, "model_deployment.rollback")
        try:
            return envelope(await service.rollback(name=name, request=body, actor=actor))
        except ModelDeploymentMutationProblemError as exc:
            raise translate(exc) from None

    @router.post(
        "/admin/api/v1/model-deployments/{name}:reconcile",
        response_model=AdminEnvelope[ModelDeploymentMutationResult],
        responses=problem_responses,
    )
    async def reconcile(
        request: Request,
        body: ModelDeploymentReconcileRequest,
        name: Annotated[str, ApiPath(min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)],
    ) -> AdminEnvelope[ModelDeploymentMutationResult]:
        actor = await authorize(request, "model_deployment.reconcile")
        try:
            return envelope(await service.reconcile(name=name, request=body, actor=actor))
        except ModelDeploymentMutationProblemError as exc:
            raise translate(exc) from None

    return router
