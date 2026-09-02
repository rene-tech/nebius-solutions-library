from __future__ import annotations

import importlib.util
import json
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from test_fast_start_benchmark import BENCHMARK, attempt, compatibility_tuple

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "fs2_cold_fast_start_identity", ROOT / "fast_start_identity.py"
)
assert SPEC and SPEC.loader
IDENTITY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = IDENTITY
SPEC.loader.exec_module(IDENTITY)


def model_deployment() -> dict[str, Any]:
    return {
        "spec": {
            "artifact": {
                "revision": "revision-1",
                "manifestDigest": "sha256:" + "a" * 64,
            },
            "runtime": {"profile": "vllm"},
            "placement": {"topologyPolicy": "SingleNode"},
        }
    }


def contracts(compatibility: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    scope = {
        "projectId": compatibility["project_id"],
        "region": compatibility["region"],
        "clusterContext": compatibility["cluster_context"],
    }
    accelerator = {
        "acceleratorClass": compatibility["accelerator_class"],
        "gpuProduct": compatibility["gpu_product"],
        "computeCapability": compatibility["gpu_compute_capability"],
        "memoryBytes": compatibility["gpu_memory_bytes"],
    }
    driver_cuda = {
        "driverVersion": compatibility["driver_version"],
        "cudaVersion": compatibility["cuda_version"],
    }
    storage = {
        "storageClass": compatibility["storage_class"],
        "storageMode": compatibility["storage_mode"],
    }
    environment = {
        "schema": IDENTITY.ENVIRONMENT_SCHEMA,
        "scopeDigest": IDENTITY.canonical_digest(scope),
        "acceleratorDigest": IDENTITY.canonical_digest(accelerator),
        "driverCudaDigest": IDENTITY.canonical_digest(driver_cuda),
        "hostRuntimeDigest": "sha256:" + "f" * 64,
        "storageRuntimeDigest": IDENTITY.canonical_digest(storage),
    }
    environment["qualificationDigest"] = IDENTITY.canonical_digest(environment)
    environments = {
        "schema": IDENTITY.ENVIRONMENT_SET_SCHEMA,
        "bindings": [
            {
                "scope": scope,
                "accelerator": accelerator,
                "driverCuda": driver_cuda,
                "storageRuntime": storage,
                "hostRuntimeDigest": environment["hostRuntimeDigest"],
                "environment": environment,
                "members": [
                    {
                        "poolRef": compatibility["pool_id"],
                        "capacityType": compatibility["capacity_type"],
                    }
                ],
                "cacheTier": compatibility["cache_tier"],
                "startupScenario": compatibility["capacity_state"],
                "validUntil": (datetime(2026, 10, 1, tzinfo=UTC))
                .isoformat()
                .replace("+00:00", "Z"),
            }
        ],
    }
    measurement = {
        "schema": IDENTITY.MEASUREMENT_SCHEMA,
        "basis": "CapacityAvailableToSemanticReady",
        "payloadDigest": "sha256:" + compatibility["payload_digest"],
        "protocol": compatibility["interface_protocol"],
        "endpointPath": compatibility["endpoint_path"],
        "streaming": compatibility["streaming"],
        "semanticValidatorDigest": "sha256:"
        + compatibility["semantic_validator_digest"],
        "benchmarkClientDigest": "sha256:" + compatibility["benchmark_client_digest"],
        "clientPlacement": compatibility["client_placement"],
    }
    measurement["contractDigest"] = IDENTITY.canonical_digest(measurement)
    return environments, measurement


def build(**overrides: Any) -> tuple[dict[str, Any], str]:
    compatibility = compatibility_tuple(capacity_type="regular", **overrides)
    environments, measurement = contracts(compatibility)
    return IDENTITY.build_runtime_evidence_identity(
        compatibility=compatibility,
        model_deployment=model_deployment(),
        environment_qualifications=environments,
        measurement_contract=measurement,
        valid_at=datetime(2026, 9, 2, tzinfo=UTC),
    )


def bound_attempt(ordinal: int) -> dict[str, Any]:
    value = attempt(ordinal, tuple_overrides={"capacity_type": "regular"})
    environments, measurement = contracts(value["compatibility_tuple"])
    identity, identity_digest = IDENTITY.build_runtime_evidence_identity(
        compatibility=value["compatibility_tuple"],
        model_deployment=model_deployment(),
        environment_qualifications=environments,
        measurement_contract=measurement,
        valid_at=datetime(2026, 9, 2, tzinfo=UTC),
    )
    value.update(
        {
            "schema": "fs2-serve.nebius.ai/fast-start-benchmark-attempt/v2",
            "evidence_identity": identity,
            "evidence_identity_digest": identity_digest,
        }
    )
    return value


def test_identity_validates_against_all_three_public_schemas() -> None:
    compatibility = compatibility_tuple(capacity_type="regular")
    environments, measurement = contracts(compatibility)
    identity, digest = IDENTITY.build_runtime_evidence_identity(
        compatibility=compatibility,
        model_deployment=model_deployment(),
        environment_qualifications=environments,
        measurement_contract=measurement,
        valid_at=datetime(2026, 9, 2, tzinfo=UTC),
    )

    for path, value in (
        (ROOT / "runtime-evidence-identity.schema.json", identity),
        (ROOT / "fast-start-measurement-contract.schema.json", measurement),
        (
            ROOT.parents[1]
            / "catalog/profiles/runtime-environment-qualification.schema.json",
            environments,
        ),
    ):
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        assert (
            list(
                Draft202012Validator(
                    schema, format_checker=FormatChecker()
                ).iter_errors(value)
            )
            == []
        )
    assert digest == IDENTITY.canonical_digest(identity)
    assert identity["runtime"]["sourceRevision"] == "revision-1"
    assert not identity["runtime"]["runtimeContractDigest"].startswith("dynamic:")


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("driver_version", "581.00", "environment_observation_mismatch"),
        ("cuda_version", "14.0", "environment_observation_mismatch"),
        ("storage_mode", "NodeLocal", "environment_observation_mismatch"),
        ("payload_digest", "0" * 64, "measurement_contract_observation_mismatch"),
        (
            "benchmark_client_digest",
            "1" * 64,
            "measurement_contract_observation_mismatch",
        ),
    ],
)
def test_observed_environment_and_measurement_changes_fail_closed(
    field: str, value: object, error: str
) -> None:
    compatibility = compatibility_tuple(capacity_type="regular")
    environments, measurement = contracts(compatibility)
    compatibility[field] = value

    with pytest.raises(IDENTITY.IdentityError, match=error):
        IDENTITY.build_runtime_evidence_identity(
            compatibility=compatibility,
            model_deployment=model_deployment(),
            environment_qualifications=environments,
            measurement_contract=measurement,
            valid_at=datetime(2026, 9, 2, tzinfo=UTC),
        )


def test_regular_and_preemptible_equivalence_requires_an_explicit_member() -> None:
    compatibility = compatibility_tuple(capacity_type="preemptible")
    environments, measurement = contracts(compatibility)
    binding = environments["bindings"][0]
    binding["members"] = [
        {"poolRef": compatibility["pool_id"], "capacityType": "regular"}
    ]

    with pytest.raises(IDENTITY.IdentityError, match="environment_binding_not_unique"):
        IDENTITY.build_runtime_evidence_identity(
            compatibility=compatibility,
            model_deployment=model_deployment(),
            environment_qualifications=environments,
            measurement_contract=measurement,
            valid_at=datetime(2026, 9, 2, tzinfo=UTC),
        )

    binding["members"].append(
        {"poolRef": compatibility["pool_id"], "capacityType": "preemptible"}
    )
    identity, _ = IDENTITY.build_runtime_evidence_identity(
        compatibility=compatibility,
        model_deployment=model_deployment(),
        environment_qualifications=environments,
        measurement_contract=measurement,
        valid_at=datetime(2026, 9, 2, tzinfo=UTC),
    )
    assert identity["environment"] == binding["environment"]


def test_expired_or_tampered_qualification_is_rejected() -> None:
    compatibility = compatibility_tuple(capacity_type="regular")
    environments, measurement = contracts(compatibility)
    expired = deepcopy(environments)
    expired["bindings"][0]["validUntil"] = (
        (datetime(2026, 9, 2, tzinfo=UTC) - timedelta(seconds=1))
        .isoformat()
        .replace("+00:00", "Z")
    )
    with pytest.raises(IDENTITY.IdentityError, match="environment_binding_expired"):
        IDENTITY.build_runtime_evidence_identity(
            compatibility=compatibility,
            model_deployment=model_deployment(),
            environment_qualifications=expired,
            measurement_contract=measurement,
            valid_at=datetime(2026, 9, 2, tzinfo=UTC),
        )

    tampered = deepcopy(measurement)
    tampered["endpointPath"] = "/changed"
    with pytest.raises(
        IDENTITY.IdentityError, match="measurement_contract_digest_mismatch"
    ):
        IDENTITY.validate_measurement_contract(tampered)


def test_v2_attempts_aggregate_to_an_immutable_v2_receipt() -> None:
    receipt = BENCHMARK.build_receipt(
        [bound_attempt(1), bound_attempt(2), bound_attempt(3)],
        generated_at="2026-09-02T13:00:00Z",
    )

    BENCHMARK.validate_receipt(receipt)
    assert receipt["schema"] == "fs2-serve.nebius.ai/fast-start-benchmark-receipt/v2"
    assert receipt["evidence_identity_digest"] == IDENTITY.canonical_digest(
        receipt["evidence_identity"]
    )

    tampered = bound_attempt(1)
    tampered["evidence_identity"]["runtime"]["argvDigest"] = "sha256:" + "0" * 64
    with pytest.raises(BENCHMARK.FastStartEvidenceError, match="identity"):
        BENCHMARK.validate_attempt(tampered)
