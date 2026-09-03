"""Strict, payload-free contracts for the scientific operations admin BFF."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from .models import StrictModel


class ScientificServiceClass(StrEnum):
    PRESENTATION = "presentation"
    INTERACTIVE = "interactive"
    CUSTOMER_BATCH = "customer-batch"
    BULK_BACKFILL = "bulk-backfill"


class ScientificEvidenceState(StrEnum):
    MEASURED = "measured"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


class ScientificMeasurement(StrictModel):
    value: float | None = Field(default=None, ge=0)
    unit: Literal["seconds", "gpu-seconds", "bytes", "count"]
    evidence: ScientificEvidenceState
    source: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    reason: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def evidence_matches_value(self) -> ScientificMeasurement:
        if self.evidence is ScientificEvidenceState.UNAVAILABLE:
            if self.value is not None or self.reason is None:
                raise ValueError("unavailable scientific measurement must be null with a reason")
        elif self.value is None:
            raise ValueError("available scientific measurement must be numeric")
        elif self.evidence is ScientificEvidenceState.ESTIMATED and self.reason is None:
            raise ValueError("estimated scientific measurement must explain the estimate")
        return self


class ScientificExplicitAlternative(StrictModel):
    model_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=200)
    relationship: Literal["explicit-alternative"] = "explicit-alternative"
    reason: str = Field(min_length=1, max_length=200)


class ScientificAccessAuthorization(StrictModel):
    """Deployment-bound academic authorization; never a caller credential.

    Every field is copied from the generated academic asset readiness
    projection. Nothing here is synthesised, so a field the projection does not
    publish is absent rather than invented.
    """

    asset_id: str = Field(min_length=1, max_length=128)
    backend_id: str | None = Field(default=None, min_length=1, max_length=128)
    license_id: str | None = Field(default=None, min_length=1, max_length=200)
    use_authorization_status: Literal["Granted"] = "Granted"
    execution_authorization_status: Literal["Authorized"] = "Authorized"
    serving_admission: Literal["PendingRuntimeReadiness", "AdmittedNoPerRequestLicenseReceipt"]
    asset_namespace: str | None = Field(default=None, min_length=1, max_length=63)


class ScientificAccessGate(StrictModel):
    profile: Literal["standard", "academic"]
    state: Literal["not-required", "unverified", "verified", "blocked"]
    gate: str = Field(min_length=1, max_length=300)
    receipt_digest: str | None = Field(default=None, max_length=128)
    request_time_license_receipt_required: Literal[False] = False
    authorization: ScientificAccessAuthorization | None = None
    formal_license_status: Literal[
        "FormalAcceptancePending", "FormalAcceptanceRecorded", "not-applicable"
    ] = "not-applicable"
    credentials_exposed: Literal[False] = False
    alternative: ScientificExplicitAlternative | None = None

    @model_validator(mode="after")
    def access_evidence_is_consistent(self) -> ScientificAccessGate:
        if self.profile == "academic":
            if self.state == "verified" and self.authorization is None and self.receipt_digest is None:
                raise ValueError("verified academic access requires deployment authorization or an access receipt")
            if self.formal_license_status == "not-applicable":
                raise ValueError("academic access must report formal licence status separately")
        elif self.authorization is not None or self.formal_license_status != "not-applicable":
            raise ValueError("standard access cannot carry academic authorization state")
        if self.credentials_exposed:
            raise ValueError("scientific access credentials cannot be exposed")
        return self


class ScientificBackendIdentity(StrictModel):
    backend_id: str = Field(min_length=1, max_length=128)
    kind: str = Field(min_length=1, max_length=64)
    source_repository: str = Field(min_length=1, max_length=512)
    source_revision: str | None = Field(default=None, max_length=256)
    model_revision: str | None = Field(default=None, max_length=256)
    runtime_image_digest: str | None = Field(default=None, max_length=256)
    execution_identity_digest: str | None = Field(default=None, max_length=256)


class ScientificQueueState(StrictModel):
    tenant_queue: str = Field(min_length=1, max_length=128)
    model_lane: str = Field(min_length=1, max_length=128)
    local_queue: str = Field(min_length=1, max_length=128)
    cluster_queue: str = Field(min_length=1, max_length=128)
    workload_priority_class: str = Field(min_length=1, max_length=128)
    priority_value: int
    admission_state: Literal["pending", "inadmissible", "admitted", "evicted", "finished"]
    admission_reason: str = Field(min_length=1, max_length=300)
    admitted_at: AwareDatetime | None = None
    queue_position: ScientificMeasurement


class ScientificServiceClassDecision(StrictModel):
    requested: ScientificServiceClass
    effective: ScientificServiceClass
    reason: str = Field(min_length=1, max_length=300)
    policy_revision: str = Field(min_length=1, max_length=200)


class ScientificFastStartObservation(StrictModel):
    tier: Literal[
        "cold",
        "container-image-local",
        "model-artifact-local",
        "runtime-checkpoint-restore",
        "gpu-memory-snapshot-restore",
        "warm-replica",
        "not-observed",
    ]
    evidence: Literal["observed", "declared", "unavailable"]
    observed_at: AwareDatetime | None = None
    runtime_identity_digest: str | None = Field(default=None, max_length=256)
    reason: str = Field(min_length=1, max_length=300)


class ScientificLifecyclePhase(StrictModel):
    phase: Literal[
        "queue",
        "admission",
        "image-pull",
        "artifact-load",
        "restore",
        "semantic-warmup",
        "active-compute",
        "allocated-idle",
        "grace-drain",
        "teardown",
    ]
    duration: ScientificMeasurement


class ScientificIdleCause(StrictModel):
    cause: Literal[
        "image-pull",
        "artifact-load",
        "restore",
        "warmup",
        "between-stages",
        "scheduler-hold",
        "unattributed",
    ]
    duration: ScientificMeasurement


class ScientificGpuAccounting(StrictModel):
    gpu_count: int | None = Field(default=None, ge=0, le=1024)
    capacity_type: Literal["regular", "preemptible", "capacity-block", "unknown"]
    allocated: ScientificMeasurement
    active: ScientificMeasurement
    idle_total: ScientificMeasurement
    idle_by_cause: list[ScientificIdleCause] = Field(max_length=16)
    grace_drain: ScientificMeasurement
    reconciliation_delta: ScientificMeasurement


class ScientificError(StrictModel):
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=300)
    retryable: bool


class ScientificAttempt(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    number: int = Field(ge=1, le=10)
    status: Literal["queued", "running", "succeeded", "failed", "preempted", "cancelled"]
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    workload_uid: str | None = Field(default=None, max_length=256)
    job_uid: str | None = Field(default=None, max_length=256)
    pod_count: int | None = Field(default=None, ge=0, le=1024)
    node_count: int | None = Field(default=None, ge=0, le=1024)
    gpu_count: int | None = Field(default=None, ge=0, le=1024)
    admitted_at: AwareDatetime | None = None
    resolved_pool_id: str | None = Field(default=None, max_length=128)
    admitted_resource_flavor: str | None = Field(default=None, max_length=253)
    accelerator_resource_name: str | None = Field(default=None, max_length=317)
    checkpoint_input_artifact_id: str | None = Field(default=None, max_length=128)
    checkpoint_output_artifact_id: str | None = Field(default=None, max_length=128)
    error: ScientificError | None = None


class ScientificStage(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=200)
    ordinal: int = Field(ge=1, le=1024)
    needs: list[str] = Field(max_length=128)
    resource_class: Literal["cpu", "gpu"]
    admission_mode: Literal["independent-jobs", "gang-jobset"]
    checkpoint_mode: Literal["none", "restart", "resume"]
    status: Literal["pending", "queued", "admitted", "running", "succeeded", "failed", "cancelled", "skipped"]
    attempts: list[ScientificAttempt] = Field(max_length=1024)


class ScientificArtifactDownload(StrictModel):
    available: bool
    href: str | None = Field(default=None, max_length=2048)
    reason: str | None = Field(default=None, max_length=200)


class ScientificArtifact(StrictModel):
    artifact_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    role: Literal["input", "checkpoint", "output", "validation", "manifest"]
    semantic_type: str = Field(min_length=1, max_length=128)
    state: Literal["available", "pending", "failed", "expired"]
    sha256: str | None = Field(default=None, max_length=128)
    size_bytes: ScientificMeasurement
    media_type: str = Field(min_length=1, max_length=128)
    created_at: AwareDatetime | None = None
    download: ScientificArtifactDownload


class ScientificObservabilityLink(StrictModel):
    kind: Literal["trace", "logs", "metrics"]
    label: str = Field(min_length=1, max_length=100)
    available: bool
    href: str | None = Field(default=None, max_length=2048)
    reason: str | None = Field(default=None, max_length=200)


class ScientificStageCounts(StrictModel):
    pending: int = Field(ge=0)
    queued: int = Field(ge=0)
    admitted: int = Field(ge=0)
    running: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    cancelled: int = Field(ge=0)
    skipped: int = Field(ge=0)


class ScientificAttribution(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=120)
    user_id: str = Field(min_length=1, max_length=200)
    principal_id: str = Field(min_length=1, max_length=200)
    api_key_prefix: str = Field(min_length=1, max_length=64)


class ScientificRunModel(StrictModel):
    model_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=200)
    execution_mode: Literal["scientific-batch", "hybrid"]
    backend: ScientificBackendIdentity


class ScientificCancellation(StrictModel):
    state: Literal["not-requested", "requested", "acknowledged", "denied"]
    requested_at: AwareDatetime | None = None
    requested_by: str | None = Field(default=None, max_length=200)
    reason: str | None = Field(default=None, max_length=300)
    mode: Literal["terminate-attempt", "checkpoint-then-terminate"]
    grace_seconds: int | None = Field(default=None, ge=0, le=3600)
    can_cancel: bool


class ScientificRunSummary(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    batch_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=200)
    operation: str = Field(min_length=1, max_length=64)
    status: Literal[
        "waiting-for-access", "queued", "admitted", "running", "succeeded", "failed", "cancelling", "cancelled"
    ]
    submitted_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    attribution: ScientificAttribution
    model: ScientificRunModel
    access: ScientificAccessGate
    service_class: ScientificServiceClassDecision
    queue: ScientificQueueState
    fast_start: ScientificFastStartObservation
    stage_counts: ScientificStageCounts
    gpu_accounting: ScientificGpuAccounting
    error: ScientificError | None = None
    cancellation: ScientificCancellation


class ScientificRetry(StrictModel):
    max_attempts_per_stage: int = Field(ge=1, le=10)
    retryable_exit_codes: list[int] = Field(max_length=64)


class ScientificSemanticValidation(StrictModel):
    validator_id: str = Field(min_length=1, max_length=128)
    status: Literal["passed", "failed", "not-run"]
    receipt_digest: str | None = Field(default=None, max_length=128)


class ScientificRunDetail(StrictModel):
    run: ScientificRunSummary
    lifecycle_phases: list[ScientificLifecyclePhase] = Field(max_length=32)
    stages: list[ScientificStage] = Field(max_length=128)
    artifacts: list[ScientificArtifact] = Field(max_length=1024)
    retry: ScientificRetry
    semantic_validation: ScientificSemanticValidation
    observability: list[ScientificObservabilityLink] = Field(max_length=8)
    payloads_exposed: Literal[False] = False


class ScientificRunList(StrictModel):
    items: list[ScientificRunSummary] = Field(max_length=200)
    next_cursor: str | None = Field(default=None, max_length=512)


class ScientificCachingReadiness(StrictModel):
    exact_tier: Literal[
        "cold",
        "container-image-local",
        "model-artifact-local",
        "runtime-checkpoint-restore",
        "gpu-memory-snapshot-restore",
        "warm-replica",
        "not-observed",
    ]
    image: Literal["verified", "candidate", "unsupported", "unavailable"]
    artifacts: Literal["verified", "candidate", "unsupported", "unavailable"]
    reference_data: Literal["verified", "candidate", "unsupported", "unavailable"]
    runtime_checkpoint: Literal["verified", "candidate", "unsupported", "unavailable"]
    gpu_snapshot: Literal["verified", "candidate", "unsupported", "unavailable"]
    reason: str = Field(min_length=1, max_length=300)


class ScientificQualificationJoin(StrictModel):
    """Why a candidate is or is not joined to a live registry model.

    A candidate is only qualified when the whole immutable execution identity
    matches. Matching a serving lane name alone would let a candidate inherit a
    different vendor's image and qualification.
    """

    state: Literal["qualified", "identity-mismatch", "not-registered", "evidence-absent"]
    reason: str = Field(min_length=1, max_length=300)
    serving_lane_id: str | None = Field(default=None, min_length=1, max_length=128)
    compared: list[str] = Field(default_factory=list, max_length=8)
    mismatched: list[str] = Field(default_factory=list, max_length=8)


class ScientificModelReadiness(StrictModel):
    model_id: str = Field(min_length=1, max_length=128)
    candidate_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=200)
    readiness: Literal["qualified", "candidate", "blocked", "unknown"]
    readiness_reason: str = Field(min_length=1, max_length=300)
    workload_profile: Literal["published", "absent"]
    missing_evidence: list[str] = Field(default_factory=list, max_length=12)
    qualification: ScientificQualificationJoin
    execution_mode: Literal["scientific-batch", "hybrid"] | None = None
    batch_supported: bool
    interactive_supported: bool | None = None
    service_classes: list[ScientificServiceClass] = Field(default_factory=list, max_length=8)
    backend: ScientificBackendIdentity
    access: ScientificAccessGate
    caching: ScientificCachingReadiness

    @model_validator(mode="after")
    def absent_profile_withholds_execution_shape(self) -> ScientificModelReadiness:
        if self.workload_profile == "absent":
            if self.readiness == "qualified":
                raise ValueError("a model without a published workload profile cannot be qualified")
            if self.execution_mode is not None or self.service_classes:
                raise ValueError("a model without a published workload profile has no execution shape")
            if "workload-profile" not in self.missing_evidence:
                raise ValueError("an absent workload profile must be listed as missing evidence")
        elif self.execution_mode is None:
            raise ValueError("a published workload profile must declare its execution mode")
        if self.readiness == "qualified" and self.qualification.state != "qualified":
            raise ValueError("qualified readiness requires a qualified execution-identity join")
        return self


class ScientificModelProjectionIssue(StrictModel):
    """A bounded catalog defect isolated from otherwise valid model rows."""

    candidate_id: str | None = Field(default=None, min_length=1, max_length=128)
    source: Literal["candidate-receipt", "workload-profile", "academic-readiness", "variant-map"]
    reason: str = Field(min_length=1, max_length=300)


class ScientificCapability(StrictModel):
    available: bool
    reason: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def unavailable_capability_explains_itself(self) -> ScientificCapability:
        if not self.available and self.reason is None:
            raise ValueError("an unavailable scientific capability must say why")
        if self.available and self.reason is not None:
            raise ValueError("an available scientific capability needs no reason")
        return self


class ScientificCapabilities(StrictModel):
    """What this control-plane build can serve, so the console can gate truthfully."""

    model_readiness: ScientificCapability
    run_history: ScientificCapability
    artifacts: ScientificCapability


class ScientificModelReadinessList(StrictModel):
    items: list[ScientificModelReadiness] = Field(max_length=256)
    projection_issues: list[ScientificModelProjectionIssue] = Field(default_factory=list, max_length=256)
