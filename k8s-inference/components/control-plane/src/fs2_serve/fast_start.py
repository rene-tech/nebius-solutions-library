"""Customer-facing fast-start performance classes and their qualification.

A fast-start level is a startup-time target measured from GPU capacity being
available until semantic endpoint readiness.  Capacity wait and total
end-to-end time are reported separately and never count against a level.
``Off`` has no target.  ``Hot`` (a ready replica) is derived runtime state
and deliberately not a configurable level.

This module is the single deterministic policy and qualification evaluator.
A level is qualified only by compatible benchmark evidence for the exact
artifact, runtime image, runtime template, cache tier, snapshot, accelerator
class, and accelerator count that a ModelDeployment will actually run.
Mechanism names (regional OCI/weights/compile caches, shared or local
snapshots, host RAM residency, ModelExpress, ...) are operator detail carried
by the evidence; a mechanism name alone never qualifies a level.  Missing
evidence is reported as unavailable, never as zero or an invented value.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from pydantic import AwareDatetime, Field, model_validator

from .fast_start_identity import (
    EvidenceIdentityState,
    FastStartIdentityMismatch,
    RuntimeCacheIdentity,
    RuntimeEvidenceIdentity,
    RuntimePlacementIdentity,
    identity_mismatches,
    mechanism_config_digest,
)
from .fast_start_mechanisms import FastStartMechanism
from .models import KubernetesModel

if TYPE_CHECKING:
    from .model_deployment import (
        FastStartEvidence,
        InfrastructureEnvelope,
        ModelDeploymentSpec,
        ModelQualification,
        PoolEnvelope,
    )


class FastStartLevel(StrEnum):
    OFF = "Off"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"

    @property
    def rank(self) -> int:
        return LEVEL_ORDER.index(self)

    @property
    def target_seconds(self) -> int | None:
        return LEVEL_TARGET_SECONDS[self]


LEVEL_ORDER: tuple[FastStartLevel, ...] = (
    FastStartLevel.OFF,
    FastStartLevel.L1,
    FastStartLevel.L2,
    FastStartLevel.L3,
    FastStartLevel.L4,
)
LEVEL_TARGET_SECONDS: dict[FastStartLevel, int | None] = {
    FastStartLevel.OFF: None,
    FastStartLevel.L1: 300,
    FastStartLevel.L2: 120,
    FastStartLevel.L3: 60,
    FastStartLevel.L4: 30,
}
MEASUREMENT_BASIS = "CapacityAvailableToSemanticReady"
# Nearest-rank p95 over fewer samples than this is just the maximum of a tiny
# set; it cannot support a customer-facing startup-time class.
MINIMUM_QUALIFYING_SAMPLES = 20
QUALIFYING_PERCENTILE = 0.95
REASON_PATTERN = r"^[A-Za-z][A-Za-z0-9]*$"


class FastStartMode(StrEnum):
    FIXED = "Fixed"
    AUTOMATIC = "Automatic"


class FastStartFallbackPolicy(StrEnum):
    ALLOW_LOWER_LEVEL = "AllowLowerLevel"
    REQUIRE_TARGET = "RequireTarget"


class FastStartQualificationState(StrEnum):
    NO_TARGET = "NoTarget"
    QUALIFIED = "Qualified"
    FALLBACK = "Fallback"
    UNQUALIFIED = "Unqualified"


class FastStartSpec(KubernetesModel):
    """Optional ``spec.fastStart`` policy; the default asks for nothing.

    ``Fixed`` targets exactly ``level``.  ``Automatic`` selects the highest
    qualified level inside ``[minimumLevel, maximumLevel]``.  ``RequireTarget``
    rejects the revision when the fixed level or the automatic lower bound is
    not qualified; ``AllowLowerLevel`` deploys at the best qualified level and
    reports the shortfall truthfully.
    """

    mode: FastStartMode = FastStartMode.FIXED
    level: FastStartLevel | None = None
    minimum_level: FastStartLevel | None = None
    maximum_level: FastStartLevel | None = None
    fallback_policy: FastStartFallbackPolicy = FastStartFallbackPolicy.ALLOW_LOWER_LEVEL

    @model_validator(mode="after")
    def valid_combination(self) -> FastStartSpec:
        if self.mode is FastStartMode.FIXED:
            if self.minimum_level is not None or self.maximum_level is not None:
                raise ValueError("Fixed fast-start uses level; minimumLevel and maximumLevel belong to Automatic mode")
            if self.level is None:
                self.level = FastStartLevel.OFF
            return self
        if self.level is not None:
            raise ValueError("Automatic fast-start uses minimumLevel and maximumLevel; level belongs to Fixed mode")
        if self.minimum_level is None:
            self.minimum_level = FastStartLevel.OFF
        if self.maximum_level is None:
            self.maximum_level = FastStartLevel.L4
        if self.minimum_level.rank > self.maximum_level.rank:
            raise ValueError("fast-start minimumLevel cannot exceed maximumLevel")
        return self

    @property
    def requested_level(self) -> FastStartLevel:
        if self.mode is FastStartMode.FIXED:
            assert self.level is not None
            return self.level
        assert self.maximum_level is not None
        return self.maximum_level

    @property
    def required_level(self) -> FastStartLevel:
        """The level that ``RequireTarget`` insists on."""

        if self.mode is FastStartMode.FIXED:
            return self.requested_level
        assert self.minimum_level is not None
        return self.minimum_level


class FastStartStatistics(KubernetesModel):
    """Nearest-rank statistics over compatible benchmark samples.

    Absent evidence is represented by omitting the whole object, never by a
    zero.  Failed attempts rank after every successful duration, so a
    percentile that lands on a failure is reported as unavailable.
    """

    sample_count: int = Field(ge=1, le=65536)
    failed_count: int = Field(default=0, ge=0, le=65536)
    latest_seconds: float | None = Field(default=None, ge=0)
    latest_observed_at: AwareDatetime
    p50_seconds: float | None = Field(default=None, ge=0)
    p95_seconds: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def bounded_failures(self) -> FastStartStatistics:
        if self.failed_count > self.sample_count:
            raise ValueError("failed samples cannot exceed the sample count")
        return self


class FastStartPathAssessment(KubernetesModel):
    """One currently compatible exact runtime-identity cohort within a pool."""

    mechanism: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9-]*$")
    identity_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    compatibility_tuple_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    qualified_level: FastStartLevel
    reason: str = Field(min_length=1, max_length=64, pattern=REASON_PATTERN)
    receipt_digests: list[str] = Field(default_factory=list, max_length=256)
    model_start: FastStartStatistics | None = None
    capacity_wait: FastStartStatistics | None = None
    end_to_end: FastStartStatistics | None = None


class FastStartRetainedPathAssessment(KubernetesModel):
    """Historical evidence that is visible but cannot qualify this pool."""

    mechanism: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9-]*$")
    identity_state: EvidenceIdentityState
    identity_digest: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    compatibility_tuple_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    observed_pool_ref: str | None = Field(default=None, min_length=1, max_length=128)
    observed_capacity_type: Literal["regular", "preemptible"] | None = None
    reason: str = Field(min_length=1, max_length=64, pattern=REASON_PATTERN)
    mismatches: list[FastStartIdentityMismatch] = Field(min_length=1, max_length=64)
    receipt_digests: list[str] = Field(default_factory=list, max_length=256)
    model_start: FastStartStatistics | None = None
    capacity_wait: FastStartStatistics | None = None
    end_to_end: FastStartStatistics | None = None


class FastStartPoolAssessment(KubernetesModel):
    """Per-pool qualification so heterogeneous placements stay truthful."""

    pool_ref: str = Field(min_length=1, max_length=128)
    accelerator_class: str | None = Field(default=None, min_length=1, max_length=128)
    qualified_level: FastStartLevel
    reason: str = Field(min_length=1, max_length=64, pattern=REASON_PATTERN)
    mechanisms: list[str] = Field(default_factory=list, max_length=64)
    selected_mechanism: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9-]*$",
    )
    selected_compatibility_tuple_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[a-f0-9]{64}$",
    )
    selected_identity_digest: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    receipt_digests: list[str] = Field(default_factory=list, max_length=256)
    model_start: FastStartStatistics | None = None
    capacity_wait: FastStartStatistics | None = None
    end_to_end: FastStartStatistics | None = None
    paths: list[FastStartPathAssessment] = Field(default_factory=list, max_length=256)
    retained_paths: list[FastStartRetainedPathAssessment] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def consistent_selected_path(self) -> FastStartPoolAssessment:
        identities = {(item.mechanism, item.identity_digest, item.compatibility_tuple_digest) for item in self.paths}
        if len(identities) != len(self.paths):
            raise ValueError("fast-start mechanism, runtime identity and compatibility-tuple paths must be unique")
        selected = (
            self.selected_mechanism,
            self.selected_identity_digest,
            self.selected_compatibility_tuple_digest,
        )
        selected_parts = (
            self.selected_mechanism,
            self.selected_identity_digest,
            self.selected_compatibility_tuple_digest,
        )
        if any(item is None for item in selected_parts) and not all(item is None for item in selected_parts):
            raise ValueError("fast-start selected mechanism, identity and compatibility tuple must be set together")
        if not self.paths:
            if selected[0] is not None:
                raise ValueError("fast-start cannot select a path when no paths are available")
            return self
        if selected[0] is None:
            raise ValueError("fast-start must identify the selected path when paths are available")
        if selected not in identities:
            raise ValueError("fast-start selected path must match an assessed path")
        return self


class FastStartQualification(KubernetesModel):
    state: FastStartQualificationState
    reason: str = Field(min_length=1, max_length=64, pattern=REASON_PATTERN)
    message: str = Field(min_length=1, max_length=240)


class FastStartAssessment(KubernetesModel):
    """Deterministic policy outcome for one desired spec against one envelope."""

    mode: FastStartMode
    fallback_policy: FastStartFallbackPolicy
    requested_level: FastStartLevel
    minimum_level: FastStartLevel | None = None
    maximum_level: FastStartLevel | None = None
    qualified_level: FastStartLevel
    assigned_level: FastStartLevel | None = None
    requested_target_seconds: int | None = Field(default=None, ge=1)
    target_seconds: int | None = Field(default=None, ge=1)
    qualification: FastStartQualification
    model_start: FastStartStatistics | None = None
    capacity_wait: FastStartStatistics | None = None
    end_to_end: FastStartStatistics | None = None
    selected_identity_digest: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    pools: list[FastStartPoolAssessment] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def consistent_outcome(self) -> FastStartAssessment:
        if len({item.pool_ref for item in self.pools}) != len(self.pools):
            raise ValueError("fast-start pool assessments must be unique")
        unqualified = self.qualification.state is FastStartQualificationState.UNQUALIFIED
        if unqualified != (self.assigned_level is None):
            raise ValueError("only an Unqualified outcome leaves the assigned level unset")
        if self.assigned_level is not None and self.assigned_level.rank > self.qualified_level.rank:
            raise ValueError("an assigned level cannot exceed the qualified level")
        if self.requested_target_seconds != self.requested_level.target_seconds:
            raise ValueError("requested target seconds must match the requested level")
        expected_target = self.assigned_level.target_seconds if self.assigned_level is not None else None
        if self.target_seconds != expected_target:
            raise ValueError("target seconds must match the assigned level")
        return self

    @property
    def admitted(self) -> bool:
        return self.assigned_level is not None


class FastStartAutomaticStatus(KubernetesModel):
    """Payload-free rolling-demand decision detail for operator diagnosis."""

    reason: str = Field(min_length=1, max_length=64, pattern=REASON_PATTERN)
    evaluated_at: AwareDatetime
    history_complete: bool
    mechanism_id: str | None = Field(default=None, min_length=1, max_length=128)
    score: float | None = Field(default=None, ge=0)
    pending_level: FastStartLevel | None = None
    pending_since: AwareDatetime | None = None
    consecutive_wins: int = Field(default=0, ge=0, le=1000)
    last_transition_at: AwareDatetime | None = None
    short_window_requests: int = Field(ge=0)
    short_window_cold_activations: int = Field(ge=0)
    short_window_idle_gap_episodes: int = Field(ge=0)
    long_window_requests: int = Field(ge=0)
    long_window_cold_activations: int = Field(ge=0)
    long_window_idle_gap_episodes: int = Field(ge=0)


class FastStartMechanismPoolTransport(KubernetesModel):
    """Rendered transport detail for one pool; not observed transfer proof."""

    mode: Literal["fallback", "nixl-rdma"]
    rdma_resource_name: str | None = None
    rdma_resource_quantity: int = Field(ge=1, le=64)
    nixl_backend: Literal["UCX", "LIBFABRIC"]
    rdma_nic_pin: str = Field(min_length=1, max_length=256)


class FastStartMechanismStatus(KubernetesModel):
    """Operator detail for a configured mechanism, never qualification proof."""

    state: Literal["Pending", "Configured"]
    config_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    deployment_mode: Literal["managed", "external"]
    endpoint: str = Field(min_length=3, max_length=2048)
    metadata_backend: Literal["kubernetes", "redis"]
    runtime_adapter: Literal["vllm"]
    client_package_version: Literal["0.5.1"]
    coordinator_network_type: Literal["pod-selector", "ip-blocks"]
    coordinator_namespace: str | None = Field(default=None, min_length=1, max_length=63)
    coordinator_pod_labels: dict[str, str] = Field(default_factory=dict, max_length=16)
    coordinator_cidrs: list[str] = Field(default_factory=list, max_length=32)
    pool_refs: list[str] = Field(min_length=1, max_length=32)
    pool_transports: dict[str, FastStartMechanismPoolTransport] = Field(min_length=1, max_length=32)
    configuration_observed: bool
    telemetry_state: Literal["Unavailable"] = "Unavailable"
    selected_path: str | None = None
    transferred_bytes: int | None = Field(default=None, ge=0)
    transfer_seconds: float | None = Field(default=None, ge=0)
    fallback_reason: str | None = Field(default=None, max_length=240)


class FastStartStatus(FastStartAssessment):
    """Controller-observed projection; adds what only runtime can tell.

    ``effectiveLevel`` is the level the converged runtime is running at.  It
    may lag behind, or after a policy change even exceed, the newly assigned
    level until the render converges.  ``hot`` is derived from observed ready
    replicas and is never a configurable level.
    """

    effective_level: FastStartLevel | None = None
    effective_identity_digest: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    hot: bool | None = None
    automatic: FastStartAutomaticStatus | None = None
    mechanisms: dict[str, FastStartMechanismStatus] = Field(default_factory=dict, max_length=16)


def nearest_rank(values: Sequence[float | None], fraction: float) -> float | None:
    """Nearest-rank percentile where ``None`` (a failed attempt) ranks last."""

    if not values or not 0 < fraction <= 1:
        return None
    ordered = sorted(value for value in values if value is not None)
    rank = max(1, math.ceil(fraction * len(values)))
    if rank > len(ordered):
        return None
    return ordered[rank - 1]


def _statistics(
    samples: Sequence[tuple[datetime, float | None]],
    *,
    failures_rank_last: bool,
) -> FastStartStatistics | None:
    """Summarise ``(observed_at, seconds)`` pairs; ``None`` seconds are failures or unmeasured."""

    if failures_rank_last:
        considered = list(samples)
    else:
        considered = [(observed_at, value) for observed_at, value in samples if value is not None]
    if not considered:
        return None
    ordered = sorted(considered, key=lambda item: item[0])
    latest_observed_at, latest = ordered[-1]
    values = [value for _, value in ordered]
    return FastStartStatistics(
        sample_count=len(values),
        failed_count=sum(value is None for value in values),
        latest_seconds=latest,
        latest_observed_at=latest_observed_at,
        p50_seconds=nearest_rank(values, 0.5),
        p95_seconds=nearest_rank(values, QUALIFYING_PERCENTILE),
    )


def _identity_compatibility(
    evidence: FastStartEvidence,
    spec: ModelDeploymentSpec,
    pool: PoolEnvelope,
    evaluation_time: datetime,
    qualification: ModelQualification | None,
) -> list[FastStartIdentityMismatch]:
    """Return why evidence is not current for this exact runtime and pool."""

    if evidence.identity_state is EvidenceIdentityState.LEGACY_UNBOUND:
        return [FastStartIdentityMismatch(code="LegacyUnbound", field="$.identity")]
    if evidence.identity is None or evidence.identity_digest is None:
        return [FastStartIdentityMismatch(code="MissingExpectedValue", field="$.identity")]
    if qualification is None:
        return [FastStartIdentityMismatch(code="MissingExpectedValue", field="$.runtime")]

    contracts = [
        item
        for item in qualification.fast_start_runtime_contracts
        if item.pool_ref == pool.pool_id
        and item.runtime.model_ref == spec.model_ref
        and item.runtime.source_revision == spec.artifact.revision
        and item.runtime.artifact_manifest_digest == spec.artifact.manifest_digest
        and item.runtime.runtime_profile == spec.runtime.profile
        and item.runtime.runtime_image == spec.runtime.image
        and item.runtime.template_digest == spec.runtime.template_ref.digest
    ]
    if len(contracts) != 1:
        return [FastStartIdentityMismatch(code="MissingExpectedValue", field="$.runtime.runtimeContractDigest")]
    contract = contracts[0]
    if pool.startup_scenario is None:
        return [FastStartIdentityMismatch(code="MissingExpectedValue", field="$.placement.startupScenario")]
    bindings = [
        item
        for item in pool.fast_start_environment_bindings
        if item.cache_tier == spec.cache.tier.value and item.startup_scenario == pool.startup_scenario
    ]
    if len(bindings) != 1:
        return [FastStartIdentityMismatch(code="MissingExpectedValue", field="$.environment.qualificationDigest")]
    binding = bindings[0]
    if not binding.current_at(evaluation_time):
        return [FastStartIdentityMismatch(code="Expired", field="$.environment.validUntil")]
    if (
        evidence.pool_ref is None
        or evidence.capacity_type is None
        or not binding.includes(
            pool_ref=evidence.pool_ref,
            capacity_type=evidence.capacity_type,
        )
    ):
        return [FastStartIdentityMismatch(code="ValueMismatch", field="$.environment.members")]

    selected = spec.cache.mechanism
    if selected is not None and evidence.mechanism != selected.value:
        # A pinned mechanism is the only path this revision renders, so another
        # mechanism's cohort cannot qualify it. This narrows compatibility; it
        # never widens it.
        return [FastStartIdentityMismatch(code="ValueMismatch", field="$.cache.mechanism")]
    if evidence.mechanism == "modelexpress":
        model_express = qualification.model_express
        if model_express is None or pool.pool_id not in model_express.pool_refs:
            return [FastStartIdentityMismatch(code="MissingExpectedValue", field="$.cache.mechanismConfigDigest")]
        expected_mechanism_digest = model_express.config_digest
    else:
        # Retained evidence may name a mechanism this build does not model,
        # because earlier campaigns used other names. An unknown name keeps the
        # historical storage-only identity instead of raising.
        known = [item for item in FastStartMechanism if item.value == evidence.mechanism]
        declaration = qualification.mechanism_declaration(known[0]) if known else None
        if declaration is not None and pool.pool_id not in declaration.pool_refs:
            return [FastStartIdentityMismatch(code="MissingExpectedValue", field="$.cache.mechanismConfigDigest")]
        expected_mechanism_digest = mechanism_config_digest(
            mechanism=evidence.mechanism,
            storage_contract_digest=contract.storage_contract_digest,
            declaration_digest=None if declaration is None else declaration.config_digest,
        )

    snapshot_digest = spec.cache.snapshot_ref.digest if spec.cache.snapshot_ref is not None else None
    expected = RuntimeEvidenceIdentity(
        runtime=contract.runtime,
        environment=binding.environment,
        placement=RuntimePlacementIdentity(
            accelerator_class=pool.accelerator_class,
            accelerators_per_replica=spec.placement.accelerators_per_replica,
            topology_policy=spec.placement.topology_policy.value,
            startup_scenario=pool.startup_scenario,
        ),
        cache=RuntimeCacheIdentity(
            tier=spec.cache.tier.value,
            mechanism=evidence.mechanism,
            mechanism_config_digest=expected_mechanism_digest,
            snapshot_digest=snapshot_digest,
            storage_contract_digest=contract.storage_contract_digest,
        ),
        measurement=contract.measurement,
    )
    mismatches = identity_mismatches(expected, evidence.identity)
    if evidence.valid_until is not None and evaluation_time >= evidence.valid_until:
        mismatches.append(FastStartIdentityMismatch(code="Expired", field="$.validUntil"))
    return mismatches


def _highest_level_within(p95_seconds: float) -> FastStartLevel:
    for level in reversed(LEVEL_ORDER):
        target = level.target_seconds
        if target is not None and p95_seconds <= target:
            return level
    return FastStartLevel.OFF


def _assess_path(
    mechanism: str,
    identity_digest: str,
    compatibility_tuple_digest: str,
    evidence: Sequence[FastStartEvidence],
) -> FastStartPathAssessment:
    model_start = _statistics(
        [(sample.observed_at, sample.model_start_seconds) for item in evidence for sample in item.samples],
        failures_rank_last=True,
    )
    capacity_wait = _statistics(
        [(sample.observed_at, sample.capacity_wait_seconds) for item in evidence for sample in item.samples],
        failures_rank_last=False,
    )
    end_to_end = _statistics(
        [(sample.observed_at, sample.end_to_end_seconds) for item in evidence for sample in item.samples],
        failures_rank_last=False,
    )
    assert model_start is not None
    qualified = FastStartLevel.OFF
    if not all(item.compatibility_tuple_complete for item in evidence):
        reason = "IncompleteCompatibilityTuple"
    elif model_start.sample_count < MINIMUM_QUALIFYING_SAMPLES:
        reason = "InsufficientBenchmarkSamples"
    elif model_start.failed_count != 0:
        # A customer-facing startup class is a reliability claim as well as a
        # latency percentile.  Do not let a low failure rate disappear above
        # the p95 rank boundary; failed and timed-out attempts keep the exact
        # tuple exploratory until a failure-free cohort is collected.
        reason = "BenchmarkFailuresPresent"
    elif model_start.p95_seconds is None:
        reason = "BenchmarkFailuresExceedPercentile"
    else:
        qualified = _highest_level_within(model_start.p95_seconds)
        reason = "BenchmarkP95WithinTarget" if qualified is not FastStartLevel.OFF else "BenchmarkP95ExceedsEveryTarget"
    return FastStartPathAssessment(
        mechanism=mechanism,
        identity_digest=identity_digest,
        compatibility_tuple_digest=compatibility_tuple_digest,
        qualified_level=qualified,
        reason=reason,
        receipt_digests=sorted({item.receipt_digest for item in evidence}),
        model_start=model_start,
        capacity_wait=capacity_wait,
        end_to_end=end_to_end,
    )


def _path_order(item: FastStartPathAssessment) -> tuple[int, float, int, str, str]:
    model_start = item.model_start
    return (
        -item.qualified_level.rank,
        math.inf if model_start is None or model_start.p95_seconds is None else model_start.p95_seconds,
        0 if model_start is None else -model_start.sample_count,
        item.mechanism,
        item.identity_digest,
    )


def _retained_reason(mismatches: Sequence[FastStartIdentityMismatch]) -> str:
    if any(item.code == "LegacyUnbound" for item in mismatches):
        return "LegacyIdentityUnbound"
    if any(item.code == "Expired" for item in mismatches):
        return "EvidenceIdentityExpired"
    if any(item.code == "MissingExpectedValue" for item in mismatches):
        return "ExpectedIdentityUnavailable"
    return "RuntimeIdentityMismatch"


def _assess_retained_path(
    evidence: Sequence[FastStartEvidence],
    mismatches: Sequence[FastStartIdentityMismatch],
) -> FastStartRetainedPathAssessment:
    first = evidence[0]
    return FastStartRetainedPathAssessment(
        mechanism=first.mechanism,
        identity_state=first.identity_state,
        identity_digest=first.identity_digest,
        compatibility_tuple_digest=first.compatibility_tuple_digest,
        observed_pool_ref=first.pool_ref,
        observed_capacity_type=first.capacity_type,
        reason=_retained_reason(mismatches),
        mismatches=list(mismatches),
        receipt_digests=sorted({item.receipt_digest for item in evidence}),
        model_start=_statistics(
            [(sample.observed_at, sample.model_start_seconds) for item in evidence for sample in item.samples],
            failures_rank_last=True,
        ),
        capacity_wait=_statistics(
            [(sample.observed_at, sample.capacity_wait_seconds) for item in evidence for sample in item.samples],
            failures_rank_last=False,
        ),
        end_to_end=_statistics(
            [(sample.observed_at, sample.end_to_end_seconds) for item in evidence for sample in item.samples],
            failures_rank_last=False,
        ),
    )


def _assess_pool(
    spec: ModelDeploymentSpec,
    pool: PoolEnvelope,
    evidence: Sequence[FastStartEvidence],
    evaluation_time: datetime,
    qualification: ModelQualification | None,
) -> FastStartPoolAssessment:
    assessed = [(item, _identity_compatibility(item, spec, pool, evaluation_time, qualification)) for item in evidence]
    compatible = [item for item, mismatches in assessed if not mismatches]
    retained_groups: dict[tuple[str, str, tuple[tuple[str, str], ...]], list[FastStartEvidence]] = {}
    retained_mismatches: dict[tuple[str, str, tuple[tuple[str, str], ...]], list[FastStartIdentityMismatch]] = {}
    for item, mismatches in assessed:
        if not mismatches:
            continue
        identity = item.identity_digest or item.compatibility_tuple_digest
        mismatch_key = tuple((mismatch.code, mismatch.field) for mismatch in mismatches)
        key = (item.mechanism, identity, mismatch_key)
        retained_groups.setdefault(key, []).append(item)
        retained_mismatches[key] = mismatches
    retained_paths = sorted(
        (_assess_retained_path(cohort, retained_mismatches[key]) for key, cohort in retained_groups.items()),
        key=lambda item: (item.mechanism, item.identity_digest or item.compatibility_tuple_digest),
    )
    if not compatible:
        return FastStartPoolAssessment(
            pool_ref=pool.pool_id,
            accelerator_class=pool.accelerator_class,
            qualified_level=FastStartLevel.OFF,
            reason="NoCurrentRuntimeEvidence",
            retained_paths=retained_paths,
        )
    grouped: dict[tuple[str, str, str], list[FastStartEvidence]] = {}
    for item in compatible:
        assert item.identity_digest is not None
        grouped.setdefault((item.mechanism, item.identity_digest, item.compatibility_tuple_digest), []).append(item)
    paths = sorted(
        (
            _assess_path(mechanism, identity_digest, compatibility_tuple_digest, cohort)
            for (mechanism, identity_digest, compatibility_tuple_digest), cohort in grouped.items()
        ),
        key=lambda item: (item.mechanism, item.identity_digest),
    )
    selected = min(paths, key=_path_order)
    return FastStartPoolAssessment(
        pool_ref=pool.pool_id,
        accelerator_class=pool.accelerator_class,
        qualified_level=selected.qualified_level,
        reason=selected.reason,
        mechanisms=sorted({item.mechanism for item in paths}),
        selected_mechanism=selected.mechanism,
        selected_identity_digest=selected.identity_digest,
        selected_compatibility_tuple_digest=selected.compatibility_tuple_digest,
        receipt_digests=sorted({digest for item in paths for digest in item.receipt_digests}),
        model_start=selected.model_start,
        capacity_wait=selected.capacity_wait,
        end_to_end=selected.end_to_end,
        paths=paths,
        retained_paths=retained_paths,
    )


def _binding_pool(pools: Sequence[FastStartPoolAssessment]) -> FastStartPoolAssessment | None:
    """The pool that limits the deployment: lowest level, then slowest p95."""

    if not pools:
        return None
    return sorted(
        pools,
        key=lambda item: (
            item.qualified_level.rank,
            -(
                math.inf
                if item.model_start is None or item.model_start.p95_seconds is None
                else item.model_start.p95_seconds
            ),
            item.pool_ref,
        ),
    )[0]


def _p95_text(assessment: FastStartPoolAssessment | None) -> str:
    if assessment is None or assessment.model_start is None or assessment.model_start.p95_seconds is None:
        return "no compatible model-start p95"
    return f"model-start p95 {assessment.model_start.p95_seconds:g}s over {assessment.model_start.sample_count} samples"


def evaluate_fast_start(
    spec: ModelDeploymentSpec,
    envelope: InfrastructureEnvelope,
    *,
    evaluation_time: datetime,
) -> FastStartAssessment:
    """Decide requested, qualified, and assigned levels from evidence alone."""

    policy = spec.fast_start
    qualification = envelope.qualifications.get(spec.model_ref)
    evidence = qualification.fast_start_evidence if qualification is not None else []
    pools: list[FastStartPoolAssessment] = []
    for pool_ref in sorted(spec.placement.pool_refs):
        pool = envelope.pools.get(pool_ref)
        if pool is None:
            pools.append(
                FastStartPoolAssessment(
                    pool_ref=pool_ref,
                    qualified_level=FastStartLevel.OFF,
                    reason="PoolOutsideEnvelope",
                )
            )
            continue
        pools.append(_assess_pool(spec, pool, evidence, evaluation_time, qualification))
    binding = _binding_pool(pools)
    qualified = binding.qualified_level if binding is not None else FastStartLevel.OFF

    requested = policy.requested_level
    if policy.mode is FastStartMode.FIXED:
        candidate = requested if qualified.rank >= requested.rank else qualified
        satisfied = candidate is requested
        shortfall_reason = "RequestedLevelUnqualified"
        shortfall_target = f"requested {requested.value}"
    else:
        assert policy.minimum_level is not None and policy.maximum_level is not None
        candidate = LEVEL_ORDER[min(qualified.rank, policy.maximum_level.rank)]
        satisfied = candidate.rank >= policy.minimum_level.rank
        shortfall_reason = "MinimumLevelUnqualified"
        shortfall_target = f"minimum {policy.minimum_level.value}"

    binding_text = f"{binding.pool_ref}: {_p95_text(binding)}" if binding is not None else "no placement pools"
    assigned: FastStartLevel | None = candidate
    if satisfied and candidate is FastStartLevel.OFF:
        qualification_state = FastStartQualification(
            state=FastStartQualificationState.NO_TARGET,
            reason="NoFastStartTarget",
            message=f"assigned level Off has no startup-time target ({binding_text})",
        )
    elif satisfied:
        qualification_state = FastStartQualification(
            state=FastStartQualificationState.QUALIFIED,
            reason="AutomaticLevelSelected" if policy.mode is FastStartMode.AUTOMATIC else "BenchmarkEvidenceQualified",
            message=(
                f"{candidate.value} (at most {candidate.target_seconds}s from capacity to semantic readiness) "
                f"is backed by compatible benchmark evidence ({binding_text})"
            ),
        )
    elif policy.fallback_policy is FastStartFallbackPolicy.ALLOW_LOWER_LEVEL:
        qualification_state = FastStartQualification(
            state=FastStartQualificationState.FALLBACK,
            reason=shortfall_reason,
            message=(
                f"{shortfall_target} lacks compatible benchmark evidence; "
                f"{candidate.value} assigned instead ({binding_text})"
            ),
        )
    else:
        assigned = None
        qualification_state = FastStartQualification(
            state=FastStartQualificationState.UNQUALIFIED,
            reason="RequireTargetUnmet",
            message=(
                f"{shortfall_target} lacks compatible benchmark evidence and fallbackPolicy is RequireTarget "
                f"({binding_text})"
            ),
        )

    return FastStartAssessment(
        mode=policy.mode,
        fallback_policy=policy.fallback_policy,
        requested_level=requested,
        minimum_level=policy.minimum_level,
        maximum_level=policy.maximum_level,
        qualified_level=qualified,
        assigned_level=assigned,
        requested_target_seconds=requested.target_seconds,
        target_seconds=assigned.target_seconds if assigned is not None else None,
        qualification=qualification_state,
        model_start=binding.model_start if binding is not None else None,
        capacity_wait=binding.capacity_wait if binding is not None else None,
        end_to_end=binding.end_to_end if binding is not None else None,
        selected_identity_digest=binding.selected_identity_digest if binding is not None else None,
        pools=pools,
    )
