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
from fs2_serve.fast_start_identity import (
    ENVIRONMENT_QUALIFICATION_SCHEMA,
    MEASUREMENT_CONTRACT_SCHEMA,
    RUNTIME_CONTRACT_SCHEMA,
    EnvironmentMember,
    EvidenceIdentityState,
    FastStartEnvironmentBinding,
    FastStartRuntimeContract,
    RuntimeCacheIdentity,
    RuntimeContractIdentity,
    RuntimeEnvironmentIdentity,
    RuntimeEvidenceIdentity,
    RuntimeMeasurementIdentity,
    RuntimePlacementIdentity,
    mechanism_config_digest,
)
from fs2_serve.fast_start_mechanisms import (
    FastStartMechanism,
    GpuResidentQualification,
    HostMemoryResidencyQualification,
    RegionalCacheQualification,
    ResidencyHolder,
    RetainedCompileCache,
)
from fs2_serve.model_deployment import (
    CacheTier,
    FastStartEvidence,
    FastStartSample,
    InfrastructureEnvelope,
    ModelDeploymentSpec,
    ModelExpressPoolTransport,
    ModelExpressQualification,
    PoolEnvelope,
    ValidationDisposition,
    canonical_digest,
    spec_digest,
    validate_model_deployment,
)
from fs2_serve.model_deployment_bridge import _normalize_keys

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
STORAGE_CONTRACT_DIGEST = digest("5")


def runtime_contract(spec: ModelDeploymentSpec, pool_ref: str = "pool-a") -> FastStartRuntimeContract:
    runtime_payload = {
        "schema": RUNTIME_CONTRACT_SCHEMA,
        "modelRef": spec.model_ref,
        "sourceRevision": spec.artifact.revision,
        "modelContentDigest": spec.artifact.manifest_digest,
        "artifactManifestDigest": spec.artifact.manifest_digest,
        "runtimeProfile": spec.runtime.profile,
        "runtimeImage": spec.runtime.image,
        "templateDigest": spec.runtime.template_ref.digest,
        "renderContractDigest": digest("1"),
        "argvDigest": digest("2"),
        "environmentDigest": digest("3"),
    }
    runtime = RuntimeContractIdentity.model_validate(
        {**runtime_payload, "runtimeContractDigest": canonical_digest(runtime_payload)}
    )
    measurement_payload = {
        "schema": MEASUREMENT_CONTRACT_SCHEMA,
        "basis": "CapacityAvailableToSemanticReady",
        "payloadDigest": digest("4"),
        "protocol": "OpenAIChatCompletions",
        "endpointPath": "/v1/chat/completions",
        "streaming": True,
        "semanticValidatorDigest": digest("6"),
        "benchmarkClientDigest": digest("7"),
        "clientPlacement": "in-cluster",
    }
    measurement = RuntimeMeasurementIdentity.model_validate(
        {**measurement_payload, "contractDigest": canonical_digest(measurement_payload)}
    )
    return FastStartRuntimeContract(
        pool_ref=pool_ref,
        runtime=runtime,
        storage_contract_digest=STORAGE_CONTRACT_DIGEST,
        measurement=measurement,
    )


def environment_identity(pool: PoolEnvelope) -> RuntimeEnvironmentIdentity:
    payload = {
        "schema": ENVIRONMENT_QUALIFICATION_SCHEMA,
        "scopeDigest": canonical_digest({"pool": pool.pool_id, "capacityType": pool.capacity_type}),
        "acceleratorDigest": canonical_digest({"acceleratorClass": pool.accelerator_class}),
        "driverCudaDigest": digest("8"),
        "hostRuntimeDigest": digest("9"),
        "storageRuntimeDigest": digest("0"),
    }
    return RuntimeEnvironmentIdentity.model_validate({**payload, "qualificationDigest": canonical_digest(payload)})


def qualify_for_fast_start(installed: InfrastructureEnvelope) -> InfrastructureEnvelope:
    base_spec = model_spec()
    pools = {
        key: pool.model_copy(
            update={
                "startup_scenario": "fresh-node-zero-pod" if pool.min_nodes == 0 else "prepared-node-zero-pod",
                "fast_start_environment_bindings": [
                    FastStartEnvironmentBinding(
                        environment=environment_identity(pool),
                        members=[EnvironmentMember(pool_ref=pool.pool_id, capacity_type=pool.capacity_type)],
                        cache_tier=base_spec.cache.tier.value,
                        startup_scenario=("fresh-node-zero-pod" if pool.min_nodes == 0 else "prepared-node-zero-pod"),
                        valid_until=NOW + timedelta(days=30),
                    )
                ],
            }
        )
        for key, pool in installed.pools.items()
    }
    qualifications = {
        key: item.model_copy(
            update={"fast_start_runtime_contracts": [runtime_contract(base_spec, pool_ref) for pool_ref in pools]}
        )
        for key, item in installed.qualifications.items()
    }
    return installed.model_copy(update={"pools": pools, "qualifications": qualifications})


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
        "compatibility_tuple_complete": True,
        "measurement_basis": "CapacityAvailableToSemanticReady",
        "accelerator_class": pool.accelerator_class,
        "pool_ref": pool.pool_id,
        "capacity_type": pool.capacity_type,
        "accelerators_per_replica": spec.placement.accelerators_per_replica,
        "artifact_manifest_digest": spec.artifact.manifest_digest,
        "runtime_image": spec.runtime.image,
        "template_digest": spec.runtime.template_ref.digest,
        "cache_tier": spec.cache.tier,
        "samples": samples(*seconds),
    }
    values.update(overrides)
    values.setdefault(
        "compatibility_tuple_digest",
        canonical_digest(
            {
                "mechanism": values["mechanism"],
                "mechanismConfigDigest": values.get("mechanism_config_digest"),
                "acceleratorClass": values["accelerator_class"],
                "poolRef": values["pool_ref"],
                "acceleratorsPerReplica": values["accelerators_per_replica"],
                "artifactManifestDigest": values["artifact_manifest_digest"],
                "runtimeImage": values["runtime_image"],
                "templateDigest": values["template_digest"],
                "cacheTier": str(values["cache_tier"]),
                "snapshotDigest": values.get("snapshot_digest"),
            }
        ),
    )
    identity_state = EvidenceIdentityState(values.get("identity_state", EvidenceIdentityState.BOUND))
    if identity_state is not EvidenceIdentityState.LEGACY_UNBOUND:
        contract = runtime_contract(spec, pool.pool_id)
        runtime_payload = contract.runtime.model_dump(
            mode="json",
            by_alias=True,
            exclude={"runtime_contract_digest"},
        )
        runtime_payload.update(
            {
                "modelContentDigest": values["artifact_manifest_digest"],
                "artifactManifestDigest": values["artifact_manifest_digest"],
                "runtimeImage": values["runtime_image"],
                "templateDigest": values["template_digest"],
            }
        )
        observed_runtime = RuntimeContractIdentity.model_validate(
            {**runtime_payload, "runtimeContractDigest": canonical_digest(runtime_payload)}
        )
        tier = values["cache_tier"]
        tier_value = tier.value if isinstance(tier, CacheTier) else tier
        config_digest = values.get("mechanism_config_digest") or mechanism_config_digest(
            mechanism=values["mechanism"],
            storage_contract_digest=STORAGE_CONTRACT_DIGEST,
        )
        identity = RuntimeEvidenceIdentity(
            runtime=observed_runtime,
            environment=environment_identity(pool),
            placement=RuntimePlacementIdentity(
                accelerator_class=values["accelerator_class"],
                accelerators_per_replica=values["accelerators_per_replica"],
                topology_policy=spec.placement.topology_policy.value,
                startup_scenario="fresh-node-zero-pod" if pool.min_nodes == 0 else "prepared-node-zero-pod",
            ),
            cache=RuntimeCacheIdentity(
                tier=tier_value,
                mechanism=values["mechanism"],
                mechanism_config_digest=config_digest,
                snapshot_digest=values.get("snapshot_digest"),
                storage_contract_digest=STORAGE_CONTRACT_DIGEST,
            ),
            measurement=contract.measurement,
        )
        values.update(
            {
                "identity_state": EvidenceIdentityState.BOUND,
                "identity_digest": identity.digest,
                "identity": identity,
                "mechanism_config_digest": config_digest,
            }
        )
    return FastStartEvidence(**values)


def with_evidence(installed: InfrastructureEnvelope, *items: FastStartEvidence) -> InfrastructureEnvelope:
    installed = qualify_for_fast_start(installed)
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
        evidence(
            base,
            pools["pool-a"],
            seconds=[40] * 10 + [45] * 5 + [50] * 3 + [58] * 2,
            mechanism="regional-oci-cache",
        ),
        evidence(
            base,
            pools["pool-b"],
            seconds=[70] * 5 + [80] * 4 + [90] * 5 + [100] * 4 + [110] * 2,
            receipt_digest=digest("e"),
        ),
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
    assert wire["cache"]["mechanism"] is None
    legacy = {key: value for key, value in wire.items() if key != "fastStart"}
    legacy["cache"] = {key: value for key, value in legacy["cache"].items() if key != "mechanism"}
    assert ModelDeploymentSpec.model_validate(legacy) == spec
    legacy["placement"]["poolRefs"] = sorted(legacy["placement"]["poolRefs"])
    legacy["exposure"]["openAIAliases"] = sorted(legacy["exposure"]["openAIAliases"])
    legacy["policy"]["allowedPrincipalIds"] = sorted(legacy["policy"]["allowedPrincipalIds"])
    assert spec_digest(spec) == canonical_digest(legacy)
    # Pinned to the digest the released contract produced before the optional
    # fast-start policy and the optional cold-start mechanism were added, so a
    # later optional field can never silently roll a running workload.
    assert spec_digest(spec) == "sha256:092bab27467b2a92ccfba642ba13cbd2896bdbde3e85080ebf687d105987f000"
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


def test_v2_identity_is_bound_and_mutable_policy_does_not_change_it() -> None:
    base = model_spec()
    pool = envelope().pools["pool-a"]
    proof = evidence(base, pool, seconds=[90.0] * MINIMUM_QUALIFYING_SAMPLES)

    assert proof.identity_state is EvidenceIdentityState.BOUND
    assert proof.identity is not None and proof.identity_digest == proof.identity.digest
    assert proof.identity.runtime.runtime_contract_digest == runtime_contract(base).runtime.runtime_contract_digest
    changed_policy = with_fast_start(
        base.model_copy(
            update={
                "availability": base.availability.model_copy(
                    update={"min_replicas": 1, "max_replicas": 8, "target_queue_depth": 4}
                )
            }
        ),
        mode="Automatic",
        minimum_level="L1",
        maximum_level="L4",
    )
    assert (
        runtime_contract(changed_policy).runtime.runtime_contract_digest
        == proof.identity.runtime.runtime_contract_digest
    )


def test_v1_unbound_evidence_is_retained_but_cannot_qualify() -> None:
    base = model_spec()
    spec = with_fast_start(
        base.model_copy(update={"placement": base.placement.model_copy(update={"pool_refs": ["pool-a"]})}),
        level="L2",
    )
    legacy = evidence(
        base,
        envelope().pools["pool-a"],
        seconds=[1.0] * MINIMUM_QUALIFYING_SAMPLES,
        identity_state=EvidenceIdentityState.LEGACY_UNBOUND,
    )

    assessment = evaluate_fast_start(spec, with_evidence(envelope(), legacy), evaluation_time=NOW)

    assert assessment.qualified_level is FastStartLevel.OFF
    assert assessment.pools[0].paths == []
    retained = assessment.pools[0].retained_paths[0]
    assert retained.identity_state is EvidenceIdentityState.LEGACY_UNBOUND
    assert retained.reason == "LegacyIdentityUnbound"
    assert [(item.code, item.field) for item in retained.mismatches] == [("LegacyUnbound", "$.identity")]


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
        "pool-a": "NoCurrentRuntimeEvidence",
        "pool-b": "NoCurrentRuntimeEvidence",
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
    fast = [5.0, 6.0, 7.0, 8.0, 9.0] * 4
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
    assert by_pool["pool-a"].reason == "NoCurrentRuntimeEvidence"
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
    assert pool_a_sparse.model_start is not None
    assert pool_a_sparse.model_start.sample_count == MINIMUM_QUALIFYING_SAMPLES - 1

    failing = evaluate_fast_start(
        spec,
        with_evidence(envelope(), evidence(spec, pool_a, seconds=[5.0] * 19 + [None])),
        evaluation_time=NOW,
    )
    pool_a_failing = next(pool for pool in failing.pools if pool.pool_ref == "pool-a")
    assert pool_a_failing.qualified_level is FastStartLevel.OFF
    assert pool_a_failing.reason == "BenchmarkFailuresPresent"
    assert pool_a_failing.model_start is not None
    assert pool_a_failing.model_start.failed_count == 1
    assert pool_a_failing.model_start.p95_seconds == 5.0 and pool_a_failing.model_start.p50_seconds == 5.0
    assert pool_a_failing.model_start.latest_seconds is None


def test_incomplete_compatibility_tuple_never_qualifies() -> None:
    base = model_spec()
    spec = with_fast_start(
        base.model_copy(update={"placement": base.placement.model_copy(update={"pool_refs": ["pool-a"]})}),
        level="L2",
    )
    pool = envelope().pools["pool-a"]
    incomplete = evidence(
        spec,
        pool,
        seconds=[90.0] * MINIMUM_QUALIFYING_SAMPLES,
        compatibility_tuple_complete=False,
    )

    assessment = evaluate_fast_start(spec, with_evidence(envelope(), incomplete), evaluation_time=NOW)

    assert assessment.qualified_level is FastStartLevel.OFF
    assert assessment.qualification.state is FastStartQualificationState.FALLBACK
    assert assessment.pools[0].reason == "IncompleteCompatibilityTuple"
    assert assessment.pools[0].paths[0].reason == "IncompleteCompatibilityTuple"


def test_different_mechanism_cohorts_never_combine_to_qualify() -> None:
    base = model_spec()
    spec = with_fast_start(
        base.model_copy(update={"placement": base.placement.model_copy(update={"pool_refs": ["pool-a"]})}),
        level="L2",
    )
    pool = envelope().pools["pool-a"]
    conventional = evidence(spec, pool, seconds=[90.0] * 10, mechanism="conventional")
    shared = evidence(
        spec,
        pool,
        seconds=[90.0] * 10,
        mechanism="shared-cache",
        receipt_digest=digest("e"),
    )

    assessment = evaluate_fast_start(
        spec,
        with_evidence(envelope(), conventional, shared),
        evaluation_time=NOW,
    )

    assert assessment.qualified_level is FastStartLevel.OFF
    assert assessment.pools[0].model_start is not None
    assert assessment.pools[0].model_start.sample_count == 10
    assert assessment.pools[0].selected_mechanism == "conventional"
    assert assessment.pools[0].selected_compatibility_tuple_digest == conventional.compatibility_tuple_digest
    assert [(path.mechanism, path.model_start.sample_count) for path in assessment.pools[0].paths] == [
        ("conventional", 10),
        ("shared-cache", 10),
    ]


def test_same_mechanism_and_exact_tuple_receipts_form_one_cohort() -> None:
    base = model_spec()
    spec = with_fast_start(
        base.model_copy(update={"placement": base.placement.model_copy(update={"pool_refs": ["pool-a"]})}),
        level="L2",
    )
    pool = envelope().pools["pool-a"]
    tuple_digest = canonical_digest("one-exact-tuple")
    first = evidence(
        spec,
        pool,
        seconds=[90.0] * 10,
        compatibility_tuple_digest=tuple_digest,
    )
    second = evidence(
        spec,
        pool,
        seconds=[90.0] * 10,
        compatibility_tuple_digest=tuple_digest,
        receipt_digest=digest("e"),
    )

    assessment = evaluate_fast_start(
        spec,
        with_evidence(envelope(), first, second),
        evaluation_time=NOW,
    )

    assert assessment.qualified_level is FastStartLevel.L2
    assert len(assessment.pools[0].paths) == 1
    assert assessment.pools[0].selected_mechanism == "shared-cache"
    assert assessment.pools[0].selected_compatibility_tuple_digest == tuple_digest
    assert assessment.pools[0].paths[0].model_start is not None
    assert assessment.pools[0].paths[0].model_start.sample_count == MINIMUM_QUALIFYING_SAMPLES


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
    assert honoured.model_start.latest_seconds == 110.0
    assert honoured.model_start.sample_count == MINIMUM_QUALIFYING_SAMPLES
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
    slow_capacity = evidence(spec, pool_a, seconds=[50.0] * MINIMUM_QUALIFYING_SAMPLES)
    slow_capacity = slow_capacity.model_copy(
        update={"samples": samples(*([50.0] * MINIMUM_QUALIFYING_SAMPLES), capacity_wait=400.0, end_to_end=460.0)}
    )
    assessment = evaluate_fast_start(spec, with_evidence(envelope(), slow_capacity), evaluation_time=NOW)
    assert assessment.qualification.state is FastStartQualificationState.QUALIFIED
    assert assessment.assigned_level is FastStartLevel.L3
    assert assessment.capacity_wait is not None and assessment.capacity_wait.p95_seconds == 400.0
    assert assessment.end_to_end is not None and assessment.end_to_end.p95_seconds == 460.0
    assert assessment.end_to_end.failed_count == 0 and assessment.end_to_end.latest_seconds == 460.0

    unmeasured = evaluate_fast_start(
        spec,
        with_evidence(envelope(), evidence(spec, pool_a, seconds=[50.0] * MINIMUM_QUALIFYING_SAMPLES)),
        evaluation_time=NOW,
    )
    assert unmeasured.model_start is not None
    assert unmeasured.capacity_wait is None and unmeasured.end_to_end is None

    with pytest.raises(ValueError, match="end-to-end"):
        FastStartSample(observed_at=NOW, model_start_seconds=50.0, end_to_end_seconds=40.0)
    with pytest.raises(ValueError, match="end-to-end"):
        FastStartSample(observed_at=NOW, capacity_wait_seconds=50.0, end_to_end_seconds=40.0)


def test_modelexpress_evidence_requires_the_active_exact_mechanism_binding() -> None:
    base = model_spec()
    single = with_fast_start(
        base.model_copy(update={"placement": base.placement.model_copy(update={"pool_refs": ["pool-a"]})}),
        level="L4",
    )
    config = ModelExpressQualification(
        config_digest=digest("9"),
        endpoint="modelexpress.fs2-modelexpress.svc.cluster.local:8001",
        deployment_mode="managed",
        metadata_backend="kubernetes",
        runtime_adapter="vllm",
        client_package_version="0.5.1",
        coordinator_network_type="pod-selector",
        coordinator_namespace="fs2-modelexpress",
        coordinator_pod_labels={"fs2-serve.nebius.ai/component": "modelexpress-server"},
        coordinator_cidrs=[],
        pool_refs=["pool-a"],
        pool_transports={"pool-a": ModelExpressPoolTransport()},
    )
    proof = evidence(
        single,
        envelope().pools["pool-a"],
        seconds=[20.0] * MINIMUM_QUALIFYING_SAMPLES,
        mechanism="modelexpress",
        mechanism_config_digest=config.config_digest,
    )
    installed = qualify_for_fast_start(envelope())
    qualification = installed.qualifications["qwen.3-8b"].model_copy(
        update={"model_express": config, "fast_start_evidence": [proof]}
    )
    installed = installed.model_copy(update={"qualifications": {qualification.model_ref: qualification}})
    assert evaluate_fast_start(single, installed, evaluation_time=NOW).assigned_level is FastStartLevel.L4

    assert proof.identity is not None
    stale_identity = proof.identity.model_copy(
        update={"cache": proof.identity.cache.model_copy(update={"mechanism_config_digest": digest("8")})}
    )
    stale = proof.model_copy(
        update={
            "mechanism_config_digest": digest("8"),
            "identity": stale_identity,
            "identity_digest": stale_identity.digest,
        }
    )
    qualification = qualification.model_copy(update={"fast_start_evidence": [stale]})
    installed = installed.model_copy(update={"qualifications": {qualification.model_ref: qualification}})
    assessment = evaluate_fast_start(single, installed, evaluation_time=NOW)
    assert assessment.assigned_level is FastStartLevel.OFF
    assert assessment.pools[0].reason == "NoCurrentRuntimeEvidence"

    unbound = proof.model_copy(
        update={
            "identity_state": EvidenceIdentityState.LEGACY_UNBOUND,
            "identity": None,
            "identity_digest": None,
            "mechanism_config_digest": None,
        }
    )
    qualification = qualification.model_copy(update={"fast_start_evidence": [unbound]})
    installed = installed.model_copy(update={"qualifications": {qualification.model_ref: qualification}})
    assert evaluate_fast_start(single, installed, evaluation_time=NOW).assigned_level is FastStartLevel.OFF


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


def test_pinning_a_cold_start_mechanism_is_a_deliberate_spec_change() -> None:
    """Selecting a mechanism must change the digest; leaving it unset must not."""

    spec = model_spec()
    wire = spec.model_dump(mode="json", by_alias=True)

    pinned = ModelDeploymentSpec.model_validate({**wire, "cache": {**wire["cache"], "mechanism": "regional-cache"}})
    assert pinned.cache.mechanism is FastStartMechanism.REGIONAL_CACHE
    assert spec_digest(pinned) != spec_digest(spec)

    unpinned = ModelDeploymentSpec.model_validate({**wire, "cache": {**wire["cache"], "mechanism": None}})
    assert spec_digest(unpinned) == spec_digest(spec)

    # The two paths this cluster has no hardware for are not selectable at all,
    # so a revision can never ask for a mechanism the pool cannot provide.
    for refused in ("node-local-restore", "shared-restore", "modelexpress"):
        with pytest.raises(ValueError, match="not selectable"):
            ModelDeploymentSpec.model_validate({**wire, "cache": {**wire["cache"], "mechanism": refused}})


H100_CAPABILITY_SELECTOR = {
    "local-nvme.fs2.nebius/eligible": "false",
    "snapshot.fs2.nebius/eligible": "false",
}
PLACEHOLDER_DIGEST = "sha256:" + "0" * 64


def _self_digested(model: Any, **fields: Any) -> Any:
    """Build a mechanism declaration with its own canonical configDigest."""

    digest_value = model.model_construct(config_digest=PLACEHOLDER_DIGEST, **fields).expected_config_digest()
    return model(config_digest=digest_value, **fields)


def _regional_cache_declaration(pool_refs: Sequence[str], **overrides: Any) -> RegionalCacheQualification:
    fields: dict[str, Any] = {
        "image_mirror_registry": "registry.example",
        "payload_claim_name": "qwen-cache-rwx",
        "payload_content_path": "/models/qwen/payload",
        "payload_bytes": 16397461266,
        "compile_cache": RetainedCompileCache(
            claim_name="fsm-compile-cache-rwx",
            sub_path="qwen/driver-580-sm90",
            abi="driver-580-sm90",
            mount_path="/runtime-cache",
            size_limit_bytes=1 << 34,
        ),
        "pool_refs": list(pool_refs),
    }
    fields.update(overrides)
    return _self_digested(RegionalCacheQualification, **fields)


def _host_memory_declaration(pool_refs: Sequence[str], **overrides: Any) -> HostMemoryResidencyQualification:
    fields: dict[str, Any] = {
        "residency_mode": "locked-payload-residency",
        "payload_claim_name": "qwen-cache-rwx",
        "payload_content_path": "/models/qwen/payload",
        "payload_digest": digest("a"),
        "payload_bytes": 16397461266,
        "reserved_bytes": 19327352832,
        "node_allocatable_bytes": 1648745732096,
        "holder": ResidencyHolder(
            name="fsm-hostmem-qwen",
            namespace="fs2-models",
            receipt_claim_name="fsm-residency-receipt-rwx",
            receipt_mount_path="/residency",
        ),
        "receipt_max_age_seconds": 180,
        "pool_refs": list(pool_refs),
    }
    fields.update(overrides)
    return _self_digested(HostMemoryResidencyQualification, **fields)


def _gpu_resident_declaration(pool_refs: Sequence[str], **overrides: Any) -> GpuResidentQualification:
    fields: dict[str, Any] = {
        "residency_mode": "standby-engine",
        "standby_replicas": 1,
        "accelerators_per_standby_replica": 1,
        "minimum_hot_replicas": 1,
        "promotion_probe_period_seconds": 1,
        "pool_refs": list(pool_refs),
    }
    fields.update(overrides)
    return _self_digested(GpuResidentQualification, **fields)


def _declared_envelope(
    *,
    mechanism: str,
    cache_tier: CacheTier = CacheTier.SHARED_FILESYSTEM,
    pool_refs: Sequence[str] = ("pool-a",),
    **declaration_overrides: Any,
) -> tuple[ModelDeploymentSpec, InfrastructureEnvelope]:
    """A spec that pins ``mechanism`` and an envelope that declares it."""

    base = envelope()
    qualification = base.qualifications["qwen.3-8b"]
    pools = {
        key: value.model_copy(update={"node_selector": {**value.node_selector, **H100_CAPABILITY_SELECTOR}})
        for key, value in base.pools.items()
    }
    declarations: dict[str, Any] = {}
    if mechanism == FastStartMechanism.REGIONAL_CACHE.value:
        declarations["regional_cache"] = _regional_cache_declaration(pool_refs, **declaration_overrides)
    elif mechanism == FastStartMechanism.HOST_MEMORY_RESIDENCY.value:
        declarations["host_memory_residency"] = _host_memory_declaration(pool_refs, **declaration_overrides)
    elif mechanism == FastStartMechanism.GPU_RESIDENT.value:
        declarations["gpu_resident"] = _gpu_resident_declaration(pool_refs, **declaration_overrides)
    updated = InfrastructureEnvelope(
        **{
            **base.model_dump(),
            "pools": {key: value.model_dump() for key, value in pools.items()},
            "qualifications": {
                "qwen.3-8b": {
                    **qualification.model_dump(),
                    "template_cache_tiers": {digest("c"): cache_tier.value},
                    **{key: value.model_dump() for key, value in declarations.items()},
                }
            },
        }
    )
    spec = model_spec()
    wire = spec.model_dump(mode="json", by_alias=True)
    wire["cache"] = {**wire["cache"], "tier": cache_tier.value, "mechanism": mechanism}
    return ModelDeploymentSpec.model_validate(wire), updated


def _codes(spec: ModelDeploymentSpec, infrastructure: InfrastructureEnvelope) -> set[str]:
    decision = validate_model_deployment(spec, infrastructure, evaluation_time=NOW)
    return {issue.code for issue in decision.issues}


def test_a_pinned_mechanism_needs_a_reviewed_declaration_from_terraform() -> None:
    spec, infrastructure = _declared_envelope(mechanism="regional-cache")
    assert "fast_start_mechanism_declaration_required" not in _codes(spec, infrastructure)

    undeclared = InfrastructureEnvelope(
        **{
            **infrastructure.model_dump(),
            "qualifications": {
                "qwen.3-8b": {
                    **infrastructure.qualifications["qwen.3-8b"].model_dump(),
                    "regional_cache": None,
                }
            },
        }
    )
    decision = validate_model_deployment(spec, undeclared, evaluation_time=NOW)
    assert "fast_start_mechanism_declaration_required" in {issue.code for issue in decision.issues}
    # The gap is Terraform's to close, and the exact input is named.
    assert "fast_start_mechanisms.qwen.3-8b.regional-cache" in decision.terraform_inputs


def test_a_retained_payload_mechanism_needs_the_shared_filesystem_tier() -> None:
    spec, infrastructure = _declared_envelope(mechanism="regional-cache", cache_tier=CacheTier.NODE_LOCAL)
    assert "fast_start_mechanism_cache_tier_incompatible" in _codes(spec, infrastructure)


def test_a_pinned_mechanism_must_be_declared_for_every_placement_pool() -> None:
    spec, infrastructure = _declared_envelope(mechanism="host-memory-residency", pool_refs=("pool-b",))
    assert "fast_start_mechanism_pool_unqualified" in _codes(spec, infrastructure)


def test_gpu_resident_is_refused_until_the_promotion_controller_exists() -> None:
    with pytest.raises(ValueError, match="not selectable"):
        _declared_envelope(mechanism="gpu-resident", minimum_hot_replicas=2)


def _mechanism_envelope(declaration: Any, *, mechanism: str) -> InfrastructureEnvelope:
    """An envelope that declares ``mechanism`` for every pool of the model."""

    base = qualify_for_fast_start(envelope())
    qualification = base.qualifications["qwen.3-8b"]
    field = {
        "regional-cache": "regional_cache",
        "host-memory-residency": "host_memory_residency",
        "gpu-resident": "gpu_resident",
    }[mechanism]
    return base.model_copy(
        update={"qualifications": {"qwen.3-8b": qualification.model_copy(update={field: declaration})}}
    )


def test_a_configured_mechanism_with_too_few_samples_still_qualifies_nothing() -> None:
    """A mechanism being present and configured never raises a level."""

    declaration = _regional_cache_declaration(("pool-a", "pool-b"))
    installed = _mechanism_envelope(declaration, mechanism="regional-cache")
    spec = ModelDeploymentSpec.model_validate(
        {
            **model_spec().model_dump(mode="json", by_alias=True),
            "cache": {**model_spec().model_dump(mode="json", by_alias=True)["cache"], "mechanism": "regional-cache"},
        }
    )
    bound = mechanism_config_digest(
        mechanism="regional-cache",
        storage_contract_digest=STORAGE_CONTRACT_DIGEST,
        declaration_digest=declaration.config_digest,
    )
    qualification = installed.qualifications["qwen.3-8b"].model_copy(
        update={
            "fast_start_evidence": [
                evidence(
                    spec,
                    installed.pools[pool_ref],
                    seconds=[41.0, 42.0, 43.0],
                    mechanism="regional-cache",
                    mechanism_config_digest=bound,
                )
                for pool_ref in ("pool-a", "pool-b")
            ]
        }
    )
    installed = installed.model_copy(update={"qualifications": {"qwen.3-8b": qualification}})

    assessment = evaluate_fast_start(spec, installed, evaluation_time=NOW)
    # Three failure-free 42-second attempts would look like L3 to a careless
    # reader. The rule is 20, so the qualified level stays Off.
    pool = next(item for item in assessment.pools if item.pool_ref == "pool-a")
    assert pool.paths[0].mechanism == "regional-cache"
    assert pool.paths[0].model_start is not None
    assert pool.paths[0].model_start.p95_seconds == 43.0
    assert pool.reason == "InsufficientBenchmarkSamples"
    assert assessment.qualified_level is FastStartLevel.OFF
    assert assessment.assigned_level is FastStartLevel.OFF


def test_a_pinned_mechanism_refuses_another_mechanisms_cohort() -> None:
    declaration = _regional_cache_declaration(("pool-a", "pool-b"))
    installed = _mechanism_envelope(declaration, mechanism="regional-cache")
    spec = ModelDeploymentSpec.model_validate(
        {
            **model_spec().model_dump(mode="json", by_alias=True),
            "cache": {**model_spec().model_dump(mode="json", by_alias=True)["cache"], "mechanism": "regional-cache"},
        }
    )
    # A full, otherwise compatible conventional cohort belonging to a different
    # mechanism must not qualify the pinned revision.
    qualification = installed.qualifications["qwen.3-8b"].model_copy(
        update={
            "fast_start_evidence": [
                evidence(
                    spec,
                    installed.pools[pool_ref],
                    seconds=[20.0] * MINIMUM_QUALIFYING_SAMPLES,
                    mechanism="conventional",
                )
                for pool_ref in ("pool-a", "pool-b")
            ]
        }
    )
    installed = installed.model_copy(update={"qualifications": {"qwen.3-8b": qualification}})

    assessment = evaluate_fast_start(spec, installed, evaluation_time=NOW)
    assert assessment.qualified_level is FastStartLevel.OFF
    pool = next(item for item in assessment.pools if item.pool_ref == "pool-a")
    assert pool.paths == []
    retained = pool.retained_paths[0]
    assert retained.mechanism == "conventional"
    assert [(item.code, item.field) for item in retained.mismatches] == [("ValueMismatch", "$.cache.mechanism")]


def test_evidence_must_carry_the_declaration_bound_mechanism_identity() -> None:
    declaration = _regional_cache_declaration(("pool-a", "pool-b"))
    installed = _mechanism_envelope(declaration, mechanism="regional-cache")
    spec = model_spec()
    # Evidence recorded before the declaration existed carries the storage-only
    # identity, so it is retained rather than counted once a declaration lands.
    stale = mechanism_config_digest(
        mechanism="regional-cache",
        storage_contract_digest=STORAGE_CONTRACT_DIGEST,
    )
    qualification = installed.qualifications["qwen.3-8b"].model_copy(
        update={
            "fast_start_evidence": [
                evidence(
                    spec,
                    installed.pools["pool-a"],
                    seconds=[20.0] * MINIMUM_QUALIFYING_SAMPLES,
                    mechanism="regional-cache",
                    mechanism_config_digest=stale,
                )
            ]
        }
    )
    installed = installed.model_copy(update={"qualifications": {"qwen.3-8b": qualification}})

    assessment = evaluate_fast_start(spec, installed, evaluation_time=NOW)
    pool = next(item for item in assessment.pools if item.pool_ref == "pool-a")
    assert pool.paths == []
    assert any(
        item.field == "$.cache.mechanismConfigDigest"
        for retained in pool.retained_paths
        for item in retained.mismatches
    )
