"""Feature-gated Kubernetes controller and fail-closed server-side-apply writer.

The controller deliberately has no cloud-provider API.  It can only reconcile
namespaced objects selected by the Terraform-owned infrastructure envelope.  A
Kubernetes Lease is checked before every mutation, generic server-side apply
never forces conflicts, and the sole exceptional write is a receipt-gated,
minimal Deployment ``/scale`` apply under a dedicated scale manager. Deletion
always uses UID/resourceVersion preconditions.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import quote
from uuid import uuid4

import asyncpg
import httpx
import uvicorn
from fastapi import FastAPI, Response
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from pydantic import ConfigDict, Field, ValidationError

from .admin import AdminAdapterUnavailableError
from .admin_adapters import HttpPrometheusScalarReader
from .fast_start import (
    FastStartAssessment,
    FastStartAutomaticStatus,
    FastStartLevel,
    FastStartMechanismPoolTransport,
    FastStartMechanismStatus,
    FastStartMode,
    FastStartPathAssessment,
    FastStartQualification,
    FastStartQualificationState,
    FastStartStatus,
)
from .fast_start_identity import mechanism_config_digest
from .fast_start_mechanisms import (
    DECLARED_MECHANISMS,
    MECHANISM_ANNOTATION,
    FastStartCacheMechanismStatus,
    FastStartMechanism,
    project_cache_mechanisms,
)
from .fast_start_policy import (
    AutomaticFastStartPolicy,
    AutomaticFastStartState,
    FastStartHistoryWindow,
    FastStartPath,
    evaluate_automatic_fast_start,
)
from .model_deployment import (
    API_VERSION,
    FIELD_MANAGER,
    FINALIZER,
    KIND,
    MODEL_DEPLOYMENT_LABEL,
    WORKLOAD_POOL_ANNOTATION,
    WORKLOAD_ROLE_ANNOTATION,
    AdoptionMode,
    DesiredState,
    DrainObservation,
    FastStartMechanismDecision,
    InfrastructureEnvelope,
    LegacyManifestRenderer,
    LegacyTemplateBundle,
    ModelDeploymentSpec,
    ObservedResource,
    ReconcileAction,
    ReconcilePlan,
    RenderContext,
    RenderedResource,
    RenderPlan,
    ValidationDisposition,
    bounded_label_value,
    canonical_digest,
    effective_hot_floor,
    operation_demand_promql,
    plan_reconciliation,
    reject_validation_decision,
    validate_model_deployment,
)
from .models import StrictModel

if TYPE_CHECKING:
    from .settings import Settings

LOGGER = logging.getLogger(__name__)
STATUS_FIELD_MANAGER = "fs2-model-controller-status"
FIXED_SCALE_FIELD_MANAGER = "fs2-model-controller-fixed-scale"
SCALE_HANDOFF_RECEIPT_FIELD_MANAGER = "fs2-model-controller-scale-handoff"
FENCE_ANNOTATION = "inference.fs2.nebius.ai/fence-token"
CONTROLLER_LABEL = "app.kubernetes.io/component=model-controller"
STALE_SCALE_FIELD_MANAGERS = frozenset({"keda", "horizontal-pod-autoscaler"})
SCALE_HANDOFF_RECEIPT_ANNOTATION = "inference.fs2.nebius.ai/scale-handoff-receipt"
SCALE_INITIALIZATION_RECEIPT_ANNOTATION = "inference.fs2.nebius.ai/scale-initialization-receipt"
SCALE_GATE_MUTATION_ANNOTATION = "inference.fs2.nebius.ai/scale-gate-mutation"
SCALE_GATE_CONFIG_MAP = "fs2-model-controller-scale-gates"
SCALE_GATE_DENIAL_MESSAGE = "fixed-scale gate blocks autoscaler targetRef creation"
SCALE_GATE_TARGET_PREFIX = "target."
SCALE_GATE_SCALED_OBJECT_PREFIX = "scaledobject."
SCALE_GATE_HPA_PREFIX = "hpa."
# Read-only compatibility key for the rejected d58 shared-ConfigMap layout.
# New evidence is never added to the aggregate gate object.
SCALE_GATE_PREDECESSOR_EVIDENCE_PREFIX = "evidence."
SCALE_GATE_PREDECESSOR_EVIDENCE_CONFIG_MAP_PREFIX = "fs2-scale-evidence-"
SCALE_GATE_PREDECESSOR_EVIDENCE_AUTHORIZATION_KEY = "authorization.json"
SCALE_GATE_PREDECESSOR_EVIDENCE_TARGET_KEY = "target.json"
SCALE_GATE_AUTHORIZATION_MAX_BYTES = 64 * 1024
SCALE_GATE_RECORD_MAX_BYTES = 128 * 1024
SCALE_GATE_PREDECESSOR_EVIDENCE_MAX_BYTES = 64 * 1024
KUBERNETES_CONFIG_MAP_MAX_BYTES = 1024 * 1024
KUBERNETES_INT64_MAX = 9_223_372_036_854_775_807


@dataclass(frozen=True)
class KubernetesFieldConflict:
    manager: str
    subresource: str | None
    api_version: str
    field: str


class ScaleGateTargetIdentity(StrictModel):
    """Exact readable target identity retained behind its bounded digest key."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    api_version: str = Field(alias="apiVersion", min_length=1, max_length=253)
    kind: str = Field(min_length=1, max_length=253)
    namespace: str = Field(min_length=1, max_length=253)
    name: str = Field(min_length=1, max_length=253)


class ScaleGateTargeterIdentity(StrictModel):
    """Exact targeter admitted for one digest-bound workload identity."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    api_version: str = Field(alias="apiVersion", min_length=1, max_length=253)
    kind: str = Field(min_length=1, max_length=253)
    namespace: str = Field(min_length=1, max_length=253)
    name: str = Field(min_length=1, max_length=253)
    owner_uid: str = Field(alias="ownerUID", min_length=1, max_length=253)


class ScaleHandoffScaler(StrictModel):
    """Exact retained identity of the deleted autoscaler."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    api_version: str = Field(alias="apiVersion", min_length=1, max_length=253)
    kind: str = Field(min_length=1, max_length=253)
    namespace: str = Field(min_length=1, max_length=253)
    name: str = Field(min_length=1, max_length=253)
    uid: str = Field(min_length=1, max_length=253)
    generation: int = Field(ge=1)


class ScaleHandoffReceipt(StrictModel):
    """Durable proof that this controller observed an exact scaler transition."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    version: Literal[1]
    deployment_uid: str = Field(alias="deploymentUID", min_length=1, max_length=253)
    model_uid: str = Field(alias="modelUID", min_length=1, max_length=253)
    model_generation: int = Field(alias="modelGeneration", ge=1)
    scaler: ScaleHandoffScaler
    predecessor_evidence_digest: str | None = Field(
        default=None,
        alias="predecessorEvidenceDigest",
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    predecessor_evidence_uid: str | None = Field(
        default=None,
        alias="predecessorEvidenceUID",
        min_length=1,
        max_length=253,
    )
    predecessor_evidence_resource_version: str | None = Field(
        default=None,
        alias="predecessorEvidenceResourceVersion",
        min_length=1,
        max_length=128,
    )

    def annotation_value(self) -> str:
        return self.model_dump_json(by_alias=True)


class ScaleInitializationReceipt(StrictModel):
    """Durable authorization for the first paused Deployment /scale write."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    version: Literal[1]
    deployment_uid: str = Field(alias="deploymentUID", min_length=1, max_length=253)
    model_uid: str = Field(alias="modelUID", min_length=1, max_length=253)
    model_generation: int = Field(alias="modelGeneration", ge=1)
    model_spec_digest: str = Field(alias="modelSpecDigest", pattern=r"^sha256:[0-9a-f]{64}$")
    desired_replicas: int = Field(alias="desiredReplicas", ge=0, le=2_147_483_647)
    target_mode: Literal["fixed", "autoscaled"] = Field(alias="targetMode")
    predecessor_evidence_digest: str | None = Field(
        default=None,
        alias="predecessorEvidenceDigest",
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    predecessor_evidence_uid: str | None = Field(
        default=None,
        alias="predecessorEvidenceUID",
        min_length=1,
        max_length=253,
    )
    predecessor_evidence_resource_version: str | None = Field(
        default=None,
        alias="predecessorEvidenceResourceVersion",
        min_length=1,
        max_length=128,
    )

    def annotation_value(self) -> str:
        return self.model_dump_json(by_alias=True)


ScaleAuthorizationReceipt = ScaleHandoffReceipt | ScaleInitializationReceipt


class ScaleGateScalerCheckpoint(StrictModel):
    """Exact ScaledObject state on one side of a fenced gate transition."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    uid: str = Field(min_length=1, max_length=253)
    resource_version: str = Field(alias="resourceVersion", min_length=1, max_length=128)
    generation: int = Field(ge=1, le=KUBERNETES_INT64_MAX)
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    managed_fields_digest: str = Field(alias="managedFieldsDigest", pattern=r"^sha256:[0-9a-f]{64}$")


class ScaleGateScalerCheckpointV2(StrictModel):
    """Decoder for persisted protocol-v2 checkpoints.

    The original v2 schema did not retain ``managedFieldsDigest``; the brief
    c574 lineage did. Keeping the field optional here decodes both immutable
    predecessors without pretending the missing proof already exists.
    """

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    uid: str = Field(min_length=1, max_length=253)
    resource_version: str = Field(alias="resourceVersion", min_length=1, max_length=128)
    generation: int = Field(ge=1, le=KUBERNETES_INT64_MAX)
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    managed_fields_digest: str | None = Field(
        default=None,
        alias="managedFieldsDigest",
        pattern=r"^sha256:[0-9a-f]{64}$",
    )


class ScaleGateReleaseAuthorizationV2(StrictModel):
    """Read-only compatibility decoder for both persisted v2 variants."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    version: Literal[2]
    deployment_uid: str = Field(alias="deploymentUID", min_length=1, max_length=253)
    model_uid: str = Field(alias="modelUID", min_length=1, max_length=253)
    model_resource_version: str = Field(alias="modelResourceVersion", min_length=1, max_length=128)
    model_generation: int = Field(alias="modelGeneration", ge=1, le=KUBERNETES_INT64_MAX)
    model_spec_digest: str = Field(alias="modelSpecDigest", pattern=r"^sha256:[0-9a-f]{64}$")
    scaler_api_version: str = Field(alias="scalerAPIVersion", min_length=1, max_length=253)
    scaler_kind: str = Field(alias="scalerKind", min_length=1, max_length=253)
    scaler_namespace: str = Field(alias="scalerNamespace", min_length=1, max_length=253)
    scaler_name: str = Field(alias="scalerName", min_length=1, max_length=253)
    desired_scaler_digest: str = Field(alias="desiredScalerDigest", pattern=r"^sha256:[0-9a-f]{64}$")
    expected_scaler_generation: int | None = Field(
        default=None,
        alias="expectedScalerGeneration",
        ge=1,
        le=KUBERNETES_INT64_MAX,
    )
    mutation_token: str | None = Field(default=None, alias="mutationToken", pattern=r"^sha256:[0-9a-f]{64}$")
    mutation_operation: Literal["Apply", "Update"] | None = Field(default=None, alias="mutationOperation")
    prior_scaler: ScaleGateScalerCheckpointV2 | None = Field(default=None, alias="priorScaler")
    applied_scaler: ScaleGateScalerCheckpointV2 | None = Field(default=None, alias="appliedScaler")
    phase: Literal["prepared", "applied", "closed"]

    def annotation_value(self) -> str:
        return self.model_dump_json(by_alias=True)


class ScaleGateReleaseAuthorization(StrictModel):
    """Generation-exact authorization for one ScaledObject create/update.

    Protocol v3 separates the immutable mutation-generation/spec fence from
    the refreshable current ModelDeployment fence. This preserves a completed
    write's token when a later, semantically identical CR generation retries.

    ``prepared`` permits exactly the absent/old state to move to the desired
    digest. ``applied`` is a durable postcondition and permits no further
    ScaledObject mutation. ``closed`` records that a newer fixed generation
    superseded the allowance after every targeter disappeared. The Deployment
    receipt remains the provenance for the scale-owner transition; this record
    binds its autoscaled reversal to the current ModelDeployment revision.
    """

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    version: Literal[3]
    deployment_uid: str = Field(alias="deploymentUID", min_length=1, max_length=253)
    model_uid: str = Field(alias="modelUID", min_length=1, max_length=253)
    model_resource_version: str = Field(alias="modelResourceVersion", min_length=1, max_length=128)
    model_generation: int = Field(alias="modelGeneration", ge=1, le=KUBERNETES_INT64_MAX)
    model_spec_digest: str = Field(alias="modelSpecDigest", pattern=r"^sha256:[0-9a-f]{64}$")
    scaler_api_version: str = Field(alias="scalerAPIVersion", min_length=1, max_length=253)
    scaler_kind: str = Field(alias="scalerKind", min_length=1, max_length=253)
    scaler_namespace: str = Field(alias="scalerNamespace", min_length=1, max_length=253)
    scaler_name: str = Field(alias="scalerName", min_length=1, max_length=253)
    desired_scaler_digest: str = Field(alias="desiredScalerDigest", pattern=r"^sha256:[0-9a-f]{64}$")
    expected_scaler_generation: int | None = Field(
        default=None,
        alias="expectedScalerGeneration",
        ge=1,
        le=KUBERNETES_INT64_MAX,
    )
    mutation_token: str | None = Field(default=None, alias="mutationToken", pattern=r"^sha256:[0-9a-f]{64}$")
    mutation_operation: Literal["Apply", "Update"] | None = Field(default=None, alias="mutationOperation")
    mutation_model_generation: int | None = Field(
        default=None,
        alias="mutationModelGeneration",
        ge=1,
        le=KUBERNETES_INT64_MAX,
    )
    mutation_model_spec_digest: str | None = Field(
        default=None,
        alias="mutationModelSpecDigest",
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    prior_scaler: ScaleGateScalerCheckpoint | None = Field(default=None, alias="priorScaler")
    applied_scaler: ScaleGateScalerCheckpoint | None = Field(default=None, alias="appliedScaler")
    predecessor_evidence_digest: str | None = Field(
        default=None,
        alias="predecessorEvidenceDigest",
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    predecessor_evidence_uid: str | None = Field(
        default=None,
        alias="predecessorEvidenceUID",
        min_length=1,
        max_length=253,
    )
    predecessor_evidence_resource_version: str | None = Field(
        default=None,
        alias="predecessorEvidenceResourceVersion",
        min_length=1,
        max_length=128,
    )
    # Decode the brief rejected v3 lineage so it can be migrated in place.
    # New records never embed this potentially large object; they retain its
    # canonical bytes in a separately keyed, content-addressed gate entry.
    predecessor_authorization: ScaleGateReleaseAuthorizationV2 | None = Field(
        default=None,
        alias="predecessorAuthorization",
    )
    phase: Literal["prepared", "applied", "closed"]

    def annotation_value(self) -> str:
        return self.model_dump_json(by_alias=True)


ScaleGateReleaseRecord = ScaleGateReleaseAuthorization | ScaleGateReleaseAuthorizationV2
ScaleGateAuthorization = ScaleAuthorizationReceipt | ScaleGateReleaseRecord
SCALE_GATE_RELEASE_AUTHORIZATION_TYPES = (
    ScaleGateReleaseAuthorization,
    ScaleGateReleaseAuthorizationV2,
)


class ScaleGateTombstone(StrictModel):
    """Persistent closed gate left after the exact Deployment is deleted."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    version: Literal[1]
    deployment_uid: str = Field(alias="deploymentUID", min_length=1, max_length=253)
    model_uid: str = Field(alias="modelUID", min_length=1, max_length=253)
    predecessor_evidence_digest: str | None = Field(
        default=None,
        alias="predecessorEvidenceDigest",
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    predecessor_evidence_uid: str | None = Field(
        default=None,
        alias="predecessorEvidenceUID",
        min_length=1,
        max_length=253,
    )
    predecessor_evidence_resource_version: str | None = Field(
        default=None,
        alias="predecessorEvidenceResourceVersion",
        min_length=1,
        max_length=128,
    )

    def annotation_value(self) -> str:
        return self.model_dump_json(by_alias=True)


class ScaleGateRecord(StrictModel):
    """Digest-keyed gate value binding readable identity to controller evidence."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    version: Literal[1]
    target: ScaleGateTargetIdentity
    authorization: str = Field(min_length=1, max_length=SCALE_GATE_AUTHORIZATION_MAX_BYTES)

    def value(self) -> str:
        return self.model_dump_json(by_alias=True)


class ScaleGateAllowance(StrictModel):
    """One exact target/targeter tuple consumed by admission CEL."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    version: Literal[1]
    target: ScaleGateTargetIdentity
    targeter: ScaleGateTargeterIdentity

    def value(self) -> str:
        return self.model_dump_json(by_alias=True)


@dataclass(frozen=True)
class ScaleGatePredecessorEvidenceSnapshot:
    """One validated immutable evidence object and its API-server identity."""

    authorization: ScaleGateReleaseAuthorizationV2
    uid: str
    resource_version: str


class ControllerError(RuntimeError):
    """A bounded controller failure that should be retried."""


class KubernetesConflictError(ControllerError):
    """The API server rejected optimistic concurrency or SSA ownership."""

    def __init__(
        self,
        message: str,
        *,
        field_conflicts: tuple[KubernetesFieldConflict, ...] = (),
    ) -> None:
        super().__init__(message)
        self.field_conflicts = field_conflicts


class FenceLostError(ControllerError):
    """This process no longer owns the exact live Lease epoch."""


class WriterDisabledError(ControllerError):
    """A mutation reached an explicitly disabled writer."""


class ControllerFiles(StrictModel):
    infrastructure_envelope: InfrastructureEnvelope
    bundles: list[LegacyTemplateBundle] = Field(min_length=1, max_length=512)

    @classmethod
    def load(cls, envelope_file: Path, bundles_file: Path) -> ControllerFiles:
        envelope = InfrastructureEnvelope.model_validate_json(envelope_file.read_bytes())
        raw_bundles = json.loads(bundles_file.read_bytes())
        if not isinstance(raw_bundles, list):
            raise ValueError("model controller bundle file must contain a JSON array")
        return cls(
            infrastructure_envelope=envelope,
            bundles=[LegacyTemplateBundle.model_validate(item) for item in raw_bundles],
        )

    def renderer(self) -> LegacyManifestRenderer:
        indexed = {(item.model_ref, item.template_digest): item for item in self.bundles}
        if len(indexed) != len(self.bundles):
            raise ValueError("model controller bundle identities must be unique")
        snapshots = {
            (qualification.model_ref, name): bundle
            for qualification in self.infrastructure_envelope.qualifications.values()
            for name, bundle in qualification.gpu_snapshot_bundles.items()
        }
        return LegacyManifestRenderer(indexed, snapshot_bundles=snapshots)


class LeaseFence(StrictModel):
    namespace: str
    name: str
    holder_identity: str
    token: str = Field(min_length=32, max_length=128)
    resource_version: str = Field(min_length=1, max_length=128)
    renew_time: datetime
    duration_seconds: int = Field(ge=5, le=120)


class ModelKey(StrictModel):
    namespace: str
    name: str

    @property
    def text(self) -> str:
        return f"{self.namespace}/{self.name}"


class ModelWriteFence(StrictModel):
    """Exact CR revision that authorized one exceptional scale handoff."""

    key: ModelKey
    uid: str = Field(min_length=1, max_length=128)
    resource_version: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=1)
    spec_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ResourceSnapshot(StrictModel):
    observed: ObservedResource
    resource_version: str = Field(min_length=1, max_length=128)
    generation: int = Field(default=0, ge=0)
    observed_generation: int | None = Field(default=None, ge=0)
    desired_replicas: int | None = Field(default=None, ge=0)
    replicas: int | None = Field(default=None, ge=0)
    updated_replicas: int | None = Field(default=None, ge=0)
    ready_replicas: int | None = Field(default=None, ge=0)
    available_replicas: int | None = Field(default=None, ge=0)
    unavailable_replicas: int | None = Field(default=None, ge=0)
    replica_field_managers: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(exclude=True)


class PodSnapshot(StrictModel):
    """Bounded, read-only lifecycle evidence for one generated runtime Pod."""

    name: str = Field(min_length=1, max_length=253)
    uid: str = Field(min_length=1, max_length=128)
    resource_version: str = Field(min_length=1, max_length=128)
    phase: str = Field(min_length=1, max_length=64)
    scheduled: bool
    initialized: bool
    containers_started: bool
    ready: bool
    deleting: bool = False


class Discovery(StrictModel):
    resources: list[ResourceSnapshot] = Field(max_length=256)
    pods: list[PodSnapshot] = Field(default_factory=list, max_length=10000)
    complete: bool

    def observed(self) -> list[ObservedResource]:
        return [item.observed for item in self.resources]


class ReconcileResult(StrictModel):
    key: ModelKey
    action: str
    generation: int = Field(ge=0)
    wrote: bool = False
    requeue: bool = False
    error_code: str | None = None


class ModelControllerApi(Protocol):
    async def acquire_or_renew_lease(
        self,
        *,
        namespace: str,
        name: str,
        holder_identity: str,
        token: str | None,
        duration_seconds: int,
    ) -> LeaseFence | None: ...

    async def assert_fence(self, fence: LeaseFence) -> None: ...

    async def list_models(self, namespace: str) -> list[dict[str, Any]]: ...

    async def get_model(self, key: ModelKey) -> dict[str, Any] | None: ...

    async def discover(
        self,
        *,
        key: ModelKey,
        owner_uid: str,
        render: RenderPlan,
    ) -> Discovery: ...

    async def apply_resource(
        self,
        resource: RenderedResource,
        *,
        owner_uid: str,
        fence: LeaseFence,
    ) -> ResourceSnapshot: ...

    async def apply_autoscaler_resource(
        self,
        resource: RenderedResource,
        *,
        target: RenderedResource,
        authorization: ScaleGateReleaseAuthorization,
        owner_uid: str,
        model_fence: ModelWriteFence,
        fence: LeaseFence,
    ) -> ResourceSnapshot: ...

    async def initialize_deployment_scale(
        self,
        resource: RenderedResource,
        *,
        replicas: int,
        target_mode: Literal["fixed", "autoscaled"],
        owner_uid: str,
        model_fence: ModelWriteFence,
        fence: LeaseFence,
    ) -> ResourceSnapshot: ...

    async def recover_fixed_initialization(
        self,
        resource: RenderedResource,
        *,
        current: ResourceSnapshot,
        replicas: int,
        owner_uid: str,
        model_fence: ModelWriteFence,
        fence: LeaseFence,
    ) -> ResourceSnapshot: ...

    async def apply_fixed_scale_handoff(
        self,
        resource: RenderedResource,
        *,
        current: ResourceSnapshot,
        owner_uid: str,
        model_generation: int,
        model_fence: ModelWriteFence,
        fence: LeaseFence,
    ) -> ResourceSnapshot: ...

    async def fixed_scale_guard_clear(
        self,
        resource: RenderedResource,
        *,
        current: ResourceSnapshot,
        owner_uid: str,
        model_generation: int,
        model_fence: ModelWriteFence,
        fence: LeaseFence,
    ) -> bool: ...

    async def record_scale_handoff_receipt(
        self,
        resource: RenderedResource,
        *,
        current: ResourceSnapshot,
        scaler: ResourceSnapshot,
        owner_uid: str,
        model_generation: int,
        model_fence: ModelWriteFence,
        fence: LeaseFence,
    ) -> ResourceSnapshot: ...

    async def apply_controller_scale(
        self,
        resource: RenderedResource,
        *,
        current: ResourceSnapshot,
        replicas: int,
        owner_uid: str,
        model_fence: ModelWriteFence,
        fence: LeaseFence,
    ) -> ResourceSnapshot: ...

    async def release_scale_gate(
        self,
        resource: RenderedResource,
        *,
        scaler: RenderedResource,
        current: ResourceSnapshot,
        owner_uid: str,
        model_fence: ModelWriteFence,
        fence: LeaseFence,
    ) -> ScaleGateReleaseAuthorization: ...

    async def prepare_scale_gate_deletion(
        self,
        resource: RenderedResource,
        *,
        current: ResourceSnapshot,
        owner_uid: str,
        model_fence: ModelWriteFence,
        fence: LeaseFence,
    ) -> None: ...

    async def confirm_scale_gate_tombstone(
        self,
        resource: RenderedResource,
        *,
        owner_uid: str,
        model_fence: ModelWriteFence,
        fence: LeaseFence,
    ) -> None: ...

    async def delete_resource(
        self,
        identity: str,
        *,
        owner_uid: str,
        fence: LeaseFence,
    ) -> bool: ...

    async def set_finalizer(
        self,
        key: ModelKey,
        *,
        owner_uid: str,
        present: bool,
        fence: LeaseFence,
    ) -> None: ...

    async def patch_status(
        self,
        key: ModelKey,
        *,
        owner_uid: str,
        generation: int,
        status: dict[str, Any],
        fence: LeaseFence,
    ) -> bool: ...


@dataclass(frozen=True)
class ResourceEndpoint:
    api_version: str
    kind: str
    plural: str

    def collection(self, namespace: str) -> str:
        if self.api_version == "v1":
            return f"/api/v1/namespaces/{quote(namespace, safe='')}/{self.plural}"
        group, version = self.api_version.split("/", 1)
        return f"/apis/{group}/{version}/namespaces/{quote(namespace, safe='')}/{self.plural}"

    def item(self, namespace: str, name: str) -> str:
        return f"{self.collection(namespace)}/{quote(name, safe='')}"


RESOURCE_ENDPOINTS = {
    ("v1", "ConfigMap"): ResourceEndpoint("v1", "ConfigMap", "configmaps"),
    ("v1", "PersistentVolumeClaim"): ResourceEndpoint("v1", "PersistentVolumeClaim", "persistentvolumeclaims"),
    ("v1", "Service"): ResourceEndpoint("v1", "Service", "services"),
    ("v1", "ServiceAccount"): ResourceEndpoint("v1", "ServiceAccount", "serviceaccounts"),
    ("apps/v1", "DaemonSet"): ResourceEndpoint("apps/v1", "DaemonSet", "daemonsets"),
    ("apps/v1", "Deployment"): ResourceEndpoint("apps/v1", "Deployment", "deployments"),
    ("keda.sh/v1alpha1", "ScaledObject"): ResourceEndpoint("keda.sh/v1alpha1", "ScaledObject", "scaledobjects"),
    ("networking.k8s.io/v1", "NetworkPolicy"): ResourceEndpoint(
        "networking.k8s.io/v1", "NetworkPolicy", "networkpolicies"
    ),
}
HPA_ENDPOINT = ResourceEndpoint("autoscaling/v2", "HorizontalPodAutoscaler", "horizontalpodautoscalers")
POD_ENDPOINT = ResourceEndpoint("v1", "Pod", "pods")
MODEL_ENDPOINT = ResourceEndpoint(API_VERSION, KIND, "modeldeployments")
LEASE_ENDPOINT = ResourceEndpoint("coordination.k8s.io/v1", "Lease", "leases")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC)


def _metadata(body: Mapping[str, Any]) -> Mapping[str, Any]:
    value = body.get("metadata")
    if not isinstance(value, Mapping):
        raise ControllerError("Kubernetes object metadata is unavailable")
    return value


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _string_list(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _required_metadata(body: Mapping[str, Any], field: str) -> str:
    value = _metadata(body).get(field)
    if not isinstance(value, str) or not value:
        raise ControllerError(f"Kubernetes object metadata.{field} is unavailable")
    return value


def _model_write_fence(body: Mapping[str, Any], key: ModelKey) -> ModelWriteFence:
    metadata = _metadata(body)
    generation = metadata.get("generation")
    if (
        body.get("apiVersion") != API_VERSION
        or body.get("kind") != KIND
        or metadata.get("namespace") != key.namespace
        or metadata.get("name") != key.name
        or metadata.get("deletionTimestamp") is not None
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 1
    ):
        raise KubernetesConflictError("ModelDeployment write fence is invalid or deleting")
    spec = body.get("spec")
    if not isinstance(spec, Mapping):
        raise KubernetesConflictError("ModelDeployment write fence has no object spec")
    return ModelWriteFence(
        key=key,
        uid=_required_metadata(body, "uid"),
        resource_version=_required_metadata(body, "resourceVersion"),
        generation=generation,
        spec_digest=canonical_digest(spec),
    )


def _model_deletion_fence(body: Mapping[str, Any], key: ModelKey) -> ModelWriteFence:
    metadata = _metadata(body)
    generation = metadata.get("generation")
    spec = body.get("spec")
    if (
        body.get("apiVersion") != API_VERSION
        or body.get("kind") != KIND
        or metadata.get("namespace") != key.namespace
        or metadata.get("name") != key.name
        or not isinstance(metadata.get("deletionTimestamp"), str)
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 1
        or not isinstance(spec, Mapping)
    ):
        raise KubernetesConflictError("ModelDeployment deletion fence is invalid")
    return ModelWriteFence(
        key=key,
        uid=_required_metadata(body, "uid"),
        resource_version=_required_metadata(body, "resourceVersion"),
        generation=generation,
        spec_digest=canonical_digest(spec),
    )


def _controller_owner_uid(body: Mapping[str, Any]) -> str | None:
    owners = _metadata(body).get("ownerReferences", [])
    if not isinstance(owners, list):
        return None
    matches = [item.get("uid") for item in owners if isinstance(item, Mapping) and item.get("controller") is True]
    return matches[0] if len(matches) == 1 and isinstance(matches[0], str) else None


def _field_managers(body: Mapping[str, Any]) -> list[str]:
    fields = _metadata(body).get("managedFields", [])
    if not isinstance(fields, list):
        return []
    return sorted(
        {manager for item in fields if isinstance(item, Mapping) and isinstance((manager := item.get("manager")), str)}
    )


@dataclass(frozen=True)
class _ReplicaFieldOwner:
    manager: str
    subresource: str | None
    api_version: str


@dataclass(frozen=True)
class _ManagedFieldOwner:
    manager: str
    operation: str
    subresource: str | None
    api_version: str


def _replica_field_owners(body: Mapping[str, Any]) -> list[_ReplicaFieldOwner]:
    fields = _metadata(body).get("managedFields", [])
    if not isinstance(fields, list):
        return []
    owners: set[_ReplicaFieldOwner] = set()
    for item in fields:
        if not isinstance(item, Mapping):
            continue
        manager = item.get("manager")
        api_version = item.get("apiVersion")
        subresource = item.get("subresource")
        fields_v1 = item.get("fieldsV1")
        if (
            not isinstance(manager, str)
            or not manager
            or not isinstance(api_version, str)
            or not api_version
            or (subresource is not None and not isinstance(subresource, str))
            or not isinstance(fields_v1, Mapping)
        ):
            continue
        spec_fields = fields_v1.get("f:spec")
        if isinstance(spec_fields, Mapping) and "f:replicas" in spec_fields:
            owners.add(_ReplicaFieldOwner(manager=manager, subresource=subresource, api_version=api_version))
    return sorted(owners, key=lambda item: (item.manager, item.subresource or "", item.api_version))


def _replica_field_managers(body: Mapping[str, Any]) -> list[str]:
    return sorted({item.manager for item in _replica_field_owners(body)})


def _annotation_field_owners(body: Mapping[str, Any], annotation: str) -> list[_ManagedFieldOwner]:
    fields = _metadata(body).get("managedFields", [])
    if not isinstance(fields, list):
        return []
    owners: set[_ManagedFieldOwner] = set()
    for item in fields:
        if not isinstance(item, Mapping):
            continue
        manager = item.get("manager")
        operation = item.get("operation")
        api_version = item.get("apiVersion")
        subresource = item.get("subresource")
        fields_v1 = item.get("fieldsV1")
        metadata_fields = fields_v1.get("f:metadata") if isinstance(fields_v1, Mapping) else None
        annotation_fields = metadata_fields.get("f:annotations") if isinstance(metadata_fields, Mapping) else None
        if (
            isinstance(manager, str)
            and manager
            and isinstance(operation, str)
            and operation
            and isinstance(api_version, str)
            and api_version
            and (subresource is None or isinstance(subresource, str))
            and isinstance(annotation_fields, Mapping)
            and f"f:{annotation}" in annotation_fields
        ):
            owners.add(
                _ManagedFieldOwner(
                    manager=manager,
                    operation=operation,
                    subresource=subresource,
                    api_version=api_version,
                )
            )
    return sorted(owners, key=lambda item: (item.manager, item.operation, item.subresource or "", item.api_version))


def _scale_handoff_receipt(body: Mapping[str, Any]) -> ScaleHandoffReceipt | None:
    annotations = _metadata(body).get("annotations")
    value = annotations.get(SCALE_HANDOFF_RECEIPT_ANNOTATION) if isinstance(annotations, Mapping) else None
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    try:
        return ScaleHandoffReceipt.model_validate_json(value)
    except (ValidationError, ValueError):
        return None


def _controller_owned_scale_handoff_receipt(body: Mapping[str, Any]) -> ScaleHandoffReceipt | None:
    receipt = _scale_handoff_receipt(body)
    expected_owner = _ManagedFieldOwner(
        manager=SCALE_HANDOFF_RECEIPT_FIELD_MANAGER,
        operation="Apply",
        subresource=None,
        api_version="apps/v1",
    )
    owners = _annotation_field_owners(body, SCALE_HANDOFF_RECEIPT_ANNOTATION)
    return receipt if receipt is not None and owners == [expected_owner] else None


def _scale_initialization_receipt(body: Mapping[str, Any]) -> ScaleInitializationReceipt | None:
    annotations = _metadata(body).get("annotations")
    value = annotations.get(SCALE_INITIALIZATION_RECEIPT_ANNOTATION) if isinstance(annotations, Mapping) else None
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    try:
        return ScaleInitializationReceipt.model_validate_json(value)
    except (ValidationError, ValueError):
        return None


def _controller_owned_scale_initialization_receipt(
    body: Mapping[str, Any],
) -> ScaleInitializationReceipt | None:
    receipt = _scale_initialization_receipt(body)
    expected_owner = _ManagedFieldOwner(
        manager=SCALE_HANDOFF_RECEIPT_FIELD_MANAGER,
        operation="Apply",
        subresource=None,
        api_version="apps/v1",
    )
    owners = _annotation_field_owners(body, SCALE_INITIALIZATION_RECEIPT_ANNOTATION)
    return receipt if receipt is not None and owners == [expected_owner] else None


def _controller_owned_scale_authorization_receipt(body: Mapping[str, Any]) -> ScaleAuthorizationReceipt | None:
    handoff = _controller_owned_scale_handoff_receipt(body)
    initialization = _controller_owned_scale_initialization_receipt(body)
    if (handoff is None) == (initialization is None):
        return None
    return handoff or initialization


def _scale_gate_record(value: Any, target: ScaleGateTargetIdentity) -> ScaleGateRecord | None:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode()) > SCALE_GATE_RECORD_MAX_BYTES
    ):
        return None
    try:
        record = ScaleGateRecord.model_validate_json(value)
    except (ValidationError, ValueError):
        return None
    if len(record.authorization.encode()) > SCALE_GATE_AUTHORIZATION_MAX_BYTES:
        return None
    return record if record.target == target else None


def _scale_gate_tombstone(value: Any, target: ScaleGateTargetIdentity) -> ScaleGateTombstone | None:
    record = _scale_gate_record(value, target)
    if record is None:
        return None
    try:
        return ScaleGateTombstone.model_validate_json(record.authorization)
    except (ValidationError, ValueError):
        return None


def _scale_gate_authorization(
    value: Any,
    target: ScaleGateTargetIdentity,
) -> ScaleGateAuthorization | None:
    record = _scale_gate_record(value, target)
    if record is None:
        return None
    for receipt_type in (
        ScaleGateReleaseAuthorization,
        ScaleGateReleaseAuthorizationV2,
        ScaleHandoffReceipt,
        ScaleInitializationReceipt,
    ):
        try:
            return receipt_type.model_validate_json(record.authorization)
        except (ValidationError, ValueError):
            pass
    return None


def _scale_gate_target(resource: RenderedResource) -> ScaleGateTargetIdentity:
    return ScaleGateTargetIdentity(
        apiVersion=resource.api_version,
        kind=resource.kind,
        namespace=resource.namespace,
        name=resource.name,
    )


def _scale_gate_target_digest(target: ScaleGateTargetIdentity) -> str:
    return hashlib.sha256(target.model_dump_json(by_alias=True).encode()).hexdigest()


def _scale_gate_companion_keys(target: ScaleGateTargetIdentity) -> tuple[str, str]:
    digest = _scale_gate_target_digest(target)
    return (
        f"{SCALE_GATE_SCALED_OBJECT_PREFIX}{digest}",
        f"{SCALE_GATE_HPA_PREFIX}{digest}",
    )


def _scale_gate_target_key(target: ScaleGateTargetIdentity) -> str:
    return f"{SCALE_GATE_TARGET_PREFIX}{_scale_gate_target_digest(target)}"


def _scale_gate_predecessor_evidence_value(
    authorization: ScaleGateReleaseAuthorizationV2,
) -> str:
    """Return the single canonical byte representation retained for v2 evidence."""

    value = json.dumps(
        authorization.model_dump(mode="json", by_alias=True),
        # ASCII escaping gives even lone Unicode surrogates a stable byte
        # representation, so every string accepted by the bounded v2 schema
        # remains canonically serializable.
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(value.encode()) > SCALE_GATE_PREDECESSOR_EVIDENCE_MAX_BYTES:
        raise ControllerError("protocol-v2 predecessor evidence exceeds its Kubernetes object bound")
    return value


def _scale_gate_predecessor_evidence_digest(
    authorization: ScaleGateReleaseAuthorizationV2,
) -> str:
    value = _scale_gate_predecessor_evidence_value(authorization)
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _scale_gate_predecessor_evidence_key(digest: str) -> str:
    if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
        raise KubernetesConflictError("protocol-v2 predecessor evidence digest is invalid")
    return f"{SCALE_GATE_PREDECESSOR_EVIDENCE_PREFIX}{digest.removeprefix('sha256:')}"


def _scale_gate_predecessor_evidence_target_value(target: ScaleGateTargetIdentity) -> str:
    return json.dumps(
        target.model_dump(mode="json", by_alias=True),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _scale_gate_predecessor_evidence_config_map_name(
    target: ScaleGateTargetIdentity,
    digest: str,
) -> str:
    if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
        raise KubernetesConflictError("protocol-v2 predecessor evidence digest is invalid")
    return (
        f"{SCALE_GATE_PREDECESSOR_EVIDENCE_CONFIG_MAP_PREFIX}"
        f"{_scale_gate_target_digest(target)}-{digest.removeprefix('sha256:')}"
    )


def _is_scale_gate_predecessor_evidence_identity(
    api_version: str,
    kind: str,
    name: str,
) -> bool:
    """Identify evidence objects independently of mutable labels or owners."""

    return (
        api_version == "v1"
        and kind == "ConfigMap"
        and name.startswith(SCALE_GATE_PREDECESSOR_EVIDENCE_CONFIG_MAP_PREFIX)
    )


def _validated_desired_resources(render: RenderPlan) -> dict[str, RenderedResource]:
    """Validate every rendered identity before discovery performs any I/O."""

    desired: dict[str, RenderedResource] = {}
    for item in render.resources:
        if _is_scale_gate_predecessor_evidence_identity(
            item.api_version,
            item.kind,
            item.name,
        ):
            raise ControllerError("rendered identity collides with immutable scale-gate evidence")
        identity = f"{item.api_version}/{item.kind}/{item.namespace}/{item.name}"
        if identity in desired:
            raise ControllerError("render plan contains a duplicate resource identity")
        desired[identity] = item
    return desired


def _scale_gate_predecessor_evidence_config_map(
    target: ScaleGateTargetIdentity,
    authorization: ScaleGateReleaseAuthorizationV2,
) -> dict[str, Any]:
    """Build one immutable, content-addressed evidence shard.

    A shard contains one bounded predecessor only. Its deterministic name binds
    both target and content digests, so retaining arbitrarily many historical
    predecessors cannot consume the shared admission-gate ConfigMap's 1 MiB
    budget. Shards have no ownerReference and this controller has no delete path
    for them; crash-orphaned evidence remains safely discoverable.
    """

    digest = _scale_gate_predecessor_evidence_digest(authorization)
    body = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": _scale_gate_predecessor_evidence_config_map_name(target, digest),
            "namespace": target.namespace,
        },
        "immutable": True,
        "data": {
            SCALE_GATE_PREDECESSOR_EVIDENCE_AUTHORIZATION_KEY: _scale_gate_predecessor_evidence_value(
                authorization
            ),
            SCALE_GATE_PREDECESSOR_EVIDENCE_TARGET_KEY: _scale_gate_predecessor_evidence_target_value(target),
        },
    }
    serialized = json.dumps(body, ensure_ascii=True, separators=(",", ":")).encode()
    if len(serialized) >= KUBERNETES_CONFIG_MAP_MAX_BYTES:
        raise ControllerError("protocol-v2 predecessor evidence shard exceeds the Kubernetes ConfigMap bound")
    return body


def _scale_gate_predecessor_lineage(
    authorization: ScaleGateReleaseAuthorization,
    evidence: ScaleGateReleaseAuthorizationV2,
) -> None:
    """Prove that referenced v2 evidence belongs to this v3 state machine."""

    evidence_mutation = (
        evidence.expected_scaler_generation,
        evidence.mutation_token,
        evidence.mutation_operation,
    )
    has_any_evidence_mutation = any(item is not None for item in evidence_mutation)
    has_complete_evidence_mutation = all(item is not None for item in evidence_mutation)
    exact_preserved_mutation = (
        has_complete_evidence_mutation
        and authorization.mutation_model_generation == evidence.model_generation
        and authorization.mutation_model_spec_digest == evidence.model_spec_digest
        and authorization.expected_scaler_generation == evidence.expected_scaler_generation
        and authorization.mutation_token == evidence.mutation_token
        and authorization.mutation_operation == evidence.mutation_operation
    )
    if (
        authorization.predecessor_evidence_digest
        != _scale_gate_predecessor_evidence_digest(evidence)
        or evidence.deployment_uid != authorization.deployment_uid
        or evidence.model_uid != authorization.model_uid
        or evidence.model_generation > authorization.model_generation
        or evidence.scaler_api_version != authorization.scaler_api_version
        or evidence.scaler_kind != authorization.scaler_kind
        or evidence.scaler_namespace != authorization.scaler_namespace
        or evidence.scaler_name != authorization.scaler_name
        or evidence.model_generation == authorization.model_generation
        and (
            evidence.model_spec_digest != authorization.model_spec_digest
            or evidence.desired_scaler_digest != authorization.desired_scaler_digest
        )
        or has_any_evidence_mutation != has_complete_evidence_mutation
        or has_complete_evidence_mutation
        and authorization.mutation_model_generation == evidence.model_generation
        and not exact_preserved_mutation
    ):
        raise KubernetesConflictError("protocol-v2 predecessor evidence lineage changed")


def _scale_gate_predecessor_evidence_from_config_map(
    body: Mapping[str, Any],
    *,
    target: ScaleGateTargetIdentity,
    digest: str,
) -> ScaleGateReleaseAuthorizationV2:
    """Strictly validate an immutable shard and return its canonical evidence."""

    return _scale_gate_predecessor_evidence_snapshot_from_config_map(
        body,
        target=target,
        digest=digest,
    ).authorization


def _scale_gate_predecessor_evidence_snapshot_from_config_map(
    body: Mapping[str, Any],
    *,
    target: ScaleGateTargetIdentity,
    digest: str,
    expected_uid: str | None = None,
    expected_resource_version: str | None = None,
) -> ScaleGatePredecessorEvidenceSnapshot:
    """Validate evidence bytes and its immutable API-server object identity."""

    expected_name = _scale_gate_predecessor_evidence_config_map_name(target, digest)
    metadata = _metadata(body)
    data = body.get("data")
    uid = metadata.get("uid")
    resource_version = metadata.get("resourceVersion")
    if (
        body.get("apiVersion") != "v1"
        or body.get("kind") != "ConfigMap"
        or metadata.get("namespace") != target.namespace
        or metadata.get("name") != expected_name
        or not isinstance(uid, str)
        or not uid
        or len(uid) > 253
        or not isinstance(resource_version, str)
        or not resource_version
        or len(resource_version) > 128
        or expected_uid is not None
        and uid != expected_uid
        or expected_resource_version is not None
        and resource_version != expected_resource_version
        or metadata.get("deletionTimestamp") is not None
        or metadata.get("deletionGracePeriodSeconds") is not None
        or metadata.get("ownerReferences") is not None
        or metadata.get("finalizers") is not None
        or metadata.get("labels") is not None
        or metadata.get("annotations") is not None
        or metadata.get("generateName") not in (None, "")
        or body.get("immutable") is not True
        or body.get("binaryData") not in (None, {})
        or not isinstance(data, Mapping)
        or set(data) != {
            SCALE_GATE_PREDECESSOR_EVIDENCE_AUTHORIZATION_KEY,
            SCALE_GATE_PREDECESSOR_EVIDENCE_TARGET_KEY,
        }
        or data.get(SCALE_GATE_PREDECESSOR_EVIDENCE_TARGET_KEY)
        != _scale_gate_predecessor_evidence_target_value(target)
    ):
        raise KubernetesConflictError("protocol-v2 predecessor evidence shard is malformed or foreign")
    value = data.get(SCALE_GATE_PREDECESSOR_EVIDENCE_AUTHORIZATION_KEY)
    if not isinstance(value, str) or len(value.encode()) > SCALE_GATE_PREDECESSOR_EVIDENCE_MAX_BYTES:
        raise KubernetesConflictError("protocol-v2 predecessor evidence shard is absent or oversized")
    try:
        evidence = ScaleGateReleaseAuthorizationV2.model_validate_json(value)
    except (ValidationError, ValueError) as exc:
        raise KubernetesConflictError("protocol-v2 predecessor evidence shard schema is invalid") from exc
    if (
        _scale_gate_predecessor_evidence_value(evidence) != value
        or _scale_gate_predecessor_evidence_digest(evidence) != digest
    ):
        raise KubernetesConflictError("protocol-v2 predecessor evidence shard changed")
    return ScaleGatePredecessorEvidenceSnapshot(
        authorization=evidence,
        uid=uid,
        resource_version=resource_version,
    )


def _scale_gate_predecessor_evidence_entry(
    authorization: ScaleGateReleaseAuthorizationV2,
) -> tuple[str, str]:
    digest = _scale_gate_predecessor_evidence_digest(authorization)
    return _scale_gate_predecessor_evidence_key(digest), _scale_gate_predecessor_evidence_value(authorization)


def _scale_gate_predecessor_evidence_update(
    data: Mapping[str, Any],
    authorization: ScaleGateReleaseAuthorizationV2,
) -> tuple[str, str]:
    """Prepare an append-only evidence entry without overwriting retained bytes."""

    key, value = _scale_gate_predecessor_evidence_entry(authorization)
    if key in data and data.get(key) != value:
        raise KubernetesConflictError("protocol-v2 predecessor evidence slot changed")
    return key, value


def _scale_gate_predecessor_evidence(
    data: Mapping[str, Any],
    authorization: ScaleGateReleaseAuthorization,
) -> ScaleGateReleaseAuthorizationV2 | None:
    """Resolve and verify retained v2 bytes before trusting a v3 reference.

    A legacy embedded v3 value is accepted only long enough to be migrated by
    the enclosing ConfigMap CAS. Digest-bearing records fail closed if their
    separately retained canonical bytes are absent or changed.
    """

    embedded = authorization.predecessor_authorization
    digest = authorization.predecessor_evidence_digest
    if digest is None:
        return embedded
    value = data.get(_scale_gate_predecessor_evidence_key(digest))
    if not isinstance(value, str) or len(value.encode()) > SCALE_GATE_PREDECESSOR_EVIDENCE_MAX_BYTES:
        raise KubernetesConflictError("protocol-v2 predecessor evidence is absent or oversized")
    try:
        retained = ScaleGateReleaseAuthorizationV2.model_validate_json(value)
    except (ValidationError, ValueError) as exc:
        raise KubernetesConflictError("protocol-v2 predecessor evidence is malformed") from exc
    if (
        _scale_gate_predecessor_evidence_value(retained) != value
        or _scale_gate_predecessor_evidence_digest(retained) != digest
        or embedded is not None
        and embedded != retained
    ):
        raise KubernetesConflictError("protocol-v2 predecessor evidence digest or canonical bytes changed")
    return retained


def _normalized_scale_gate_predecessor_reference(
    authorization: ScaleGateReleaseAuthorization,
    evidence: ScaleGatePredecessorEvidenceSnapshot | None,
) -> ScaleGateReleaseAuthorization:
    """Replace a legacy inline predecessor with its bounded digest reference."""

    if evidence is None:
        return authorization
    digest = _scale_gate_predecessor_evidence_digest(evidence.authorization)
    if authorization.predecessor_evidence_digest not in (None, digest):
        raise KubernetesConflictError("protocol-v2 predecessor evidence reference changed")
    if authorization.predecessor_evidence_uid not in (None, evidence.uid):
        raise KubernetesConflictError("protocol-v2 predecessor evidence UID changed")
    if authorization.predecessor_evidence_resource_version not in (None, evidence.resource_version):
        raise KubernetesConflictError("protocol-v2 predecessor evidence resourceVersion changed")
    return authorization.model_copy(
        update={
            "predecessor_evidence_digest": digest,
            "predecessor_evidence_uid": evidence.uid,
            "predecessor_evidence_resource_version": evidence.resource_version,
            "predecessor_authorization": None,
        }
    )


def _scale_gate_evidence_reference(
    record: ScaleGateReleaseAuthorization | ScaleAuthorizationReceipt | ScaleGateTombstone,
    *,
    allow_incomplete_identity: bool = False,
) -> tuple[str, str | None, str | None] | None:
    """Return a complete content/object/RV identity tuple or fail closed."""

    digest = record.predecessor_evidence_digest
    uid = record.predecessor_evidence_uid
    resource_version = record.predecessor_evidence_resource_version
    if digest is None and uid is None and resource_version is None:
        return None
    if digest is None or (
        (uid is None or resource_version is None)
        and not allow_incomplete_identity
    ):
        raise KubernetesConflictError("protocol-v2 predecessor evidence reference is incomplete")
    return digest, uid, resource_version


def _scale_gate_successor_with_evidence(
    record: ScaleGateReleaseAuthorization | ScaleAuthorizationReceipt | ScaleGateTombstone,
    evidence: ScaleGatePredecessorEvidenceSnapshot | None,
) -> ScaleGateReleaseAuthorization | ScaleAuthorizationReceipt | ScaleGateTombstone:
    """Carry an immutable predecessor pointer through every successor kind."""

    if evidence is None:
        if _scale_gate_evidence_reference(record) is not None:
            raise KubernetesConflictError("successor evidence reference cannot be resolved")
        return record
    digest = _scale_gate_predecessor_evidence_digest(evidence.authorization)
    reference = _scale_gate_evidence_reference(
        record,
        # The caller supplied a freshly validated shard snapshot. This is the
        # one safe adoption point for rejected digest/UID-only predecessors;
        # the returned successor always persists the complete UID+RV tuple.
        allow_incomplete_identity=True,
    )
    if reference is not None and (
        reference[0] != digest
        or reference[1] not in (None, evidence.uid)
        or reference[2] not in (None, evidence.resource_version)
    ):
        raise KubernetesConflictError("successor evidence reference changed")
    if (
        record.deployment_uid != evidence.authorization.deployment_uid
        or record.model_uid != evidence.authorization.model_uid
        or isinstance(record, (ScaleHandoffReceipt, ScaleInitializationReceipt))
        and record.model_generation < evidence.authorization.model_generation
        or isinstance(record, ScaleHandoffReceipt)
        and (
            record.scaler.api_version != evidence.authorization.scaler_api_version
            or record.scaler.kind != evidence.authorization.scaler_kind
            or record.scaler.namespace != evidence.authorization.scaler_namespace
            or record.scaler.name != evidence.authorization.scaler_name
        )
    ):
        raise KubernetesConflictError("successor evidence lineage changed")
    updated = record.model_copy(
        update={
            "predecessor_evidence_digest": digest,
            "predecessor_evidence_uid": evidence.uid,
            "predecessor_evidence_resource_version": evidence.resource_version,
            **(
                {"predecessor_authorization": None}
                if isinstance(record, ScaleGateReleaseAuthorization)
                else {}
            ),
        }
    )
    if isinstance(updated, ScaleGateReleaseAuthorization):
        _scale_gate_predecessor_lineage(updated, evidence.authorization)
    return updated


def _scale_gate_receipt_matches(
    retained: ScaleGateAuthorization | ScaleGateTombstone | None,
    receipt: ScaleAuthorizationReceipt,
) -> bool:
    """Compare receipt semantics while permitting a gate-only lineage pointer."""

    if type(retained) is not type(receipt):
        return False
    assert isinstance(retained, (ScaleHandoffReceipt, ScaleInitializationReceipt))
    receipt_reference = _scale_gate_evidence_reference(receipt)
    retained_reference = _scale_gate_evidence_reference(retained)
    if receipt_reference is not None and receipt_reference != retained_reference:
        return False
    empty_reference = {
        "predecessor_evidence_digest": None,
        "predecessor_evidence_uid": None,
        "predecessor_evidence_resource_version": None,
    }
    return retained.model_copy(update=empty_reference) == receipt.model_copy(update=empty_reference)


def _encoded_scale_gate_value(target: ScaleGateTargetIdentity, authorization: ScaleGateAuthorization) -> str:
    authorization_value = authorization.annotation_value()
    if len(authorization_value.encode()) > SCALE_GATE_AUTHORIZATION_MAX_BYTES:
        raise ControllerError("scale gate authorization exceeds its Kubernetes object bound")
    value = ScaleGateRecord(
        version=1,
        target=target,
        authorization=authorization_value,
    ).value()
    if len(value.encode()) > SCALE_GATE_RECORD_MAX_BYTES:
        raise ControllerError("scale gate record exceeds its Kubernetes object bound")
    return value


def _encoded_scale_gate_tombstone_value(target: ScaleGateTargetIdentity, tombstone: ScaleGateTombstone) -> str:
    return ScaleGateRecord(
        version=1,
        target=target,
        authorization=tombstone.annotation_value(),
    ).value()


def _scale_gate_allowance_value(
    target: ScaleGateTargetIdentity,
    *,
    api_version: str,
    kind: str,
    namespace: str,
    name: str,
    owner_uid: str,
) -> str:
    return ScaleGateAllowance(
        version=1,
        target=target,
        targeter=ScaleGateTargeterIdentity(
            apiVersion=api_version,
            kind=kind,
            namespace=namespace,
            name=name,
            ownerUID=owner_uid,
        ),
    ).value()


def _scaler_managed_fields_digest(body: Mapping[str, Any]) -> str:
    """Fingerprint the controller's exact ScaledObject field ownership.

    Status writers may append their own managedFields entries and advance the
    object resourceVersion.  They must not own any part of ``spec``.  Hashing
    only the controller's complete, non-status entries therefore survives
    status-only churn without treating a foreign/co-owned spec as authorized.
    """

    fields = _metadata(body).get("managedFields")
    if not isinstance(fields, list):
        raise KubernetesConflictError("ScaledObject managedFields are unavailable")
    controller_entries: list[dict[str, Any]] = []
    spec_managers: set[str] = set()
    for item in fields:
        if not isinstance(item, Mapping):
            continue
        fields_v1 = item.get("fieldsV1")
        if not isinstance(fields_v1, Mapping):
            continue
        spec_fields = fields_v1.get("f:spec")
        manager = item.get("manager")
        if isinstance(spec_fields, Mapping) and spec_fields:
            if not isinstance(manager, str) or not manager:
                raise KubernetesConflictError("ScaledObject spec has an invalid field owner")
            spec_managers.add(manager)
        if (
            manager == FIELD_MANAGER
            and item.get("operation") in {"Apply", "Update"}
            and item.get("apiVersion") == "keda.sh/v1alpha1"
            and item.get("subresource") in (None, "")
            and item.get("fieldsType") == "FieldsV1"
        ):
            controller_entries.append(
                {
                    "manager": FIELD_MANAGER,
                    "operation": item.get("operation"),
                    "apiVersion": item.get("apiVersion"),
                    "fieldsType": "FieldsV1",
                    "fieldsV1": copy.deepcopy(dict(fields_v1)),
                }
            )
    if spec_managers != {FIELD_MANAGER} or not controller_entries:
        raise KubernetesConflictError("ScaledObject spec ownership is not exclusively controller-authored")
    controller_entries.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
    return canonical_digest(controller_entries)


def _scale_gate_scaler_checkpoint(snapshot: ResourceSnapshot) -> ScaleGateScalerCheckpoint:
    if (
        snapshot.observed.api_version != "keda.sh/v1alpha1"
        or snapshot.observed.kind != "ScaledObject"
        or snapshot.observed.deleting
        or snapshot.generation < 1
    ):
        raise KubernetesConflictError("ScaledObject checkpoint identity is invalid")
    return ScaleGateScalerCheckpoint(
        uid=snapshot.observed.uid,
        resourceVersion=snapshot.resource_version,
        generation=snapshot.generation,
        digest=snapshot.observed.digest,
        managedFieldsDigest=_scaler_managed_fields_digest(snapshot.raw),
    )


def _scale_gate_v2_checkpoint_with_managed_fields(
    checkpoint: ScaleGateScalerCheckpointV2 | None,
) -> ScaleGateScalerCheckpoint | None:
    """Promote v2 checkpoint evidence only when its ownership digest exists."""

    if checkpoint is None or checkpoint.managed_fields_digest is None:
        return None
    return ScaleGateScalerCheckpoint(
        uid=checkpoint.uid,
        resourceVersion=checkpoint.resource_version,
        generation=checkpoint.generation,
        digest=checkpoint.digest,
        managedFieldsDigest=checkpoint.managed_fields_digest,
    )


def _scale_gate_mutation_token(
    *,
    deployment_uid: str,
    model_fence: ModelWriteFence,
    scaler: RenderedResource,
    prior_scaler: ScaleGateScalerCheckpoint | None,
    expected_generation: int,
    operation: Literal["Apply", "Update"],
) -> str:
    """Deterministic durable nonce for exactly one fenced scaler successor."""

    return canonical_digest(
        {
            "deploymentUID": deployment_uid,
            "modelUID": model_fence.uid,
            "modelGeneration": model_fence.generation,
            "modelSpecDigest": model_fence.spec_digest,
            "scaler": {
                "apiVersion": scaler.api_version,
                "kind": scaler.kind,
                "namespace": scaler.namespace,
                "name": scaler.name,
                "desiredDigest": scaler.digest,
            },
            "priorScaler": prior_scaler.model_dump(mode="json", by_alias=True) if prior_scaler else None,
            "expectedScalerGeneration": expected_generation,
            "operation": operation,
        }
    )


def _expected_scaler_mutation_generation(body: Mapping[str, Any] | None, scaler: RenderedResource) -> int:
    """Return the exact generation expected from create or one desired update."""

    if body is None:
        return 1
    generation = _metadata(body).get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise KubernetesConflictError("ScaledObject generation is invalid")
    live_spec = body.get("spec")
    desired_spec = scaler.manifest.get("spec")
    if not isinstance(desired_spec, Mapping):
        raise ControllerError("rendered ScaledObject spec is invalid")
    projected_spec = _project_like(live_spec, desired_spec)
    return generation if canonical_digest(projected_spec) == canonical_digest(desired_spec) else generation + 1


_FIELD_MANAGER_CONFLICT_MESSAGE = re.compile(
    r'^conflict with "(?P<manager>[^"\r\n]{1,128})"'
    r'(?: with subresource "(?P<subresource>[^"\r\n]{1,64})")?'
    r" using (?P<api_version>[A-Za-z0-9.-]+(?:/[A-Za-z0-9.-]+)?): "
    r"(?P<field>\.[^\r\n]{1,512})$"
)


def _field_manager_conflicts(response: httpx.Response) -> tuple[KubernetesFieldConflict, ...]:
    """Extract only bounded structured SSA conflict metadata from a Status."""

    try:
        status = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return ()
    if (
        response.status_code != 409
        or not isinstance(status, Mapping)
        or status.get("apiVersion") != "v1"
        or status.get("kind") != "Status"
        or status.get("status") != "Failure"
        or status.get("reason") != "Conflict"
        or status.get("code") != 409
    ):
        return ()
    causes = _mapping(status.get("details")).get("causes")
    if not isinstance(causes, list) or not causes or len(causes) > 32:
        return ()
    conflicts: list[KubernetesFieldConflict] = []
    for cause in causes:
        if not isinstance(cause, Mapping) or cause.get("reason") != "FieldManagerConflict":
            return ()
        field = cause.get("field")
        message = cause.get("message")
        if not isinstance(field, str) or not isinstance(message, str):
            return ()
        match = _FIELD_MANAGER_CONFLICT_MESSAGE.fullmatch(message)
        if match is None or match.group("field") != field:
            return ()
        conflicts.append(
            KubernetesFieldConflict(
                manager=match.group("manager"),
                subresource=match.group("subresource"),
                api_version=match.group("api_version"),
                field=field,
            )
        )
    return tuple(conflicts)


def _nonnegative_status_int(status: Mapping[str, Any], field: str, *, zero_when_observed: bool) -> int | None:
    value = status.get(field)
    if isinstance(value, int) and value >= 0:
        return value
    return 0 if zero_when_observed else None


def _project_like(actual: object, desired: object) -> object:
    """Project API defaults/status away while retaining every desired field."""

    if isinstance(desired, Mapping):
        source = actual if isinstance(actual, Mapping) else {}
        return {key: _project_like(source.get(key), value) for key, value in desired.items()}
    if isinstance(desired, list):
        source_list = actual if isinstance(actual, list) else []
        projected = []
        for index, value in enumerate(desired):
            current = source_list[index] if index < len(source_list) else None
            projected.append(_project_like(current, value))
        return projected
    return actual


def _snapshot(body: dict[str, Any], desired: RenderedResource | None = None) -> ResourceSnapshot:
    metadata = _metadata(body)
    api_version = body.get("apiVersion")
    kind = body.get("kind")
    namespace = metadata.get("namespace")
    name = metadata.get("name")
    if not all(isinstance(value, str) and value for value in (api_version, kind, namespace, name)):
        raise ControllerError("discovered resource identity is incomplete")
    desired_digest = (
        canonical_digest(_project_like(body, desired.manifest))
        if desired is not None
        else canonical_digest(
            {
                "apiVersion": api_version,
                "kind": kind,
                "metadata": {
                    "namespace": namespace,
                    "name": name,
                    "uid": metadata.get("uid"),
                    "resourceVersion": metadata.get("resourceVersion"),
                },
            }
        )
    )
    status = _mapping(body.get("status"))
    spec = _mapping(body.get("spec"))
    observed_generation = status.get("observedGeneration")
    observed_generation = (
        observed_generation if isinstance(observed_generation, int) and observed_generation >= 0 else None
    )
    if kind == "DaemonSet":
        desired_replicas = _nonnegative_status_int(status, "desiredNumberScheduled", zero_when_observed=False)
        replicas = _nonnegative_status_int(status, "currentNumberScheduled", zero_when_observed=False)
        updated_replicas = _nonnegative_status_int(status, "updatedNumberScheduled", zero_when_observed=False)
        ready_replicas = _nonnegative_status_int(status, "numberReady", zero_when_observed=False)
        available_replicas = _nonnegative_status_int(status, "numberAvailable", zero_when_observed=False)
        unavailable_replicas = _nonnegative_status_int(
            status,
            "numberUnavailable",
            zero_when_observed=observed_generation is not None,
        )
    else:
        desired_replicas = spec.get("replicas")
        desired_replicas = desired_replicas if isinstance(desired_replicas, int) and desired_replicas >= 0 else None
        zero_when_observed = kind == "Deployment" and observed_generation is not None
        replicas = _nonnegative_status_int(status, "replicas", zero_when_observed=zero_when_observed)
        updated_replicas = _nonnegative_status_int(status, "updatedReplicas", zero_when_observed=zero_when_observed)
        ready_replicas = _nonnegative_status_int(status, "readyReplicas", zero_when_observed=zero_when_observed)
        available_replicas = _nonnegative_status_int(status, "availableReplicas", zero_when_observed=zero_when_observed)
        unavailable_replicas = _nonnegative_status_int(
            status, "unavailableReplicas", zero_when_observed=zero_when_observed
        )
    return ResourceSnapshot(
        observed=ObservedResource(
            api_version=str(api_version),
            kind=str(kind),
            namespace=str(namespace),
            name=str(name),
            uid=_required_metadata(body, "uid"),
            digest=desired_digest,
            controller_owner_uid=_controller_owner_uid(body),
            field_managers=_field_managers(body),
            deleting=isinstance(metadata.get("deletionTimestamp"), str),
        ),
        resource_version=_required_metadata(body, "resourceVersion"),
        generation=int(metadata.get("generation", 0)),
        observed_generation=observed_generation,
        desired_replicas=desired_replicas,
        replicas=replicas,
        updated_replicas=updated_replicas,
        ready_replicas=ready_replicas,
        available_replicas=available_replicas,
        unavailable_replicas=unavailable_replicas,
        replica_field_managers=_replica_field_managers(body),
        raw=body,
    )


def _pod_snapshot(body: Mapping[str, Any]) -> PodSnapshot:
    metadata = _metadata(body)
    name = _required_metadata(body, "name")
    uid = _required_metadata(body, "uid")
    resource_version = _required_metadata(body, "resourceVersion")
    status = _mapping(body.get("status"))
    conditions = status.get("conditions")
    conditions = conditions if isinstance(conditions, list) else []

    def condition_true(condition_type: str) -> bool:
        return any(
            isinstance(condition, Mapping)
            and condition.get("type") == condition_type
            and condition.get("status") == "True"
            for condition in conditions
        )

    container_statuses = status.get("containerStatuses")
    container_statuses = container_statuses if isinstance(container_statuses, list) else []
    containers_started = bool(container_statuses) and all(
        isinstance(item, Mapping)
        and isinstance(item.get("state"), Mapping)
        and isinstance(_mapping(item.get("state")).get("running"), Mapping)
        for item in container_statuses
    )
    phase = status.get("phase")
    return PodSnapshot(
        name=name,
        uid=uid,
        resource_version=resource_version,
        phase=phase if isinstance(phase, str) and phase else "Unknown",
        scheduled=condition_true("PodScheduled"),
        initialized=condition_true("Initialized"),
        containers_started=containers_started,
        ready=condition_true("Ready"),
        deleting=isinstance(metadata.get("deletionTimestamp"), str),
    )


class HttpKubernetesModelClient:
    """Small in-cluster REST adapter with an independent write kill switch."""

    def __init__(
        self,
        *,
        base_url: str,
        token_file: Path,
        ca_file: Path,
        writes_enabled: bool,
        timeout_seconds: float = 5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.token_file = token_file
        self.writes_enabled = writes_enabled
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
        token = self.token_file.read_text().strip()
        if len(token) < 16:
            raise ControllerError("projected Kubernetes token is unavailable")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if content_type is not None:
            headers["Content-Type"] = content_type
        return headers

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self.client.request(
                method, path, headers=self._headers(kwargs.pop("content_type", None)), **kwargs
            )
        except (OSError, httpx.HTTPError) as exc:
            raise ControllerError("Kubernetes API request failed") from exc
        if response.status_code == 409:
            raise KubernetesConflictError(
                "Kubernetes optimistic concurrency or field ownership conflict",
                field_conflicts=_field_manager_conflicts(response),
            )
        if response.status_code >= 400 and response.status_code != 404:
            raise ControllerError(f"Kubernetes API returned HTTP {response.status_code}")
        return response

    def _allow_write(self) -> None:
        if not self.writes_enabled:
            raise WriterDisabledError("model controller Kubernetes writes are disabled")

    async def acquire_or_renew_lease(
        self,
        *,
        namespace: str,
        name: str,
        holder_identity: str,
        token: str | None,
        duration_seconds: int,
    ) -> LeaseFence | None:
        # Lease writes are also disabled by the global kill switch.  An
        # observe-only process can poll objects without impersonating a leader.
        if not self.writes_enabled:
            return None
        now = _utc_now()
        path = LEASE_ENDPOINT.item(namespace, name)
        response = await self._request("GET", path)
        new_token = token or uuid4().hex
        if response.status_code == 404:
            body = {
                "apiVersion": LEASE_ENDPOINT.api_version,
                "kind": LEASE_ENDPOINT.kind,
                "metadata": {"name": name, "namespace": namespace, "annotations": {FENCE_ANNOTATION: new_token}},
                "spec": {
                    "holderIdentity": holder_identity,
                    "leaseDurationSeconds": duration_seconds,
                    "acquireTime": _timestamp(now),
                    "renewTime": _timestamp(now),
                    "leaseTransitions": 0,
                },
            }
            created = await self._request("POST", LEASE_ENDPOINT.collection(namespace), json=body)
            if created.status_code == 409:  # pragma: no cover - normalized above
                return None
            value = created.json()
        else:
            current = response.json()
            metadata = _metadata(current)
            spec = _mapping(current.get("spec"))
            annotations = _mapping(metadata.get("annotations"))
            current_holder = spec.get("holderIdentity")
            current_token = annotations.get(FENCE_ANNOTATION)
            renew_time = _parse_timestamp(spec.get("renewTime"))
            lease_duration = spec.get("leaseDurationSeconds")
            expires = (
                renew_time + timedelta(seconds=lease_duration)
                if renew_time is not None and isinstance(lease_duration, int)
                else now - timedelta(seconds=1)
            )
            ours = current_holder == holder_identity and token is not None and current_token == token
            if not ours and expires > now:
                return None
            if not ours:
                new_token = uuid4().hex
            transitions = int(spec.get("leaseTransitions", 0)) + (0 if ours else 1)
            patch = {
                "metadata": {
                    "resourceVersion": _required_metadata(current, "resourceVersion"),
                    "annotations": {FENCE_ANNOTATION: new_token},
                },
                "spec": {
                    "holderIdentity": holder_identity,
                    "leaseDurationSeconds": duration_seconds,
                    "acquireTime": spec.get("acquireTime") if ours else _timestamp(now),
                    "renewTime": _timestamp(now),
                    "leaseTransitions": transitions,
                },
            }
            updated = await self._request(
                "PATCH", path, content_type="application/merge-patch+json", content=json.dumps(patch).encode()
            )
            value = updated.json()
        return LeaseFence(
            namespace=namespace,
            name=name,
            holder_identity=holder_identity,
            token=new_token,
            resource_version=_required_metadata(value, "resourceVersion"),
            renew_time=now,
            duration_seconds=duration_seconds,
        )

    async def assert_fence(self, fence: LeaseFence) -> None:
        response = await self._request("GET", LEASE_ENDPOINT.item(fence.namespace, fence.name))
        if response.status_code == 404:
            raise FenceLostError("leader Lease disappeared")
        body = response.json()
        metadata = _metadata(body)
        annotations = _mapping(metadata.get("annotations"))
        spec = _mapping(body.get("spec"))
        renew_time = _parse_timestamp(spec.get("renewTime"))
        duration = spec.get("leaseDurationSeconds")
        if (
            spec.get("holderIdentity") != fence.holder_identity
            or annotations.get(FENCE_ANNOTATION) != fence.token
            or renew_time is None
            or not isinstance(duration, int)
            or renew_time + timedelta(seconds=duration) <= _utc_now()
        ):
            raise FenceLostError("leader Lease holder, epoch, or deadline changed")

    async def list_models(self, namespace: str) -> list[dict[str, Any]]:
        response = await self._request("GET", MODEL_ENDPOINT.collection(namespace), params={"limit": "500"})
        body = response.json()
        items = body.get("items")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ControllerError("ModelDeployment list response is invalid")
        return items

    async def get_model(self, key: ModelKey) -> dict[str, Any] | None:
        response = await self._request("GET", MODEL_ENDPOINT.item(key.namespace, key.name))
        return None if response.status_code == 404 else response.json()

    @staticmethod
    def _endpoint(api_version: str, kind: str) -> ResourceEndpoint:
        try:
            return RESOURCE_ENDPOINTS[(api_version, kind)]
        except KeyError as exc:
            raise ControllerError("rendered resource GVK is outside the writer allowlist") from exc

    async def _get_resource(self, api_version: str, kind: str, namespace: str, name: str) -> dict[str, Any] | None:
        endpoint = (
            HPA_ENDPOINT
            if (api_version, kind) == (HPA_ENDPOINT.api_version, HPA_ENDPOINT.kind)
            else self._endpoint(api_version, kind)
        )
        response = await self._request("GET", endpoint.item(namespace, name))
        return None if response.status_code == 404 else response.json()

    async def _list_all(self, endpoint: ResourceEndpoint, namespace: str) -> list[dict[str, Any]]:
        """Return a complete namespace list; a partial page can never prove absence."""

        items: list[dict[str, Any]] = []
        continuation: str | None = None
        seen: set[str] = set()
        for _ in range(64):
            params = {"limit": "500"}
            if continuation is not None:
                params["continue"] = continuation
            response = await self._request("GET", endpoint.collection(namespace), params=params)
            if response.status_code == 404 and endpoint == RESOURCE_ENDPOINTS[("keda.sh/v1alpha1", "ScaledObject")]:
                return []
            body = response.json()
            raw_items = body.get("items") if isinstance(body, Mapping) else None
            metadata = body.get("metadata") if isinstance(body, Mapping) else None
            if not isinstance(raw_items, list) or not all(isinstance(item, dict) for item in raw_items):
                raise ControllerError("Kubernetes collection response is invalid")
            items.extend(raw_items)
            next_token = metadata.get("continue") if isinstance(metadata, Mapping) else None
            if next_token in (None, ""):
                return items
            if not isinstance(next_token, str) or len(next_token) > 4096 or next_token in seen:
                raise ControllerError("Kubernetes collection continuation is invalid")
            seen.add(next_token)
            continuation = next_token
        raise ControllerError("Kubernetes collection exceeded the pagination bound")

    async def _targeting_autoscalers(self, resource: RenderedResource) -> list[dict[str, Any]]:
        """Return all targeters after complete namespace scans, including unlabeled objects."""

        if resource.api_version != "apps/v1" or resource.kind != "Deployment":
            raise ControllerError("autoscaler absence check target is not a Deployment")
        matches: list[dict[str, Any]] = []
        for endpoint in (HPA_ENDPOINT, RESOURCE_ENDPOINTS[("keda.sh/v1alpha1", "ScaledObject")]):
            for body in await self._list_all(endpoint, resource.namespace):
                target = _mapping(_mapping(body.get("spec")).get("scaleTargetRef"))
                api_version = target.get("apiVersion")
                kind = target.get("kind")
                if endpoint == RESOURCE_ENDPOINTS[("keda.sh/v1alpha1", "ScaledObject")]:
                    # KEDA defaults these optional fields for Deployment
                    # targets. Absence checks must interpret the same defaults
                    # or an unlabeled foreign ScaledObject containing only a
                    # target name could bypass the handoff fence.
                    api_version = api_version or "apps/v1"
                    kind = kind or "Deployment"
                if (
                    api_version == resource.api_version
                    and kind == resource.kind
                    and target.get("name") == resource.name
                ):
                    normalized = copy.deepcopy(body)
                    normalized.setdefault("apiVersion", endpoint.api_version)
                    normalized.setdefault("kind", endpoint.kind)
                    matches.append(normalized)
        return matches

    async def _targeting_autoscaler_exists(self, resource: RenderedResource) -> bool:
        return bool(await self._targeting_autoscalers(resource))

    async def _assert_no_targeting_autoscalers(self, resource: RenderedResource) -> None:
        """Fail closed on every namespaced HPA/ScaledObject target, labeled or not."""

        if await self._targeting_autoscaler_exists(resource):
            raise KubernetesConflictError("a live autoscaler still targets the fixed Deployment")

    @staticmethod
    def _validate_exact_autoscaler_targeters(
        matches: list[dict[str, Any]],
        *,
        scaler_name: str,
        scaler_uid: str | None,
        model_uid: str,
    ) -> None:
        expected_hpa_name = f"keda-hpa-{scaler_name}"
        seen: set[tuple[str, str]] = set()
        for body in matches:
            metadata = _metadata(body)
            kind = body.get("kind")
            name = metadata.get("name")
            identity = (str(kind), str(name))
            if identity in seen:
                raise KubernetesConflictError("duplicate autoscaler target identity was observed")
            seen.add(identity)
            if kind == "ScaledObject":
                if (
                    name != scaler_name
                    or _controller_owner_uid(body) != model_uid
                    or (scaler_uid is not None and metadata.get("uid") != scaler_uid)
                    or metadata.get("deletionTimestamp") is not None
                ):
                    raise KubernetesConflictError("an unexpected ScaledObject targets the Deployment")
            elif kind == HPA_ENDPOINT.kind:
                if (
                    scaler_uid is None
                    or name != expected_hpa_name
                    or _controller_owner_uid(body) != scaler_uid
                    or metadata.get("deletionTimestamp") is not None
                ):
                    raise KubernetesConflictError("an unexpected HPA targets the Deployment")
            else:  # pragma: no cover - only the two scanned endpoints can reach this branch
                raise KubernetesConflictError("an unexpected autoscaler kind targets the Deployment")

    async def _assert_exact_autoscaler_targeters(
        self,
        resource: RenderedResource,
        *,
        scaler_name: str,
        scaler_uid: str | None,
        model_uid: str,
    ) -> list[dict[str, Any]]:
        """Allow only the rendered KEDA object and its exact UID-owned HPA."""

        matches = await self._targeting_autoscalers(resource)
        self._validate_exact_autoscaler_targeters(
            matches,
            scaler_name=scaler_name,
            scaler_uid=scaler_uid,
            model_uid=model_uid,
        )
        return matches

    async def discover(self, *, key: ModelKey, owner_uid: str, render: RenderPlan) -> Discovery:
        # Security-reserved identities are rejected before the first LIST,
        # exact GET, HPA re-add lookup, or Pod inventory request.
        desired = _validated_desired_resources(render)
        bodies: dict[str, dict[str, Any]] = {}
        selector = f"{MODEL_DEPLOYMENT_LABEL}={bounded_label_value(key.name)}"
        for endpoint in RESOURCE_ENDPOINTS.values():
            response = await self._request(
                "GET", endpoint.collection(key.namespace), params={"labelSelector": selector}
            )
            raw = response.json().get("items")
            if not isinstance(raw, list):
                raise ControllerError("owned-resource list response is invalid")
            for body in raw:
                if not isinstance(body, dict):
                    raise ControllerError("owned-resource list item is invalid")
                normalized = dict(body)
                for field, expected in (
                    ("apiVersion", endpoint.api_version),
                    ("kind", endpoint.kind),
                ):
                    actual = body.get(field)
                    if actual in (None, ""):
                        normalized[field] = expected
                metadata = _metadata(normalized)
                name = metadata.get("name")
                if (
                    isinstance(name, str)
                    and _is_scale_gate_predecessor_evidence_identity(
                        endpoint.api_version,
                        endpoint.kind,
                        name,
                    )
                ):
                    # Evidence is security-owned append-only state, never a
                    # rendered workload resource. Ignore even a deliberately
                    # injected discovery label/owner so no repair plan can
                    # route it into the generic deletion path.
                    continue
                identity = (
                    f"{normalized['apiVersion']}/{normalized['kind']}/"
                    f"{metadata.get('namespace')}/{name}"
                )
                bodies[identity] = normalized
        # Exact GETs detect a foreign collision even when it deliberately lacks
        # the controller's discovery label.
        for identity, item in desired.items():
            if identity in bodies:
                continue
            body = await self._get_resource(item.api_version, item.kind, item.namespace, item.name)
            if body is not None:
                bodies[identity] = body
        # KEDA creates each HPA as a child of its ScaledObject, not as a direct
        # child of ModelDeployment. Exact GETs for every desired or stale
        # scaler are required for safe multi-segment ownership handoff and
        # foreground cleanup during drain.
        hpa_identities: set[str] = set()
        scaler_names = sorted(
            {
                str(_metadata(body).get("name"))
                for body in bodies.values()
                if body.get("apiVersion") == "keda.sh/v1alpha1"
                and body.get("kind") == "ScaledObject"
                and isinstance(_metadata(body).get("name"), str)
            }
            | {
                item.name
                for item in render.resources
                if item.api_version == "keda.sh/v1alpha1" and item.kind == "ScaledObject"
            }
        )
        for scaler_name in scaler_names:
            hpa_name = f"keda-hpa-{scaler_name}"
            hpa_identity = f"{HPA_ENDPOINT.api_version}/{HPA_ENDPOINT.kind}/{key.namespace}/{hpa_name}"
            hpa = await self._get_resource(
                HPA_ENDPOINT.api_version,
                HPA_ENDPOINT.kind,
                key.namespace,
                hpa_name,
            )
            if hpa is not None:
                bodies[hpa_identity] = hpa
                hpa_identities.add(hpa_identity)
        pod_response = await self._request(
            "GET",
            POD_ENDPOINT.collection(key.namespace),
            params={"labelSelector": selector, "limit": "10000"},
        )
        raw_pods = pod_response.json().get("items")
        if not isinstance(raw_pods, list) or not all(isinstance(item, dict) for item in raw_pods):
            raise ControllerError("runtime Pod list response is invalid")
        snapshots = [_snapshot(body, desired.get(identity)) for identity, body in sorted(bodies.items())]
        if any(
            item.observed.controller_owner_uid not in {None, owner_uid}
            and item.observed.identity not in desired
            and item.observed.identity not in hpa_identities
            for item in snapshots
        ):
            raise ControllerError("label-selected inventory contains another controller owner")
        pods = sorted((_pod_snapshot(item) for item in raw_pods), key=lambda item: (item.name, item.uid))
        return Discovery(resources=snapshots, pods=pods, complete=True)

    async def apply_resource(
        self,
        resource: RenderedResource,
        *,
        owner_uid: str,
        fence: LeaseFence,
    ) -> ResourceSnapshot:
        self._allow_write()
        if _is_scale_gate_predecessor_evidence_identity(
            resource.api_version,
            resource.kind,
            resource.name,
        ):
            raise ControllerError("immutable scale-gate evidence is outside generic reconciliation")
        if resource.field_manager != FIELD_MANAGER or resource.force_conflicts:
            raise ControllerError("renderer requested an unsafe field-manager policy")
        if resource.api_version == "keda.sh/v1alpha1" and resource.kind == "ScaledObject":
            raise ControllerError("ScaledObject SSA requires a generation-exact scale gate authorization")
        manifest = copy.deepcopy(resource.manifest)
        spec = manifest.get("spec")
        if (
            resource.api_version == "apps/v1"
            and resource.kind == "Deployment"
            and isinstance(spec, Mapping)
            and "replicas" in spec
        ):
            raise ControllerError("generic Deployment SSA must never include spec.replicas")
        if _controller_owner_uid(manifest) != owner_uid:
            raise ControllerError("rendered resource is not controller-owned by the exact CR UID")
        endpoint = self._endpoint(resource.api_version, resource.kind)
        current = await self._get_resource(resource.api_version, resource.kind, resource.namespace, resource.name)
        if resource.api_version == "apps/v1" and resource.kind == "Deployment":
            if current is None and _mapping(manifest.get("spec")).get("paused") is not True:
                raise KubernetesConflictError(
                    "new Deployment replica initialization requires the fenced scale protocol"
                )
            owners = [] if current is None else _replica_field_owners(current)
            if current is not None and (not owners or any(owner.manager == FIELD_MANAGER for owner in owners)):
                raise KubernetesConflictError(
                    "generic Deployment replica ownership requires a manual protocol-v2 migration"
                )
        if current is not None:
            current_owner = _controller_owner_uid(current)
            if current_owner not in {None, owner_uid}:
                raise KubernetesConflictError("resource has a foreign controller owner")
            manifest.setdefault("metadata", {})["resourceVersion"] = _required_metadata(current, "resourceVersion")
        await self.assert_fence(fence)
        response = await self._request(
            "PATCH",
            endpoint.item(resource.namespace, resource.name),
            params={"fieldManager": FIELD_MANAGER, "force": "false", "fieldValidation": "Strict"},
            content_type="application/apply-patch+yaml",
            content=json.dumps(manifest, separators=(",", ":")).encode(),
        )
        applied = response.json()
        # Read-after-write is mandatory: an admission controller may have
        # mutated fields, and a successful HTTP response is not ownership proof.
        reread = await self._get_resource(resource.api_version, resource.kind, resource.namespace, resource.name)
        if reread is None or _required_metadata(reread, "uid") != _required_metadata(applied, "uid"):
            raise ControllerError("applied resource failed read-after-write UID verification")
        if _controller_owner_uid(reread) != owner_uid:
            raise KubernetesConflictError("applied resource did not retain the exact controller owner")
        return _snapshot(reread, resource)

    @staticmethod
    def _validate_scale_release_authorization(
        authorization: ScaleGateReleaseAuthorization,
        *,
        resource: RenderedResource,
        target: RenderedResource,
        owner_uid: str,
        model_fence: ModelWriteFence,
    ) -> None:
        target_ref = _mapping(_mapping(resource.manifest.get("spec")).get("scaleTargetRef"))
        if (
            resource.api_version != "keda.sh/v1alpha1"
            or resource.kind != "ScaledObject"
            or target.api_version != "apps/v1"
            or target.kind != "Deployment"
            or resource.namespace != target.namespace
            or target_ref.get("apiVersion") != target.api_version
            or target_ref.get("kind") != target.kind
            or target_ref.get("name") != target.name
            or _controller_owner_uid(resource.manifest) != owner_uid
            or authorization.deployment_uid == ""
            or authorization.model_uid != owner_uid
            or model_fence.uid != owner_uid
            or model_fence.key.namespace != resource.namespace
            or authorization.model_resource_version != model_fence.resource_version
            or authorization.model_generation != model_fence.generation
            or authorization.model_spec_digest != model_fence.spec_digest
            or authorization.scaler_api_version != resource.api_version
            or authorization.scaler_kind != resource.kind
            or authorization.scaler_namespace != resource.namespace
            or authorization.scaler_name != resource.name
            or authorization.desired_scaler_digest != resource.digest
            or authorization.phase == "prepared"
            and (
                authorization.expected_scaler_generation is None
                or authorization.mutation_token is None
                or authorization.mutation_operation is None
                or authorization.mutation_model_generation != model_fence.generation
                or authorization.mutation_model_spec_digest != model_fence.spec_digest
                or authorization.applied_scaler is not None
            )
        ):
            raise KubernetesConflictError("ScaledObject mutation authorization is stale or foreign")
        if authorization.phase == "prepared":
            assert authorization.expected_scaler_generation is not None
            assert authorization.mutation_operation is not None
            if authorization.mutation_token != _scale_gate_mutation_token(
                deployment_uid=authorization.deployment_uid,
                model_fence=model_fence,
                scaler=resource,
                prior_scaler=authorization.prior_scaler,
                expected_generation=authorization.expected_scaler_generation,
                operation=authorization.mutation_operation,
            ):
                raise KubernetesConflictError("ScaledObject mutation authorization token is invalid")

    @staticmethod
    def _validate_authorized_scaler_state(
        body: Mapping[str, Any],
        *,
        resource: RenderedResource,
        owner_uid: str,
        expected: ScaleGateScalerCheckpoint,
    ) -> ResourceSnapshot:
        snapshot = _snapshot(dict(body), resource)
        observed = _scale_gate_scaler_checkpoint(snapshot)
        if (
            snapshot.observed.identity != _rendered_identity(resource)
            or snapshot.observed.controller_owner_uid != owner_uid
            or snapshot.observed.deleting
            or FIELD_MANAGER not in snapshot.observed.field_managers
            or observed.uid != expected.uid
            or observed.generation != expected.generation
            or observed.digest != expected.digest
            or observed.resource_version != expected.resource_version
            or observed.managed_fields_digest != expected.managed_fields_digest
        ):
            raise KubernetesConflictError("ScaledObject changed outside its exact gate authorization")
        return snapshot

    @staticmethod
    def _validate_scaler_mutation_postcondition(
        body: Mapping[str, Any],
        *,
        resource: RenderedResource,
        owner_uid: str,
        authorization: ScaleGateReleaseAuthorization,
    ) -> tuple[ResourceSnapshot, ScaleGateScalerCheckpoint]:
        """Validate the exact successor written by one prepared gate record."""

        expected_generation = authorization.expected_scaler_generation
        mutation_token = authorization.mutation_token
        operation = authorization.mutation_operation
        if expected_generation is None or mutation_token is None or operation is None:
            raise KubernetesConflictError("ScaledObject mutation provenance is incomplete")
        snapshot = _snapshot(dict(body), resource)
        checkpoint = _scale_gate_scaler_checkpoint(snapshot)
        annotations = _mapping(_metadata(body).get("annotations"))
        annotation_owners = _annotation_field_owners(body, SCALE_GATE_MUTATION_ANNOTATION)
        expected_annotation_owner = _ManagedFieldOwner(
            manager=FIELD_MANAGER,
            operation=operation,
            subresource=None,
            api_version=resource.api_version,
        )
        if (
            snapshot.observed.identity != _rendered_identity(resource)
            or snapshot.observed.controller_owner_uid != owner_uid
            or snapshot.observed.deleting
            or snapshot.observed.digest != resource.digest
            or snapshot.generation != expected_generation
            or annotations.get(SCALE_GATE_MUTATION_ANNOTATION) != mutation_token
            or annotation_owners != [expected_annotation_owner]
            or authorization.prior_scaler is not None
            and snapshot.observed.uid != authorization.prior_scaler.uid
        ):
            raise KubernetesConflictError("ScaledObject is not the exact authorized mutation successor")
        return snapshot, checkpoint

    @staticmethod
    def _adopt_scale_release_authorization_v2(
        authorization: ScaleGateReleaseAuthorizationV2,
        *,
        scaler: RenderedResource,
        live_scaler: Mapping[str, Any] | None,
        scaler_snapshot: ResourceSnapshot | None,
        deployment_uid: str,
        owner_uid: str,
        model_fence: ModelWriteFence,
    ) -> ScaleGateReleaseAuthorization:
        """Upgrade immutable protocol-v2 evidence without inventing proof.

        Missing managed-fields evidence is adopted only from an exact live
        UID/generation/digest state with exclusive controller spec ownership.
        A prepared create that already produced an object remains unprovable
        because v2 recorded no successor UID, and therefore fails closed.
        """

        if (
            authorization.deployment_uid != deployment_uid
            or authorization.model_uid != owner_uid
            or authorization.model_generation > model_fence.generation
            or authorization.model_generation == model_fence.generation
            and authorization.model_spec_digest != model_fence.spec_digest
        ):
            raise KubernetesConflictError("protocol-v2 autoscaler authorization is stale or foreign")
        live_checkpoint = (
            _scale_gate_scaler_checkpoint(scaler_snapshot) if scaler_snapshot is not None else None
        )

        def checkpoint_matches(checkpoint: ScaleGateScalerCheckpointV2 | None) -> bool:
            return bool(
                checkpoint is not None
                and live_checkpoint is not None
                and live_checkpoint.uid == checkpoint.uid
                and live_checkpoint.generation == checkpoint.generation
                and live_checkpoint.digest == checkpoint.digest
                and (
                    checkpoint.managed_fields_digest is None
                    or live_checkpoint.managed_fields_digest == checkpoint.managed_fields_digest
                )
            )

        mutation_parts = (
            authorization.expected_scaler_generation,
            authorization.mutation_token,
            authorization.mutation_operation,
        )
        has_any_mutation_provenance = any(item is not None for item in mutation_parts)
        has_complete_mutation_provenance = all(item is not None for item in mutation_parts)
        if has_any_mutation_provenance != has_complete_mutation_provenance:
            raise KubernetesConflictError("protocol-v2 mutation provenance is incomplete")
        upgraded_prior = _scale_gate_v2_checkpoint_with_managed_fields(authorization.prior_scaler)
        upgraded_applied = _scale_gate_v2_checkpoint_with_managed_fields(authorization.applied_scaler)
        if authorization.prior_scaler is not None:
            if upgraded_prior is None and checkpoint_matches(authorization.prior_scaler):
                assert live_checkpoint is not None
                upgraded_prior = live_checkpoint
        if has_complete_mutation_provenance:
            if authorization.prior_scaler is not None and upgraded_prior is None:
                raise KubernetesConflictError("protocol-v2 mutation predecessor cannot be proven")
            assert authorization.expected_scaler_generation is not None
            assert authorization.mutation_operation is not None
            legacy_fence = ModelWriteFence(
                key=model_fence.key,
                uid=authorization.model_uid,
                resource_version=authorization.model_resource_version,
                generation=authorization.model_generation,
                spec_digest=authorization.model_spec_digest,
            )
            legacy_scaler = scaler.model_copy(update={"digest": authorization.desired_scaler_digest})
            if authorization.mutation_token != _scale_gate_mutation_token(
                deployment_uid=authorization.deployment_uid,
                model_fence=legacy_fence,
                scaler=legacy_scaler,
                prior_scaler=upgraded_prior,
                expected_generation=authorization.expected_scaler_generation,
                operation=authorization.mutation_operation,
            ):
                raise KubernetesConflictError("protocol-v2 mutation token is invalid")

        common = {
            "version": 3,
            "deploymentUID": authorization.deployment_uid,
            "modelUID": authorization.model_uid,
            "modelResourceVersion": authorization.model_resource_version,
            "modelGeneration": authorization.model_generation,
            "modelSpecDigest": authorization.model_spec_digest,
            "scalerAPIVersion": authorization.scaler_api_version,
            "scalerKind": authorization.scaler_kind,
            "scalerNamespace": authorization.scaler_namespace,
            "scalerName": authorization.scaler_name,
            "desiredScalerDigest": authorization.desired_scaler_digest,
            "predecessorEvidenceDigest": _scale_gate_predecessor_evidence_digest(authorization),
        }
        preserved_mutation = {}
        if has_complete_mutation_provenance:
            preserved_mutation = {
                "expectedScalerGeneration": authorization.expected_scaler_generation,
                "mutationToken": authorization.mutation_token,
                "mutationOperation": authorization.mutation_operation,
                "mutationModelGeneration": authorization.model_generation,
                "mutationModelSpecDigest": authorization.model_spec_digest,
            }
        if authorization.phase == "closed":
            if live_scaler is not None:
                raise KubernetesConflictError("protocol-v2 closed gate retained a live scaler")
            return ScaleGateReleaseAuthorization(
                **common,
                **preserved_mutation,
                priorScaler=upgraded_prior,
                appliedScaler=upgraded_applied,
                phase="closed",
            )

        if authorization.phase == "applied":
            if not checkpoint_matches(authorization.applied_scaler):
                raise KubernetesConflictError("protocol-v2 applied scaler checkpoint cannot be proven")
            assert live_checkpoint is not None
            return ScaleGateReleaseAuthorization(
                **common,
                **preserved_mutation,
                priorScaler=upgraded_prior,
                appliedScaler=live_checkpoint,
                phase="applied",
            )

        if authorization.applied_scaler is not None:
            raise KubernetesConflictError("protocol-v2 prepared gate contains an applied checkpoint")
        if live_scaler is None:
            if authorization.prior_scaler is not None:
                raise KubernetesConflictError("protocol-v2 prepared scaler predecessor disappeared")
            expected_generation = 1
            operation: Literal["Apply", "Update"] = "Update"
            prior = None
        elif checkpoint_matches(authorization.prior_scaler):
            assert live_checkpoint is not None
            expected_generation = _expected_scaler_mutation_generation(live_scaler, scaler)
            operation = "Apply"
            prior = live_checkpoint
        elif (
            authorization.prior_scaler is not None
            and live_checkpoint is not None
            and live_checkpoint.uid == authorization.prior_scaler.uid
            and live_checkpoint.digest == authorization.desired_scaler_digest
            and live_checkpoint.generation == authorization.prior_scaler.generation + 1
        ):
            # The original v2 prepared record plus one exact successor
            # generation is sufficient to adopt an interrupted old SSA write.
            # A toggle away/back necessarily advances generation again.
            return ScaleGateReleaseAuthorization(
                **common,
                **preserved_mutation,
                priorScaler=upgraded_prior,
                appliedScaler=live_checkpoint,
                phase="applied",
            )
        else:
            raise KubernetesConflictError("protocol-v2 prepared scaler state cannot be proven")

        current_common = common | {
            "modelResourceVersion": model_fence.resource_version,
            "modelGeneration": model_fence.generation,
            "modelSpecDigest": model_fence.spec_digest,
            "desiredScalerDigest": scaler.digest,
        }
        token = _scale_gate_mutation_token(
            deployment_uid=deployment_uid,
            model_fence=model_fence,
            scaler=scaler,
            prior_scaler=prior,
            expected_generation=expected_generation,
            operation=operation,
        )
        return ScaleGateReleaseAuthorization(
            **current_common,
            expectedScalerGeneration=expected_generation,
            mutationToken=token,
            mutationOperation=operation,
            mutationModelGeneration=model_fence.generation,
            mutationModelSpecDigest=model_fence.spec_digest,
            priorScaler=prior,
            phase="prepared",
        )

    async def apply_autoscaler_resource(
        self,
        resource: RenderedResource,
        *,
        target: RenderedResource,
        authorization: ScaleGateReleaseAuthorization,
        owner_uid: str,
        model_fence: ModelWriteFence,
        fence: LeaseFence,
    ) -> ResourceSnapshot:
        """Apply one ScaledObject through its exact prepared gate record.

        The prepared record names either absence or one UID/RV/generation/spec
        tuple.  A retry that observes the desired digest records completion
        without replaying SSA; any third state fails closed.  The exact CR and
        gate record are read immediately before the only mutating object apply.
        """

        self._allow_write()
        target_identity = _scale_gate_target(target)
        target_key = _scale_gate_target_key(target_identity)
        prepared_value = _encoded_scale_gate_value(target_identity, authorization)
        gate = await self._scale_gate_config_map(resource.namespace)
        retained_authorization, _ = await self._verified_scale_gate_authorization(gate, target_identity)
        if (
            _mapping(gate.get("data")).get(target_key) != prepared_value
            or retained_authorization != authorization
        ):
            raise KubernetesConflictError("ScaledObject mutation gate changed before apply")
        self._validate_scale_release_authorization(
            authorization,
            resource=resource,
            target=target,
            owner_uid=owner_uid,
            model_fence=model_fence,
        )

        deployment = await self._get_resource(target.api_version, target.kind, target.namespace, target.name)
        if deployment is None:
            raise KubernetesConflictError("ScaledObject target disappeared before apply")
        self._validate_fixed_scale_identity(
            deployment,
            resource=target,
            owner_uid=owner_uid,
            expected_uid=authorization.deployment_uid,
        )

        live = await self._get_resource(resource.api_version, resource.kind, resource.namespace, resource.name)
        desired_snapshot: ResourceSnapshot | None = None
        applied_checkpoint: ScaleGateScalerCheckpoint | None = None
        create_only = False
        if authorization.phase == "applied":
            if live is None or authorization.applied_scaler is None:
                raise KubernetesConflictError("applied ScaledObject gate has no exact live postcondition")
            desired_snapshot = self._validate_authorized_scaler_state(
                live,
                resource=resource,
                owner_uid=owner_uid,
                expected=authorization.applied_scaler,
            )
            if desired_snapshot.observed.digest != resource.digest:
                raise KubernetesConflictError("applied ScaledObject gate no longer matches desired state")
            mutation_parts = (
                authorization.expected_scaler_generation,
                authorization.mutation_token,
                authorization.mutation_operation,
                authorization.mutation_model_generation,
                authorization.mutation_model_spec_digest,
            )
            if any(item is not None for item in mutation_parts):
                if not all(item is not None for item in mutation_parts):
                    raise KubernetesConflictError("applied ScaledObject mutation provenance is incomplete")
                _, mutation_checkpoint = self._validate_scaler_mutation_postcondition(
                    live,
                    resource=resource,
                    owner_uid=owner_uid,
                    authorization=authorization,
                )
                if mutation_checkpoint != authorization.applied_scaler:
                    raise KubernetesConflictError("applied ScaledObject mutation provenance changed")
            return desired_snapshot

        if authorization.phase != "prepared":
            raise KubernetesConflictError("closed ScaledObject gate cannot authorize a mutation")

        if authorization.applied_scaler is not None:
            raise KubernetesConflictError("prepared ScaledObject gate contains an applied checkpoint")
        if live is None:
            if (
                authorization.prior_scaler is not None
                or authorization.mutation_operation != "Update"
                or authorization.expected_scaler_generation != 1
            ):
                raise KubernetesConflictError("authorized ScaledObject update target disappeared")
            manifest = copy.deepcopy(resource.manifest)
            create_only = True
        else:
            live_snapshot = _snapshot(live, resource)
            if live_snapshot.observed.digest == resource.digest:
                desired_snapshot, applied_checkpoint = self._validate_scaler_mutation_postcondition(
                    live,
                    resource=resource,
                    owner_uid=owner_uid,
                    authorization=authorization,
                )
                manifest = {}
            else:
                if authorization.prior_scaler is None or authorization.mutation_operation != "Apply":
                    raise KubernetesConflictError("authorized ScaledObject creation found an unexpected live object")
                self._validate_authorized_scaler_state(
                    live,
                    resource=resource,
                    owner_uid=owner_uid,
                    expected=authorization.prior_scaler,
                )
                if authorization.expected_scaler_generation != _expected_scaler_mutation_generation(live, resource):
                    raise KubernetesConflictError("authorized ScaledObject successor generation is invalid")
                manifest = copy.deepcopy(resource.manifest)
                manifest.setdefault("metadata", {})["resourceVersion"] = authorization.prior_scaler.resource_version

        if desired_snapshot is None:
            manifest_metadata = manifest.setdefault("metadata", {})
            manifest_annotations = manifest_metadata.setdefault("annotations", {})
            if not isinstance(manifest_annotations, dict) or authorization.mutation_token is None:
                raise KubernetesConflictError("ScaledObject mutation annotation cannot be established")
            manifest_annotations[SCALE_GATE_MUTATION_ANNOTATION] = authorization.mutation_token

        if desired_snapshot is None:
            await self.assert_fence(fence)
            model = await self.get_model(model_fence.key)
            if model is None:
                raise KubernetesConflictError("ModelDeployment disappeared before ScaledObject apply")
            self._validate_model_write_fence(model, model_fence)
            final_gate = await self._scale_gate_config_map(resource.namespace)
            final_authorization, _ = await self._verified_scale_gate_authorization(
                final_gate,
                target_identity,
            )
            if (
                _mapping(final_gate.get("data")).get(target_key) != prepared_value
                or final_authorization != authorization
            ):
                raise KubernetesConflictError("ScaledObject mutation gate changed at apply boundary")
            endpoint = RESOURCE_ENDPOINTS[(resource.api_version, resource.kind)]
            if create_only:
                # SSA PATCH is an upsert: an object created after the last GET
                # could be overwritten even though the gate authorized absence.
                # Collection POST is the Kubernetes create-only primitive and
                # atomically returns 409 when any same-name third state won.
                response = await self._request(
                    "POST",
                    endpoint.collection(resource.namespace),
                    params={"fieldManager": FIELD_MANAGER, "fieldValidation": "Strict"},
                    content_type="application/json",
                    content=json.dumps(manifest, separators=(",", ":")).encode(),
                )
            else:
                response = await self._request(
                    "PATCH",
                    endpoint.item(resource.namespace, resource.name),
                    params={"fieldManager": FIELD_MANAGER, "force": "false", "fieldValidation": "Strict"},
                    content_type="application/apply-patch+yaml",
                    content=json.dumps(manifest, separators=(",", ":")).encode(),
                )
            applied = response.json()
            applied_snapshot, applied_checkpoint = self._validate_scaler_mutation_postcondition(
                applied,
                resource=resource,
                owner_uid=owner_uid,
                authorization=authorization,
            )
            live = await self._get_resource(resource.api_version, resource.kind, resource.namespace, resource.name)
            if live is None or _required_metadata(live, "uid") != applied_snapshot.observed.uid:
                raise ControllerError("ScaledObject apply failed read-after-write UID verification")
            desired_snapshot, reread_checkpoint = self._validate_scaler_mutation_postcondition(
                live,
                resource=resource,
                owner_uid=owner_uid,
                authorization=authorization,
            )
            if (
                reread_checkpoint.uid != applied_checkpoint.uid
                or reread_checkpoint.generation != applied_checkpoint.generation
                or reread_checkpoint.digest != applied_checkpoint.digest
                or reread_checkpoint.managed_fields_digest != applied_checkpoint.managed_fields_digest
            ):
                raise KubernetesConflictError("ScaledObject apply did not establish the exact desired postcondition")

        if applied_checkpoint is None:
            raise ControllerError("ScaledObject completion checkpoint is unavailable")
        completed = authorization.model_copy(
            update={
                "phase": "applied",
                # Bind the successful mutation response RV and controller
                # managedFields. A status-only reread RV may advance, but its
                # UID/generation/digest/ownership fingerprint was checked
                # above and is refreshed by release_scale_gate on retry.
                "applied_scaler": applied_checkpoint,
            }
        )
        await self.assert_fence(fence)
        model = await self.get_model(model_fence.key)
        if model is None:
            raise KubernetesConflictError("ModelDeployment disappeared before ScaledObject completion record")
        self._validate_model_write_fence(model, model_fence)
        completion_gate = await self._scale_gate_config_map(resource.namespace)
        completion_authorization, _ = await self._verified_scale_gate_authorization(
            completion_gate,
            target_identity,
        )
        if (
            _mapping(completion_gate.get("data")).get(target_key) != prepared_value
            or completion_authorization != authorization
        ):
            raise KubernetesConflictError("ScaledObject mutation gate changed before completion record")
        await self._patch_scale_gate_data(
            namespace=resource.namespace,
            config_map=completion_gate,
            data={target_key: _encoded_scale_gate_value(target_identity, completed)},
        )
        confirmed = await self._scale_gate_config_map(resource.namespace)
        confirmed_authorization, _ = await self._verified_scale_gate_authorization(
            confirmed,
            target_identity,
        )
        if (
            _mapping(confirmed.get("data")).get(target_key)
            != _encoded_scale_gate_value(target_identity, completed)
            or confirmed_authorization != completed
        ):
            raise KubernetesConflictError("ScaledObject completion record was not durable")
        return desired_snapshot

    async def initialize_deployment_scale(
        self,
        resource: RenderedResource,
        *,
        replicas: int,
        target_mode: Literal["fixed", "autoscaled"],
        owner_uid: str,
        model_fence: ModelWriteFence,
        fence: LeaseFence,
    ) -> ResourceSnapshot:
        """Create paused, acquire /scale ownership, then expose the Deployment.

        Kubernetes defaults a Deployment with no replica field to one replica.
        ``spec.paused`` is therefore the crash-safe creation barrier: no
        ReplicaSet is created until the exact fenced /scale initialization and
        its durable receipt have both been observed.
        """

        self._allow_write()
        if (
            resource.api_version != "apps/v1"
            or resource.kind != "Deployment"
            or resource.field_manager != FIELD_MANAGER
            or resource.force_conflicts
            or not isinstance(replicas, int)
            or isinstance(replicas, bool)
            or replicas < 0
            or model_fence.uid != owner_uid
            or model_fence.key.namespace != resource.namespace
            or _controller_owner_uid(resource.manifest) != owner_uid
        ):
            raise ControllerError("Deployment scale initialization preconditions are not satisfied")
        await self.assert_fence(fence)
        model = await self.get_model(model_fence.key)
        if model is None:
            raise KubernetesConflictError("ModelDeployment disappeared before Deployment initialization")
        self._validate_model_write_fence(model, model_fence)

        live = await self._get_resource(resource.api_version, resource.kind, resource.namespace, resource.name)
        if live is None:
            created = await self.apply_resource(
                _paused_deployment_without_replicas(resource),
                owner_uid=owner_uid,
                fence=fence,
            )
            live = created.raw
        self._validate_fixed_scale_identity(
            live,
            resource=resource,
            owner_uid=owner_uid,
            expected_uid=_required_metadata(live, "uid"),
        )
        live_spec = _mapping(live.get("spec"))
        retained = _controller_owned_scale_initialization_receipt(live)
        expected = ScaleInitializationReceipt(
            version=1,
            deploymentUID=_required_metadata(live, "uid"),
            modelUID=owner_uid,
            modelGeneration=model_fence.generation,
            modelSpecDigest=model_fence.spec_digest,
            desiredReplicas=replicas,
            targetMode=target_mode,
        )
        annotations = _mapping(_metadata(live).get("annotations"))
        if retained is None and SCALE_INITIALIZATION_RECEIPT_ANNOTATION in annotations:
            raise KubernetesConflictError("Deployment initialization receipt is malformed or foreign")
        if live_spec.get("paused") is not True:
            raise KubernetesConflictError("Deployment initialization receipt is missing, foreign, or too late")
        if retained is not None and retained != expected:
            if (
                retained.deployment_uid != expected.deployment_uid
                or retained.model_uid != expected.model_uid
                or SCALE_HANDOFF_RECEIPT_ANNOTATION in annotations
                or _replica_field_owners(live)
                not in (
                    [],
                    [
                        _ReplicaFieldOwner(
                            manager=FIXED_SCALE_FIELD_MANAGER,
                            subresource="scale",
                            api_version="apps/v1",
                        )
                    ],
                )
            ):
                raise KubernetesConflictError(
                    "paused Deployment initialization receipt cannot be rebound to the exact CR revision"
                )

        # Establish or atomically rebind the persistent admission gate before
        # changing the receipt. The Deployment remains paused throughout, so
        # every old->new checkpoint is no-Pod and a crash resumes safely.
        expected = await self._ensure_scale_gate(
            resource,
            receipt=expected,
            model_fence=model_fence,
            fence=fence,
        )
        await self._assert_no_targeting_autoscalers(resource)
        if retained != expected:
            await self.assert_fence(fence)
            model = await self.get_model(model_fence.key)
            if model is None:
                raise KubernetesConflictError("ModelDeployment disappeared before initialization receipt write")
            self._validate_model_write_fence(model, model_fence)
            await self._request(
                "PATCH",
                RESOURCE_ENDPOINTS[("apps/v1", "Deployment")].item(resource.namespace, resource.name),
                params={
                    "fieldManager": SCALE_HANDOFF_RECEIPT_FIELD_MANAGER,
                    "force": "false",
                    "fieldValidation": "Strict",
                },
                content_type="application/apply-patch+yaml",
                content=json.dumps(
                    {
                        "apiVersion": "apps/v1",
                        "kind": "Deployment",
                        "metadata": {
                            "name": resource.name,
                            "namespace": resource.namespace,
                            "resourceVersion": _required_metadata(live, "resourceVersion"),
                            "annotations": {SCALE_INITIALIZATION_RECEIPT_ANNOTATION: expected.annotation_value()},
                        },
                    },
                    separators=(",", ":"),
                ).encode(),
            )
            live = await self._get_resource(resource.api_version, resource.kind, resource.namespace, resource.name)
            if live is None:
                raise KubernetesConflictError("Deployment disappeared after initialization receipt write")
            retained = _controller_owned_scale_initialization_receipt(live)
        if retained != expected:
            raise KubernetesConflictError("Deployment initialization receipt does not match the exact CR revision")

        current = _snapshot(live, resource)
        await self._assert_no_targeting_autoscalers(resource)
        owners = _replica_field_owners(live)
        fixed_owner = _ReplicaFieldOwner(
            manager=FIXED_SCALE_FIELD_MANAGER,
            subresource="scale",
            api_version="apps/v1",
        )
        if owners != [fixed_owner]:
            # Kubernetes defaults replicas=1 on a first apply that omitted the
            # field. That virtual default has no managedFields entry, but a
            # scale-subresource apply reports the exact synthetic manager
            # ``before-first-apply`` in its 409 Status. Any visible owner here
            # is therefore foreign or a partial migration.
            if owners:
                raise KubernetesConflictError(
                    "new Deployment has unexpected initial replica ownership; no scale write was attempted"
                )
            if current.desired_replicas != 1:
                raise KubernetesConflictError(
                    "new Deployment does not have the exact API-server replica default; no scale write was attempted"
                )
            # A same-value SSA of the API-server default creates co-ownership
            # with before-first-apply even with force=true. For desired=1,
            # acquire exclusive ownership at a paused zero checkpoint, then
            # restore one through a normal non-forcing /scale apply. The
            # Deployment cannot create a ReplicaSet while paused, zero never
            # exceeds admitted capacity, and a crash leaves a safe durable
            # checkpoint that the exact receipt can resume.
            bootstrap_replicas = 0 if replicas == 1 else replicas
            scale = {
                "apiVersion": "autoscaling/v1",
                "kind": "Scale",
                "metadata": {
                    "name": resource.name,
                    "namespace": resource.namespace,
                    "uid": current.observed.uid,
                    "resourceVersion": current.resource_version,
                },
                "spec": {"replicas": bootstrap_replicas},
            }
            expected_conflict = KubernetesFieldConflict(
                manager="before-first-apply",
                subresource="scale",
                api_version="autoscaling/v1",
                field=".spec.replicas",
            )
            try:
                await self._request(
                    "PATCH",
                    f"{RESOURCE_ENDPOINTS[('apps/v1', 'Deployment')].item(resource.namespace, resource.name)}/scale",
                    params={
                        "fieldManager": FIXED_SCALE_FIELD_MANAGER,
                        "force": "false",
                        "fieldValidation": "Strict",
                        "dryRun": "All",
                    },
                    content_type="application/apply-patch+yaml",
                    content=json.dumps(scale, separators=(",", ":")).encode(),
                )
            except KubernetesConflictError as exc:
                if exc.field_conflicts != (expected_conflict,):
                    raise
            else:
                raise KubernetesConflictError(
                    "Deployment initialization did not prove the exact API-server default conflict"
                )
            await self.assert_fence(fence)
            model = await self.get_model(model_fence.key)
            if model is None:
                raise KubernetesConflictError("ModelDeployment disappeared before initial /scale write")
            self._validate_model_write_fence(model, model_fence)
            latest = await self._get_resource(resource.api_version, resource.kind, resource.namespace, resource.name)
            if latest is None or _controller_owned_scale_initialization_receipt(latest) != expected:
                raise KubernetesConflictError("Deployment initialization tuple changed before /scale write")
            latest_snapshot = _snapshot(latest, resource)
            if _replica_field_owners(latest):
                raise KubernetesConflictError("Deployment initial replica ownership changed before /scale write")
            current = await self._patch_prepared_controller_scale(
                resource,
                current=latest_snapshot,
                replicas=bootstrap_replicas,
                owner_uid=owner_uid,
                force=True,
            )
        else:
            if current.desired_replicas is None:
                raise KubernetesConflictError("paused Deployment replica checkpoint is unavailable")
            current = self._validate_controller_scale_owner(
                live,
                resource=resource,
                owner_uid=owner_uid,
                expected_uid=current.observed.uid,
                expected_replicas=current.desired_replicas,
                model_generation=model_fence.generation,
                receipt=expected,
            )
        if current.desired_replicas != replicas:
            current = await self.apply_controller_scale(
                resource,
                current=current,
                replicas=replicas,
                owner_uid=owner_uid,
                model_fence=model_fence,
                fence=fence,
            )
        await self._postcheck_exceptional_scale(
            resource,
            written=current,
            expected_replicas=replicas,
            owner_uid=owner_uid,
            model_generation=model_fence.generation,
            model_fence=model_fence,
            receipt=expected,
        )
        await self.assert_fence(fence)
        model = await self.get_model(model_fence.key)
        if model is None:
            raise KubernetesConflictError("ModelDeployment disappeared before Deployment unpause")
        self._validate_model_write_fence(model, model_fence)
        latest = await self._get_resource(resource.api_version, resource.kind, resource.namespace, resource.name)
        if latest is None or _mapping(latest.get("spec")).get("paused") is not True:
            raise KubernetesConflictError("Deployment initialization barrier changed before unpause")
        self._validate_controller_scale_owner(
            latest,
            resource=resource,
            owner_uid=owner_uid,
            expected_uid=current.observed.uid,
            expected_replicas=replicas,
            model_generation=model_fence.generation,
            receipt=expected,
        )
        initialized = await self.apply_resource(
            _without_deployment_replicas(resource),
            owner_uid=owner_uid,
            fence=fence,
        )
        return initialized

    async def recover_fixed_initialization(
        self,
        resource: RenderedResource,
        *,
        current: ResourceSnapshot,
        replicas: int,
        owner_uid: str,
        model_fence: ModelWriteFence,
        fence: LeaseFence,
    ) -> ResourceSnapshot:
        """Rebind a fresh zero autoscaled bootstrap that reversed to fixed.

        The Deployment is already unpaused, but no scaler was admitted and the
        dedicated scale owner still holds zero. The persistent gate is changed
        first, then the controller-owned annotation. Either checkpoint can be
        retried or safely reversed back to autoscaled mode.
        """

        self._allow_write()
        retained = _controller_owned_scale_initialization_receipt(current.raw)
        annotations = _mapping(_metadata(current.raw).get("annotations"))
        if (
            resource.api_version != "apps/v1"
            or resource.kind != "Deployment"
            or not isinstance(replicas, int)
            or isinstance(replicas, bool)
            or replicas < 0
            or current.observed.identity != _rendered_identity(resource)
            or current.observed.controller_owner_uid != owner_uid
            or current.observed.deleting
            or _mapping(current.raw.get("spec")).get("paused") is True
            or current.desired_replicas != 0
            or not _fixed_scale_manager_owns_replicas(current)
            or retained is None
            or retained.deployment_uid != current.observed.uid
            or retained.model_uid != owner_uid
            or SCALE_HANDOFF_RECEIPT_ANNOTATION in annotations
            or model_fence.uid != owner_uid
            or model_fence.key.namespace != resource.namespace
        ):
            raise KubernetesConflictError("fixed initialization reversal is not authorized")
        replacement = ScaleInitializationReceipt(
            version=1,
            deploymentUID=current.observed.uid,
            modelUID=owner_uid,
            modelGeneration=model_fence.generation,
            modelSpecDigest=model_fence.spec_digest,
            desiredReplicas=replicas,
            targetMode="fixed",
        )
        if retained.target_mode != "autoscaled" and not _scale_gate_receipt_matches(retained, replacement):
            raise KubernetesConflictError("fixed initialization reversal receipt is stale or foreign")
        target = _scale_gate_target(resource)
        target_key = _scale_gate_target_key(target)
        old_gate_value = self._scale_gate_value(resource, retained)
        await self._assert_no_targeting_autoscalers(resource)
        config_map = await self._scale_gate_config_map(resource.namespace)
        data = _mapping(config_map.get("data"))
        gate_authorization, predecessor_evidence = await self._verified_scale_gate_authorization(
            config_map,
            target,
            migrate_legacy=True,
            retain_v2=True,
            fence=fence,
        )
        replacement = _scale_gate_successor_with_evidence(replacement, predecessor_evidence)
        assert isinstance(replacement, ScaleInitializationReceipt)
        replacement_gate_value = self._scale_gate_value(resource, replacement)
        interrupted_release = (
            isinstance(gate_authorization, SCALE_GATE_RELEASE_AUTHORIZATION_TYPES)
            and gate_authorization.deployment_uid == current.observed.uid
            and gate_authorization.model_uid == owner_uid
            and gate_authorization.model_generation < model_fence.generation
        )
        if data.get(target_key) not in {old_gate_value, replacement_gate_value} and not interrupted_release:
            raise KubernetesConflictError("fixed initialization reversal gate is stale or foreign")
        if not interrupted_release and any(key in data for key in _scale_gate_companion_keys(target)):
            raise KubernetesConflictError("fixed initialization reversal gate is stale or foreign")
        if data.get(target_key) != replacement_gate_value:
            await self.assert_fence(fence)
            model = await self.get_model(model_fence.key)
            if model is None:
                raise KubernetesConflictError("ModelDeployment disappeared before reversal gate write")
            self._validate_model_write_fence(model, model_fence)
            await self._patch_scale_gate_data(
                namespace=resource.namespace,
                config_map=config_map,
                data=self._closed_scale_gate_data(resource, replacement_gate_value),
            )
        confirmed_gate = await self._scale_gate_config_map(resource.namespace)
        confirmed_data = _mapping(confirmed_gate.get("data"))
        confirmed_authorization, _ = await self._verified_scale_gate_authorization(
            confirmed_gate,
            target,
        )
        if confirmed_data.get(target_key) != replacement_gate_value or any(
            key in confirmed_data for key in _scale_gate_companion_keys(target)
        ) or confirmed_authorization != replacement:
            raise KubernetesConflictError("fixed initialization reversal gate was not durably closed")
        await self._assert_scale_gate_admission(resource)
        await self._assert_no_targeting_autoscalers(resource)
        latest = await self._get_resource(resource.api_version, resource.kind, resource.namespace, resource.name)
        if latest is None:
            raise KubernetesConflictError("Deployment disappeared during fixed initialization reversal")
        self._validate_fixed_scale_identity(
            latest,
            resource=resource,
            owner_uid=owner_uid,
            expected_uid=current.observed.uid,
        )
        latest_receipt = _controller_owned_scale_initialization_receipt(latest)
        if latest_receipt not in (retained, replacement) or _mapping(latest.get("spec")).get("paused") is True:
            raise KubernetesConflictError("fixed initialization reversal receipt changed")
        self._validate_controller_scale_owner(
            latest,
            resource=resource,
            owner_uid=owner_uid,
            expected_uid=current.observed.uid,
            expected_replicas=0,
            model_generation=model_fence.generation,
            receipt=latest_receipt,
        )
        if latest_receipt != replacement:
            await self.assert_fence(fence)
            model = await self.get_model(model_fence.key)
            if model is None:
                raise KubernetesConflictError("ModelDeployment disappeared before reversal receipt write")
            self._validate_model_write_fence(model, model_fence)
            await self._request(
                "PATCH",
                RESOURCE_ENDPOINTS[("apps/v1", "Deployment")].item(resource.namespace, resource.name),
                params={
                    "fieldManager": SCALE_HANDOFF_RECEIPT_FIELD_MANAGER,
                    "force": "false",
                    "fieldValidation": "Strict",
                },
                content_type="application/apply-patch+yaml",
                content=json.dumps(
                    {
                        "apiVersion": "apps/v1",
                        "kind": "Deployment",
                        "metadata": {
                            "name": resource.name,
                            "namespace": resource.namespace,
                            "resourceVersion": _required_metadata(latest, "resourceVersion"),
                            "annotations": {
                                SCALE_INITIALIZATION_RECEIPT_ANNOTATION: replacement.annotation_value(),
                            },
                        },
                    },
                    separators=(",", ":"),
                ).encode(),
            )
        reread = await self._get_resource(resource.api_version, resource.kind, resource.namespace, resource.name)
        if reread is None:
            raise KubernetesConflictError("Deployment disappeared after reversal receipt write")
        self._validate_controller_scale_owner(
            reread,
            resource=resource,
            owner_uid=owner_uid,
            expected_uid=current.observed.uid,
            expected_replicas=0,
            model_generation=model_fence.generation,
            receipt=replacement,
        )
        await self._assert_no_targeting_autoscalers(resource)
        return _snapshot(reread, resource)

    @staticmethod
    def _receipt_for(
        *,
        current: ResourceSnapshot,
        scaler: ResourceSnapshot,
        owner_uid: str,
        model_generation: int,
    ) -> ScaleHandoffReceipt:
        if (
            current.observed.api_version != "apps/v1"
            or current.observed.kind != "Deployment"
            or current.observed.controller_owner_uid != owner_uid
            or current.observed.deleting
            or scaler.observed.api_version != "keda.sh/v1alpha1"
            or scaler.observed.kind != "ScaledObject"
            or scaler.observed.namespace != current.observed.namespace
            or scaler.observed.controller_owner_uid != owner_uid
            or scaler.observed.deleting
            or scaler.generation < 1
            or model_generation < 1
        ):
            raise ControllerError("scale handoff receipt preconditions are not satisfied")
        target = _mapping(_mapping(scaler.raw.get("spec")).get("scaleTargetRef"))
        if (
            target.get("apiVersion") != current.observed.api_version
            or target.get("kind") != current.observed.kind
            or target.get("name") != current.observed.name
        ):
            raise ControllerError("scale handoff receipt scaler target is not the exact Deployment")
        return ScaleHandoffReceipt(
            version=1,
            deploymentUID=current.observed.uid,
            modelUID=owner_uid,
            modelGeneration=model_generation,
            scaler=ScaleHandoffScaler(
                apiVersion=scaler.observed.api_version,
                kind=scaler.observed.kind,
                namespace=scaler.observed.namespace,
                name=scaler.observed.name,
                uid=scaler.observed.uid,
                generation=scaler.generation,
            ),
        )

    async def record_scale_handoff_receipt(
        self,
        resource: RenderedResource,
        *,
        current: ResourceSnapshot,
        scaler: ResourceSnapshot,
        owner_uid: str,
        model_generation: int,
        model_fence: ModelWriteFence,
        fence: LeaseFence,
    ) -> ResourceSnapshot:
        """Persist exact transition evidence before deleting its ScaledObject."""

        self._allow_write()
        if resource.api_version != "apps/v1" or resource.kind != "Deployment":
            raise ControllerError("scale handoff receipt target is not a Deployment")
        expected = self._receipt_for(
            current=current,
            scaler=scaler,
            owner_uid=owner_uid,
            model_generation=model_generation,
        )
        if current.observed.identity != _rendered_identity(resource):
            raise ControllerError("scale handoff receipt target identity changed")
        annotations = _mapping(_metadata(current.raw).get("annotations"))
        retained = None
        if SCALE_HANDOFF_RECEIPT_ANNOTATION in annotations:
            retained = _controller_owned_scale_handoff_receipt(current.raw)
            if retained is None:
                raise KubernetesConflictError("Deployment has an invalid or foreign scale handoff receipt")

        live = await self._get_resource(resource.api_version, resource.kind, resource.namespace, resource.name)
        live_scaler = await self._get_resource(
            scaler.observed.api_version,
            scaler.observed.kind,
            scaler.observed.namespace,
            scaler.observed.name,
        )
        if live is None or live_scaler is None:
            raise KubernetesConflictError("scale handoff receipt objects disappeared")
        self._validate_fixed_scale_identity(
            live,
            resource=resource,
            owner_uid=owner_uid,
            expected_uid=current.observed.uid,
            resource_version=current.resource_version,
        )
        if (
            _required_metadata(live_scaler, "uid") != scaler.observed.uid
            or _required_metadata(live_scaler, "resourceVersion") != scaler.resource_version
            or _controller_owner_uid(live_scaler) != owner_uid
            or _metadata(live_scaler).get("deletionTimestamp") is not None
            or int(_metadata(live_scaler).get("generation", 0)) != scaler.generation
        ):
            raise KubernetesConflictError("scale handoff receipt scaler changed before recording")
        live_scaler_snapshot = _snapshot(live_scaler)
        if (
            self._receipt_for(
                current=_snapshot(live, resource),
                scaler=live_scaler_snapshot,
                owner_uid=owner_uid,
                model_generation=model_generation,
            )
            != expected
        ):
            raise KubernetesConflictError("scale handoff receipt evidence changed before recording")
        await self._assert_exact_autoscaler_targeters(
            resource,
            scaler_name=expected.scaler.name,
            scaler_uid=expected.scaler.uid,
            model_uid=owner_uid,
        )
        # Close every autoscaler allowance before the scaler can be deleted.
        # A crash before the annotation write leaves a conservative closed
        # gate; a retry can finish the same exact receipt transition.
        expected = await self._ensure_scale_gate(
            resource,
            receipt=expected,
            model_fence=model_fence,
            fence=fence,
        )
        if retained == expected:
            return _snapshot(live, resource)
        await self.assert_fence(fence)
        model = await self.get_model(model_fence.key)
        if model is None:
            raise KubernetesConflictError("ModelDeployment disappeared before handoff receipt write")
        self._validate_model_write_fence(model, model_fence)
        body = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": resource.name,
                "namespace": resource.namespace,
                "resourceVersion": current.resource_version,
                "annotations": {
                    SCALE_HANDOFF_RECEIPT_ANNOTATION: expected.annotation_value(),
                    SCALE_INITIALIZATION_RECEIPT_ANNOTATION: None,
                },
            },
        }
        await self._request(
            "PATCH",
            RESOURCE_ENDPOINTS[("apps/v1", "Deployment")].item(resource.namespace, resource.name),
            params={
                "fieldManager": SCALE_HANDOFF_RECEIPT_FIELD_MANAGER,
                "force": "false",
                "fieldValidation": "Strict",
            },
            content_type="application/apply-patch+yaml",
            content=json.dumps(body, separators=(",", ":")).encode(),
        )
        reread = await self._get_resource(resource.api_version, resource.kind, resource.namespace, resource.name)
        if reread is None:
            raise ControllerError("scale handoff receipt target disappeared after write")
        self._validate_fixed_scale_identity(
            reread,
            resource=resource,
            owner_uid=owner_uid,
            expected_uid=current.observed.uid,
        )
        if _controller_owned_scale_handoff_receipt(reread) != expected:
            raise ControllerError("scale handoff receipt was not durably controller-owned")
        if SCALE_INITIALIZATION_RECEIPT_ANNOTATION in _mapping(_metadata(reread).get("annotations")):
            raise ControllerError("superseded scale initialization receipt was not removed")
        return _snapshot(reread, resource)

    @staticmethod
    def _validate_fixed_scale_identity(
        body: Mapping[str, Any],
        *,
        resource: RenderedResource,
        owner_uid: str,
        expected_uid: str,
        resource_version: str | None = None,
    ) -> None:
        if (
            body.get("apiVersion") != "apps/v1"
            or body.get("kind") != "Deployment"
            or _required_metadata(body, "namespace") != resource.namespace
            or _required_metadata(body, "name") != resource.name
            or _required_metadata(body, "uid") != expected_uid
            or (resource_version is not None and _required_metadata(body, "resourceVersion") != resource_version)
            or _controller_owner_uid(body) != owner_uid
            or _metadata(body).get("deletionTimestamp") is not None
        ):
            raise KubernetesConflictError("fixed scale handoff target changed before ownership transfer")

    @staticmethod
    def _validate_model_write_fence(body: Mapping[str, Any], expected: ModelWriteFence) -> None:
        metadata = _metadata(body)
        spec = body.get("spec")
        if (
            body.get("apiVersion") != API_VERSION
            or body.get("kind") != KIND
            or metadata.get("namespace") != expected.key.namespace
            or metadata.get("name") != expected.key.name
            or metadata.get("uid") != expected.uid
            or metadata.get("resourceVersion") != expected.resource_version
            or metadata.get("generation") != expected.generation
            or metadata.get("deletionTimestamp") is not None
            or not isinstance(spec, Mapping)
            or canonical_digest(spec) != expected.spec_digest
        ):
            raise KubernetesConflictError("ModelDeployment changed before fixed scale ownership transfer")

    async def _assert_handoff_authorized(
        self,
        resource: RenderedResource,
        model_fence: ModelWriteFence,
    ) -> None:
        await self._assert_scale_gate_admission(resource)
        await self._assert_no_targeting_autoscalers(resource)
        model = await self.get_model(model_fence.key)
        if model is None:
            raise KubernetesConflictError("ModelDeployment disappeared before fixed scale ownership transfer")
        self._validate_model_write_fence(model, model_fence)

    @staticmethod
    def _validate_model_deletion_fence(body: Mapping[str, Any], expected: ModelWriteFence) -> None:
        metadata = _metadata(body)
        spec = body.get("spec")
        if (
            body.get("apiVersion") != API_VERSION
            or body.get("kind") != KIND
            or metadata.get("namespace") != expected.key.namespace
            or metadata.get("name") != expected.key.name
            or metadata.get("uid") != expected.uid
            or metadata.get("resourceVersion") != expected.resource_version
            or metadata.get("generation") != expected.generation
            or not isinstance(metadata.get("deletionTimestamp"), str)
            or not isinstance(spec, Mapping)
            or canonical_digest(spec) != expected.spec_digest
        ):
            raise KubernetesConflictError("ModelDeployment changed during scale gate tombstoning")

    async def prepare_scale_gate_deletion(
        self,
        resource: RenderedResource,
        *,
        current: ResourceSnapshot,
        owner_uid: str,
        model_fence: ModelWriteFence,
        fence: LeaseFence,
    ) -> None:
        """Close all scaler allowances and retain an exact deletion tombstone."""

        self._allow_write()
        receipt = _controller_owned_scale_authorization_receipt(current.raw)
        annotations = _mapping(_metadata(current.raw).get("annotations"))
        paused_without_receipt = (
            receipt is None
            and _mapping(current.raw.get("spec")).get("paused") is True
            and SCALE_HANDOFF_RECEIPT_ANNOTATION not in annotations
            and SCALE_INITIALIZATION_RECEIPT_ANNOTATION not in annotations
            and not _replica_field_owners(current.raw)
        )
        if (
            resource.api_version != "apps/v1"
            or resource.kind != "Deployment"
            or current.observed.identity != _rendered_identity(resource)
            or current.observed.controller_owner_uid != owner_uid
            or not (
                paused_without_receipt
                or receipt is not None
                and receipt.deployment_uid == current.observed.uid
                and receipt.model_uid == owner_uid
            )
            or model_fence.uid != owner_uid
            or model_fence.key.namespace != resource.namespace
        ):
            raise KubernetesConflictError("scale gate deletion tombstone is not authorized")
        tombstone: ScaleGateTombstone = ScaleGateTombstone(
            version=1,
            deploymentUID=current.observed.uid,
            modelUID=owner_uid,
        )
        config_map = await self._scale_gate_config_map(resource.namespace)
        data = _mapping(config_map.get("data"))
        target = _scale_gate_target(resource)
        target_key = _scale_gate_target_key(target)
        retained = data.get(target_key)
        retained_tombstone = _scale_gate_tombstone(retained, target)
        retained_authorization, predecessor_evidence = await self._verified_scale_gate_authorization(
            config_map,
            target,
            migrate_legacy=True,
            retain_v2=True,
            fence=fence,
        )
        if predecessor_evidence is not None and (
            predecessor_evidence.authorization.deployment_uid != current.observed.uid
            or predecessor_evidence.authorization.model_uid != owner_uid
        ):
            if not isinstance(retained_authorization, ScaleGateTombstone):
                raise KubernetesConflictError("scale gate deletion predecessor is foreign")
            predecessor_evidence = None
        tombstone = _scale_gate_successor_with_evidence(tombstone, predecessor_evidence)
        assert isinstance(tombstone, ScaleGateTombstone)
        tombstone_value = _encoded_scale_gate_tombstone_value(target, tombstone)
        if paused_without_receipt:
            allowed_retained = retained is None or retained_tombstone is not None
        else:
            assert receipt is not None
            allowed_retained = (
                retained in {self._scale_gate_value(resource, receipt), tombstone_value}
                or _scale_gate_receipt_matches(retained_authorization, receipt)
                or (
                    isinstance(retained_authorization, SCALE_GATE_RELEASE_AUTHORIZATION_TYPES)
                    and retained_authorization.deployment_uid == current.observed.uid
                    and retained_authorization.model_uid == owner_uid
                    and retained_authorization.model_generation <= model_fence.generation
                )
            )
        if not allowed_retained:
            raise KubernetesConflictError("scale gate changed before deletion tombstoning")
        if paused_without_receipt:
            await self._assert_no_targeting_autoscalers(resource)
        await self.assert_fence(fence)
        model = await self.get_model(model_fence.key)
        if model is None:
            raise KubernetesConflictError("ModelDeployment disappeared before deletion tombstoning")
        self._validate_model_deletion_fence(model, model_fence)
        expected_data = self._closed_scale_gate_data(resource, tombstone_value)
        if retained != tombstone_value or any(key in data for key in _scale_gate_companion_keys(target)):
            await self._patch_scale_gate_data(
                namespace=resource.namespace,
                config_map=config_map,
                data=expected_data,
            )
        confirmed = await self._scale_gate_config_map(resource.namespace)
        confirmed_data = _mapping(confirmed.get("data"))
        confirmed_tombstone, _ = await self._verified_scale_gate_authorization(
            confirmed,
            target,
        )
        if confirmed_data.get(target_key) != tombstone_value or any(
            key in confirmed_data for key in _scale_gate_companion_keys(target)
        ) or confirmed_tombstone != tombstone:
            raise KubernetesConflictError("scale gate deletion tombstone was not durably closed")
        await self._assert_scale_gate_admission(resource)

    async def confirm_scale_gate_tombstone(
        self,
        resource: RenderedResource,
        *,
        owner_uid: str,
        model_fence: ModelWriteFence,
        fence: LeaseFence,
    ) -> None:
        """Keep the tombstone after empty rediscovery; never open a name race."""

        self._allow_write()
        await self.assert_fence(fence)
        model = await self.get_model(model_fence.key)
        if model is None:
            raise KubernetesConflictError("ModelDeployment disappeared before tombstone confirmation")
        self._validate_model_deletion_fence(model, model_fence)
        if await self._get_resource(resource.api_version, resource.kind, resource.namespace, resource.name) is not None:
            raise KubernetesConflictError("Deployment still exists during tombstone confirmation")
        await self._assert_no_targeting_autoscalers(resource)
        config_map = await self._scale_gate_config_map(resource.namespace)
        data = _mapping(config_map.get("data"))
        target = _scale_gate_target(resource)
        tombstone, _ = await self._verified_scale_gate_authorization(config_map, target)
        if (
            not isinstance(tombstone, ScaleGateTombstone)
            or tombstone.model_uid != owner_uid
            or any(key in data for key in _scale_gate_companion_keys(target))
        ):
            raise KubernetesConflictError("closed scale gate tombstone is absent, stale, or foreign")
        await self._assert_scale_gate_admission(resource)

    @staticmethod
    def _scale_gate_value(resource: RenderedResource, receipt: ScaleAuthorizationReceipt) -> str:
        return _encoded_scale_gate_value(_scale_gate_target(resource), receipt)

    @staticmethod
    def _closed_scale_gate_data(resource: RenderedResource, value: str) -> dict[str, str | None]:
        target = _scale_gate_target(resource)
        return {_scale_gate_target_key(target): value, **dict.fromkeys(_scale_gate_companion_keys(target), None)}

    async def _patch_scale_gate_data(
        self,
        *,
        namespace: str,
        config_map: Mapping[str, Any],
        data: Mapping[str, str | None],
    ) -> None:
        await self._request(
            "PATCH",
            RESOURCE_ENDPOINTS[("v1", "ConfigMap")].item(namespace, SCALE_GATE_CONFIG_MAP),
            content_type="application/merge-patch+json",
            content=json.dumps(
                {
                    "metadata": {"resourceVersion": _required_metadata(config_map, "resourceVersion")},
                    "data": dict(data),
                },
                separators=(",", ":"),
            ).encode(),
        )

    async def _scale_gate_config_map(self, namespace: str) -> dict[str, Any]:
        config_map = await self._get_resource("v1", "ConfigMap", namespace, SCALE_GATE_CONFIG_MAP)
        if (
            config_map is None
            or config_map.get("apiVersion") != "v1"
            or config_map.get("kind") != "ConfigMap"
            or _required_metadata(config_map, "namespace") != namespace
            or _required_metadata(config_map, "name") != SCALE_GATE_CONFIG_MAP
            or _metadata(config_map).get("deletionTimestamp") is not None
            or not isinstance(config_map.get("data", {}), Mapping)
        ):
            raise KubernetesConflictError("external fixed-scale admission gate is unavailable")
        return config_map

    async def _scale_gate_predecessor_evidence_shard(
        self,
        target: ScaleGateTargetIdentity,
        digest: str,
    ) -> dict[str, Any] | None:
        name = _scale_gate_predecessor_evidence_config_map_name(target, digest)
        return await self._get_resource("v1", "ConfigMap", target.namespace, name)

    async def _ensure_scale_gate_predecessor_evidence_shard(
        self,
        target: ScaleGateTargetIdentity,
        evidence: ScaleGateReleaseAuthorizationV2,
        *,
        fence: LeaseFence,
    ) -> ScaleGatePredecessorEvidenceSnapshot:
        """Create one immutable content-addressed shard or verify its exact twin."""

        self._allow_write()
        digest = _scale_gate_predecessor_evidence_digest(evidence)
        body = _scale_gate_predecessor_evidence_config_map(target, evidence)
        retained = await self._scale_gate_predecessor_evidence_shard(target, digest)
        if retained is None:
            await self.assert_fence(fence)
            try:
                response = await self._request(
                    "POST",
                    RESOURCE_ENDPOINTS[("v1", "ConfigMap")].collection(target.namespace),
                    content_type="application/json",
                    content=json.dumps(body, separators=(",", ":")).encode(),
                )
                retained = response.json()
            except KubernetesConflictError:
                # Create-only is the CAS primitive. A concurrent writer may
                # win only by creating this deterministic name; prove its
                # immutable bytes instead of patching or replacing it.
                retained = await self._scale_gate_predecessor_evidence_shard(target, digest)
                if retained is None:
                    raise
        parsed = _scale_gate_predecessor_evidence_snapshot_from_config_map(
            retained,
            target=target,
            digest=digest,
        )
        if parsed.authorization != evidence:
            raise KubernetesConflictError("protocol-v2 predecessor evidence shard content changed")
        confirmed = await self._scale_gate_predecessor_evidence_shard(target, digest)
        if confirmed is None:
            raise KubernetesConflictError("protocol-v2 predecessor evidence shard disappeared")
        confirmed_evidence = _scale_gate_predecessor_evidence_snapshot_from_config_map(
            confirmed,
            target=target,
            digest=digest,
            expected_uid=parsed.uid,
            expected_resource_version=parsed.resource_version,
        )
        if confirmed_evidence.authorization != evidence:
            raise KubernetesConflictError("protocol-v2 predecessor evidence shard was not durable")
        return confirmed_evidence

    async def _verified_scale_gate_authorization(
        self,
        config_map: Mapping[str, Any],
        target: ScaleGateTargetIdentity,
        *,
        migrate_legacy: bool = False,
        retain_v2: bool = False,
        fence: LeaseFence | None = None,
    ) -> tuple[
        ScaleGateAuthorization | ScaleGateTombstone | None,
        ScaleGatePredecessorEvidenceSnapshot | None,
    ]:
        """Resolve one gate authorization and all referenced evidence.

        The authorization is parsed from exactly ``config_map``. Every v3
        predecessor digest is then resolved through its deterministic immutable
        shard and checked for canonical bytes, schema, digest, target binding,
        and state-machine lineage before the caller may authorize or advance.
        """

        data = _mapping(config_map.get("data"))
        value = data.get(_scale_gate_target_key(target))
        authorization = _scale_gate_authorization(value, target)
        if authorization is None:
            authorization = _scale_gate_tombstone(value, target)
        if isinstance(authorization, ScaleGateReleaseAuthorizationV2):
            evidence: ScaleGatePredecessorEvidenceSnapshot | None = None
            if retain_v2:
                if fence is None:
                    raise ControllerError("protocol-v2 predecessor retention requires a Lease fence")
                evidence = await self._ensure_scale_gate_predecessor_evidence_shard(
                    target,
                    authorization,
                    fence=fence,
                )
            return authorization, evidence
        if not isinstance(
            authorization,
            (ScaleGateReleaseAuthorization, ScaleHandoffReceipt, ScaleInitializationReceipt, ScaleGateTombstone),
        ):
            return authorization, None

        evidence = None
        digest = authorization.predecessor_evidence_digest
        if digest is not None:
            shard = await self._scale_gate_predecessor_evidence_shard(target, digest)
            if shard is None and migrate_legacy and isinstance(authorization, ScaleGateReleaseAuthorization):
                # Adopt the rejected d58 aggregate entry without removing it.
                legacy_evidence = _scale_gate_predecessor_evidence(data, authorization)
                if legacy_evidence is not None:
                    if fence is None:
                        raise ControllerError("legacy predecessor migration requires a Lease fence")
                    evidence = await self._ensure_scale_gate_predecessor_evidence_shard(
                        target,
                        legacy_evidence,
                        fence=fence,
                    )
                    normalized = _normalized_scale_gate_predecessor_reference(authorization, evidence)
                    _scale_gate_predecessor_lineage(normalized, evidence.authorization)
                    shard = await self._scale_gate_predecessor_evidence_shard(target, digest)
            if shard is None:
                raise KubernetesConflictError("protocol-v2 predecessor evidence shard is absent")
            reference = _scale_gate_evidence_reference(
                authorization,
                allow_incomplete_identity=migrate_legacy,
            )
            assert reference is not None
            evidence = _scale_gate_predecessor_evidence_snapshot_from_config_map(
                shard,
                target=target,
                digest=digest,
                expected_uid=reference[1],
                expected_resource_version=reference[2],
            )
            if (
                isinstance(authorization, ScaleGateReleaseAuthorization)
                and authorization.predecessor_authorization is not None
                and authorization.predecessor_authorization != evidence.authorization
            ):
                raise KubernetesConflictError("embedded protocol-v2 predecessor evidence changed")
            authorization = _scale_gate_successor_with_evidence(authorization, evidence)
        elif (
            isinstance(authorization, ScaleGateReleaseAuthorization)
            and authorization.predecessor_authorization is not None
        ):
            if not migrate_legacy:
                raise KubernetesConflictError("embedded protocol-v2 predecessor evidence is not durable")
            if fence is None:
                raise ControllerError("legacy predecessor migration requires a Lease fence")
            evidence = await self._ensure_scale_gate_predecessor_evidence_shard(
                target,
                authorization.predecessor_authorization,
                fence=fence,
            )
            authorization = _normalized_scale_gate_predecessor_reference(authorization, evidence)
            _scale_gate_predecessor_lineage(authorization, evidence.authorization)
        elif (
            authorization.predecessor_evidence_uid is not None
            or authorization.predecessor_evidence_resource_version is not None
        ):
            raise KubernetesConflictError("protocol-v2 predecessor evidence reference is incomplete")
        return authorization, evidence

    async def _assert_scale_gate_admission(self, resource: RenderedResource) -> None:
        """Prove that the API server, not this process, rejects a late scaler."""

        probe = {
            "apiVersion": "keda.sh/v1alpha1",
            "kind": "ScaledObject",
            "metadata": {
                "name": f"fs2-scale-gate-probe-{uuid4().hex}",
                "namespace": resource.namespace,
            },
            "spec": {
                "scaleTargetRef": {
                    "apiVersion": resource.api_version,
                    "kind": resource.kind,
                    "name": resource.name,
                },
                "triggers": [
                    {
                        "type": "prometheus",
                        "metadata": {
                            "serverAddress": "http://127.0.0.1:9090",
                            "metricName": "fs2_scale_gate_probe",
                            "threshold": "1",
                            "query": "vector(0)",
                        },
                    }
                ],
            },
        }
        try:
            response = await self.client.request(
                "POST",
                RESOURCE_ENDPOINTS[("keda.sh/v1alpha1", "ScaledObject")].collection(resource.namespace),
                headers=self._headers("application/json"),
                params={"dryRun": "All", "fieldValidation": "Strict"},
                content=json.dumps(probe, separators=(",", ":")).encode(),
            )
        except (OSError, httpx.HTTPError) as exc:
            raise ControllerError("Kubernetes admission gate probe failed") from exc
        try:
            status = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            status = {}
        message = status.get("message") if isinstance(status, Mapping) else None
        if response.status_code != 403 or not isinstance(message, str) or SCALE_GATE_DENIAL_MESSAGE not in message:
            raise KubernetesConflictError("external fixed-scale admission gate was not enforced")

    async def _ensure_scale_gate(
        self,
        resource: RenderedResource,
        *,
        receipt: ScaleAuthorizationReceipt,
        model_fence: ModelWriteFence,
        fence: LeaseFence,
    ) -> ScaleAuthorizationReceipt:
        """Durably lock this target before proving autoscaler absence."""

        await self.assert_fence(fence)
        model = await self.get_model(model_fence.key)
        if model is None:
            raise KubernetesConflictError("ModelDeployment disappeared before scale gate acquisition")
        self._validate_model_write_fence(model, model_fence)
        target = _scale_gate_target(resource)
        config_map = await self._scale_gate_config_map(resource.namespace)
        data = _mapping(config_map.get("data"))
        target_key = _scale_gate_target_key(target)
        retained = data.get(target_key)
        retained_authorization, predecessor_evidence = await self._verified_scale_gate_authorization(
            config_map,
            target,
            migrate_legacy=True,
            retain_v2=True,
            fence=fence,
        )
        if predecessor_evidence is not None and (
            predecessor_evidence.authorization.deployment_uid != receipt.deployment_uid
            or predecessor_evidence.authorization.model_uid != receipt.model_uid
        ):
            if not isinstance(retained_authorization, ScaleGateTombstone):
                raise KubernetesConflictError("fixed-scale admission gate predecessor is foreign")
            predecessor_evidence = None
        gate_receipt = _scale_gate_successor_with_evidence(receipt, predecessor_evidence)
        assert isinstance(gate_receipt, (ScaleHandoffReceipt, ScaleInitializationReceipt))
        expected = self._scale_gate_value(resource, gate_receipt)
        retained_exact_closure = (
            isinstance(retained_authorization, ScaleGateReleaseAuthorization)
            and retained_authorization.phase == "closed"
            and retained_authorization.deployment_uid == receipt.deployment_uid
            and retained_authorization.model_uid == receipt.model_uid
            and retained_authorization.model_resource_version == model_fence.resource_version
            and retained_authorization.model_generation == model_fence.generation
            and retained_authorization.model_spec_digest == model_fence.spec_digest
        )
        if retained_exact_closure:
            assert isinstance(retained_authorization, ScaleGateReleaseAuthorization)
            expected = _encoded_scale_gate_value(target, retained_authorization)
        replace = retained is None or retained_exact_closure and retained != expected
        if retained is not None and retained != expected:
            prior = retained_authorization
            tombstone = _scale_gate_tombstone(retained, target)
            same_deployment_transition = (
                prior is not None
                and prior.deployment_uid == receipt.deployment_uid
                and prior.model_uid == receipt.model_uid
            )
            same_name_recreation = (
                tombstone is not None
                and tombstone.model_uid != receipt.model_uid
                and tombstone.deployment_uid != receipt.deployment_uid
            )
            if not (same_deployment_transition or same_name_recreation):
                raise KubernetesConflictError("fixed-scale admission gate is owned by another transition")
            if not isinstance(receipt, ScaleHandoffReceipt):
                await self._assert_no_targeting_autoscalers(resource)
            replace = True
        closed_receipt = isinstance(receipt, ScaleHandoffReceipt) or (
            isinstance(receipt, ScaleInitializationReceipt) and receipt.target_mode == "fixed"
        )
        companions_present = any(key in data for key in _scale_gate_companion_keys(target))
        if replace or (closed_receipt and companions_present):
            await self.assert_fence(fence)
            model = await self.get_model(model_fence.key)
            if model is None:
                raise KubernetesConflictError("ModelDeployment disappeared before scale gate write")
            self._validate_model_write_fence(model, model_fence)
            await self._patch_scale_gate_data(
                namespace=resource.namespace,
                config_map=config_map,
                data=self._closed_scale_gate_data(resource, expected)
                if closed_receipt or replace
                else {target_key: expected},
            )
        confirmed = await self._scale_gate_config_map(resource.namespace)
        confirmed_data = _mapping(confirmed.get("data"))
        confirmed_authorization, _ = await self._verified_scale_gate_authorization(confirmed, target)
        if confirmed_data.get(target_key) != expected or (
            not retained_exact_closure and confirmed_authorization != gate_receipt
        ):
            raise KubernetesConflictError("fixed-scale admission gate did not persist exact transition evidence")
        if retained_exact_closure and confirmed_authorization != retained_authorization:
            raise KubernetesConflictError("fixed-scale admission gate closure evidence changed")
        if closed_receipt and any(key in confirmed_data for key in _scale_gate_companion_keys(target)):
            raise KubernetesConflictError("fixed-scale admission gate retained an autoscaler allowance")
        await self._assert_scale_gate_admission(resource)
        return gate_receipt

    async def _assert_scale_gate(
        self,
        resource: RenderedResource,
        receipt: ScaleAuthorizationReceipt,
    ) -> None:
        config_map = await self._scale_gate_config_map(resource.namespace)
        data = _mapping(config_map.get("data"))
        target = _scale_gate_target(resource)
        retained = data.get(_scale_gate_target_key(target))
        closure, _ = await self._verified_scale_gate_authorization(config_map, target)
        valid_closure = (
            isinstance(closure, ScaleGateReleaseAuthorization)
            and closure.phase == "closed"
            and closure.deployment_uid == receipt.deployment_uid
            and closure.model_uid == receipt.model_uid
            and (
                not isinstance(receipt, ScaleHandoffReceipt)
                or closure.scaler_api_version == receipt.scaler.api_version
                and closure.scaler_kind == receipt.scaler.kind
                and closure.scaler_namespace == receipt.scaler.namespace
                and closure.scaler_name == receipt.scaler.name
            )
            and (
                _scale_gate_evidence_reference(receipt) is None
                or _scale_gate_evidence_reference(receipt)
                == _scale_gate_evidence_reference(closure)
            )
        )
        if (
            retained != self._scale_gate_value(resource, receipt)
            and not _scale_gate_receipt_matches(closure, receipt)
            and not valid_closure
        ):
            raise KubernetesConflictError("fixed-scale admission gate is absent, stale, or foreign")
        if (
            isinstance(receipt, ScaleHandoffReceipt)
            or isinstance(receipt, ScaleInitializationReceipt)
            and receipt.target_mode == "fixed"
        ) and any(key in data for key in _scale_gate_companion_keys(target)):
            raise KubernetesConflictError("fixed-scale admission gate has an autoscaler allowance")
        await self._assert_scale_gate_admission(resource)

    async def _assert_superseded_handoff_closure(
        self,
        resource: RenderedResource,
        *,
        receipt: ScaleHandoffReceipt,
        model_fence: ModelWriteFence,
    ) -> None:
        """Require the exact current fixed closure before using an older receipt.

        The Deployment annotation remains the immutable identity of the scaler
        that handed off ownership. A later fixed generation may consume it only
        after ``fixed_scale_guard_clear`` has durably superseded every intervening
        autoscaler allowance. The old receipt is never copied back into the gate,
        so this recovery cannot reopen a closed targetRef window.
        """

        gate = await self._scale_gate_config_map(resource.namespace)
        data = _mapping(gate.get("data"))
        target = _scale_gate_target(resource)
        closure, _ = await self._verified_scale_gate_authorization(gate, target)
        if (
            not isinstance(closure, ScaleGateReleaseAuthorization)
            or closure.phase != "closed"
            or receipt.model_generation >= model_fence.generation
            or closure.deployment_uid != receipt.deployment_uid
            or closure.model_uid != receipt.model_uid
            or closure.model_resource_version != model_fence.resource_version
            or closure.model_generation != model_fence.generation
            or closure.model_spec_digest != model_fence.spec_digest
            or closure.scaler_api_version != receipt.scaler.api_version
            or closure.scaler_kind != receipt.scaler.kind
            or closure.scaler_namespace != receipt.scaler.namespace
            or closure.scaler_name != receipt.scaler.name
            or _scale_gate_evidence_reference(receipt) is not None
            and _scale_gate_evidence_reference(receipt) != _scale_gate_evidence_reference(closure)
            or any(key in data for key in _scale_gate_companion_keys(target))
        ):
            raise KubernetesConflictError("older scale handoff receipt lacks the exact current fixed closure")
        await self._assert_scale_gate_admission(resource)

    @classmethod
    def _validate_fixed_scale_owner(
        cls,
        body: Mapping[str, Any],
        *,
        resource: RenderedResource,
        owner_uid: str,
        expected_uid: str,
        expected_replicas: int,
        receipt: ScaleHandoffReceipt,
    ) -> tuple[_ReplicaFieldOwner, ResourceSnapshot]:
        cls._validate_fixed_scale_identity(
            body,
            resource=resource,
            owner_uid=owner_uid,
            expected_uid=expected_uid,
        )
        if (
            receipt.deployment_uid != expected_uid
            or receipt.model_uid != owner_uid
            or receipt.scaler.api_version != "keda.sh/v1alpha1"
            or receipt.scaler.kind != "ScaledObject"
            or receipt.scaler.namespace != resource.namespace
            or _controller_owned_scale_handoff_receipt(body) != receipt
        ):
            raise KubernetesConflictError("fixed scale handoff receipt is absent, stale, or foreign")
        snapshot = _snapshot(dict(body), resource)
        if snapshot.desired_replicas != expected_replicas:
            raise KubernetesConflictError("fixed scale handoff replica value changed before ownership transfer")
        owners = _replica_field_owners(body)
        stale = [
            owner
            for owner in owners
            if owner.manager in STALE_SCALE_FIELD_MANAGERS
            and owner.subresource == "scale"
            and owner.api_version == "apps/v1"
        ]
        if len(stale) != 1 or owners != stale:
            raise KubernetesConflictError(
                "Deployment replica ownership is not one canonical stale scale owner; manual migration is required"
            )
        return stale[0], snapshot

    @classmethod
    def _validate_controller_scale_owner(
        cls,
        body: Mapping[str, Any],
        *,
        resource: RenderedResource,
        owner_uid: str,
        expected_uid: str,
        expected_replicas: int,
        model_generation: int,
        receipt: ScaleAuthorizationReceipt,
    ) -> ResourceSnapshot:
        cls._validate_fixed_scale_identity(
            body,
            resource=resource,
            owner_uid=owner_uid,
            expected_uid=expected_uid,
        )
        valid_handoff = (
            isinstance(receipt, ScaleHandoffReceipt)
            and receipt.scaler.api_version == "keda.sh/v1alpha1"
            and receipt.scaler.kind == "ScaledObject"
            and receipt.scaler.namespace == resource.namespace
        )
        valid_initialization = isinstance(receipt, ScaleInitializationReceipt)
        if (
            receipt.deployment_uid != expected_uid
            or receipt.model_uid != owner_uid
            or receipt.model_generation > model_generation
            or not (valid_handoff or valid_initialization)
            or _controller_owned_scale_authorization_receipt(body) != receipt
        ):
            raise KubernetesConflictError("controller scale write lacks its exact durable transition receipt")
        snapshot = _snapshot(dict(body), resource)
        if snapshot.desired_replicas != expected_replicas:
            raise KubernetesConflictError("controller scale replica value changed before write")
        if _replica_field_owners(body) != [
            _ReplicaFieldOwner(
                manager=FIXED_SCALE_FIELD_MANAGER,
                subresource="scale",
                api_version="apps/v1",
            )
        ]:
            raise KubernetesConflictError("controller scale target has unexpected replica ownership")
        return snapshot

    @classmethod
    def _verified_fixed_scale_result(
        cls,
        body: Mapping[str, Any],
        *,
        resource: RenderedResource,
        owner_uid: str,
        expected_uid: str,
        desired_replicas: int,
        expected_manager: str,
        expected_subresource: str | None,
    ) -> ResourceSnapshot:
        cls._validate_fixed_scale_identity(
            body,
            resource=resource,
            owner_uid=owner_uid,
            expected_uid=expected_uid,
        )
        expected_owner = _ReplicaFieldOwner(
            manager=expected_manager,
            subresource=expected_subresource,
            api_version="apps/v1",
        )
        if _mapping(body.get("spec")).get("replicas") != desired_replicas or _replica_field_owners(body) != [
            expected_owner
        ]:
            raise ControllerError("fixed scale handoff did not establish exact exclusive replica ownership")
        return _snapshot(dict(body), resource)

    async def _prepare_controller_scale_write(
        self,
        resource: RenderedResource,
        *,
        current: ResourceSnapshot,
        expected_replicas: int,
        owner_uid: str,
        model_generation: int,
        model_fence: ModelWriteFence,
        receipt: ScaleAuthorizationReceipt,
        fence: LeaseFence,
        stale_owner: bool,
    ) -> tuple[ResourceSnapshot, _ReplicaFieldOwner | None]:
        """Return a fresh material tuple with the exact CR check last."""

        await self._ensure_scale_gate(
            resource,
            receipt=receipt,
            model_fence=model_fence,
            fence=fence,
        )
        await self._assert_no_targeting_autoscalers(resource)
        live = await self._get_resource(resource.api_version, resource.kind, resource.namespace, resource.name)
        if live is None:
            raise KubernetesConflictError("controller scale target disappeared before write")
        observed_stale: _ReplicaFieldOwner | None = None
        if stale_owner:
            if not isinstance(receipt, ScaleHandoffReceipt):
                raise KubernetesConflictError("stale scale takeover requires an exact KEDA handoff receipt")
            observed_stale, live_snapshot = self._validate_fixed_scale_owner(
                live,
                resource=resource,
                owner_uid=owner_uid,
                expected_uid=current.observed.uid,
                expected_replicas=expected_replicas,
                receipt=receipt,
            )
        else:
            live_snapshot = self._validate_controller_scale_owner(
                live,
                resource=resource,
                owner_uid=owner_uid,
                expected_uid=current.observed.uid,
                expected_replicas=expected_replicas,
                model_generation=model_generation,
                receipt=receipt,
            )
        await self.assert_fence(fence)
        model = await self.get_model(model_fence.key)
        if model is None:
            raise KubernetesConflictError("ModelDeployment disappeared before controller scale write")
        self._validate_model_write_fence(model, model_fence)
        return live_snapshot, observed_stale

    async def _patch_prepared_controller_scale(
        self,
        resource: RenderedResource,
        *,
        current: ResourceSnapshot,
        replicas: int,
        owner_uid: str,
        force: bool,
    ) -> ResourceSnapshot:
        if not isinstance(replicas, int) or isinstance(replicas, bool) or replicas < 0 or replicas > 2_147_483_647:
            raise ControllerError("controller scale write has an invalid replica value")
        scale = {
            "apiVersion": "autoscaling/v1",
            "kind": "Scale",
            "metadata": {
                "name": resource.name,
                "namespace": resource.namespace,
                "uid": current.observed.uid,
                "resourceVersion": current.resource_version,
            },
            "spec": {"replicas": replicas},
        }
        await self._request(
            "PATCH",
            f"{RESOURCE_ENDPOINTS[('apps/v1', 'Deployment')].item(resource.namespace, resource.name)}/scale",
            params={
                "fieldManager": FIXED_SCALE_FIELD_MANAGER,
                "force": "true" if force else "false",
                "fieldValidation": "Strict",
            },
            content_type="application/apply-patch+yaml",
            content=json.dumps(scale, separators=(",", ":")).encode(),
        )
        reread = await self._get_resource(resource.api_version, resource.kind, resource.namespace, resource.name)
        if reread is None:
            raise ControllerError("controller scale target disappeared after write")
        return self._verified_fixed_scale_result(
            reread,
            resource=resource,
            owner_uid=owner_uid,
            expected_uid=current.observed.uid,
            desired_replicas=replicas,
            expected_manager=FIXED_SCALE_FIELD_MANAGER,
            expected_subresource="scale",
        )

    async def _postcheck_exceptional_scale(
        self,
        resource: RenderedResource,
        *,
        written: ResourceSnapshot,
        expected_replicas: int,
        owner_uid: str,
        model_generation: int,
        model_fence: ModelWriteFence,
        receipt: ScaleAuthorizationReceipt,
    ) -> ResourceSnapshot:
        """Verify cross-object postconditions without making a stale compensating write.

        The exceptional request writes only the admitted desired replica count.
        If authorization changes afterwards, the next receipt-backed reconcile
        performs complete scaler scans and fails closed; it never restores a
        replica value authorized by an obsolete ModelDeployment revision.
        """

        await self._assert_handoff_authorized(resource, model_fence)
        reread = await self._get_resource(resource.api_version, resource.kind, resource.namespace, resource.name)
        if reread is None:
            raise KubernetesConflictError("fixed scale target disappeared after exceptional write")
        return self._validate_controller_scale_owner(
            reread,
            resource=resource,
            owner_uid=owner_uid,
            expected_uid=written.observed.uid,
            expected_replicas=expected_replicas,
            model_generation=model_generation,
            receipt=receipt,
        )

    async def _take_over_controller_scale(
        self,
        resource: RenderedResource,
        *,
        current: ResourceSnapshot,
        desired_replicas: int,
        owner_uid: str,
        model_generation: int,
        model_fence: ModelWriteFence,
        receipt: ScaleHandoffReceipt,
        fence: LeaseFence,
    ) -> ResourceSnapshot:
        """Take replicas through /scale without ever leaving the admitted value."""

        if current.desired_replicas is None:
            raise KubernetesConflictError("fixed scale handoff has no live replica value")
        prepared, _ = await self._prepare_controller_scale_write(
            resource,
            current=current,
            expected_replicas=current.desired_replicas,
            owner_uid=owner_uid,
            model_generation=model_generation,
            model_fence=model_fence,
            receipt=receipt,
            fence=fence,
            stale_owner=True,
        )
        if prepared.desired_replicas == desired_replicas:
            # SSA cannot evict a previous manager with a same-value apply. A
            # replica pulse would start a zero-replica workload, exceed a
            # fixed maximum, or violate pool capacity, and a crash can strand
            # that value. There is no safe automatic write for this state.
            raise KubernetesConflictError(
                "equal-value fixed scale ownership requires manual migration; no replica write was attempted"
            )
        final = await self._patch_prepared_controller_scale(
            resource,
            current=prepared,
            replicas=desired_replicas,
            owner_uid=owner_uid,
            force=True,
        )
        return await self._postcheck_exceptional_scale(
            resource,
            written=final,
            expected_replicas=desired_replicas,
            owner_uid=owner_uid,
            model_generation=model_generation,
            model_fence=model_fence,
            receipt=receipt,
        )

    @classmethod
    def _validate_post_delete_handoff_reversal(
        cls,
        body: Mapping[str, Any],
        *,
        resource: RenderedResource,
        scaler: RenderedResource,
        current: ResourceSnapshot,
        owner_uid: str,
        model_fence: ModelWriteFence,
        receipt: ScaleHandoffReceipt,
    ) -> ResourceSnapshot:
        """Authorize exact ScaledObject recreation without touching replicas.

        A fixed transition can durably close its gate and delete the
        ScaledObject before it takes ownership from the old scale manager. If
        the CR then reverses to autoscaled, the old scale owner is safe to
        retain: the recovery only admits the newly rendered ScaledObject and
        never writes ``/scale``. Every mutable identity/value is checked from
        the fresh Deployment read, while the newer exact CR is fenced by the
        caller immediately before the gate write.
        """

        cls._validate_fixed_scale_identity(
            body,
            resource=resource,
            owner_uid=owner_uid,
            expected_uid=current.observed.uid,
        )
        if (
            receipt.deployment_uid != current.observed.uid
            or receipt.model_uid != owner_uid
            or receipt.model_generation >= model_fence.generation
            or receipt.scaler.api_version != scaler.api_version
            or receipt.scaler.kind != scaler.kind
            or receipt.scaler.namespace != scaler.namespace
            or receipt.scaler.name != scaler.name
            or _controller_owned_scale_handoff_receipt(body) != receipt
        ):
            raise KubernetesConflictError("post-delete autoscaler reversal lacks exact handoff evidence")
        snapshot = _snapshot(dict(body), resource)
        if (
            current.desired_replicas is None
            or snapshot.desired_replicas != current.desired_replicas
            or _replica_field_owners(body) != _replica_field_owners(current.raw)
            or not _autoscaler_scale_manager_owns_replicas(snapshot)
        ):
            raise KubernetesConflictError(
                "post-delete autoscaler reversal lacks one unchanged canonical stale scale owner"
            )
        return snapshot

    async def release_scale_gate(
        self,
        resource: RenderedResource,
        *,
        scaler: RenderedResource,
        current: ResourceSnapshot,
        owner_uid: str,
        model_fence: ModelWriteFence,
        fence: LeaseFence,
    ) -> ScaleGateReleaseAuthorization:
        """Authorize one generation-exact ScaledObject transition.

        The durable primary record is independent of the older Deployment
        transition receipt.  It binds the open targeter allowances to the
        current CR and to either absence, one exact old scaler state, or one
        exact applied state.  A later fixed generation can therefore identify
        and close a superseded allowance without inventing a new Deployment
        receipt or touching replicas.
        """

        self._allow_write()
        receipt = _controller_owned_scale_authorization_receipt(current.raw)
        scaler_target = _mapping(_mapping(scaler.manifest.get("spec")).get("scaleTargetRef"))
        if (
            resource.api_version != "apps/v1"
            or resource.kind != "Deployment"
            or scaler.api_version != "keda.sh/v1alpha1"
            or scaler.kind != "ScaledObject"
            or scaler.namespace != resource.namespace
            or scaler_target.get("apiVersion") != resource.api_version
            or scaler_target.get("kind") != resource.kind
            or scaler_target.get("name") != resource.name
            or _controller_owner_uid(scaler.manifest) != owner_uid
            or current.observed.identity != _rendered_identity(resource)
            or current.observed.controller_owner_uid != owner_uid
            or current.observed.deleting
            or receipt is None
            or receipt.deployment_uid != current.observed.uid
            or receipt.model_uid != owner_uid
            or receipt.model_generation > model_fence.generation
            or isinstance(receipt, ScaleInitializationReceipt)
            and receipt.target_mode == "fixed"
            and receipt.model_generation >= model_fence.generation
            or model_fence.uid != owner_uid
            or model_fence.key.namespace != resource.namespace
        ):
            raise KubernetesConflictError("fixed-scale admission gate release is not authorized")
        target = _scale_gate_target(resource)
        retained_gate = await self._scale_gate_config_map(resource.namespace)
        retained_gate_data = _mapping(retained_gate.get("data"))
        retained_gate_value = retained_gate_data.get(_scale_gate_target_key(target))
        retained_authorization, predecessor_evidence = await self._verified_scale_gate_authorization(
            retained_gate,
            target,
            migrate_legacy=True,
            retain_v2=True,
            fence=fence,
        )
        source_retained_authorization = retained_authorization
        if (
            not isinstance(
                retained_authorization,
                (
                    ScaleGateReleaseAuthorization,
                    ScaleGateReleaseAuthorizationV2,
                    ScaleHandoffReceipt,
                    ScaleInitializationReceipt,
                ),
            )
            or retained_authorization.deployment_uid != current.observed.uid
            or retained_authorization.model_uid != owner_uid
            or retained_authorization.model_generation > model_fence.generation
        ):
            raise KubernetesConflictError("autoscaler admission gate is absent, stale, or foreign")
        if (
            isinstance(retained_authorization, ScaleInitializationReceipt)
            and retained_authorization.target_mode == "fixed"
            and retained_authorization.model_generation >= model_fence.generation
        ):
            raise KubernetesConflictError("autoscaler admission gate has no newer reversal generation")
        if isinstance(retained_authorization, ScaleHandoffReceipt) and (
            retained_authorization.scaler.api_version != scaler.api_version
            or retained_authorization.scaler.kind != scaler.kind
            or retained_authorization.scaler.namespace != scaler.namespace
            or retained_authorization.scaler.name != scaler.name
        ):
            raise KubernetesConflictError("autoscaler admission gate handoff identity changed")
        if isinstance(retained_authorization, SCALE_GATE_RELEASE_AUTHORIZATION_TYPES) and (
            retained_authorization.scaler_api_version != scaler.api_version
            or retained_authorization.scaler_kind != scaler.kind
            or retained_authorization.scaler_namespace != scaler.namespace
            or retained_authorization.scaler_name != scaler.name
            or retained_authorization.model_generation == model_fence.generation
            and retained_authorization.model_spec_digest != model_fence.spec_digest
        ):
            raise KubernetesConflictError("autoscaler admission gate release revision changed")
        if isinstance(retained_authorization, ScaleGateReleaseAuthorization):
            mutation_parts = (
                retained_authorization.expected_scaler_generation,
                retained_authorization.mutation_token,
                retained_authorization.mutation_operation,
                retained_authorization.mutation_model_generation,
                retained_authorization.mutation_model_spec_digest,
            )
            has_any_mutation_provenance = any(item is not None for item in mutation_parts)
            has_complete_mutation_provenance = all(item is not None for item in mutation_parts)
            if (
                has_any_mutation_provenance != has_complete_mutation_provenance
                or retained_authorization.phase == "prepared" and not has_complete_mutation_provenance
                or retained_authorization.phase == "prepared"
                and (
                    retained_authorization.mutation_model_generation
                    != retained_authorization.model_generation
                    or retained_authorization.mutation_model_spec_digest
                    != retained_authorization.model_spec_digest
                )
            ):
                raise KubernetesConflictError("autoscaler gate mutation provenance is incomplete")
            if has_complete_mutation_provenance:
                assert retained_authorization.expected_scaler_generation is not None
                assert retained_authorization.mutation_operation is not None
                assert retained_authorization.mutation_model_generation is not None
                assert retained_authorization.mutation_model_spec_digest is not None
                retained_fence = ModelWriteFence(
                    key=model_fence.key,
                    uid=retained_authorization.model_uid,
                    resource_version=retained_authorization.model_resource_version,
                    generation=retained_authorization.mutation_model_generation,
                    spec_digest=retained_authorization.mutation_model_spec_digest,
                )
                retained_scaler = scaler.model_copy(
                    update={"digest": retained_authorization.desired_scaler_digest}
                )
                if retained_authorization.mutation_token != _scale_gate_mutation_token(
                    deployment_uid=retained_authorization.deployment_uid,
                    model_fence=retained_fence,
                    scaler=retained_scaler,
                    prior_scaler=retained_authorization.prior_scaler,
                    expected_generation=retained_authorization.expected_scaler_generation,
                    operation=retained_authorization.mutation_operation,
                ):
                    raise KubernetesConflictError("autoscaler gate mutation token is invalid")
        await self._assert_scale_gate_admission(resource)
        targeters = await self._targeting_autoscalers(resource)
        live_scalers = [
            body
            for body in targeters
            if body.get("kind") == "ScaledObject" and _metadata(body).get("name") == scaler.name
        ]
        if len(live_scalers) > 1:
            raise KubernetesConflictError("more than one rendered ScaledObject targeter was observed")
        live_scaler = live_scalers[0] if live_scalers else None
        scaler_uid = _metadata(live_scaler).get("uid") if live_scaler is not None else None
        if scaler_uid is not None and not isinstance(scaler_uid, str):
            raise KubernetesConflictError("rendered ScaledObject UID is invalid")
        if isinstance(retained_authorization, ScaleHandoffReceipt) and live_scaler is not None and (
            retained_authorization.scaler.uid != scaler_uid
            or retained_authorization.scaler.generation != int(_metadata(live_scaler).get("generation", 0))
        ):
            raise KubernetesConflictError("interrupted handoff scaler changed before gate recovery")
        if isinstance(retained_authorization, ScaleInitializationReceipt) and live_scaler is not None:
            raise KubernetesConflictError("interrupted initialization acquired a scaler before gate recovery")
        self._validate_exact_autoscaler_targeters(
            targeters,
            scaler_name=scaler.name,
            scaler_uid=scaler_uid,
            model_uid=owner_uid,
        )
        scaler_snapshot: ResourceSnapshot | None = None
        if live_scaler is not None:
            scaler_snapshot = _snapshot(live_scaler, scaler)
            if (
                scaler_snapshot.observed.deleting
                or scaler_snapshot.observed.controller_owner_uid != owner_uid
                or FIELD_MANAGER not in scaler_snapshot.observed.field_managers
            ):
                raise KubernetesConflictError("rendered ScaledObject is deleting or foreign")
        if isinstance(retained_authorization, ScaleGateReleaseAuthorizationV2):
            retained_authorization = self._adopt_scale_release_authorization_v2(
                retained_authorization,
                scaler=scaler,
                live_scaler=live_scaler,
                scaler_snapshot=scaler_snapshot,
                deployment_uid=current.observed.uid,
                owner_uid=owner_uid,
                model_fence=model_fence,
            )
        retain_applied_authorization = False
        recovered_mutation_checkpoint: ScaleGateScalerCheckpoint | None = None
        if isinstance(retained_authorization, ScaleGateReleaseAuthorization):
            if (
                retained_authorization.model_generation == model_fence.generation
                and retained_authorization.desired_scaler_digest != scaler.digest
            ):
                raise KubernetesConflictError("current autoscaler authorization has a different desired digest")
            retained_prior = retained_authorization.prior_scaler
            retained_applied = retained_authorization.applied_scaler
            live_checkpoint = (
                _scale_gate_scaler_checkpoint(scaler_snapshot) if scaler_snapshot is not None else None
            )
            live_semantically_matches_prior = (
                scaler_snapshot is not None
                and retained_prior is not None
                and scaler_snapshot.observed.uid == retained_prior.uid
                and scaler_snapshot.generation == retained_prior.generation
                and scaler_snapshot.observed.digest == retained_prior.digest
                and live_checkpoint is not None
                and live_checkpoint.managed_fields_digest == retained_prior.managed_fields_digest
            )
            if (
                retained_authorization.phase == "prepared"
                and live_scaler is not None
                and scaler_snapshot is not None
                and scaler_snapshot.observed.digest == retained_authorization.desired_scaler_digest
            ):
                try:
                    _, recovered_mutation_checkpoint = self._validate_scaler_mutation_postcondition(
                        live_scaler,
                        resource=scaler,
                        owner_uid=owner_uid,
                        authorization=retained_authorization,
                    )
                except KubernetesConflictError:
                    recovered_mutation_checkpoint = None
            live_matches_result = recovered_mutation_checkpoint is not None
            applied_mutation_provenance_matches = True
            applied_mutation_parts = (
                retained_authorization.expected_scaler_generation,
                retained_authorization.mutation_token,
                retained_authorization.mutation_operation,
                retained_authorization.mutation_model_generation,
                retained_authorization.mutation_model_spec_digest,
            )
            if any(item is not None for item in applied_mutation_parts):
                expected_generation = retained_authorization.expected_scaler_generation
                mutation_token = retained_authorization.mutation_token
                operation = retained_authorization.mutation_operation
                assert expected_generation is not None and mutation_token is not None and operation is not None
                expected_owner = _ManagedFieldOwner(
                    manager=FIELD_MANAGER,
                    operation=operation,
                    subresource=None,
                    api_version=scaler.api_version,
                )
                applied_mutation_provenance_matches = (
                    live_scaler is not None
                    and scaler_snapshot is not None
                    and scaler_snapshot.generation == expected_generation
                    and _mapping(_metadata(live_scaler).get("annotations")).get(
                        SCALE_GATE_MUTATION_ANNOTATION
                    )
                    == mutation_token
                    and _annotation_field_owners(live_scaler, SCALE_GATE_MUTATION_ANNOTATION)
                    == [expected_owner]
                )
            live_semantically_matches_applied = (
                scaler_snapshot is not None
                and retained_applied is not None
                and scaler_snapshot.observed.uid == retained_applied.uid
                and scaler_snapshot.generation == retained_applied.generation
                and scaler_snapshot.observed.digest == retained_applied.digest
                and live_checkpoint is not None
                and live_checkpoint.managed_fields_digest == retained_applied.managed_fields_digest
                and applied_mutation_provenance_matches
            )
            live_prior_status_rv_churn = (
                live_semantically_matches_prior
                and live_checkpoint is not None
                and retained_prior is not None
                and live_checkpoint.resource_version != retained_prior.resource_version
            )
            live_applied_status_rv_churn = (
                live_semantically_matches_applied
                and live_checkpoint is not None
                and retained_applied is not None
                and live_checkpoint.resource_version != retained_applied.resource_version
            )
            live_matches_prior = (
                live_checkpoint is not None
                and retained_prior is not None
                and (live_checkpoint == retained_prior or live_prior_status_rv_churn)
            )
            live_matches_applied = (
                live_checkpoint is not None
                and retained_applied is not None
                and (live_checkpoint == retained_applied or live_applied_status_rv_churn)
            )
            retained_state_valid = (
                (retained_authorization.phase == "closed" and live_scaler is None)
                or (
                    retained_authorization.phase == "prepared"
                    and (
                        (retained_prior is None and (live_scaler is None or live_matches_result))
                        or (retained_prior is not None and (live_matches_prior or live_matches_result))
                    )
                )
                or (retained_authorization.phase == "applied" and live_matches_applied)
            )
            if not retained_state_valid:
                raise KubernetesConflictError("autoscaler gate recovery observed an unauthorized third scaler state")
            retain_applied_authorization = (
                retained_authorization.phase == "applied"
                and retained_authorization.model_generation == model_fence.generation
                and retained_authorization.model_spec_digest == model_fence.spec_digest
                and retained_authorization.desired_scaler_digest == scaler.digest
                and live_matches_applied
            )
        live = await self._get_resource(resource.api_version, resource.kind, resource.namespace, resource.name)
        if live is None:
            raise KubernetesConflictError("scale gate target disappeared before release")
        self._validate_fixed_scale_identity(
            live,
            resource=resource,
            owner_uid=owner_uid,
            expected_uid=current.observed.uid,
        )
        if _controller_owned_scale_authorization_receipt(live) != receipt:
            raise KubernetesConflictError("autoscaler gate target receipt changed")
        live_snapshot = _snapshot(live, resource)
        if live_scaler is None:
            if current.desired_replicas == 0 and _fixed_scale_manager_owns_replicas(current):
                self._validate_controller_scale_owner(
                    live,
                    resource=resource,
                    owner_uid=owner_uid,
                    expected_uid=current.observed.uid,
                    expected_replicas=0,
                    model_generation=model_fence.generation,
                    receipt=receipt,
                )
            elif isinstance(receipt, ScaleHandoffReceipt):
                live_snapshot = self._validate_post_delete_handoff_reversal(
                    live,
                    resource=resource,
                    scaler=scaler,
                    current=current,
                    owner_uid=owner_uid,
                    model_fence=model_fence,
                    receipt=receipt,
                )
            else:
                raise KubernetesConflictError("autoscaler admission cannot begin before exact zero-scale ownership")
        elif not (
            _fixed_scale_manager_owns_replicas(live_snapshot) or _autoscaler_scale_manager_owns_replicas(live_snapshot)
        ):
            raise KubernetesConflictError("autoscaled Deployment has unexpected replica ownership")

        if retain_applied_authorization:
            assert isinstance(retained_authorization, ScaleGateReleaseAuthorization)
            assert scaler_snapshot is not None
            # Status writes legitimately advance both the ModelDeployment and
            # ScaledObject resourceVersions without changing the authorized
            # generation/spec tuple. Reissue the durable postcondition with the
            # freshly observed RVs under the ConfigMap CAS below. A UID,
            # generation, digest, owner, manager, or deletion change was already
            # rejected and can never be normalized as status-only churn.
            authorization = retained_authorization.model_copy(
                update={
                    "model_resource_version": model_fence.resource_version,
                    "applied_scaler": _scale_gate_scaler_checkpoint(scaler_snapshot),
                }
            )
        elif live_scaler is None or (
            scaler_snapshot is not None and scaler_snapshot.observed.digest != scaler.digest
        ):
            prior_scaler = _scale_gate_scaler_checkpoint(scaler_snapshot) if scaler_snapshot is not None else None
            mutation_operation: Literal["Apply", "Update"] = "Apply" if prior_scaler is not None else "Update"
            expected_generation = _expected_scaler_mutation_generation(live_scaler, scaler)
            authorization = ScaleGateReleaseAuthorization(
                version=3,
                deploymentUID=current.observed.uid,
                modelUID=owner_uid,
                modelResourceVersion=model_fence.resource_version,
                modelGeneration=model_fence.generation,
                modelSpecDigest=model_fence.spec_digest,
                scalerAPIVersion=scaler.api_version,
                scalerKind=scaler.kind,
                scalerNamespace=scaler.namespace,
                scalerName=scaler.name,
                desiredScalerDigest=scaler.digest,
                expectedScalerGeneration=expected_generation,
                mutationToken=_scale_gate_mutation_token(
                    deployment_uid=current.observed.uid,
                    model_fence=model_fence,
                    scaler=scaler,
                    prior_scaler=prior_scaler,
                    expected_generation=expected_generation,
                    operation=mutation_operation,
                ),
                mutationOperation=mutation_operation,
                mutationModelGeneration=model_fence.generation,
                mutationModelSpecDigest=model_fence.spec_digest,
                priorScaler=prior_scaler,
                predecessorEvidenceDigest=(
                    retained_authorization.predecessor_evidence_digest
                    if isinstance(retained_authorization, ScaleGateReleaseAuthorization)
                    else None
                ),
                phase="prepared",
            )
        else:
            assert scaler_snapshot is not None
            scaler_checkpoint = _scale_gate_scaler_checkpoint(scaler_snapshot)
            if isinstance(retained_authorization, ScaleGateReleaseAuthorization):
                if retained_authorization.phase == "prepared" and recovered_mutation_checkpoint is None:
                    raise KubernetesConflictError("prepared scaler successor lacks exact mutation provenance")
                if retained_authorization.phase not in {"prepared", "applied"}:
                    raise KubernetesConflictError("closed scaler authorization cannot normalize a live scaler")
                authorization = retained_authorization.model_copy(
                    update={
                        "model_resource_version": model_fence.resource_version,
                        "model_generation": model_fence.generation,
                        "model_spec_digest": model_fence.spec_digest,
                        "desired_scaler_digest": scaler.digest,
                        "applied_scaler": scaler_checkpoint,
                        "phase": "applied",
                    }
                )
            else:
                # The retained transition receipt already binds this exact
                # live scaler UID/generation. No mutation is being recovered,
                # so no synthetic mutation token is minted.
                authorization = ScaleGateReleaseAuthorization(
                    version=3,
                    deploymentUID=current.observed.uid,
                    modelUID=owner_uid,
                    modelResourceVersion=model_fence.resource_version,
                    modelGeneration=model_fence.generation,
                    modelSpecDigest=model_fence.spec_digest,
                    scalerAPIVersion=scaler.api_version,
                    scalerKind=scaler.kind,
                    scalerNamespace=scaler.namespace,
                    scalerName=scaler.name,
                    desiredScalerDigest=scaler.digest,
                    appliedScaler=scaler_checkpoint,
                    phase="applied",
                )
        authorization = _scale_gate_successor_with_evidence(authorization, predecessor_evidence)
        assert isinstance(authorization, ScaleGateReleaseAuthorization)
        expected_gate_value = _encoded_scale_gate_value(target, authorization)
        await self.assert_fence(fence)
        model = await self.get_model(model_fence.key)
        if model is None:
            raise KubernetesConflictError("ModelDeployment disappeared before scale gate release")
        self._validate_model_write_fence(model, model_fence)
        config_map = await self._scale_gate_config_map(resource.namespace)
        data = _mapping(config_map.get("data"))
        target_key = _scale_gate_target_key(target)
        final_retained_authorization, _ = await self._verified_scale_gate_authorization(
            config_map,
            target,
            migrate_legacy=True,
            retain_v2=True,
            fence=fence,
        )
        if (
            data.get(target_key) != retained_gate_value
            or final_retained_authorization != source_retained_authorization
        ):
            raise KubernetesConflictError("fixed-scale admission gate changed before release")
        so_key, hpa_key = _scale_gate_companion_keys(target)
        expected_data: dict[str, str | None] = {
            so_key: _scale_gate_allowance_value(
                target,
                api_version=scaler.api_version,
                kind=scaler.kind,
                namespace=scaler.namespace,
                name=scaler.name,
                owner_uid=owner_uid,
            ),
            hpa_key: (
                _scale_gate_allowance_value(
                    target,
                    api_version=HPA_ENDPOINT.api_version,
                    kind=HPA_ENDPOINT.kind,
                    namespace=scaler.namespace,
                    name=f"keda-hpa-{scaler.name}",
                    owner_uid=scaler_uid,
                )
                if scaler_uid is not None
                else None
            ),
        }
        expected_data[target_key] = expected_gate_value
        gate_change = any(
            (value is None and key in data) or (value is not None and data.get(key) != value)
            for key, value in expected_data.items()
        )
        if gate_change:
            await self._patch_scale_gate_data(
                namespace=resource.namespace,
                config_map=config_map,
                data=expected_data,
            )
        confirmed = await self._scale_gate_config_map(resource.namespace) if gate_change else config_map
        confirmed_data = _mapping(confirmed.get("data"))
        confirmed_authorization, _ = await self._verified_scale_gate_authorization(confirmed, target)
        if confirmed_data.get(target_key) != expected_gate_value or any(
            (value is None and key in confirmed_data) or (value is not None and confirmed_data.get(key) != value)
            for key, value in expected_data.items()
        ) or confirmed_authorization != authorization:
            raise KubernetesConflictError("exact autoscaler gate allowance was not observed")
        if gate_change:
            # The post-write complete scan is protected by the still-closed
            # admission policy: only these exact name+owner targeters can appear.
            await self._assert_exact_autoscaler_targeters(
                resource,
                scaler_name=scaler.name,
                scaler_uid=scaler_uid,
                model_uid=owner_uid,
            )
        return authorization

    async def fixed_scale_guard_clear(
        self,
        resource: RenderedResource,
        *,
        current: ResourceSnapshot,
        owner_uid: str,
        model_generation: int,
        model_fence: ModelWriteFence,
        fence: LeaseFence,
    ) -> bool:
        """Continuously guard every receipt-backed fixed Deployment.

        This read-only check closes crash/retry gaps around exceptional writes:
        a late, unlabeled HPA or ScaledObject blocks steady fixed reconciliation
        before the controller can write or fight the scaler.
        """

        receipt = _controller_owned_scale_authorization_receipt(current.raw)
        if (
            resource.api_version != "apps/v1"
            or resource.kind != "Deployment"
            or current.observed.identity != _rendered_identity(resource)
            or current.observed.controller_owner_uid != owner_uid
            or current.observed.deleting
            or receipt is None
            or receipt.deployment_uid != current.observed.uid
            or receipt.model_uid != owner_uid
            or receipt.model_generation > model_generation
            or model_fence.uid != owner_uid
            or model_fence.generation != model_generation
        ):
            raise KubernetesConflictError("receipt-backed fixed scale guard evidence changed")
        if isinstance(receipt, ScaleHandoffReceipt):
            if (
                receipt.scaler.api_version != "keda.sh/v1alpha1"
                or receipt.scaler.kind != "ScaledObject"
                or receipt.scaler.namespace != resource.namespace
            ):
                raise KubernetesConflictError("receipt-backed fixed scale guard scaler evidence changed")
        elif receipt.target_mode != "fixed":
            raise KubernetesConflictError("fixed scale guard has an autoscaled initialization receipt")
        if await self._targeting_autoscaler_exists(resource):
            return False
        model = await self.get_model(model_fence.key)
        if model is None:
            raise KubernetesConflictError("ModelDeployment disappeared during fixed scale guard")
        self._validate_model_write_fence(model, model_fence)
        live = await self._get_resource(resource.api_version, resource.kind, resource.namespace, resource.name)
        if live is None:
            raise KubernetesConflictError("receipt-backed fixed Deployment disappeared")
        self._validate_fixed_scale_identity(
            live,
            resource=resource,
            owner_uid=owner_uid,
            expected_uid=current.observed.uid,
        )
        if _controller_owned_scale_authorization_receipt(live) != receipt:
            raise KubernetesConflictError("receipt-backed fixed scale guard receipt changed")
        live_snapshot = _snapshot(live, resource)
        if current.desired_replicas is None or live_snapshot.desired_replicas != current.desired_replicas:
            raise KubernetesConflictError("receipt-backed fixed Deployment replica value changed")
        owners = _replica_field_owners(live)
        fixed_owner = _ReplicaFieldOwner(
            manager=FIXED_SCALE_FIELD_MANAGER,
            subresource="scale",
            api_version="apps/v1",
        )
        if owners != [fixed_owner]:
            stale = [
                owner
                for owner in owners
                if owner.manager in STALE_SCALE_FIELD_MANAGERS
                and owner.subresource == "scale"
                and owner.api_version == "apps/v1"
            ]
            if len(stale) != 1 or owners != stale:
                raise KubernetesConflictError(
                    "receipt-backed fixed Deployment lacks one canonical scale owner; manual migration is required"
                )
        target = _scale_gate_target(resource)
        target_key = _scale_gate_target_key(target)
        gate = await self._scale_gate_config_map(resource.namespace)
        data = _mapping(gate.get("data"))
        retained_gate_value = data.get(target_key)
        retained_authorization, predecessor_evidence = await self._verified_scale_gate_authorization(
            gate,
            target,
            migrate_legacy=True,
            retain_v2=True,
            fence=fence,
        )
        source_retained_authorization = retained_authorization
        if isinstance(retained_authorization, SCALE_GATE_RELEASE_AUTHORIZATION_TYPES):
            if (
                retained_authorization.deployment_uid != current.observed.uid
                or retained_authorization.model_uid != owner_uid
                or retained_authorization.model_generation > model_fence.generation
                or isinstance(receipt, ScaleHandoffReceipt)
                and (
                    retained_authorization.scaler_api_version != receipt.scaler.api_version
                    or retained_authorization.scaler_kind != receipt.scaler.kind
                    or retained_authorization.scaler_namespace != receipt.scaler.namespace
                    or retained_authorization.scaler_name != receipt.scaler.name
                )
            ):
                raise KubernetesConflictError("superseded autoscaler allowance is stale or foreign")
            if retained_authorization.phase != "closed" and (
                retained_authorization.model_generation >= model_fence.generation
            ):
                raise KubernetesConflictError("current fixed generation cannot close a non-superseded allowance")
            if retained_authorization.phase == "closed" and (
                retained_authorization.model_generation == model_fence.generation
                and retained_authorization.model_spec_digest != model_fence.spec_digest
            ):
                raise KubernetesConflictError("fixed scale closure is not bound to the current CR semantics")
            if isinstance(retained_authorization, ScaleGateReleaseAuthorization):
                # Closure is a state transition, not evidence erasure. Retain
                # the complete mutation/checkpoint lineage while rebinding only
                # the current fixed CR fence.
                closure = retained_authorization.model_copy(
                    update={
                        "model_resource_version": model_fence.resource_version,
                        "model_generation": model_fence.generation,
                        "model_spec_digest": model_fence.spec_digest,
                        "phase": "closed",
                    }
                )
            else:
                v2_prior = _scale_gate_v2_checkpoint_with_managed_fields(
                    retained_authorization.prior_scaler
                )
                v2_applied = _scale_gate_v2_checkpoint_with_managed_fields(
                    retained_authorization.applied_scaler
                )
                mutation_parts = (
                    retained_authorization.expected_scaler_generation,
                    retained_authorization.mutation_token,
                    retained_authorization.mutation_operation,
                )
                preserved_mutation: dict[str, Any] = {}
                if all(item is not None for item in mutation_parts) and (
                    retained_authorization.prior_scaler is None or v2_prior is not None
                ):
                    assert retained_authorization.expected_scaler_generation is not None
                    assert retained_authorization.mutation_operation is not None
                    legacy_fence = ModelWriteFence(
                        key=model_fence.key,
                        uid=retained_authorization.model_uid,
                        resource_version=retained_authorization.model_resource_version,
                        generation=retained_authorization.model_generation,
                        spec_digest=retained_authorization.model_spec_digest,
                    )
                    legacy_scaler = RenderedResource(
                        api_version=retained_authorization.scaler_api_version,
                        kind=retained_authorization.scaler_kind,
                        namespace=retained_authorization.scaler_namespace,
                        name=retained_authorization.scaler_name,
                        manifest={},
                        digest=retained_authorization.desired_scaler_digest,
                    )
                    if retained_authorization.mutation_token == _scale_gate_mutation_token(
                        deployment_uid=retained_authorization.deployment_uid,
                        model_fence=legacy_fence,
                        scaler=legacy_scaler,
                        prior_scaler=v2_prior,
                        expected_generation=retained_authorization.expected_scaler_generation,
                        operation=retained_authorization.mutation_operation,
                    ):
                        preserved_mutation = {
                            "expectedScalerGeneration": retained_authorization.expected_scaler_generation,
                            "mutationToken": retained_authorization.mutation_token,
                            "mutationOperation": retained_authorization.mutation_operation,
                            "mutationModelGeneration": retained_authorization.model_generation,
                            "mutationModelSpecDigest": retained_authorization.model_spec_digest,
                        }
                closure = ScaleGateReleaseAuthorization(
                    version=3,
                    deploymentUID=retained_authorization.deployment_uid,
                    modelUID=retained_authorization.model_uid,
                    modelResourceVersion=model_fence.resource_version,
                    modelGeneration=model_fence.generation,
                    modelSpecDigest=model_fence.spec_digest,
                    scalerAPIVersion=retained_authorization.scaler_api_version,
                    scalerKind=retained_authorization.scaler_kind,
                    scalerNamespace=retained_authorization.scaler_namespace,
                    scalerName=retained_authorization.scaler_name,
                    desiredScalerDigest=retained_authorization.desired_scaler_digest,
                    priorScaler=v2_prior,
                    appliedScaler=v2_applied,
                    predecessorEvidenceDigest=_scale_gate_predecessor_evidence_digest(
                        retained_authorization
                    ),
                    **preserved_mutation,
                    phase="closed",
                )
            closure = _scale_gate_successor_with_evidence(closure, predecessor_evidence)
            assert isinstance(closure, ScaleGateReleaseAuthorization)
            closure_value = _encoded_scale_gate_value(target, closure)
            # No targeter remains. Close the exact older autoscaled generation
            # before any fixed-mode write can proceed. A crash before this
            # patch leaves the allowance visibly open and retries this branch;
            # a crash afterwards observes the receipt-backed closed state.
            if closure_value != retained_gate_value or any(key in data for key in _scale_gate_companion_keys(target)):
                await self.assert_fence(fence)
                latest_model = await self.get_model(model_fence.key)
                if latest_model is None:
                    raise KubernetesConflictError("ModelDeployment disappeared before closing autoscaler allowance")
                self._validate_model_write_fence(latest_model, model_fence)
                latest_gate = await self._scale_gate_config_map(resource.namespace)
                latest_authorization, _ = await self._verified_scale_gate_authorization(
                    latest_gate,
                    target,
                    migrate_legacy=True,
                    retain_v2=True,
                    fence=fence,
                )
                if (
                    _mapping(latest_gate.get("data")).get(target_key) != retained_gate_value
                    or latest_authorization != source_retained_authorization
                ):
                    raise KubernetesConflictError("autoscaler allowance changed before fixed-mode closure")
                closure_data = self._closed_scale_gate_data(resource, closure_value)
                await self._patch_scale_gate_data(
                    namespace=resource.namespace,
                    config_map=latest_gate,
                    data=closure_data,
                )
        elif (
            isinstance(receipt, ScaleHandoffReceipt)
            and _scale_gate_receipt_matches(retained_authorization, receipt)
            and receipt.model_generation < model_fence.generation
        ):
            # A crash may leave the original handoff receipt as the primary
            # gate while a later autoscaled generation never durably records
            # its release. Once a still-newer fixed generation proves the
            # exact scaler chain absent, replace that exact old checkpoint
            # with a generation-current closed authorization. This never
            # opens a ScaledObject/HPA allowance and makes the later fixed
            # /scale takeover resumable.
            closure = ScaleGateReleaseAuthorization(
                version=3,
                deploymentUID=current.observed.uid,
                modelUID=owner_uid,
                modelResourceVersion=model_fence.resource_version,
                modelGeneration=model_fence.generation,
                modelSpecDigest=model_fence.spec_digest,
                scalerAPIVersion=receipt.scaler.api_version,
                scalerKind=receipt.scaler.kind,
                scalerNamespace=receipt.scaler.namespace,
                scalerName=receipt.scaler.name,
                desiredScalerDigest=canonical_digest(receipt.scaler.model_dump(mode="json", by_alias=True)),
                phase="closed",
            )
            closure = _scale_gate_successor_with_evidence(closure, predecessor_evidence)
            assert isinstance(closure, ScaleGateReleaseAuthorization)
            closure_value = _encoded_scale_gate_value(target, closure)
            await self.assert_fence(fence)
            latest_model = await self.get_model(model_fence.key)
            if latest_model is None:
                raise KubernetesConflictError("ModelDeployment disappeared before closing legacy handoff gate")
            self._validate_model_write_fence(latest_model, model_fence)
            latest_gate = await self._scale_gate_config_map(resource.namespace)
            if _mapping(latest_gate.get("data")).get(target_key) != retained_gate_value:
                raise KubernetesConflictError("legacy handoff gate changed before fixed-mode closure")
            await self._patch_scale_gate_data(
                namespace=resource.namespace,
                config_map=latest_gate,
                data=self._closed_scale_gate_data(resource, closure_value),
            )
        # A completed fixed takeover is safe only while the API-server
        # admission gate remains durably active. This is checked on every
        # steady fixed reconcile, including reversal/crash recovery.
        await self._assert_scale_gate(resource, receipt)
        return True

    async def apply_controller_scale(
        self,
        resource: RenderedResource,
        *,
        current: ResourceSnapshot,
        replicas: int,
        owner_uid: str,
        model_fence: ModelWriteFence,
        fence: LeaseFence,
    ) -> ResourceSnapshot:
        """Update a replica field already exclusively owned by the fixed-scale manager."""

        self._allow_write()
        if (
            resource.api_version != "apps/v1"
            or resource.kind != "Deployment"
            or resource.field_manager != FIELD_MANAGER
            or resource.force_conflicts
            or current.observed.identity != _rendered_identity(resource)
            or current.observed.controller_owner_uid != owner_uid
            or current.observed.deleting
            or _controller_owner_uid(resource.manifest) != owner_uid
            or current.desired_replicas is None
            or model_fence.uid != owner_uid
            or model_fence.key.namespace != resource.namespace
        ):
            raise ControllerError("controller scale write preconditions are not satisfied")
        receipt = _controller_owned_scale_authorization_receipt(current.raw)
        if receipt is None:
            raise KubernetesConflictError("controller scale write lacks a controller-owned transition receipt")
        prepared, _ = await self._prepare_controller_scale_write(
            resource=resource,
            current=current,
            expected_replicas=current.desired_replicas,
            owner_uid=owner_uid,
            model_generation=model_fence.generation,
            model_fence=model_fence,
            receipt=receipt,
            fence=fence,
            stale_owner=False,
        )
        written = await self._patch_prepared_controller_scale(
            resource,
            current=prepared,
            replicas=replicas,
            owner_uid=owner_uid,
            force=False,
        )
        return await self._postcheck_exceptional_scale(
            resource,
            written=written,
            expected_replicas=replicas,
            owner_uid=owner_uid,
            model_generation=model_fence.generation,
            model_fence=model_fence,
            receipt=receipt,
        )

    async def apply_fixed_scale_handoff(
        self,
        resource: RenderedResource,
        *,
        current: ResourceSnapshot,
        owner_uid: str,
        model_generation: int,
        model_fence: ModelWriteFence,
        fence: LeaseFence,
    ) -> ResourceSnapshot:
        """Take back fixed replica ownership from a deleted, observed KEDA scaler.

        Generic SSA remains non-forcing. On the exact expected conflict, the
        exceptional request uses only the Deployment ``/scale`` subresource
        under a dedicated manager, and only after durable transition evidence
        plus fresh namespace-wide autoscaler absence checks survive.
        """

        self._allow_write()
        manifest = copy.deepcopy(resource.manifest)
        spec = manifest.get("spec")
        desired_replicas = spec.get("replicas") if isinstance(spec, Mapping) else None
        if (
            resource.api_version != "apps/v1"
            or resource.kind != "Deployment"
            or resource.field_manager != FIELD_MANAGER
            or resource.force_conflicts
            or not isinstance(desired_replicas, int)
            or isinstance(desired_replicas, bool)
            or desired_replicas < 0
            or current.observed.identity != _rendered_identity(resource)
            or current.observed.controller_owner_uid != owner_uid
            or current.observed.deleting
            or not current.replica_field_managers
            or model_fence.uid != owner_uid
            or model_fence.generation != model_generation
            or model_fence.key.namespace != resource.namespace
        ):
            raise ControllerError("fixed scale handoff preconditions are not satisfied")
        if _controller_owner_uid(manifest) != owner_uid:
            raise ControllerError("fixed scale handoff manifest has a different controller owner")
        receipt = _controller_owned_scale_handoff_receipt(current.raw)
        if receipt is None or receipt.model_generation > model_generation:
            raise KubernetesConflictError("fixed scale handoff lacks a controller-owned transition receipt")
        if receipt.model_generation < model_generation:
            await self._assert_superseded_handoff_closure(
                resource,
                receipt=receipt,
                model_fence=model_fence,
            )

        if current.desired_replicas is None:
            raise KubernetesConflictError("fixed scale handoff has no live replica value")
        if current.desired_replicas == desired_replicas:
            raise KubernetesConflictError(
                "equal-value fixed scale ownership requires manual migration; no replica write was attempted"
            )

        # A dry-run preserves strict API-server 409 Status validation without
        # ever submitting a full-Deployment mutation. The complete scaler,
        # Deployment material tuple, Lease, and exact CR are freshly checked
        # before this request, with the CR read last.
        prepared, stale_owner = await self._prepare_controller_scale_write(
            resource,
            current=current,
            expected_replicas=current.desired_replicas,
            owner_uid=owner_uid,
            model_generation=model_generation,
            model_fence=model_fence,
            receipt=receipt,
            fence=fence,
            stale_owner=True,
        )
        assert stale_owner is not None
        manifest.setdefault("metadata", {})["resourceVersion"] = prepared.resource_version
        expected = KubernetesFieldConflict(
            manager=stale_owner.manager,
            subresource="scale",
            api_version="apps/v1",
            field=".spec.replicas",
        )
        try:
            await self._request(
                "PATCH",
                self._endpoint(resource.api_version, resource.kind).item(resource.namespace, resource.name),
                params={
                    "fieldManager": FIELD_MANAGER,
                    "force": "false",
                    "fieldValidation": "Strict",
                    "dryRun": "All",
                },
                content_type="application/apply-patch+yaml",
                content=json.dumps(manifest, separators=(",", ":")).encode(),
            )
        except KubernetesConflictError as exc:
            if exc.field_conflicts != (expected,):
                raise
        else:
            raise KubernetesConflictError(
                "fixed scale handoff dry-run did not prove the exact stale scale conflict; "
                "no replica write was attempted"
            )

        # Re-run every material and authorization check after the dry-run and
        # immediately before the only mutating request, which is /scale-only.
        return await self._take_over_controller_scale(
            resource,
            current=current,
            desired_replicas=desired_replicas,
            owner_uid=owner_uid,
            model_generation=model_generation,
            model_fence=model_fence,
            receipt=receipt,
            fence=fence,
        )

    @staticmethod
    def _parse_identity(identity: str) -> tuple[str, str, str, str]:
        try:
            api_version, kind, namespace, name = identity.rsplit("/", 3)
        except ValueError as exc:
            raise ControllerError("resource identity is malformed") from exc
        if not all((api_version, kind, namespace, name)):
            raise ControllerError("resource identity is incomplete")
        return api_version, kind, namespace, name

    async def delete_resource(
        self,
        identity: str,
        *,
        owner_uid: str,
        fence: LeaseFence,
    ) -> bool:
        self._allow_write()
        api_version, kind, namespace, name = self._parse_identity(identity)
        if _is_scale_gate_predecessor_evidence_identity(api_version, kind, name):
            raise KubernetesConflictError("immutable scale-gate evidence can never enter a delete path")
        endpoint = self._endpoint(api_version, kind)
        current = await self._get_resource(api_version, kind, namespace, name)
        if current is None:
            return False
        if _controller_owner_uid(current) != owner_uid:
            raise KubernetesConflictError("refusing to delete a resource without the exact controller owner")
        await self.assert_fence(fence)
        response = await self._request(
            "DELETE",
            endpoint.item(namespace, name),
            json={
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "propagationPolicy": "Foreground" if kind == "ScaledObject" else "Background",
                "preconditions": {
                    "uid": _required_metadata(current, "uid"),
                    "resourceVersion": _required_metadata(current, "resourceVersion"),
                },
            },
        )
        return response.status_code != 404

    async def set_finalizer(
        self,
        key: ModelKey,
        *,
        owner_uid: str,
        present: bool,
        fence: LeaseFence,
    ) -> None:
        self._allow_write()
        current = await self.get_model(key)
        if current is None or _required_metadata(current, "uid") != owner_uid:
            raise KubernetesConflictError("ModelDeployment UID changed before finalizer write")
        metadata = _metadata(current)
        finalizers = _string_list(metadata.get("finalizers"))
        updated = (
            list(dict.fromkeys([*finalizers, FINALIZER]))
            if present
            else [value for value in finalizers if value != FINALIZER]
        )
        if updated == finalizers:
            return
        if present and metadata.get("deletionTimestamp") is not None:
            raise KubernetesConflictError("cannot add the cleanup finalizer after deletion started")
        await self.assert_fence(fence)
        await self._request(
            "PATCH",
            MODEL_ENDPOINT.item(key.namespace, key.name),
            content_type="application/merge-patch+json",
            content=json.dumps(
                {"metadata": {"resourceVersion": _required_metadata(current, "resourceVersion"), "finalizers": updated}}
            ).encode(),
        )

    async def patch_status(
        self,
        key: ModelKey,
        *,
        owner_uid: str,
        generation: int,
        status: dict[str, Any],
        fence: LeaseFence,
    ) -> bool:
        self._allow_write()
        current = await self.get_model(key)
        if current is None or _required_metadata(current, "uid") != owner_uid:
            raise KubernetesConflictError("ModelDeployment UID changed before status write")
        metadata = _metadata(current)
        if int(metadata.get("generation", 0)) != generation:
            raise KubernetesConflictError("ModelDeployment generation changed before status write")
        current_status = _mapping(current.get("status"))
        if int(current_status.get("observedGeneration", 0)) > generation:
            return False
        await self.assert_fence(fence)
        body = {
            "apiVersion": API_VERSION,
            "kind": KIND,
            "metadata": {
                "name": key.name,
                "namespace": key.namespace,
                "resourceVersion": _required_metadata(current, "resourceVersion"),
            },
            "status": status,
        }
        await self._request(
            "PATCH",
            f"{MODEL_ENDPOINT.item(key.namespace, key.name)}/status",
            params={"fieldManager": STATUS_FIELD_MANAGER, "force": "false", "fieldValidation": "Strict"},
            content_type="application/apply-patch+yaml",
            content=json.dumps(body, separators=(",", ":")).encode(),
        )
        return True


class BoundedKeyQueue:
    """Deduplicating bounded queue; overload converges on the next list pass."""

    def __init__(self, maximum: int) -> None:
        self._queue: asyncio.Queue[ModelKey] = asyncio.Queue(maxsize=maximum)
        self._pending: set[str] = set()
        self.dropped = 0

    def put(self, key: ModelKey) -> bool:
        if key.text in self._pending:
            return True
        try:
            self._queue.put_nowait(key)
        except asyncio.QueueFull:
            self.dropped += 1
            return False
        self._pending.add(key.text)
        return True

    async def get(self) -> ModelKey:
        return await self._queue.get()

    def done(self, key: ModelKey) -> None:
        self._pending.discard(key.text)
        self._queue.task_done()

    @property
    def depth(self) -> int:
        return self._queue.qsize()


class ControllerMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry(auto_describe=True)
        self.leader = Gauge(
            "fs2_model_controller_leader", "Whether this replica owns the live Lease", registry=self.registry
        )
        self.queue_depth = Gauge(
            "fs2_model_controller_queue_depth", "Bounded reconcile queue depth", registry=self.registry
        )
        self.queue_dropped = Counter(
            "fs2_model_controller_queue_dropped_total",
            "Keys deferred after bounded queue saturation",
            registry=self.registry,
        )
        self.reconciles = Counter(
            "fs2_model_controller_reconciles_total",
            "Reconciles by bounded outcome",
            ("outcome",),
            registry=self.registry,
        )
        self.duration = Histogram(
            "fs2_model_controller_reconcile_duration_seconds",
            "End-to-end reconcile duration",
            registry=self.registry,
            buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
        )
        self.modelexpress_configured = Gauge(
            "fs2_model_controller_modelexpress_configured",
            "Whether an exact ModelExpress client binding is configured for a model and pool",
            ("model", "pool", "deployment_mode", "runtime_adapter", "transport_mode"),
            registry=self.registry,
        )

    def render(self) -> bytes:
        return generate_latest(self.registry)


class ControllerHealth:
    def __init__(self, *, reconcile_error_threshold: int = 3) -> None:
        self.live = True
        self.leader = False
        self.cycle_ready = False
        self.last_cycle_error: str | None = None
        self.last_reconcile_error: str | None = None
        self.reconcile_error_threshold = reconcile_error_threshold
        self.reconcile_errors: dict[str, int] = {}

    @property
    def ready(self) -> bool:
        return self.cycle_ready and all(
            count < self.reconcile_error_threshold for count in self.reconcile_errors.values()
        )

    @property
    def last_error(self) -> str | None:
        return self.last_cycle_error or self.last_reconcile_error

    def cycle_succeeded(self) -> None:
        self.cycle_ready = True
        self.last_cycle_error = None

    def cycle_failed(self, error: str) -> None:
        self.cycle_ready = False
        self.last_cycle_error = error

    def reconcile_succeeded(self, key: ModelKey) -> None:
        self.reconcile_errors.pop(key.text, None)
        self.last_reconcile_error = None

    def reconcile_failed(self, key: ModelKey, error: str) -> None:
        self.reconcile_errors[key.text] = self.reconcile_errors.get(key.text, 0) + 1
        self.last_reconcile_error = error


def _old_conditions(status: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    value = status.get("conditions")
    if not isinstance(value, list):
        return {}
    return {
        str(item["type"]): item for item in value if isinstance(item, Mapping) and isinstance(item.get("type"), str)
    }


def _condition(
    condition_type: str,
    value: Literal["True", "False", "Unknown"],
    reason: str,
    message: str,
    generation: int,
    *,
    previous: Mapping[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    unchanged = previous is not None and previous.get("status") == value and previous.get("reason") == reason
    transition = previous.get("lastTransitionTime") if unchanged and previous is not None else None
    return {
        "type": condition_type,
        "status": value,
        "observedGeneration": generation,
        "reason": reason,
        "message": message,
        "lastTransitionTime": transition if isinstance(transition, str) else _timestamp(now),
    }


def _known_total(values: list[int | None], *, empty: int = 0) -> int | None:
    if not values:
        return empty
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _resource_snapshot(
    discovery: Discovery, api_version: str, kind: str, namespace: str, name: str
) -> ResourceSnapshot | None:
    identity = f"{api_version}/{kind}/{namespace}/{name}"
    return next((item for item in discovery.resources if item.observed.identity == identity), None)


def _rendered_identity(resource: RenderedResource) -> str:
    return f"{resource.api_version}/{resource.kind}/{resource.namespace}/{resource.name}"


def _autoscaler_pairs(
    render: RenderPlan | None,
) -> list[tuple[RenderedResource, RenderedResource]]:
    """Return every scaler with its unique rendered Deployment target."""

    if render is None:
        return []
    pairs: list[tuple[RenderedResource, RenderedResource]] = []
    target_identities: set[str] = set()
    for scaler in sorted(
        (item for item in render.resources if item.kind == "ScaledObject"),
        key=lambda item: (item.namespace, item.name),
    ):
        target = _mapping(_mapping(scaler.manifest.get("spec")).get("scaleTargetRef"))
        api_version = target.get("apiVersion")
        kind = target.get("kind")
        name = target.get("name")
        matches = [
            item
            for item in render.resources
            if item.api_version == api_version
            and item.kind == kind
            and item.name == name
            and item.namespace == scaler.namespace
        ]
        if len(matches) != 1 or api_version != "apps/v1" or kind != "Deployment":
            raise ControllerError("ScaledObject target is not the exact rendered Deployment")
        identity = _rendered_identity(matches[0])
        if identity in target_identities:
            raise ControllerError("more than one ScaledObject targets the same Deployment")
        target_identities.add(identity)
        pairs.append((scaler, matches[0]))
    return pairs


def _without_deployment_replicas(resource: RenderedResource) -> RenderedResource:
    manifest = copy.deepcopy(resource.manifest)
    spec = manifest.get("spec")
    if resource.kind != "Deployment" or not isinstance(spec, dict):
        raise ControllerError("fixed-scale target is not a Deployment")
    spec.pop("replicas", None)
    return resource.model_copy(update={"manifest": manifest, "digest": canonical_digest(manifest)})


def _paused_deployment_without_replicas(resource: RenderedResource) -> RenderedResource:
    paused = _without_deployment_replicas(resource)
    manifest = copy.deepcopy(paused.manifest)
    spec = manifest.get("spec")
    if not isinstance(spec, dict):  # pragma: no cover - guarded above
        raise ControllerError("Deployment initialization spec is unavailable")
    spec["paused"] = True
    return paused.model_copy(update={"manifest": manifest, "digest": canonical_digest(manifest)})


def _fixed_scale_manager_owns_replicas(snapshot: ResourceSnapshot) -> bool:
    return _replica_field_owners(snapshot.raw) == [
        _ReplicaFieldOwner(
            manager=FIXED_SCALE_FIELD_MANAGER,
            subresource="scale",
            api_version="apps/v1",
        )
    ]


def _autoscaler_scale_manager_owns_replicas(snapshot: ResourceSnapshot) -> bool:
    owners = _replica_field_owners(snapshot.raw)
    return (
        len(owners) == 1
        and snapshot.replica_field_managers == [owners[0].manager]
        and owners[0].manager in STALE_SCALE_FIELD_MANAGERS
        and owners[0].subresource == "scale"
        and owners[0].api_version == "apps/v1"
    )


def _generic_apply_resource(resource: RenderedResource, discovery: Discovery) -> RenderedResource:
    del discovery  # kept in the signature to avoid broad reconcile call churn
    if resource.api_version == "apps/v1" and resource.kind == "Deployment":
        return _without_deployment_replicas(resource)
    return resource


def _autoscaler_installed(
    scaler: RenderedResource,
    target: RenderedResource,
    discovery: Discovery,
    owner_uid: str,
    *,
    allow_idle_acknowledgement_lag: bool = False,
) -> bool:
    live_target = _resource_snapshot(
        discovery,
        target.api_version,
        target.kind,
        target.namespace,
        target.name,
    )
    if live_target is None:
        return False
    live_scaler = _resource_snapshot(discovery, scaler.api_version, scaler.kind, scaler.namespace, scaler.name)
    if (
        live_scaler is None
        or live_scaler.observed.deleting
        or live_scaler.observed.controller_owner_uid != owner_uid
        or FIELD_MANAGER not in live_scaler.observed.field_managers
        or live_scaler.observed.digest != scaler.digest
    ):
        return False
    expected_hpa_name = f"keda-hpa-{scaler.name}"
    scaler_status = _mapping(live_scaler.raw.get("status"))
    scaler_conditions = scaler_status.get("conditions")
    if (
        scaler_status.get("hpaName") != expected_hpa_name
        or not isinstance(scaler_conditions, list)
        or not any(
            isinstance(condition, Mapping) and condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in scaler_conditions
        )
    ):
        return False
    hpa = _resource_snapshot(
        discovery,
        HPA_ENDPOINT.api_version,
        HPA_ENDPOINT.kind,
        scaler.namespace,
        expected_hpa_name,
    )
    if hpa is None or hpa.observed.deleting or hpa.observed.controller_owner_uid != live_scaler.observed.uid:
        return False
    hpa_status = _mapping(hpa.raw.get("status"))
    hpa_conditions = hpa_status.get("conditions")
    hpa_conditions = hpa_conditions if isinstance(hpa_conditions, list) else []
    able_to_scale = any(
        isinstance(condition, Mapping) and condition.get("type") == "AbleToScale" and condition.get("status") == "True"
        for condition in hpa_conditions
    )
    scaling_active = any(
        isinstance(condition, Mapping)
        and condition.get("type") == "ScalingActive"
        and condition.get("status") == "True"
        for condition in hpa_conditions
    )
    hpa_zero_idle = (
        live_target.desired_replicas == 0
        and hpa_status.get("desiredReplicas") == 0
        and any(
            isinstance(condition, Mapping)
            and condition.get("type") == "ScalingActive"
            and condition.get("status") == "False"
            and condition.get("reason") == "ScalingDisabled"
            for condition in hpa_conditions
        )
    )
    keda_zero_idle = (
        hpa_zero_idle
        and live_target.replicas == 0
        and any(
            isinstance(condition, Mapping)
            and condition.get("type") == "HPAActive"
            and condition.get("status") == "True"
            and condition.get("reason") == "ScalingDisabled"
            for condition in scaler_conditions
        )
    )
    target_ref = _mapping(_mapping(hpa.raw.get("spec")).get("scaleTargetRef"))
    generation_observed = hpa_status.get("observedGeneration")
    generation_converged = generation_observed == hpa.generation or (
        generation_observed is None and hpa.observed_generation is None and hpa.generation == 0
    )
    return (
        able_to_scale
        and (scaling_active or keda_zero_idle or (allow_idle_acknowledgement_lag and hpa_zero_idle))
        and generation_converged
        and target_ref.get("apiVersion") == target.api_version
        and target_ref.get("kind") == target.kind
        and target_ref.get("name") == target.name
    )


def _autoscaler_handoff_complete(
    render: RenderPlan | None,
    discovery: Discovery,
    owner_uid: str,
    *,
    allow_idle_acknowledgement_lag: bool = False,
) -> bool:
    return all(
        (
            (
                live_target := _resource_snapshot(
                    discovery,
                    target.api_version,
                    target.kind,
                    target.namespace,
                    target.name,
                )
            )
            is not None
            and _autoscaler_installed(
                scaler,
                target,
                discovery,
                owner_uid,
                allow_idle_acknowledgement_lag=allow_idle_acknowledgement_lag,
            )
            and _autoscaler_scale_manager_owns_replicas(live_target)
        )
        for scaler, target in _autoscaler_pairs(render)
    )


def _autoscaler_resources_present(discovery: Discovery) -> bool:
    return any(item.observed.kind in {"ScaledObject", HPA_ENDPOINT.kind} for item in discovery.resources)


def _autoscaler_targets_present(
    discovery: Discovery,
    targets: set[str],
) -> bool:
    for item in discovery.resources:
        if item.observed.kind not in {"ScaledObject", HPA_ENDPOINT.kind}:
            continue
        target = _mapping(_mapping(item.raw.get("spec")).get("scaleTargetRef"))
        api_version = target.get("apiVersion")
        kind = target.get("kind")
        name = target.get("name")
        if all(isinstance(value, str) and value for value in (api_version, kind, name)):
            identity = f"{api_version}/{kind}/{item.observed.namespace}/{name}"
            if identity in targets:
                return True
    return False


def _pending_scale_handoff_receipts(
    render: RenderPlan | None,
    discovery: Discovery,
    delete_identities: set[str],
    owner_uid: str,
    model_generation: int,
) -> list[tuple[RenderedResource, ResourceSnapshot, ResourceSnapshot]]:
    """Find exact autoscaled-to-fixed transitions that need durable evidence."""

    if render is None:
        return []
    desired = {_rendered_identity(item): item for item in render.resources}
    pending: list[tuple[RenderedResource, ResourceSnapshot, ResourceSnapshot]] = []
    for scaler in discovery.resources:
        if scaler.observed.kind != "ScaledObject" or scaler.observed.identity not in delete_identities:
            continue
        if (
            scaler.observed.api_version != "keda.sh/v1alpha1"
            or scaler.observed.controller_owner_uid != owner_uid
            or scaler.observed.deleting
            or scaler.generation < 1
        ):
            raise ControllerError("scale handoff receipt source is not the exact live owned ScaledObject")
        target_ref = _mapping(_mapping(scaler.raw.get("spec")).get("scaleTargetRef"))
        target_identity = (
            f"{target_ref.get('apiVersion')}/{target_ref.get('kind')}/"
            f"{scaler.observed.namespace}/{target_ref.get('name')}"
        )
        # A scaler and its Deployment may both be retiring during drain or CR
        # deletion. No replica takeover occurs in that path, so no exceptional
        # scale receipt is warranted.
        if target_identity in delete_identities:
            continue
        resource = desired.get(target_identity)
        target = next((item for item in discovery.resources if item.observed.identity == target_identity), None)
        spec = resource.manifest.get("spec") if resource is not None else None
        replicas = spec.get("replicas") if isinstance(spec, Mapping) else None
        if (
            resource is None
            or resource.api_version != "apps/v1"
            or resource.kind != "Deployment"
            or not isinstance(replicas, int)
            or isinstance(replicas, bool)
            or replicas < 0
            or target is None
            or target.observed.controller_owner_uid != owner_uid
            or target.observed.deleting
        ):
            raise ControllerError("deleted ScaledObject does not map to one exact fixed Deployment")
        expected = ScaleHandoffReceipt(
            version=1,
            deploymentUID=target.observed.uid,
            modelUID=owner_uid,
            modelGeneration=model_generation,
            scaler=ScaleHandoffScaler(
                apiVersion=scaler.observed.api_version,
                kind=scaler.observed.kind,
                namespace=scaler.observed.namespace,
                name=scaler.observed.name,
                uid=scaler.observed.uid,
                generation=scaler.generation,
            ),
        )
        annotations = _mapping(_metadata(target.raw).get("annotations"))
        if SCALE_HANDOFF_RECEIPT_ANNOTATION in annotations:
            retained = _controller_owned_scale_handoff_receipt(target.raw)
            if retained is None:
                raise ControllerError("Deployment scale handoff receipt is stale, malformed, or foreign")
            if retained == expected:
                continue
        pending.append((resource, target, scaler))
    return pending


def _fixed_scale_handoff_targets(
    render: RenderPlan | None,
    discovery: Discovery,
    owner_uid: str,
    model_generation: int,
) -> list[tuple[RenderedResource, ResourceSnapshot]]:
    if render is None:
        return []
    targets: list[tuple[RenderedResource, ResourceSnapshot]] = []
    for resource in render.resources:
        spec = resource.manifest.get("spec")
        replicas = spec.get("replicas") if isinstance(spec, Mapping) else None
        if (
            resource.api_version != "apps/v1"
            or resource.kind != "Deployment"
            or not isinstance(replicas, int)
            or isinstance(replicas, bool)
            or replicas < 0
        ):
            continue
        current = _resource_snapshot(
            discovery,
            resource.api_version,
            resource.kind,
            resource.namespace,
            resource.name,
        )
        if current is None or not current.replica_field_managers:
            continue
        if _fixed_scale_manager_owns_replicas(current):
            continue
        external = set(current.replica_field_managers) - {FIELD_MANAGER}
        if not external:
            continue
        if current.observed.controller_owner_uid != owner_uid or current.observed.deleting:
            raise ControllerError("fixed Deployment scale handoff target is not exclusively controller-owned")
        if not external.issubset(STALE_SCALE_FIELD_MANAGERS):
            raise ControllerError("Deployment replica field has a foreign manager")
        if not _autoscaler_scale_manager_owns_replicas(current):
            raise ControllerError(
                "fixed Deployment handoff lacks one canonical stale scale owner; manual migration is required"
            )
        receipt = _controller_owned_scale_handoff_receipt(current.raw)
        if (
            receipt is None
            or receipt.deployment_uid != current.observed.uid
            or receipt.model_uid != owner_uid
            or receipt.model_generation > model_generation
            or receipt.scaler.api_version != "keda.sh/v1alpha1"
            or receipt.scaler.kind != "ScaledObject"
            or receipt.scaler.namespace != resource.namespace
        ):
            raise ControllerError("fixed Deployment scale handoff lacks exact durable transition evidence")
        # A receipt can predate the current fixed revision only when the same
        # Deployment still exposes that receipt and stale scaler owner. The
        # generation-exact gate guard runs before this selector and must first
        # close any intervening autoscaled allowance. This is the crash-safe
        # post-delete reversal path; no new scaler evidence is invented.
        targets.append((resource, current))
    return targets


def _fixed_initialization_reversal_targets(
    render: RenderPlan | None,
    discovery: Discovery,
    owner_uid: str,
) -> list[tuple[RenderedResource, ResourceSnapshot, int]]:
    """Find fresh autoscaled zero bootstraps whose CR reversed to fixed."""

    if render is None:
        return []
    autoscaled_targets = {_rendered_identity(target) for _, target in _autoscaler_pairs(render)}
    targets: list[tuple[RenderedResource, ResourceSnapshot, int]] = []
    for resource in render.resources:
        spec = resource.manifest.get("spec")
        replicas = spec.get("replicas") if isinstance(spec, Mapping) else None
        if (
            resource.api_version != "apps/v1"
            or resource.kind != "Deployment"
            or not isinstance(replicas, int)
            or isinstance(replicas, bool)
            or replicas < 0
            or _rendered_identity(resource) in autoscaled_targets
        ):
            continue
        current = _resource_snapshot(
            discovery,
            resource.api_version,
            resource.kind,
            resource.namespace,
            resource.name,
        )
        if current is None:
            continue
        receipt = _controller_owned_scale_initialization_receipt(current.raw)
        if receipt is None or receipt.target_mode != "autoscaled":
            continue
        if (
            current.observed.controller_owner_uid != owner_uid
            or current.observed.deleting
            or receipt.deployment_uid != current.observed.uid
            or receipt.model_uid != owner_uid
        ):
            raise ControllerError("fixed initialization reversal identity changed")
        if (
            current.desired_replicas == 0
            and _fixed_scale_manager_owns_replicas(current)
            and _mapping(current.raw.get("spec")).get("paused") is not True
        ):
            targets.append((resource, current, replicas))
    return targets


def _receipt_backed_fixed_scale_targets(
    render: RenderPlan | None,
    discovery: Discovery,
    owner_uid: str,
    model_generation: int,
) -> list[tuple[RenderedResource, ResourceSnapshot]]:
    """Return every fixed target whose past autoscaled transition is durable.

    Receipts intentionally survive later fixed revisions. That makes the
    complete targetRef scan a steady-state safety property, including after a
    controller crash between the exceptional /scale write and its postcheck.
    """

    if render is None:
        return []
    autoscaled_targets = {_rendered_identity(target) for _, target in _autoscaler_pairs(render)}
    targets: list[tuple[RenderedResource, ResourceSnapshot]] = []
    for resource in render.resources:
        spec = resource.manifest.get("spec")
        replicas = spec.get("replicas") if isinstance(spec, Mapping) else None
        if (
            resource.api_version != "apps/v1"
            or resource.kind != "Deployment"
            or not isinstance(replicas, int)
            or isinstance(replicas, bool)
            or replicas < 0
            or _rendered_identity(resource) in autoscaled_targets
        ):
            continue
        current = _resource_snapshot(
            discovery,
            resource.api_version,
            resource.kind,
            resource.namespace,
            resource.name,
        )
        if current is None:
            continue
        annotations = _mapping(_metadata(current.raw).get("annotations"))
        receipt = _controller_owned_scale_authorization_receipt(current.raw)
        if receipt is None:
            if (
                SCALE_HANDOFF_RECEIPT_ANNOTATION in annotations
                or SCALE_INITIALIZATION_RECEIPT_ANNOTATION in annotations
            ):
                raise ControllerError("fixed Deployment scale handoff receipt is malformed or foreign")
            if _fixed_scale_manager_owns_replicas(current):
                raise ControllerError("dedicated fixed scale owner lacks a durable transition receipt")
            continue
        if (
            current.observed.controller_owner_uid != owner_uid
            or current.observed.deleting
            or receipt.deployment_uid != current.observed.uid
            or receipt.model_uid != owner_uid
            or receipt.model_generation > model_generation
        ):
            raise ControllerError("receipt-backed fixed Deployment identity changed")
        if isinstance(receipt, ScaleHandoffReceipt):
            if (
                receipt.scaler.api_version != "keda.sh/v1alpha1"
                or receipt.scaler.kind != "ScaledObject"
                or receipt.scaler.namespace != resource.namespace
            ):
                raise ControllerError("receipt-backed fixed Deployment scaler identity changed")
        elif receipt.target_mode != "fixed":
            raise ControllerError("fixed Deployment has an autoscaled initialization receipt")
        targets.append((resource, current))
    return targets


def _controller_fixed_scale_updates(
    render: RenderPlan | None,
    discovery: Discovery,
    owner_uid: str,
    model_generation: int,
) -> list[tuple[RenderedResource, ResourceSnapshot, int]]:
    if render is None:
        return []
    updates: list[tuple[RenderedResource, ResourceSnapshot, int]] = []
    for resource in render.resources:
        spec = resource.manifest.get("spec")
        replicas = spec.get("replicas") if isinstance(spec, Mapping) else None
        if (
            resource.api_version != "apps/v1"
            or resource.kind != "Deployment"
            or not isinstance(replicas, int)
            or isinstance(replicas, bool)
            or replicas < 0
        ):
            continue
        current = _resource_snapshot(
            discovery,
            resource.api_version,
            resource.kind,
            resource.namespace,
            resource.name,
        )
        if current is None or not _fixed_scale_manager_owns_replicas(current):
            continue
        if current.observed.controller_owner_uid != owner_uid or current.observed.deleting:
            raise ControllerError("controller scale target lost its exact ModelDeployment owner")
        receipt = _controller_owned_scale_authorization_receipt(current.raw)
        if (
            receipt is None
            or receipt.deployment_uid != current.observed.uid
            or receipt.model_uid != owner_uid
            or receipt.model_generation > model_generation
        ):
            raise ControllerError("controller fixed scale update lacks exact durable transition evidence")
        if isinstance(receipt, ScaleHandoffReceipt):
            if (
                receipt.scaler.api_version != "keda.sh/v1alpha1"
                or receipt.scaler.kind != "ScaledObject"
                or receipt.scaler.namespace != resource.namespace
            ):
                raise ControllerError("controller fixed scale update scaler identity changed")
        elif receipt.target_mode != "fixed":
            raise ControllerError("controller fixed scale update has an autoscaled initialization receipt")
        if current.desired_replicas != replicas:
            updates.append((resource, current, replicas))
    return updates


def _observed_hot_floor(discovery: Discovery) -> int:
    """Return the exact fixed-hot boundary already owned by this model.

    A drain may preserve autoscaled replicas through KEDA's minimum, but fixed
    hot Deployments have a separate replica owner. Losing this boundary while
    active operations exist would replace or delete serving Pods mid-drain.
    """

    total = 0
    for item in discovery.resources:
        if item.observed.kind != "Deployment":
            continue
        annotations = _mapping(_metadata(item.raw).get("annotations"))
        if annotations.get(WORKLOAD_ROLE_ANNOTATION) != "hot":
            continue
        if item.desired_replicas is None:
            raise ControllerError("observed hot Deployment replica count is unavailable during drain")
        total += item.desired_replicas
    return total


def _deployment_rollout_complete(item: ResourceSnapshot) -> bool:
    desired = item.desired_replicas
    return (
        desired is not None
        and item.observed_generation == item.generation
        and item.replicas == desired
        and item.updated_replicas == desired
        and item.ready_replicas == desired
        and item.available_replicas == desired
        and item.unavailable_replicas == 0
    )


def _residency_holder_ready(
    desired: RenderedResource,
    observed: ResourceSnapshot | None,
    owner_uid: str,
) -> bool:
    """Require a non-empty, current DaemonSet whose receipt-backed probe is Ready."""

    if observed is None:
        return False
    desired_nodes = observed.desired_replicas
    return bool(
        desired_nodes is not None
        and desired_nodes > 0
        and observed.observed.controller_owner_uid == owner_uid
        and FIELD_MANAGER in observed.observed.field_managers
        and observed.observed.digest == desired.digest
        and observed.observed_generation == observed.generation
        and observed.replicas == desired_nodes
        and observed.updated_replicas == desired_nodes
        and observed.ready_replicas == desired_nodes
        and observed.available_replicas == desired_nodes
        and observed.unavailable_replicas == 0
    )


def _pod_phase_counts(pods: list[PodSnapshot]) -> dict[str, int]:
    active = [pod for pod in pods if not pod.deleting]
    return {
        "admitted": sum(pod.scheduled for pod in active),
        "nodePending": sum(not pod.scheduled and pod.phase not in {"Failed", "Succeeded"} for pod in active),
        "localizing": sum(
            pod.scheduled and not pod.initialized and pod.phase not in {"Failed", "Succeeded"} for pod in active
        ),
        "runtimeStarting": sum(
            pod.initialized
            and not pod.containers_started
            and not pod.ready
            and pod.phase not in {"Failed", "Succeeded"}
            for pod in active
        ),
        "warming": sum(
            pod.initialized and pod.containers_started and not pod.ready and pod.phase not in {"Failed", "Succeeded"}
            for pod in active
        ),
        "ready": sum(pod.ready for pod in active),
        "failed": sum(pod.phase == "Failed" for pod in active),
    }


def _cache_status(
    *,
    spec: ModelDeploymentSpec,
    phase_counts: Mapping[str, int],
    desired_replicas: int | None,
    previous_status: Mapping[str, Any],
    observed_at: datetime,
) -> dict[str, Any] | None:
    if spec.cache.tier.value == "Disabled":
        return None
    tier = spec.cache.tier.value
    digest = spec.artifact.manifest_digest
    previous = _mapping(previous_status.get("cache"))
    if phase_counts["failed"] > 0:
        return {"state": "Failed", "tier": tier, "digest": digest, "observedAt": _timestamp(observed_at)}
    if phase_counts["localizing"] > 0:
        return {
            "state": "Localizing",
            "tier": tier,
            "digest": digest,
            "observedAt": _timestamp(observed_at),
        }
    if phase_counts["ready"] > 0:
        return {"state": "Cached", "tier": tier, "digest": digest, "observedAt": _timestamp(observed_at)}
    if (
        desired_replicas == 0
        and previous.get("state") == "Cached"
        and previous.get("tier") == tier
        and previous.get("digest") == digest
        and isinstance(previous.get("observedAt"), str)
    ):
        return dict(previous)
    return {"state": "Unknown", "tier": tier, "digest": digest}


def _previous_automatic_state(previous: Mapping[str, Any]) -> AutomaticFastStartState | None:
    detail = _mapping(previous.get("automatic"))
    assigned = previous.get("assignedLevel")
    if not isinstance(assigned, str) or assigned not in {level.value for level in FastStartLevel}:
        return None
    pending = detail.get("pendingLevel")
    pending_level = FastStartLevel(pending) if pending in {level.value for level in FastStartLevel} else None
    pending_since = _parse_timestamp(detail.get("pendingSince"))
    last_transition = _parse_timestamp(detail.get("lastTransitionAt"))
    wins = detail.get("consecutiveWins", 0)
    if not isinstance(wins, int) or wins < 0 or (pending_level is None) != (pending_since is None):
        return None
    try:
        return AutomaticFastStartState(
            assigned_level=FastStartLevel(assigned),
            pending_level=pending_level,
            pending_since=pending_since,
            consecutive_wins=wins,
            last_transition_at=last_transition,
        )
    except ValueError:
        return None


def _automatic_fast_start_assessment(
    *,
    spec: ModelDeploymentSpec,
    envelope: InfrastructureEnvelope | None,
    assessment: FastStartAssessment,
    converged: bool,
    previous: Mapping[str, Any],
    history: tuple[FastStartHistoryWindow, FastStartHistoryWindow] | None,
    now: datetime,
    mechanism_decision: FastStartMechanismDecision,
) -> tuple[FastStartAssessment, FastStartAutomaticStatus | None]:
    if spec.fast_start.mode is not FastStartMode.AUTOMATIC or envelope is None:
        return assessment, None
    assert spec.fast_start.minimum_level is not None and spec.fast_start.maximum_level is not None
    previous_detail_raw = _mapping(previous.get("automatic"))
    previous_detail: FastStartAutomaticStatus | None = None
    if previous_detail_raw:
        try:
            previous_detail = FastStartAutomaticStatus.model_validate(previous_detail_raw)
        except ValueError:
            previous_detail = None
    prior_state = _previous_automatic_state(previous)
    effective_mechanism = mechanism_decision.mechanism.value
    if (
        previous_detail is not None
        and prior_state is not None
        and previous_detail.evaluated_at <= now
        and now - previous_detail.evaluated_at < timedelta(minutes=5)
        and spec.fast_start.minimum_level.rank <= prior_state.assigned_level.rank <= spec.fast_start.maximum_level.rank
        and prior_state.assigned_level.rank <= assessment.qualified_level.rank
        and converged
        and history is not None
        and previous_detail.mechanism_id == effective_mechanism
    ):
        retained_level = prior_state.assigned_level
        qualification = FastStartQualification(
            state=(
                FastStartQualificationState.NO_TARGET
                if retained_level is FastStartLevel.OFF
                else FastStartQualificationState.QUALIFIED
            ),
            reason=previous_detail.reason,
            message=(
                "automatic policy assigned Off; no model-start target is claimed"
                if retained_level is FastStartLevel.OFF
                else f"automatic policy retains qualified {retained_level.value} until its next five-minute evaluation"
            ),
        )
        return (
            FastStartAssessment.model_validate(
                {
                    **assessment.model_dump(),
                    "assigned_level": retained_level,
                    "target_seconds": retained_level.target_seconds,
                    "qualification": qualification,
                }
            ),
            previous_detail,
        )
    pool_paths = [pool.paths for pool in assessment.pools]
    common_mechanisms = (
        set.intersection(*({path.mechanism for path in paths} for paths in pool_paths))
        if pool_paths and all(pool_paths)
        else set()
    )
    common_mechanisms.intersection_update({effective_mechanism})
    paths: list[FastStartPath] = []
    for mechanism in sorted(common_mechanisms):
        selected_paths = [
            min(
                (path for path in pool.paths if path.mechanism == mechanism),
                key=lambda path: (
                    -path.qualified_level.rank,
                    float("inf")
                    if path.model_start is None or path.model_start.p95_seconds is None
                    else path.model_start.p95_seconds,
                    path.compatibility_tuple_digest,
                ),
            )
            for pool in assessment.pools
        ]
        binding = min(
            selected_paths,
            key=lambda path: (
                path.qualified_level.rank,
                -(
                    0.0
                    if path.model_start is None or path.model_start.p95_seconds is None
                    else path.model_start.p95_seconds
                ),
            ),
        )
        statistics = binding.model_start
        paths.append(
            FastStartPath(
                mechanism_id=mechanism,
                qualified_level=binding.qualified_level,
                ready=converged,
                qualification_current=statistics is not None,
                qualified_p95_model_start_seconds=(None if statistics is None else statistics.p95_seconds),
                successful_attempts=(0 if statistics is None else statistics.sample_count - statistics.failed_count),
                failed_attempts=0 if statistics is None else statistics.failed_count,
                hourly_cost=envelope.fast_start_mechanism_hourly_costs.get(mechanism, 0.0),
            )
        )
    if not paths:
        paths.append(
            FastStartPath(
                mechanism_id=effective_mechanism,
                qualified_level=FastStartLevel.OFF,
                ready=converged,
                qualification_current=False,
                qualified_p95_model_start_seconds=None,
                successful_attempts=0,
                failed_attempts=0,
                hourly_cost=envelope.fast_start_mechanism_hourly_costs.get(effective_mechanism, 0.0),
            )
        )
    policy = AutomaticFastStartPolicy(
        minimum_level=spec.fast_start.minimum_level,
        maximum_level=spec.fast_start.maximum_level,
        wait_second_value=envelope.fast_start_wait_second_value,
        fallback_policy=spec.fast_start.fallback_policy,
    )
    short_history = None if history is None else history[0]
    long_history = None if history is None else history[1]
    decision = evaluate_automatic_fast_start(
        policy=policy,
        paths=paths,
        short_history=short_history,
        long_history=long_history,
        prior_state=prior_state,
        now=now,
    )
    chosen_mechanism = effective_mechanism
    selected_statistics = None
    selected_capacity_wait = None
    selected_end_to_end = None
    selected_identity_digest = None
    if chosen_mechanism is not None:
        selected_pool_paths: list[FastStartPathAssessment] = []
        for pool in assessment.pools:
            candidates = [path for path in pool.paths if path.mechanism == chosen_mechanism]
            if not candidates:
                selected_pool_paths = []
                break
            selected_pool_paths.append(
                min(
                    candidates,
                    key=lambda path: (
                        -path.qualified_level.rank,
                        float("inf")
                        if path.model_start is None or path.model_start.p95_seconds is None
                        else path.model_start.p95_seconds,
                        path.compatibility_tuple_digest,
                    ),
                )
            )
        if selected_pool_paths:
            binding_path = min(
                selected_pool_paths,
                key=lambda path: (
                    path.qualified_level.rank,
                    -(
                        0.0
                        if path.model_start is None or path.model_start.p95_seconds is None
                        else path.model_start.p95_seconds
                    ),
                ),
            )
            selected_statistics = binding_path.model_start
            selected_capacity_wait = binding_path.capacity_wait
            selected_end_to_end = binding_path.end_to_end
            selected_identity_digest = binding_path.identity_digest
    selected = decision.assigned_level if decision.satisfied else decision.fallback_level
    if selected is None:
        qualification = FastStartQualification(
            state=FastStartQualificationState.UNQUALIFIED,
            reason="AutomaticTargetUnavailable",
            message=f"automatic policy {decision.reason.value} has no qualified path inside the required bounds",
        )
    elif selected is FastStartLevel.OFF and decision.satisfied:
        qualification = FastStartQualification(
            state=FastStartQualificationState.NO_TARGET,
            reason=decision.reason.value,
            message="automatic policy assigned Off; no model-start target is claimed",
        )
    elif selected.rank < spec.fast_start.minimum_level.rank:
        qualification = FastStartQualification(
            state=FastStartQualificationState.FALLBACK,
            reason=decision.reason.value,
            message=f"automatic policy fell back to qualified {selected.value} below the configured minimum",
        )
    else:
        qualification = FastStartQualification(
            state=FastStartQualificationState.QUALIFIED,
            reason=decision.reason.value,
            message=(f"automatic policy assigned qualified {selected.value} from rolling demand and mechanism cost"),
        )
    updated = FastStartAssessment.model_validate(
        {
            **assessment.model_dump(),
            "assigned_level": selected,
            "target_seconds": None if selected is None else selected.target_seconds,
            "qualification": qualification,
            "model_start": selected_statistics,
            "capacity_wait": selected_capacity_wait,
            "end_to_end": selected_end_to_end,
            "selected_identity_digest": selected_identity_digest,
        }
    )
    automatic = FastStartAutomaticStatus(
        reason=decision.reason.value,
        evaluated_at=now,
        history_complete=history is not None,
        mechanism_id=effective_mechanism,
        score=decision.score,
        pending_level=decision.state.pending_level,
        pending_since=decision.state.pending_since,
        consecutive_wins=decision.state.consecutive_wins,
        last_transition_at=decision.state.last_transition_at,
        short_window_requests=0 if short_history is None else short_history.request_count,
        short_window_cold_activations=0 if short_history is None else short_history.cold_activation_count,
        short_window_idle_gap_episodes=0 if short_history is None else short_history.idle_gap_episode_count,
        long_window_requests=0 if long_history is None else long_history.request_count,
        long_window_cold_activations=0 if long_history is None else long_history.cold_activation_count,
        long_window_idle_gap_episodes=0 if long_history is None else long_history.idle_gap_episode_count,
    )
    return updated, automatic


def _fast_start_status(
    *,
    spec: ModelDeploymentSpec,
    envelope: InfrastructureEnvelope | None,
    assessment: FastStartAssessment | None,
    converged: bool,
    ready_replicas: int | None,
    previous_status: Mapping[str, Any],
    history: tuple[FastStartHistoryWindow, FastStartHistoryWindow] | None,
    now: datetime,
    mechanism_decision: FastStartMechanismDecision,
    host_residency_pool_refs: set[str] | None = None,
    host_residency_ready: bool | None = None,
) -> FastStartStatus | None:
    """Add what only observed runtime can tell to the deterministic policy outcome.

    The effective level is claimed only once the desired render has converged.
    Until then the previously effective level, if any, is carried forward so a
    rollout never advertises a startup class it has not reached.
    """

    if assessment is None:
        return None
    mechanism_converged = converged and (
        mechanism_decision.mechanism is not FastStartMechanism.HOST_MEMORY_RESIDENCY or host_residency_ready is True
    )
    previous = _mapping(previous_status.get("fastStart"))
    assessment, automatic = _automatic_fast_start_assessment(
        spec=spec,
        envelope=envelope,
        assessment=assessment,
        converged=mechanism_converged,
        previous=previous,
        history=history,
        now=now,
        mechanism_decision=mechanism_decision,
    )
    effective: FastStartLevel | None = None
    effective_identity_digest: str | None = None
    if mechanism_converged and assessment.assigned_level is not None:
        effective = assessment.assigned_level
        effective_identity_digest = assessment.selected_identity_digest
    else:
        carried = previous.get("effectiveLevel")
        carried_identity = previous.get("effectiveIdentityDigest")
        if (
            isinstance(carried, str)
            and carried in {level.value for level in FastStartLevel}
            and isinstance(carried_identity, str)
            and carried_identity == assessment.selected_identity_digest
        ):
            effective = FastStartLevel(carried)
            effective_identity_digest = carried_identity
    mechanisms: dict[str, FastStartMechanismStatus] = {}
    qualification = envelope.qualifications.get(spec.model_ref) if envelope is not None else None
    if qualification is not None and qualification.model_express is not None:
        configured = qualification.model_express
        mechanisms["modelexpress"] = FastStartMechanismStatus(
            state="Configured" if converged else "Pending",
            config_digest=configured.config_digest,
            deployment_mode=configured.deployment_mode,
            endpoint=configured.endpoint,
            metadata_backend=configured.metadata_backend,
            runtime_adapter=configured.runtime_adapter,
            client_package_version=configured.client_package_version,
            coordinator_network_type=configured.coordinator_network_type,
            coordinator_namespace=configured.coordinator_namespace,
            coordinator_pod_labels=configured.coordinator_pod_labels,
            coordinator_cidrs=configured.coordinator_cidrs,
            pool_refs=configured.pool_refs,
            pool_transports={
                pool_ref: FastStartMechanismPoolTransport(
                    mode=transport.mode,
                    rdma_resource_name=transport.rdma_resource_name,
                    rdma_resource_quantity=transport.rdma_resource_quantity,
                    nixl_backend=transport.nixl_backend,
                    rdma_nic_pin=transport.rdma_nic_pin,
                )
                for pool_ref, transport in configured.pool_transports.items()
            },
            configuration_observed=converged,
            # Upstream 0.5.1 does not expose a qualified per-ModelDeployment
            # transfer-path record. Keep these values unavailable rather than
            # inferring them from readiness.
            telemetry_state="Unavailable",
        )
    cache_mechanisms: dict[str, FastStartCacheMechanismStatus] = {}
    if envelope is not None:
        placement_pools = {
            pool_ref: envelope.pools[pool_ref].node_selector
            for pool_ref in sorted(spec.placement.pool_refs)
            if pool_ref in envelope.pools
        }
        placement_pool_max_nodes = {
            pool_ref: envelope.pools[pool_ref].max_nodes
            for pool_ref in sorted(spec.placement.pool_refs)
            if pool_ref in envelope.pools
        }
        placement_pool_memory = {
            pool_ref: envelope.pools[pool_ref].allocatable_memory_bytes
            for pool_ref in sorted(spec.placement.pool_refs)
            if pool_ref in envelope.pools and envelope.pools[pool_ref].allocatable_memory_bytes is not None
        }
        storage_contract_digests = {
            item.pool_ref: item.storage_contract_digest
            for item in (qualification.fast_start_runtime_contracts if qualification is not None else [])
            if item.runtime.runtime_image == spec.runtime.image
            and item.runtime.template_digest == spec.runtime.template_ref.digest
        }
        declarations = {}
        if qualification is not None:
            for candidate in DECLARED_MECHANISMS:
                declaration = qualification.mechanism_declaration(candidate)
                if declaration is not None:
                    declarations[candidate] = declaration
        cache_mechanisms = project_cache_mechanisms(
            selected=mechanism_decision.mechanism,
            declarations=declarations,
            pools=placement_pools,
            pool_allocatable_memory_bytes=placement_pool_memory,
            pool_max_nodes=placement_pool_max_nodes,
            host_residency_pool_refs=host_residency_pool_refs,
            host_residency_ready=host_residency_ready,
            storage_contract_digests=storage_contract_digests,
            converged=converged,
            configured_hot_replicas=effective_hot_floor(spec.availability, at=now),
            configured_max_replicas=spec.availability.max_replicas,
            mechanism_config_digest=mechanism_config_digest,
        )
    return FastStartStatus(
        **assessment.model_dump(),
        effective_level=effective,
        effective_identity_digest=effective_identity_digest,
        hot=None if ready_replicas is None else ready_replicas > 0,
        automatic=automatic,
        mechanisms=mechanisms,
        cache_mechanisms=cache_mechanisms,
    )


def build_status(
    *,
    spec: ModelDeploymentSpec,
    owner_uid: str,
    generation: int,
    plan: ReconcilePlan,
    discovery: Discovery,
    previous_status: Mapping[str, Any],
    drain: DrainObservation | None,
    now: datetime | None = None,
    envelope: InfrastructureEnvelope | None = None,
    fast_start_history: tuple[FastStartHistoryWindow, FastStartHistoryWindow] | None = None,
) -> dict[str, Any]:
    """Project only observed state; desired state alone never becomes Ready."""

    observed_at = now or _utc_now()
    previous_observed_at = _parse_timestamp(previous_status.get("lastReconcileTime"))
    if previous_observed_at is not None and observed_at <= previous_observed_at:
        observed_at = previous_observed_at + timedelta(microseconds=1)
    old = _old_conditions(previous_status)
    deployments = [item for item in discovery.resources if item.observed.kind == "Deployment"]
    desired = _known_total([item.desired_replicas for item in deployments])
    replicas = _known_total([item.replicas for item in deployments])
    ready = _known_total([item.ready_replicas for item in deployments])
    available = _known_total([item.available_replicas for item in deployments])
    phase_counts = _pod_phase_counts(discovery.pods)
    rollout_complete = bool(deployments) and all(_deployment_rollout_complete(item) for item in deployments)
    # Serving availability and total elastic-capacity convergence are separate.
    # A burst Deployment may change replicas before its controller observes the
    # generation or creates a Pod. That must not withdraw an unchanged, fully
    # observed hot Deployment. Each serving candidate still needs its exact
    # rollout, and `converged` below fences the complete desired inventory.
    serving_rollout_ready = any(
        _deployment_rollout_complete(item) and item.ready_replicas is not None and item.ready_replicas > 0
        for item in deployments
    )
    autoscaled_identities = {_rendered_identity(target) for _, target in _autoscaler_pairs(plan.render)}
    fixed_serving_rollout_ready = any(
        item.observed.identity not in autoscaled_identities
        and _deployment_rollout_complete(item)
        and item.ready_replicas is not None
        and item.ready_replicas > 0
        for item in deployments
    )
    # HPA can acknowledge scale-to-zero before ScaledObject copies its condition.
    # That asynchronous acknowledgement must not withdraw a separate fixed hot
    # runtime. Require the exact scaler/HPA ownership, target, generation and
    # released replica field below; bootstrap and all-cold status stay strict.
    autoscaler_handoff_complete = _autoscaler_handoff_complete(
        plan.render,
        discovery,
        owner_uid,
        allow_idle_acknowledgement_lag=fixed_serving_rollout_ready,
    )
    desired_resources = {
        f"{item.api_version}/{item.kind}/{item.namespace}/{item.name}": item
        for item in (plan.render.resources if plan.render is not None else [])
    }
    observed_by_identity = {item.observed.identity: item.observed for item in discovery.resources}
    snapshots_by_identity = {item.observed.identity: item for item in discovery.resources}
    converged = bool(desired_resources) and all(
        identity in observed_by_identity
        and observed_by_identity[identity].controller_owner_uid == owner_uid
        and FIELD_MANAGER in observed_by_identity[identity].field_managers
        and observed_by_identity[identity].digest == resource.digest
        for identity, resource in desired_resources.items()
    )

    terminal = "Progressing"
    reason = "Reconciling"
    message = "controller is reconciling the observed resource inventory"
    phase = "Desired"
    if plan.action is ReconcileAction.INFRASTRUCTURE_REQUIRED:
        terminal, reason, phase = "InfrastructureRequired", "TerraformEnvelopeRequired", "InfrastructureRequired"
        message = "requested placement or capability is outside the Terraform-owned envelope"
    elif plan.action is ReconcileAction.REJECT:
        terminal, reason, phase = "Failed", "ValidationRejected", "Failed"
        message = "desired state failed controller validation or ownership checks"
    elif spec.lifecycle.desired_state in {DesiredState.DRAINING, DesiredState.DISABLED} or drain is not None:
        if drain is not None and drain.complete:
            terminal, reason, phase = "Cold", "DrainComplete", "Cold"
            message = "publication is withdrawn and observed runtime demand is zero"
        else:
            terminal, reason, phase = "Draining", "DrainInProgress", "Draining"
            message = "publication, active-operation, or zero-replica observation is incomplete"
    elif converged and serving_rollout_ready and autoscaler_handoff_complete:
        terminal, reason, phase = "Ready", "RuntimeObservedReady", "Ready"
        message = "at least one controller-owned runtime replica is observed ready"
    elif converged and rollout_complete and autoscaler_handoff_complete and desired == 0 and replicas == 0:
        terminal, reason, phase = "Cold", "ScaleToZeroObserved", "Cold"
        message = "enabled model is reconciled and currently scaled to zero"
    elif phase_counts["nodePending"] > 0:
        terminal, reason, phase = "Progressing", "RuntimeNodePending", "NodePending"
        message = "runtime Pods are waiting for a schedulable node"
    elif phase_counts["localizing"] > 0:
        terminal, reason, phase = "Loading", "ArtifactLocalizing", "Localizing"
        message = "runtime Pods are localizing the qualified model artifact"
    elif phase_counts["runtimeStarting"] > 0:
        terminal, reason, phase = "Loading", "RuntimeStarting", "RuntimeStarting"
        message = "runtime containers are waiting to start"
    elif phase_counts["warming"] > 0:
        terminal, reason, phase = "Loading", "RuntimeWarming", "Warming"
        message = "runtime containers are running but readiness has not converged"
    elif replicas is not None and ready is not None and replicas > ready:
        terminal, reason, phase = "Loading", "RuntimeStarting", "RuntimeStarting"
        message = "runtime replicas exist but readiness has not converged"

    cache = _cache_status(
        spec=spec,
        phase_counts=phase_counts,
        desired_replicas=desired,
        previous_status=previous_status,
        observed_at=observed_at,
    )
    condition_types = ("Ready", "Cold", "Loading", "Draining", "InfrastructureRequired", "Failed", "Progressing")
    conditions = [
        _condition(
            condition_type,
            "True" if condition_type == terminal else "False",
            reason if condition_type == terminal else f"Not{condition_type}",
            message if condition_type == terminal else f"{condition_type} is not the current observed state",
            generation,
            previous=old.get(condition_type),
            now=observed_at,
        )
        for condition_type in condition_types
    ]
    raw_cache_state = cache.get("state") if cache is not None else None
    cache_state = raw_cache_state if isinstance(raw_cache_state, str) else None
    cache_conditions: dict[str, tuple[Literal["True", "False", "Unknown"], str, str]] = {
        "Cached": ("True", "ArtifactCacheObserved", "qualified artifact cache was observed through a ready runtime"),
        "Missing": ("False", "ArtifactCacheMissing", "qualified artifact cache is missing"),
        "Failed": ("False", "ArtifactCacheFailed", "artifact cache preparation failed"),
        "Localizing": ("Unknown", "ArtifactCacheLocalizing", "artifact cache preparation is in progress"),
    }
    cache_condition = cache_conditions.get(
        cache_state or "",
        ("Unknown", "ArtifactCacheObservationUnavailable", "artifact cache state is not observed"),
    )
    conditions.append(
        _condition(
            "Cached",
            cache_condition[0],
            cache_condition[1],
            cache_condition[2],
            generation,
            previous=old.get("Cached"),
            now=observed_at,
        )
    )
    host_residency_pool_refs: set[str] | None = None
    host_residency_ready: bool | None = None
    if plan.render is not None:
        holder_resources = [
            item
            for item in plan.render.resources
            if item.kind == "DaemonSet"
            and _mapping(_mapping(item.manifest.get("metadata")).get("annotations")).get(MECHANISM_ANNOTATION)
            == FastStartMechanism.HOST_MEMORY_RESIDENCY.value
        ]
        host_residency_pool_refs = {
            pool_ref
            for item in holder_resources
            if isinstance(
                pool_ref := _mapping(_mapping(item.manifest.get("metadata")).get("annotations")).get(
                    WORKLOAD_POOL_ANNOTATION
                ),
                str,
            )
        }
        if plan.validation.fast_start_mechanism.mechanism is FastStartMechanism.HOST_MEMORY_RESIDENCY:
            host_residency_ready = (
                host_residency_pool_refs == set(spec.placement.pool_refs)
                and bool(holder_resources)
                and all(
                    _residency_holder_ready(item, snapshots_by_identity.get(_rendered_identity(item)), owner_uid)
                    for item in holder_resources
                )
            )
    fast_start = _fast_start_status(
        spec=spec,
        envelope=envelope,
        assessment=plan.validation.fast_start,
        converged=converged,
        ready_replicas=ready,
        previous_status=previous_status,
        history=fast_start_history,
        now=observed_at,
        mechanism_decision=plan.validation.fast_start_mechanism,
        host_residency_pool_refs=host_residency_pool_refs,
        host_residency_ready=host_residency_ready,
    )
    if fast_start is not None:
        satisfied = fast_start.qualification.state in {
            FastStartQualificationState.NO_TARGET,
            FastStartQualificationState.QUALIFIED,
        }
        conditions.append(
            _condition(
                "FastStartQualified",
                "True" if satisfied else "False",
                fast_start.qualification.reason,
                fast_start.qualification.message,
                generation,
                previous=old.get("FastStartQualified"),
                now=observed_at,
            )
        )
    resources = [
        {
            "identity": item.observed.identity,
            "apiVersion": item.observed.api_version,
            "kind": item.observed.kind,
            "namespace": item.observed.namespace,
            "name": item.observed.name,
            "uid": item.observed.uid,
            "generation": item.generation,
            "digest": item.observed.digest,
        }
        for item in discovery.resources
        if item.observed.controller_owner_uid is not None
    ]
    placements: list[dict[str, Any]] = []
    for item in sorted(deployments, key=lambda value: value.observed.name):
        annotations = _mapping(_metadata(item.raw).get("annotations"))
        pool_ref = annotations.get(WORKLOAD_POOL_ANNOTATION)
        role = annotations.get(WORKLOAD_ROLE_ANNOTATION)
        if not isinstance(pool_ref, str) or role not in {"hot", "burst"}:
            continue
        placement: dict[str, Any] = {
            "deploymentName": item.observed.name,
            "poolRef": pool_ref,
            "role": role,
        }
        for field, value in (
            ("desired", item.desired_replicas),
            ("ready", item.ready_replicas),
            ("available", item.available_replicas),
        ):
            if value is not None:
                placement[field] = value
        placements.append(placement)
    status: dict[str, Any] = {
        "observedGeneration": generation,
        "phase": phase,
        "specDigest": plan.spec_digest,
        "activeRevision": spec.artifact.revision,
        "replicas": {
            "desired": desired,
            "admitted": phase_counts["admitted"],
            "nodePending": phase_counts["nodePending"],
            "localizing": phase_counts["localizing"],
            "runtimeStarting": phase_counts["runtimeStarting"],
            "warming": phase_counts["warming"],
            "ready": ready,
            "available": available,
        },
        "resources": resources,
        "retryCount": 0,
        "lastReconcileTime": _timestamp(observed_at),
        "conditions": conditions,
    }
    if plan.validation.disposition is ValidationDisposition.ACCEPTED:
        status["eligiblePoolRefs"] = sorted(spec.placement.pool_refs)
    if placements:
        status["placements"] = placements
    if cache is not None:
        status["cache"] = cache
    if fast_start is not None:
        # Unavailable measurements are omitted rather than serialised as zero.
        status["fastStart"] = fast_start.model_dump(mode="json", by_alias=True, exclude_none=True)
    if plan.validation.admitted_pool_ref is not None:
        status["admittedPoolRef"] = plan.validation.admitted_pool_ref
    if plan.render is not None:
        status["renderDigest"] = plan.render.render_digest
        endpoint = plan.render.endpoint
        snapshot = next(
            (item for item in discovery.resources if item.observed.identity == endpoint.identity),
            None,
        )
        rendered_service = next(
            (item for item in plan.render.resources if item.kind == "Service" and item.name == endpoint.service_name),
            None,
        )
        if (
            snapshot is not None
            and rendered_service is not None
            and snapshot.observed.controller_owner_uid == owner_uid
            and FIELD_MANAGER in snapshot.observed.field_managers
            and snapshot.observed.digest == rendered_service.digest
        ):
            status["endpoint"] = {
                "namespace": endpoint.namespace,
                "serviceName": endpoint.service_name,
                "servicePort": endpoint.service_port,
                "uid": snapshot.observed.uid,
                "digest": snapshot.observed.digest,
            }
        publication_resources = [
            item
            for item in plan.render.resources
            if item.kind == "ConfigMap"
            and _mapping(item.manifest.get("metadata")).get("labels", {}).get("fs2-serve.nebius.ai/component")
            == "publication-intent"
        ]
        if converged and len(publication_resources) <= 1:
            # This is the exact controller-observed publication intent. The
            # runtime bridge still performs its independent Ready/Cold,
            # policy, endpoint and registry fencing before exposing a route.
            status["publication"] = {
                "openAI": spec.exposure.open_ai if publication_resources else False,
                "mcp": spec.exposure.mcp if publication_resources else False,
                "observedAt": _timestamp(observed_at),
            }
    if plan.action is ReconcileAction.INFRASTRUCTURE_REQUIRED:
        status["infrastructureHandoff"] = {
            "reason": message,
            "owner": "Terraform",
            "requiredInputs": plan.validation.terraform_inputs,
        }
    status["adoption"] = {
        "state": {
            AdoptionMode.NONE: "None",
            AdoptionMode.OBSERVE: "ObserveOnly",
            AdoptionMode.CLAIM: "Owned" if converged else "Claiming",
        }[spec.adoption.mode]
    }
    if spec.adoption.receipt_ref is not None:
        status["adoption"]["receiptDigest"] = spec.adoption.receipt_ref.digest
    return status


class ActiveOperationsReader(Protocol):
    async def active_operations(self, *, tenant_id: str, model_ref: str) -> int | None: ...

    async def fast_start_history(
        self,
        *,
        model_ref: str,
        idle_seconds: int,
        now: datetime,
    ) -> tuple[FastStartHistoryWindow, FastStartHistoryWindow] | None: ...


class UnknownActiveOperations:
    async def active_operations(self, *, tenant_id: str, model_ref: str) -> int | None:
        return None

    async def fast_start_history(
        self,
        *,
        model_ref: str,
        idle_seconds: int,
        now: datetime,
    ) -> tuple[FastStartHistoryWindow, FastStartHistoryWindow] | None:
        return None


class PostgresActiveOperations:
    """Read drain demand under the same per-model fence as admission."""

    def __init__(self, pool: asyncpg.Pool[Any], *, owns_pool: bool = False) -> None:
        self.pool = pool
        self.owns_pool = owns_pool

    @classmethod
    async def connect(cls, database_url: str, *, maximum_connections: int = 2) -> PostgresActiveOperations:
        dsn = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=1,
            max_size=max(1, maximum_connections),
            command_timeout=30,
            server_settings={"application_name": "fs2-model-controller"},
        )
        assert pool is not None
        return cls(pool, owns_pool=True)

    async def close(self) -> None:
        if self.owns_pool:
            await self.pool.close()

    async def active_operations(self, *, tenant_id: str, model_ref: str) -> int | None:
        del tenant_id
        try:
            async with self.pool.acquire() as connection, connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(fs2_activation_model_lock_key($1))",
                    model_ref,
                )
                value = await connection.fetchval(
                    """
                    SELECT count(*) FROM fs2_operations
                    WHERE model_id=$1 AND status IN ('queued','activating','running')
                    """,
                    model_ref,
                )
        except (asyncpg.PostgresError, TimeoutError):
            LOGGER.warning("durable active-operation evidence is unavailable for model %s", model_ref)
            return None
        return int(value) if isinstance(value, int) and 0 <= value <= 1_000_000_000 else None

    async def fast_start_history(
        self,
        *,
        model_ref: str,
        idle_seconds: int,
        now: datetime,
    ) -> tuple[FastStartHistoryWindow, FastStartHistoryWindow] | None:
        """Read payload-free demand without reusing the legacy cold-start clock."""

        async def window(connection: asyncpg.Connection[Any], started_at: datetime) -> FastStartHistoryWindow:
            row = await connection.fetchrow(
                """
                WITH ordered AS (
                    SELECT accepted_at,
                           lag(accepted_at) OVER (ORDER BY accepted_at,id) AS previous_accepted_at
                    FROM fs2_operations
                    WHERE model_id=$1 AND accepted_at >= $2 AND accepted_at < $3
                )
                SELECT count(*)::bigint AS request_count,
                       count(*) FILTER (
                           WHERE previous_accepted_at IS NOT NULL
                             AND extract(epoch FROM accepted_at-previous_accepted_at) >= $4
                       )::bigint AS idle_gap_episode_count
                FROM ordered
                """,
                model_ref,
                started_at,
                now,
                float(idle_seconds),
            )
            assert row is not None
            return FastStartHistoryWindow(
                started_at=started_at,
                ended_at=now,
                request_count=int(row["request_count"]),
                # activation_started_at is an operation-worker transition and
                # occurs for hot requests too.  Only idle-gap episodes are a
                # defensible cold-start demand proxy until an exact model
                # activation boundary is persisted.
                cold_activation_count=0,
                idle_gap_episode_count=int(row["idle_gap_episode_count"]),
                # The retained accepted-to-ready value includes capacity wait.
                # It must not be re-labelled as a model-start target miss.
                target_miss_count=0,
                complete=True,
            )

        try:
            async with self.pool.acquire() as connection, connection.transaction(readonly=True):
                return (
                    await window(connection, now - timedelta(hours=1)),
                    await window(connection, now - timedelta(days=7)),
                )
        except (asyncpg.PostgresError, TimeoutError, ValueError):
            LOGGER.warning("automatic fast-start demand history is unavailable for model %s", model_ref)
            return None


class PrometheusActiveOperations:
    """Conservatively project in-flight durable demand for controller drains.

    The exported operation metric intentionally has no tenant label.  A model
    drain therefore waits for operations across every tenant using the same
    canonical model reference, which is safer than deleting a shared runtime
    while another tenant still has work in flight.
    """

    def __init__(self, reader: HttpPrometheusScalarReader) -> None:
        self.reader = reader

    async def active_operations(self, *, tenant_id: str, model_ref: str) -> int | None:
        del tenant_id
        query = operation_demand_promql(model_ref)
        try:
            value = await self.reader.scalar(query, at=_utc_now())
        except (AdminAdapterUnavailableError, ValueError):
            LOGGER.warning("active-operation evidence is unavailable for model %s", model_ref)
            return None
        if value is None or value > 1_000_000_000:
            return None
        return math.ceil(value)

    async def fast_start_history(
        self,
        *,
        model_ref: str,
        idle_seconds: int,
        now: datetime,
    ) -> tuple[FastStartHistoryWindow, FastStartHistoryWindow] | None:
        del model_ref, idle_seconds, now
        return None


class ModelDeploymentController:
    def __init__(
        self,
        *,
        api: ModelControllerApi,
        envelope: InfrastructureEnvelope,
        renderer: LegacyManifestRenderer,
        namespace: str,
        holder_identity: str,
        prometheus_server_address: str,
        writes_enabled: bool,
        active_operations: ActiveOperationsReader | None = None,
        lease_namespace: str = "fs2-system",
        lease_name: str = "fs2-model-controller",
        lease_duration_seconds: int = 15,
        queue_capacity: int = 256,
        worker_count: int = 2,
        poll_seconds: float = 5,
    ) -> None:
        self.api = api
        self.envelope = envelope
        self.renderer = renderer
        self.namespace = namespace
        self.holder_identity = holder_identity
        self.prometheus_server_address = prometheus_server_address
        self.writes_enabled = writes_enabled
        self.active_operations = active_operations or UnknownActiveOperations()
        self.lease_namespace = lease_namespace
        self.lease_name = lease_name
        self.lease_duration_seconds = lease_duration_seconds
        self.worker_count = worker_count
        self.poll_seconds = poll_seconds
        self.queue = BoundedKeyQueue(queue_capacity)
        self.metrics = ControllerMetrics()
        for model_ref, qualification in envelope.qualifications.items():
            if qualification.model_express is None:
                continue
            for pool_ref in qualification.model_express.pool_refs:
                self.metrics.modelexpress_configured.labels(
                    model_ref,
                    pool_ref,
                    qualification.model_express.deployment_mode,
                    qualification.model_express.runtime_adapter,
                    qualification.model_express.pool_transports[pool_ref].mode,
                ).set(1)
        self.health = ControllerHealth()
        self._fence: LeaseFence | None = None
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def _drain_observation(self, spec: ModelDeploymentSpec, discovery: Discovery) -> DrainObservation:
        publication_present = any(
            item.raw.get("metadata", {}).get("labels", {}).get("fs2-serve.nebius.ai/component") == "publication-intent"
            and not item.observed.deleting
            for item in discovery.resources
        )
        deployments = [item for item in discovery.resources if item.observed.kind == "Deployment"]
        return DrainObservation(
            publication_withdrawn=not publication_present,
            active_operations=await self.active_operations.active_operations(
                tenant_id=spec.tenant_id, model_ref=spec.public_model_id
            ),
            observed_replicas=_known_total([item.replicas for item in deployments]),
            ready_replicas=_known_total([item.ready_replicas for item in deployments]),
        )

    async def reconcile(self, key: ModelKey, fence: LeaseFence | None = None) -> ReconcileResult:
        fence = fence or self._fence
        raw = await self.api.get_model(key)
        if raw is None:
            return ReconcileResult(key=key, action="gone", generation=0)
        metadata = _metadata(raw)
        uid = _required_metadata(raw, "uid")
        generation = int(metadata.get("generation", 0))
        if generation < 1:
            raise ControllerError("ModelDeployment generation is unavailable")
        spec = ModelDeploymentSpec.model_validate(raw.get("spec"))
        fast_start_history = (
            await self.active_operations.fast_start_history(
                model_ref=spec.public_model_id,
                idle_seconds=spec.availability.idle_seconds,
                now=_utc_now(),
            )
            if spec.fast_start.mode is FastStartMode.AUTOMATIC
            else None
        )
        deleting = isinstance(metadata.get("deletionTimestamp"), str)
        finalizers = _string_list(metadata.get("finalizers"))
        has_finalizer = FINALIZER in finalizers

        evaluation_time = _utc_now()
        validation = validate_model_deployment(
            spec,
            self.envelope,
            evaluation_time=evaluation_time,
            automatic_history=fast_start_history,
            previous_status=_mapping(raw.get("status")),
        )
        plan: ReconcilePlan | None = None
        rendered_plan: RenderPlan | None = None
        if validation.disposition is ValidationDisposition.ACCEPTED:
            assert validation.admitted_pool_ref is not None
            qualification = self.envelope.qualifications[spec.model_ref]
            context = RenderContext(
                name=key.name,
                namespace=key.namespace,
                uid=uid,
                generation=generation,
                pool=self.envelope.pools[validation.admitted_pool_ref],
                eligible_pools=[self.envelope.pools[pool_ref] for pool_ref in spec.placement.pool_refs],
                prometheus_server_address=self.prometheus_server_address,
                evaluation_time=evaluation_time,
                fast_start_mechanism=validation.fast_start_mechanism,
                model_express=qualification.model_express,
                regional_cache=qualification.regional_cache,
                host_memory_residency=qualification.host_memory_residency,
                gpu_resident=qualification.gpu_resident,
                residency_holder_image=self.envelope.residency_holder_image,
            )
            try:
                render = self.renderer.render(spec, context)
            except ValueError as exc:
                rejected = reject_validation_decision(
                    validation,
                    code="render_contract_invalid",
                    path="$.spec",
                    message=str(exc),
                )
                plan = ReconcilePlan(
                    action=ReconcileAction.REJECT,
                    target_generation=generation,
                    spec_digest=rejected.spec_digest,
                    validation=rejected,
                )
                discovery = Discovery(resources=[], complete=True)
            else:
                rendered_plan = render
                plan = None
                discovery = await self.api.discover(key=key, owner_uid=uid, render=render)
        else:
            # The planner returns before rendering for invalid or
            # infrastructure-required revisions; an empty authoritative
            # inventory cannot authorize finalizer removal.
            context_pool = next(iter(self.envelope.pools.values()))
            context = RenderContext(
                name=key.name,
                namespace=key.namespace,
                uid=uid,
                generation=generation,
                pool=context_pool,
                prometheus_server_address=self.prometheus_server_address,
            )
            discovery = Discovery(resources=[], complete=not deleting)
        drain = (
            await self._drain_observation(spec, discovery)
            if deleting or spec.lifecycle.desired_state is not DesiredState.ENABLED
            else None
        )
        if drain is not None and drain.preserve_runtime:
            context = context.model_copy(update={"hot_floor_override": _observed_hot_floor(discovery)})
        if plan is None:
            plan = plan_reconciliation(
                generation=generation,
                deleting=deleting,
                spec=spec,
                envelope=self.envelope,
                renderer=self.renderer,
                render_context=context,
                observed=discovery.observed(),
                discovery_complete=discovery.complete,
                drain_observation=drain,
                adoption_verification=None,
            )

        if not self.writes_enabled:
            return ReconcileResult(
                key=key,
                action=f"observe-only:{plan.action}",
                generation=generation,
                requeue=plan.action not in {ReconcileAction.NOOP, ReconcileAction.OBSERVE},
            )
        if fence is None:
            raise FenceLostError("no live leader fence is available")

        # Never create generated resources before the deletion backstop is
        # durably present. Observe-only adoption intentionally has no finalizer.
        if (
            not deleting
            and plan.action
            not in {
                ReconcileAction.OBSERVE,
                ReconcileAction.REJECT,
                ReconcileAction.INFRASTRUCTURE_REQUIRED,
                ReconcileAction.RETRY,
            }
            and not has_finalizer
        ):
            await self.api.set_finalizer(key, owner_uid=uid, present=True, fence=fence)
            return ReconcileResult(key=key, action="finalizer-added", generation=generation, wrote=True, requeue=True)
        if deleting and not has_finalizer:
            return ReconcileResult(
                key=key,
                action="delete-without-finalizer-blocked",
                generation=generation,
                error_code="finalizer_missing",
            )

        wrote = False
        phase_action: str | None = None
        phase_requeue = False
        deletion_scale_targets: list[RenderedResource] = []
        deletion_model_fence: ModelWriteFence | None = None
        deletion_render = plan.render or rendered_plan
        if deleting and deletion_render is not None:
            deletion_model_fence = _model_deletion_fence(raw, key)
            for resource in deletion_render.resources:
                if resource.api_version != "apps/v1" or resource.kind != "Deployment":
                    continue
                deletion_scale_targets.append(resource)
                current = _resource_snapshot(
                    discovery,
                    resource.api_version,
                    resource.kind,
                    resource.namespace,
                    resource.name,
                )
                if current is not None:
                    await self.api.prepare_scale_gate_deletion(
                        resource,
                        current=current,
                        owner_uid=uid,
                        model_fence=deletion_model_fence,
                        fence=fence,
                    )
        fixed_initialization_reversals = (
            [] if deleting else _fixed_initialization_reversal_targets(plan.render, discovery, uid)
        )
        if fixed_initialization_reversals:
            reversal_model_fence = _model_write_fence(raw, key)
            for resource, current, replicas in fixed_initialization_reversals:
                await self.api.recover_fixed_initialization(
                    resource,
                    current=current,
                    replicas=replicas,
                    owner_uid=uid,
                    model_fence=reversal_model_fence,
                    fence=fence,
                )
            return ReconcileResult(
                key=key,
                action="fixed-initialization-reversal",
                generation=generation,
                wrote=True,
                requeue=True,
            )
        # Delete stale scaler/publication resources first.  Applying the
        # explicit drain replica zero in the same pass could race an HPA that
        # still owns the scale subresource.
        if plan.delete_resource_identities:
            handoff_identities = [
                identity
                for identity in plan.delete_resource_identities
                if "/ScaledObject/" in identity or "fs2-model-publication-" in identity
            ]
            receipt_writes = _pending_scale_handoff_receipts(
                plan.render,
                discovery,
                set(plan.delete_resource_identities),
                uid,
                generation,
            )
            if receipt_writes:
                # Every receipt candidate is validated before the first write.
                # Scaler deletion happens only on a subsequent fresh reconcile.
                for resource, current, scaler_snapshot in receipt_writes:
                    await self.api.record_scale_handoff_receipt(
                        resource,
                        current=current,
                        scaler=scaler_snapshot,
                        owner_uid=uid,
                        model_generation=generation,
                        model_fence=_model_write_fence(raw, key),
                        fence=fence,
                    )
                    wrote = True
                return ReconcileResult(
                    key=key,
                    action="fixed-scale-handoff:receipt-recorded",
                    generation=generation,
                    wrote=wrote,
                    requeue=True,
                )
            if (
                not handoff_identities
                and drain is not None
                and not drain.preserve_runtime
                and _autoscaler_resources_present(discovery)
            ):
                return ReconcileResult(
                    key=key,
                    action="drain:autoscaler-removal-pending",
                    generation=generation,
                    wrote=wrote,
                    requeue=True,
                )
            delete_identities = handoff_identities or plan.delete_resource_identities
            for identity in delete_identities:
                wrote = await self.api.delete_resource(identity, owner_uid=uid, fence=fence) or wrote
            safe_zero_cutover = (
                not handoff_identities
                and plan.action is ReconcileAction.DRAIN
                and drain is not None
                and not drain.preserve_runtime
                and not _autoscaler_resources_present(discovery)
            )
            if not safe_zero_cutover:
                return ReconcileResult(
                    key=key,
                    action=f"{plan.action}:delete-first",
                    generation=generation,
                    wrote=wrote,
                    requeue=True,
                )

        # A delete admitted after an observed Cold state may arrive after the
        # scaler has already disappeared. If every Deployment is still
        # authoritatively zero and no autoscaler remains, deletion needs no
        # replica mutation or ownership takeover: delete the exact owned
        # inventory and retain the finalizer until empty rediscovery.
        owned_deployments = [
            item
            for item in discovery.resources
            if item.observed.kind == "Deployment" and item.observed.controller_owner_uid == uid
        ]
        cold_delete_authorized = (
            deleting
            and discovery.complete
            and bool(owned_deployments)
            and not _autoscaler_resources_present(discovery)
            and all(
                item.desired_replicas == 0
                and item.replicas == 0
                and item.updated_replicas == 0
                and item.ready_replicas == 0
                and item.available_replicas == 0
                and item.unavailable_replicas == 0
                for item in owned_deployments
            )
        )
        if cold_delete_authorized:
            for item in discovery.resources:
                if item.observed.controller_owner_uid != uid or item.observed.kind == HPA_ENDPOINT.kind:
                    continue
                wrote = await self.api.delete_resource(item.observed.identity, owner_uid=uid, fence=fence) or wrote
            return ReconcileResult(
                key=key,
                action="delete:delete-first",
                generation=generation,
                wrote=wrote,
                requeue=True,
            )

        # Every new Deployment is created paused without replicas, initialized
        # through the fenced /scale protocol, and only then exposed. A paused
        # object is also an explicit crash-recovery checkpoint.
        initialization_targets: list[tuple[RenderedResource, int, Literal["fixed", "autoscaled"]]] = []
        model_fence: ModelWriteFence | None = None
        autoscaled_identities = {_rendered_identity(target) for _, target in _autoscaler_pairs(plan.render)}
        if plan.render is not None:
            for resource in plan.render.resources:
                spec_manifest = resource.manifest.get("spec")
                desired_replicas = spec_manifest.get("replicas") if isinstance(spec_manifest, Mapping) else None
                is_autoscaled = _rendered_identity(resource) in autoscaled_identities
                if (
                    resource.api_version != "apps/v1"
                    or resource.kind != "Deployment"
                    or (
                        not is_autoscaled
                        and (
                            not isinstance(desired_replicas, int)
                            or isinstance(desired_replicas, bool)
                            or desired_replicas < 0
                        )
                    )
                ):
                    continue
                initialization_current = _resource_snapshot(
                    discovery,
                    resource.api_version,
                    resource.kind,
                    resource.namespace,
                    resource.name,
                )
                if (
                    initialization_current is None
                    or _mapping(initialization_current.raw.get("spec")).get("paused") is True
                ):
                    mode: Literal["fixed", "autoscaled"] = "autoscaled" if is_autoscaled else "fixed"
                    if mode == "autoscaled":
                        initial_replicas = 0
                    else:
                        assert isinstance(desired_replicas, int)
                        initial_replicas = desired_replicas
                    initialization_targets.append((resource, initial_replicas, mode))
        if initialization_targets:
            model_fence = _model_write_fence(raw, key)
            for resource, replicas, mode in initialization_targets:
                await self.api.initialize_deployment_scale(
                    resource,
                    replicas=replicas,
                    target_mode=mode,
                    owner_uid=uid,
                    model_fence=model_fence,
                    fence=fence,
                )
                wrote = True
            if any(mode == "autoscaled" for _, _, mode in initialization_targets):
                for supporting in plan.apply_resources:
                    if supporting.kind in {"Deployment", "ScaledObject"}:
                        continue
                    await self.api.apply_resource(
                        _generic_apply_resource(supporting, discovery),
                        owner_uid=uid,
                        fence=fence,
                    )
                return ReconcileResult(
                    key=key,
                    action="autoscaler-bootstrap",
                    generation=generation,
                    wrote=wrote,
                    requeue=True,
                )
            assert plan.render is not None
            discovery = await self.api.discover(key=key, owner_uid=uid, render=plan.render)

        receipt_backed_fixed = (
            [] if deleting else _receipt_backed_fixed_scale_targets(plan.render, discovery, uid, generation)
        )
        model_fence = _model_write_fence(raw, key) if receipt_backed_fixed else None
        for resource, current in receipt_backed_fixed:
            assert model_fence is not None
            if not await self.api.fixed_scale_guard_clear(
                resource,
                current=current,
                owner_uid=uid,
                model_generation=generation,
                model_fence=model_fence,
                fence=fence,
            ):
                return ReconcileResult(
                    key=key,
                    action="fixed-scale-handoff:autoscaler-removal-pending",
                    generation=generation,
                    wrote=wrote,
                    requeue=True,
                )

        fixed_scale_handoffs = [] if deleting else _fixed_scale_handoff_targets(plan.render, discovery, uid, generation)
        if fixed_scale_handoffs:
            model_fence = model_fence or _model_write_fence(raw, key)
            target_identities = {_rendered_identity(resource) for resource, _ in fixed_scale_handoffs}
            if _autoscaler_targets_present(discovery, target_identities):
                return ReconcileResult(
                    key=key,
                    action="fixed-scale-handoff:autoscaler-removal-pending",
                    generation=generation,
                    wrote=wrote,
                    requeue=True,
                )
            # All candidates are validated before the first write. A partial
            # multi-target handoff is safe and repaired by the next reconcile.
            for resource, current in fixed_scale_handoffs:
                await self.api.apply_fixed_scale_handoff(
                    resource,
                    current=current,
                    owner_uid=uid,
                    model_generation=generation,
                    model_fence=model_fence,
                    fence=fence,
                )
                wrote = True
            return ReconcileResult(
                key=key,
                action="fixed-scale-handoff",
                generation=generation,
                wrote=wrote,
                requeue=True,
            )

        controller_scale_updates = (
            [] if deleting else _controller_fixed_scale_updates(plan.render, discovery, uid, generation)
        )
        if controller_scale_updates:
            model_fence = model_fence or _model_write_fence(raw, key)
            for resource, current, replicas in controller_scale_updates:
                await self.api.apply_controller_scale(
                    resource,
                    current=current,
                    replicas=replicas,
                    owner_uid=uid,
                    model_fence=model_fence,
                    fence=fence,
                )
                wrote = True
            return ReconcileResult(
                key=key,
                action="fixed-scale-update",
                generation=generation,
                wrote=wrote,
                requeue=True,
            )

        # A drain may write replicas=0 only after the foreground ScaledObject
        # deletion and its generated HPA garbage collection are both observed.
        if drain is not None and not drain.preserve_runtime and _autoscaler_resources_present(discovery):
            phase_action = "drain:autoscaler-removal-pending"
            phase_requeue = True

        autoscaler_pairs = _autoscaler_pairs(plan.render)
        autoscaled_target_identities = {_rendered_identity(target) for _, target in autoscaler_pairs}
        autoscaler_authorizations: dict[
            str, tuple[RenderedResource, ScaleGateReleaseAuthorization, ModelWriteFence]
        ] = {}
        if phase_action is None and autoscaler_pairs:
            live_targets = {
                _rendered_identity(target): _resource_snapshot(
                    discovery,
                    target.api_version,
                    target.kind,
                    target.namespace,
                    target.name,
                )
                for _, target in autoscaler_pairs
            }
            apply_without_targets = [
                resource
                for resource in plan.apply_resources
                if _rendered_identity(resource) not in autoscaled_target_identities
            ]
            missing_targets = [
                target for _, target in autoscaler_pairs if live_targets[_rendered_identity(target)] is None
            ]
            if missing_targets:
                raise KubernetesConflictError(
                    "new autoscaled Deployment requires protocol-v2 fenced /scale initialization; "
                    "generic bootstrap is forbidden"
                )
            for scaler, target in autoscaler_pairs:
                live_target = live_targets[_rendered_identity(target)]
                assert live_target is not None
                live_scaler = _resource_snapshot(
                    discovery,
                    scaler.api_version,
                    scaler.kind,
                    scaler.namespace,
                    scaler.name,
                )
                if (
                    live_scaler is None
                    and _fixed_scale_manager_owns_replicas(live_target)
                    and live_target.desired_replicas != 0
                ):
                    model_fence = model_fence or _model_write_fence(raw, key)
                    bootstrap = await self.api.apply_controller_scale(
                        target,
                        current=live_target,
                        replicas=0,
                        owner_uid=uid,
                        model_fence=model_fence,
                        fence=fence,
                    )
                    if bootstrap.desired_replicas != 0 or not _fixed_scale_manager_owns_replicas(bootstrap):
                        raise ControllerError("autoscaled Deployment dedicated scale bootstrap was not observed")
                    return ReconcileResult(
                        key=key,
                        action="autoscaler-zero-scale",
                        generation=generation,
                        wrote=True,
                        requeue=True,
                    )
                model_fence = model_fence or _model_write_fence(raw, key)
                authorization = await self.api.release_scale_gate(
                    target,
                    scaler=scaler,
                    current=live_target,
                    owner_uid=uid,
                    model_fence=model_fence,
                    fence=fence,
                )
                autoscaler_authorizations[_rendered_identity(scaler)] = (target, authorization, model_fence)
            if not all(_autoscaler_installed(scaler, target, discovery, uid) for scaler, target in autoscaler_pairs):
                for scaler, target in autoscaler_pairs:
                    live_target = live_targets[_rendered_identity(target)]
                    assert live_target is not None
                    live_scaler = _resource_snapshot(
                        discovery,
                        scaler.api_version,
                        scaler.kind,
                        scaler.namespace,
                        scaler.name,
                    )
                    if live_scaler is None and _fixed_scale_manager_owns_replicas(live_target):
                        wrote = True
                    elif live_scaler is None and FIELD_MANAGER in live_target.replica_field_managers:
                        raise KubernetesConflictError(
                            "autoscaled Deployment has legacy generic replica ownership; manual migration is required"
                        )
                for resource in apply_without_targets:
                    autoscaler_authorization = autoscaler_authorizations.get(_rendered_identity(resource))
                    if autoscaler_authorization is None:
                        await self.api.apply_resource(
                            _generic_apply_resource(resource, discovery), owner_uid=uid, fence=fence
                        )
                    else:
                        target, authorization, authorization_fence = autoscaler_authorization
                        await self.api.apply_autoscaler_resource(
                            resource,
                            target=target,
                            authorization=authorization,
                            owner_uid=uid,
                            model_fence=authorization_fence,
                            fence=fence,
                        )
                    wrote = True
                phase_action = "autoscaler-install-pending"
                phase_requeue = True
            elif any(
                _fixed_scale_manager_owns_replicas(live_target)
                for live_target in live_targets.values()
                if live_target is not None
            ):
                for resource in apply_without_targets:
                    autoscaler_authorization = autoscaler_authorizations.get(_rendered_identity(resource))
                    if autoscaler_authorization is None:
                        await self.api.apply_resource(
                            _generic_apply_resource(resource, discovery), owner_uid=uid, fence=fence
                        )
                    else:
                        target, authorization, authorization_fence = autoscaler_authorization
                        await self.api.apply_autoscaler_resource(
                            resource,
                            target=target,
                            authorization=authorization,
                            owner_uid=uid,
                            model_fence=authorization_fence,
                            fence=fence,
                        )
                    wrote = True
                phase_action = "autoscaler-scale-ownership-pending"
                phase_requeue = True
            elif any(
                FIELD_MANAGER in live_target.replica_field_managers
                for live_target in live_targets.values()
                if live_target is not None
            ):
                raise KubernetesConflictError(
                    "autoscaled Deployment retains legacy generic replica ownership; manual migration is required"
                )

        if phase_action is None:
            for resource in plan.apply_resources:
                autoscaler_authorization = autoscaler_authorizations.get(_rendered_identity(resource))
                if autoscaler_authorization is None:
                    await self.api.apply_resource(
                        _generic_apply_resource(resource, discovery), owner_uid=uid, fence=fence
                    )
                else:
                    target, authorization, authorization_fence = autoscaler_authorization
                    await self.api.apply_autoscaler_resource(
                        resource,
                        target=target,
                        authorization=authorization,
                        owner_uid=uid,
                        model_fence=authorization_fence,
                        fence=fence,
                    )
                wrote = True
        if plan.remove_finalizer:
            if phase_action is not None:
                # Descendant cleanup must be observed before removing the CR's
                # deletion backstop.
                phase_requeue = True
            else:
                if not deletion_scale_targets or deletion_model_fence is None:
                    raise KubernetesConflictError(
                        "deletion cannot release its finalizer without exact scale gate tombstones"
                    )
                for resource in deletion_scale_targets:
                    await self.api.confirm_scale_gate_tombstone(
                        resource,
                        owner_uid=uid,
                        model_fence=deletion_model_fence,
                        fence=fence,
                    )
                await self.api.set_finalizer(key, owner_uid=uid, present=False, fence=fence)
                return ReconcileResult(key=key, action="finalizer-removed", generation=generation, wrote=True)

        # Re-read after all writes and derive status exclusively from observed
        # objects. Partial apply remains Progressing and is repaired next pass.
        raw_after = await self.api.get_model(key)
        if raw_after is None:
            return ReconcileResult(key=key, action="gone-after-write", generation=generation, wrote=wrote)
        if plan.render is not None:
            discovery = await self.api.discover(key=key, owner_uid=uid, render=plan.render)
        previous = _mapping(raw_after.get("status"))
        status = build_status(
            spec=spec,
            owner_uid=uid,
            generation=generation,
            plan=plan,
            discovery=discovery,
            previous_status=previous,
            drain=drain,
            envelope=self.envelope,
            fast_start_history=fast_start_history,
        )
        wrote = (
            await self.api.patch_status(
                key,
                owner_uid=uid,
                generation=generation,
                status=status,
                fence=fence,
            )
            or wrote
        )
        return ReconcileResult(
            key=key,
            action=phase_action or str(plan.action),
            generation=generation,
            wrote=wrote,
            requeue=(
                phase_requeue
                or bool(plan.apply_resources)
                or plan.action in {ReconcileAction.DRAIN, ReconcileAction.RETRY}
            ),
        )

    async def run_cycle(self) -> bool:
        """Acquire/renew leadership, list exact namespace, and enqueue keys."""

        if not self.writes_enabled:
            models = await self.api.list_models(self.namespace)
            for raw in models:
                metadata = _metadata(raw)
                self.queue.put(ModelKey(namespace=self.namespace, name=str(metadata.get("name"))))
            self.health.cycle_succeeded()
            self.health.leader = False
            return True
        fence = await self.api.acquire_or_renew_lease(
            namespace=self.lease_namespace,
            name=self.lease_name,
            holder_identity=self.holder_identity,
            token=self._fence.token if self._fence is not None else None,
            duration_seconds=self.lease_duration_seconds,
        )
        self._fence = fence
        self.health.leader = fence is not None
        self.metrics.leader.set(1 if fence is not None else 0)
        if fence is None:
            self.health.cycle_succeeded()
            return False
        models = await self.api.list_models(self.namespace)
        for raw in models:
            metadata = _metadata(raw)
            name = metadata.get("name")
            if not isinstance(name, str) or not name:
                raise ControllerError("ModelDeployment list item name is invalid")
            before = self.queue.dropped
            self.queue.put(ModelKey(namespace=self.namespace, name=name))
            if self.queue.dropped > before:
                self.metrics.queue_dropped.inc()
        self.metrics.queue_depth.set(self.queue.depth)
        self.health.cycle_succeeded()
        return True

    async def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                key = await asyncio.wait_for(self.queue.get(), timeout=1)
            except TimeoutError:
                continue
            requeue = False
            try:
                with self.metrics.duration.time():
                    result = await self.reconcile(key)
                self.metrics.reconciles.labels(result.action).inc()
                self.health.reconcile_succeeded(key)
                requeue = result.requeue
            except (ControllerError, ValueError) as exc:
                LOGGER.warning("ModelDeployment reconcile failed for %s: %s", key.text, exc)
                self.metrics.reconciles.labels(type(exc).__name__).inc()
                self.health.reconcile_failed(key, type(exc).__name__)
            finally:
                self.queue.done(key)
                if requeue:
                    before = self.queue.dropped
                    self.queue.put(key)
                    if self.queue.dropped > before:
                        self.metrics.queue_dropped.inc()
                self.metrics.queue_depth.set(self.queue.depth)

    async def run(self) -> None:
        workers = [asyncio.create_task(self._worker()) for _ in range(self.worker_count)]
        try:
            while not self._stop.is_set():
                try:
                    await self.run_cycle()
                except (ControllerError, ValueError) as exc:
                    LOGGER.warning("model controller list/lease cycle failed: %s", exc)
                    self.health.cycle_failed(type(exc).__name__)
                    self._fence = None
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
                except TimeoutError:
                    pass
        finally:
            self._stop.set()
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)


def controller_health_app(controller: ModelDeploymentController) -> FastAPI:
    app = FastAPI(
        title="FS2 ModelDeployment controller health",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/livez", include_in_schema=False)
    async def live() -> Response:
        return Response(status_code=200 if controller.health.live else 503)

    @app.get("/readyz", include_in_schema=False)
    async def ready() -> Response:
        return Response(status_code=200 if controller.health.ready else 503)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(controller.metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8")

    return app


async def run_model_controller(settings: Settings) -> None:
    """Run the independent controller process after both source gates pass."""

    if not settings.model_controller_enabled:
        raise RuntimeError("model controller feature gate is disabled")
    holder = settings.model_controller_holder_identity
    if holder is None or ":" not in holder:
        raise RuntimeError("model controller holder identity must bind pod name and UID")
    files = ControllerFiles.load(
        settings.model_controller_envelope_file,
        settings.model_controller_bundles_file,
    )
    api = HttpKubernetesModelClient(
        base_url=settings.model_controller_api_url,
        token_file=settings.model_controller_token_file,
        ca_file=settings.model_controller_ca_file,
        writes_enabled=settings.model_controller_writes_enabled,
        timeout_seconds=settings.model_controller_api_timeout_seconds,
    )
    active_operations = await PostgresActiveOperations.connect(
        settings.database_url,
        maximum_connections=settings.model_controller_workers,
    )
    controller = ModelDeploymentController(
        api=api,
        envelope=files.infrastructure_envelope,
        renderer=files.renderer(),
        namespace=settings.model_controller_namespace,
        holder_identity=holder,
        prometheus_server_address=settings.model_controller_prometheus_server_address,
        writes_enabled=settings.model_controller_writes_enabled,
        active_operations=active_operations,
        lease_namespace=settings.model_controller_system_namespace,
        lease_name=settings.model_controller_lease_name,
        lease_duration_seconds=settings.model_controller_lease_duration_seconds,
        queue_capacity=settings.model_controller_queue_capacity,
        worker_count=settings.model_controller_workers,
        poll_seconds=settings.model_controller_poll_seconds,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            controller_health_app(controller),
            host="0.0.0.0",  # noqa: S104 - pod-local health/metrics endpoint
            port=settings.model_controller_health_port,
            log_level=settings.log_level.lower(),
        )
    )
    controller_task = asyncio.create_task(controller.run())
    try:
        await server.serve()
    finally:
        controller.stop()
        await controller_task
        await active_operations.close()
        await api.close()
