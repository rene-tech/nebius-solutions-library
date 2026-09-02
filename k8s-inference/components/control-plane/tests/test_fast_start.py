"""Deterministic fast-start policy and qualification evaluator tests."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_model_deployment import digest, envelope, model_spec

from fs2_serve.fast_start import (
    LEVEL_TARGET_SECONDS,
    MINIMUM_QUALIFYING_SAMPLES,
    FastStartFallbackPolicy,
    FastStartLevel,
    FastStartMode,
    FastStartQualificationState,
    FastStartSpec,
    FastStartStatus,
    evaluate_fast_start,
    nearest_rank,
)
from fs2_serve.model_deployment import (
    CacheTier,
    FastStartEvidence,
    FastStartSample,
    InfrastructureEnvelope,
    ModelDeploymentSpec,
    PoolEnvelope,
    ValidationDisposition,
    canonical_digest,
    spec_digest,
    validate_model_deployment,
)
from fs2_serve.model_deployment_bridge import _normalize_keys

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def samples(
    *seconds: float | None,
    capacity_wait: float | None = None,
    end_to_end: float | None = None,
) -> list[FastStartSample]:
    return [
        FastStartSample(
            observed_at=NOW - timedelta(minutes=len(seconds) - index),
            model_start_seconds=value,
            capacity_wait_seconds=capacity_wait,
            end_to_end_seconds=end_to_end,
        )
        for index, value in enumerate(seconds)
    ]


def evidence(
    spec: ModelDeploymentSpec,
    pool: PoolEnvelope,
    *,
    seconds: Sequence[float | None],
    **overrides: Any,
) -> FastStartEvidence:
    values: dict[str, Any] = {
        "receipt_digest": digest("f"),
        "mechanism": "shared-cache",
        "measurement_basis": "CapacityAvailableToSemanticReady",
        "accelerator_class": pool.accelerator_class,
        "accelerators_per_replica": spec.placement.accelerators_per_replica,
        "artifact_manifest_digest": spec.artifact.manifest_digest,
        "runtime_image": spec.runtime.image,
        "template_digest": spec.runtime.template_ref.digest,
        "cache_tier": spec.cache.tier,
        "samples": samples(*seconds),
    }
    values.update(overrides)
    return FastStartEvidence(**values)


def with_evidence(installed: InfrastructureEnvelope, *items: FastStartEvidence) -> InfrastructureEnvelope:
    qualification = installed.qualifications["qwen.3-8b"].model_copy(update={"fast_start_evidence": list(items)})
    return installed.model_copy(update={"qualifications": {qualification.model_ref: qualification}})


def with_fast_start(spec: ModelDeploymentSpec, **policy: Any) -> ModelDeploymentSpec:
    return spec.model_copy(update={"fast_start": FastStartSpec(**policy)})


def qualified_l2_envelope() -> InfrastructureEnvelope:
    """pool-a qualifies L3 (p95 58s); the slower pool-b binds the model at L2 (p95 110s)."""

    base = model_spec()
    pools = envelope().pools
    return with_evidence(
        envelope(),
        evidence(base, pools["pool-a"], seconds=[40, 45, 50, 55, 58], mechanism="regional-oci-cache"),
        evidence(base, pools["pool-b"], seconds=[70, 80, 90, 100, 110], receipt_digest=digest("e")),
    )


def test_levels_carry_the_product_targets_and_hot_is_runtime_state_not_a_level() -> None:
    assert LEVEL_TARGET_SECONDS == {
        FastStartLevel.OFF: None,
        FastStartLevel.L1: 300,
        FastStartLevel.L2: 120,
        FastStartLevel.L3: 60,
        FastStartLevel.L4: 30,
    }
    assert [level.rank for level in FastStartLevel] == [0, 1, 2, 3, 4]
    assert FastStartLevel.OFF.target_seconds is None and FastStartLevel.L4.target_seconds == 30
    assert "Hot" not in {level.value for level in FastStartLevel}


def test_fixed_and_automatic_combinations_are_validated_with_defaults() -> None:
    default = FastStartSpec()
    assert default.mode is FastStartMode.FIXED
    assert default.level is FastStartLevel.OFF
    assert default.fallback_policy is FastStartFallbackPolicy.ALLOW_LOWER_LEVEL
    assert default == FastStartSpec.model_validate({"mode": "Fixed", "level": "Off"})
    assert default.requested_level is FastStartLevel.OFF

    automatic = FastStartSpec(mode="Automatic")
    assert (automatic.minimum_level, automatic.maximum_level) == (FastStartLevel.OFF, FastStartLevel.L4)
    assert automatic.requested_level is FastStartLevel.L4 and automatic.required_level is FastStartLevel.OFF
    bounded = FastStartSpec.model_validate({"mode": "Automatic", "minimumLevel": "L2", "maximumLevel": "L3"})
    assert bounded.required_level is FastStartLevel.L2 and bounded.requested_level is FastStartLevel.L3

    with pytest.raises(ValueError, match="belong to Automatic"):
        FastStartSpec(mode="Fixed", level="L2", maximum_level="L3")
    with pytest.raises(ValueError, match="belongs to Fixed"):
        FastStartSpec(mode="Automatic", level="L2")
    with pytest.raises(ValueError, match="cannot exceed"):
        FastStartSpec(mode="Automatic", minimum_level="L3", maximum_level="L1")
    with pytest.raises(ValueError):
        FastStartSpec(mode="Fixed", level="Hot")


def test_default_fast_start_keeps_every_existing_spec_digest_stable() -> None:
    spec = model_spec()
    wire = spec.model_dump(mode="json", by_alias=True)
    assert wire["fastStart"] == {
        "mode": "Fixed",
        "level": "Off",
        "minimumLevel": None,
        "maximumLevel": None,
        "fallbackPolicy": "AllowLowerLevel",
    }
    legacy = {key: value for key, value in wire.items() if key != "fastStart"}
    assert ModelDeploymentSpec.model_validate(legacy) == spec
    legacy["placement"]["poolRefs"] = sorted(legacy["placement"]["poolRefs"])
    legacy["exposure"]["openAIAliases"] = sorted(legacy["exposure"]["openAIAliases"])
    legacy["policy"]["allowedPrincipalIds"] = sorted(legacy["policy"]["allowedPrincipalIds"])
    assert spec_digest(spec) == canonical_digest(legacy)
    explicit_default = with_fast_start(spec, mode="Fixed", level="Off", fallback_policy="AllowLowerLevel")
    assert spec_digest(explicit_default) == spec_digest(spec)
    assert spec_digest(with_fast_start(spec, mode="Fixed", level="L1")) != spec_digest(spec)
    assert spec_digest(with_fast_start(spec, mode="Automatic")) != spec_digest(spec)


def test_nearest_rank_percentiles_rank_failures_after_every_duration() -> None:
    assert nearest_rank([], 0.95) is None
    assert nearest_rank([10.0, 20.0, 30.0, 40.0, 50.0], 0.5) == 30.0
    assert nearest_rank([10.0, 20.0, 30.0, 40.0, 50.0], 0.95) == 50.0
    assert nearest_rank([50.0, 10.0, 40.0, 20.0, None], 0.5) == 40.0
    assert nearest_rank([10.0, 20.0, 30.0, 40.0, None], 0.95) is None
    assert nearest_rank([10.0, 20.0, 30.0, 40.0, None], 0.5) == 30.0
    twenty = [float(value) for value in range(1, 20)] + [None]
    assert nearest_rank(twenty, 0.95) == 19.0


def test_absent_evidence_is_unavailable_and_unqualified_never_zero() -> None:
    spec = with_fast_start(model_spec(), mode="Fixed", level="L2")
    assessment = evaluate_fast_start(spec, envelope(), evaluation_time=NOW)
    assert assessment.requested_level is FastStartLevel.L2 and assessment.requested_target_seconds == 120
    assert assessment.qualified_level is FastStartLevel.OFF
    assert assessment.assigned_level is FastStartLevel.OFF and assessment.target_seconds is None
    assert assessment.qualification.state is FastStartQualificationState.FALLBACK
    assert assessment.qualification.reason == "RequestedLevelUnqualified"
    assert assessment.model_start is None and assessment.capacity_wait is None and assessment.end_to_end is None
    assert {pool.pool_ref: pool.reason for pool in assessment.pools} == {
        "pool-a": "NoCompatibleBenchmarkEvidence",
        "pool-b": "NoCompatibleBenchmarkEvidence",
    }
    projected = FastStartStatus(**assessment.model_dump()).model_dump(mode="json", by_alias=True, exclude_none=True)
    assert projected["assignedLevel"] == "Off"
    for absent in ("modelStart", "capacityWait", "endToEnd", "targetSeconds", "effectiveLevel", "hot"):
        assert absent not in projected
    assert all("modelStart" not in pool for pool in projected["pools"])

    default = evaluate_fast_start(model_spec(), envelope(), evaluation_time=NOW)
    assert default.qualification.state is FastStartQualificationState.NO_TARGET
    assert default.assigned_level is FastStartLevel.OFF and default.requested_target_seconds is None


def test_mechanism_names_and_incompatible_tuples_never_qualify() -> None:
    spec = with_fast_start(model_spec(), mode="Fixed", level="L4")
    pool_a, pool_b = envelope().pools["pool-a"], envelope().pools["pool-b"]
    fast = [5.0, 6.0, 7.0, 8.0, 9.0]
    incompatible = [
        evidence(spec, pool_a, seconds=fast, mechanism="cuda-criu-snapshot", template_digest=digest("9")),
        evidence(
            spec,
            pool_a,
            seconds=fast,
            mechanism="host-ram-residency",
            runtime_image=f"registry.example/fs2/vllm@{digest('8')}",
        ),
        evidence(spec, pool_a, seconds=fast, mechanism="model-express", artifact_manifest_digest=digest("7")),
        evidence(spec, pool_a, seconds=fast, mechanism="local-nvme", cache_tier=CacheTier.SHARED_FILESYSTEM),
        evidence(spec, pool_a, seconds=fast, accelerators_per_replica=2),
        evidence(spec, pool_a, seconds=fast, accelerator_class="vendor-other-gpu"),
        evidence(spec, pool_a, seconds=fast, pool_ref="pool-b"),
        evidence(spec, pool_a, seconds=fast, snapshot_digest=digest("6")),
        evidence(spec, pool_a, seconds=fast, valid_until=NOW - timedelta(seconds=1)),
        evidence(spec, pool_b, seconds=fast),
    ]
    assessment = evaluate_fast_start(spec, with_evidence(envelope(), *incompatible), evaluation_time=NOW)
    by_pool = {pool.pool_ref: pool for pool in assessment.pools}
    assert by_pool["pool-a"].qualified_level is FastStartLevel.OFF
    assert by_pool["pool-a"].reason == "NoCompatibleBenchmarkEvidence"
    assert by_pool["pool-a"].mechanisms == [] and by_pool["pool-a"].model_start is None
    assert by_pool["pool-b"].qualified_level is FastStartLevel.L4
    # A heterogeneous placement is bound by its slowest pool, so nothing is claimed.
    assert assessment.qualified_level is FastStartLevel.OFF
    assert assessment.qualification.state is FastStartQualificationState.FALLBACK

    with pytest.raises(ValueError):
        evidence(spec, pool_a, seconds=fast, measurement_basis="ActivationToReady")

    sparse = evaluate_fast_start(
        spec,
        with_evidence(envelope(), evidence(spec, pool_a, seconds=fast[: MINIMUM_QUALIFYING_SAMPLES - 1])),
        evaluation_time=NOW,
    )
    pool_a_sparse = next(pool for pool in sparse.pools if pool.pool_ref == "pool-a")
    assert pool_a_sparse.qualified_level is FastStartLevel.OFF
    assert pool_a_sparse.reason == "InsufficientBenchmarkSamples"
    assert pool_a_sparse.model_start is not None and pool_a_sparse.model_start.sample_count == 4

    failing = evaluate_fast_start(
        spec,
        with_evidence(envelope(), evidence(spec, pool_a, seconds=[5.0, 6.0, 7.0, 8.0, None])),
        evaluation_time=NOW,
    )
    pool_a_failing = next(pool for pool in failing.pools if pool.pool_ref == "pool-a")
    assert pool_a_failing.qualified_level is FastStartLevel.OFF
    assert pool_a_failing.reason == "BenchmarkFailuresExceedPercentile"
    assert pool_a_failing.model_start is not None
    assert pool_a_failing.model_start.failed_count == 1
    assert pool_a_failing.model_start.p95_seconds is None and pool_a_failing.model_start.p50_seconds == 7.0
    assert pool_a_failing.model_start.latest_seconds is None


def test_fixed_mode_honours_requests_only_with_evidence_then_falls_back_or_rejects() -> None:
    base = model_spec()
    installed = qualified_l2_envelope()

    honoured = evaluate_fast_start(with_fast_start(base, level="L2"), installed, evaluation_time=NOW)
    assert honoured.qualification.state is FastStartQualificationState.QUALIFIED
    assert honoured.qualification.reason == "BenchmarkEvidenceQualified"
    assert honoured.assigned_level is FastStartLevel.L2 and honoured.target_seconds == 120
    assert honoured.qualified_level is FastStartLevel.L2
    assert honoured.model_start is not None
    assert (honoured.model_start.p50_seconds, honoured.model_start.p95_seconds) == (90.0, 110.0)
    assert honoured.model_start.latest_seconds == 110.0 and honoured.model_start.sample_count == 5
    assert [pool.qualified_level for pool in honoured.pools] == [FastStartLevel.L3, FastStartLevel.L2]
    assert honoured.pools[0].mechanisms == ["regional-oci-cache"]
    assert honoured.pools[1].receipt_digests == [digest("e")]

    fallback = evaluate_fast_start(with_fast_start(base, level="L3"), installed, evaluation_time=NOW)
    assert fallback.qualification.state is FastStartQualificationState.FALLBACK
    assert fallback.qualification.reason == "RequestedLevelUnqualified"
    assert (fallback.requested_level, fallback.assigned_level) == (FastStartLevel.L3, FastStartLevel.L2)
    assert (fallback.requested_target_seconds, fallback.target_seconds) == (60, 120)

    strict = with_fast_start(base, level="L3", fallback_policy="RequireTarget")
    decision = validate_model_deployment(strict, installed, evaluation_time=NOW)
    assert decision.disposition is ValidationDisposition.REJECTED
    assert [(issue.code, issue.path) for issue in decision.issues] == [
        ("fast_start_target_unqualified", "$.spec.fastStart.level")
    ]
    assert decision.fast_start is not None
    assert decision.fast_start.qualification.state is FastStartQualificationState.UNQUALIFIED
    assert decision.fast_start.assigned_level is None and decision.fast_start.target_seconds is None

    accepted = validate_model_deployment(
        with_fast_start(base, level="L2", fallback_policy="RequireTarget"), installed, evaluation_time=NOW
    )
    assert accepted.disposition is ValidationDisposition.ACCEPTED
    assert accepted.fast_start is not None and accepted.fast_start.assigned_level is FastStartLevel.L2


def test_automatic_mode_selects_only_qualified_levels_inside_bounds() -> None:
    base = model_spec()
    installed = qualified_l2_envelope()

    clamped = evaluate_fast_start(
        with_fast_start(base, mode="Automatic", minimum_level="L1", maximum_level="L1"),
        installed,
        evaluation_time=NOW,
    )
    assert clamped.qualification.state is FastStartQualificationState.QUALIFIED
    assert clamped.qualification.reason == "AutomaticLevelSelected"
    assert clamped.assigned_level is FastStartLevel.L1 and clamped.target_seconds == 300
    assert clamped.qualified_level is FastStartLevel.L2

    best = evaluate_fast_start(with_fast_start(base, mode="Automatic"), installed, evaluation_time=NOW)
    assert best.requested_level is FastStartLevel.L4 and best.requested_target_seconds == 30
    assert (best.minimum_level, best.maximum_level) == (FastStartLevel.OFF, FastStartLevel.L4)
    assert best.assigned_level is FastStartLevel.L2 and best.target_seconds == 120

    below = evaluate_fast_start(
        with_fast_start(base, mode="Automatic", minimum_level="L3", maximum_level="L4"),
        installed,
        evaluation_time=NOW,
    )
    assert below.qualification.state is FastStartQualificationState.FALLBACK
    assert below.qualification.reason == "MinimumLevelUnqualified"
    assert below.assigned_level is FastStartLevel.L2

    strict = with_fast_start(
        base, mode="Automatic", minimum_level="L3", maximum_level="L4", fallback_policy="RequireTarget"
    )
    decision = validate_model_deployment(strict, installed, evaluation_time=NOW)
    assert decision.disposition is ValidationDisposition.REJECTED
    assert [(issue.code, issue.path) for issue in decision.issues] == [
        ("fast_start_target_unqualified", "$.spec.fastStart.minimumLevel")
    ]

    nothing = evaluate_fast_start(with_fast_start(base, mode="Automatic"), envelope(), evaluation_time=NOW)
    assert nothing.qualification.state is FastStartQualificationState.NO_TARGET
    assert nothing.assigned_level is FastStartLevel.OFF and nothing.model_start is None

    off_only = evaluate_fast_start(
        with_fast_start(base, mode="Automatic", minimum_level="Off", maximum_level="Off"),
        installed,
        evaluation_time=NOW,
    )
    assert off_only.qualification.state is FastStartQualificationState.NO_TARGET
    assert off_only.qualified_level is FastStartLevel.L2 and off_only.assigned_level is FastStartLevel.OFF


def test_capacity_wait_and_end_to_end_are_measured_separately_from_model_start() -> None:
    base = model_spec()
    single = base.model_copy(update={"placement": base.placement.model_copy(update={"pool_refs": ["pool-a"]})})
    spec = with_fast_start(single, level="L3")
    pool_a = envelope().pools["pool-a"]
    slow_capacity = evidence(spec, pool_a, seconds=[50.0] * 5)
    slow_capacity = slow_capacity.model_copy(
        update={"samples": samples(50.0, 50.0, 50.0, 50.0, 50.0, capacity_wait=400.0, end_to_end=460.0)}
    )
    assessment = evaluate_fast_start(spec, with_evidence(envelope(), slow_capacity), evaluation_time=NOW)
    assert assessment.qualification.state is FastStartQualificationState.QUALIFIED
    assert assessment.assigned_level is FastStartLevel.L3
    assert assessment.capacity_wait is not None and assessment.capacity_wait.p95_seconds == 400.0
    assert assessment.end_to_end is not None and assessment.end_to_end.p95_seconds == 460.0
    assert assessment.end_to_end.failed_count == 0 and assessment.end_to_end.latest_seconds == 460.0

    unmeasured = evaluate_fast_start(
        spec, with_evidence(envelope(), evidence(spec, pool_a, seconds=[50.0] * 5)), evaluation_time=NOW
    )
    assert unmeasured.model_start is not None
    assert unmeasured.capacity_wait is None and unmeasured.end_to_end is None

    with pytest.raises(ValueError, match="end-to-end"):
        FastStartSample(observed_at=NOW, model_start_seconds=50.0, end_to_end_seconds=40.0)
    with pytest.raises(ValueError, match="end-to-end"):
        FastStartSample(observed_at=NOW, capacity_wait_seconds=50.0, end_to_end_seconds=40.0)


def test_status_projection_round_trips_through_kubernetes_camel_case_records() -> None:
    assessment = evaluate_fast_start(
        with_fast_start(model_spec(), level="L2"), qualified_l2_envelope(), evaluation_time=NOW
    )
    status = FastStartStatus(**assessment.model_dump(), effective_level=FastStartLevel.L2, hot=True)
    wire = status.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert wire["modelStart"]["p95Seconds"] == 110.0 and wire["effectiveLevel"] == "L2" and wire["hot"] is True
    assert "minimumLevel" not in wire and "capacityWait" not in wire
    assert wire["pools"][0]["receiptDigests"] == [digest("f")]
    restored = FastStartStatus.model_validate(_normalize_keys(wire))
    assert restored == status
