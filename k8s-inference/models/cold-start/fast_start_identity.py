#!/usr/bin/env python3
"""Canonical v2 fast-start runtime-evidence identity construction.

This module is intentionally dependency-free so the live benchmark runner and
offline evidence tools can use the same fail-closed contract.  It contains no
cluster mutation and never includes credentials or request/response payloads in
the identity: only their immutable digests are bound.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

RUNTIME_IDENTITY_SCHEMA = "fs2-serve.nebius.ai/runtime-evidence-identity/v2"
RUNTIME_CONTRACT_SCHEMA = "fs2-serve.nebius.ai/runtime-contract/v1"
ENVIRONMENT_SCHEMA = "fs2-serve.nebius.ai/runtime-environment-qualification/v1"
ENVIRONMENT_SET_SCHEMA = "fs2-serve.nebius.ai/runtime-environment-qualification-set/v1"
MEASUREMENT_SCHEMA = "fs2-serve.nebius.ai/fast-start-measurement-contract/v1"
MECHANISM_SCHEMA = "fs2-serve.nebius.ai/fast-start-mechanism-contract/v1"
STORAGE_SCHEMA = "fs2-serve.nebius.ai/fast-start-storage-contract/v1"


class IdentityError(ValueError):
    """A bounded identity-contract failure that does not echo input values."""


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def canonical_digest(value: object) -> str:
    return f"sha256:{hashlib.sha256(canonical_json(value)).hexdigest()}"


def _exact_keys(value: object, required: set[str], *, where: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != required:
        raise IdentityError(f"{where}_shape_invalid")
    return value


def _digest(value: object, *, where: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise IdentityError(f"{where}_digest_invalid")
    return value


def _utc_timestamp(value: object, *, where: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise IdentityError(f"{where}_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        raise IdentityError(f"{where}_timestamp_invalid") from None
    if parsed.utcoffset() != UTC.utcoffset(parsed):
        raise IdentityError(f"{where}_timestamp_invalid")
    return parsed


def _self_digest(value: dict[str, Any], field: str, *, where: str) -> None:
    claimed = _digest(value.get(field), where=where)
    unsigned = dict(value)
    unsigned.pop(field, None)
    if claimed != canonical_digest(unsigned):
        raise IdentityError(f"{where}_digest_mismatch")


def validate_measurement_contract(value: object) -> dict[str, Any]:
    contract = _exact_keys(
        value,
        {
            "schema",
            "basis",
            "payloadDigest",
            "protocol",
            "endpointPath",
            "streaming",
            "semanticValidatorDigest",
            "benchmarkClientDigest",
            "clientPlacement",
            "contractDigest",
        },
        where="measurement_contract",
    )
    if (
        contract["schema"] != MEASUREMENT_SCHEMA
        or contract["basis"] != "CapacityAvailableToSemanticReady"
        or not isinstance(contract["protocol"], str)
        or not contract["protocol"]
        or not isinstance(contract["endpointPath"], str)
        or not contract["endpointPath"].startswith("/")
        or not isinstance(contract["streaming"], bool)
        or contract["clientPlacement"]
        not in {
            "same-pod",
            "same-node",
            "in-cluster",
            "same-region",
            "cross-region",
            "external",
        }
    ):
        raise IdentityError("measurement_contract_value_invalid")
    for field in (
        "payloadDigest",
        "semanticValidatorDigest",
        "benchmarkClientDigest",
        "contractDigest",
    ):
        _digest(contract[field], where=f"measurement_contract_{field}")
    _self_digest(contract, "contractDigest", where="measurement_contract")
    return contract


def _validate_environment(value: object) -> dict[str, Any]:
    environment = _exact_keys(
        value,
        {
            "schema",
            "qualificationDigest",
            "scopeDigest",
            "acceleratorDigest",
            "driverCudaDigest",
            "hostRuntimeDigest",
            "storageRuntimeDigest",
        },
        where="environment",
    )
    if environment["schema"] != ENVIRONMENT_SCHEMA:
        raise IdentityError("environment_schema_invalid")
    for field in (
        "qualificationDigest",
        "scopeDigest",
        "acceleratorDigest",
        "driverCudaDigest",
        "hostRuntimeDigest",
        "storageRuntimeDigest",
    ):
        _digest(environment[field], where=f"environment_{field}")
    _self_digest(environment, "qualificationDigest", where="environment")
    return environment


def validate_runtime_evidence_identity(value: object) -> dict[str, Any]:
    """Validate the complete self-bound identity carried by a v2 receipt."""

    identity = _exact_keys(
        value,
        {"schema", "runtime", "environment", "placement", "cache", "measurement"},
        where="runtime_evidence_identity",
    )
    if identity["schema"] != RUNTIME_IDENTITY_SCHEMA:
        raise IdentityError("runtime_evidence_identity_schema_invalid")
    runtime = _exact_keys(
        identity["runtime"],
        {
            "schema",
            "modelRef",
            "sourceRevision",
            "modelContentDigest",
            "artifactManifestDigest",
            "runtimeProfile",
            "runtimeImage",
            "templateDigest",
            "renderContractDigest",
            "argvDigest",
            "environmentDigest",
            "runtimeContractDigest",
        },
        where="runtime_contract",
    )
    if runtime["schema"] != RUNTIME_CONTRACT_SCHEMA:
        raise IdentityError("runtime_contract_schema_invalid")
    for field in (
        "modelContentDigest",
        "artifactManifestDigest",
        "templateDigest",
        "renderContractDigest",
        "argvDigest",
        "environmentDigest",
        "runtimeContractDigest",
    ):
        _digest(runtime[field], where=f"runtime_contract_{field}")
    if (
        not isinstance(runtime["runtimeImage"], str)
        or "@sha256:" not in runtime["runtimeImage"]
        or any(
            not isinstance(runtime[field], str) or not runtime[field]
            for field in ("modelRef", "sourceRevision", "runtimeProfile")
        )
    ):
        raise IdentityError("runtime_contract_value_invalid")
    _self_digest(runtime, "runtimeContractDigest", where="runtime_contract")
    _validate_environment(identity["environment"])
    validate_measurement_contract(identity["measurement"])
    placement = _exact_keys(
        identity["placement"],
        {
            "acceleratorClass",
            "acceleratorsPerReplica",
            "topologyPolicy",
            "startupScenario",
        },
        where="placement",
    )
    if (
        not isinstance(placement["acceleratorClass"], str)
        or not placement["acceleratorClass"]
        or isinstance(placement["acceleratorsPerReplica"], bool)
        or not isinstance(placement["acceleratorsPerReplica"], int)
        or not 1 <= placement["acceleratorsPerReplica"] <= 64
        or placement["topologyPolicy"]
        not in {"Any", "SingleNode", "HighBandwidthDomain"}
        or placement["startupScenario"]
        not in {
            "prepared-node-zero-pod",
            "fresh-node-zero-pod",
            "preemption-replacement",
            "durable-cache-loss-fallback",
        }
    ):
        raise IdentityError("placement_value_invalid")
    cache = _exact_keys(
        identity["cache"],
        {
            "tier",
            "mechanism",
            "mechanismConfigDigest",
            "snapshotDigest",
            "storageContractDigest",
        },
        where="cache",
    )
    if (
        cache["tier"]
        not in {"Disabled", "ObjectStore", "SharedFilesystem", "NodeLocal"}
        or not isinstance(cache["mechanism"], str)
        or not cache["mechanism"]
    ):
        raise IdentityError("cache_value_invalid")
    _digest(cache["mechanismConfigDigest"], where="cache_mechanism_config")
    _digest(cache["storageContractDigest"], where="cache_storage_contract")
    if cache["snapshotDigest"] is not None:
        _digest(cache["snapshotDigest"], where="cache_snapshot")
    return identity


def validate_environment_qualifications(document: object) -> list[dict[str, Any]]:
    """Validate a qualification set without claiming it matches a live node."""

    root = _exact_keys(document, {"schema", "bindings"}, where="environment_set")
    if root["schema"] != ENVIRONMENT_SET_SCHEMA or not isinstance(
        root["bindings"], list
    ):
        raise IdentityError("environment_set_invalid")
    validated: list[dict[str, Any]] = []
    for raw in root["bindings"]:
        binding = _exact_keys(
            raw,
            {
                "scope",
                "accelerator",
                "driverCuda",
                "storageRuntime",
                "hostRuntimeDigest",
                "environment",
                "members",
                "cacheTier",
                "startupScenario",
                "validUntil",
            },
            where="environment_binding",
        )
        scope = _exact_keys(
            binding["scope"], {"projectId", "region", "clusterContext"}, where="scope"
        )
        accelerator = _exact_keys(
            binding["accelerator"],
            {"acceleratorClass", "gpuProduct", "computeCapability", "memoryBytes"},
            where="accelerator",
        )
        driver_cuda = _exact_keys(
            binding["driverCuda"], {"driverVersion", "cudaVersion"}, where="driver_cuda"
        )
        storage = _exact_keys(
            binding["storageRuntime"], {"storageClass", "storageMode"}, where="storage"
        )
        if (
            not isinstance(binding["members"], list)
            or not binding["members"]
            or any(
                not isinstance(member, dict)
                or set(member) != {"poolRef", "capacityType"}
                or member["capacityType"] not in {"regular", "preemptible"}
                for member in binding["members"]
            )
            or len(
                {(item["poolRef"], item["capacityType"]) for item in binding["members"]}
            )
            != len(binding["members"])
        ):
            raise IdentityError("environment_members_invalid")
        _utc_timestamp(binding["validUntil"], where="environment_binding")
        environment = _validate_environment(binding["environment"])
        expected_components = {
            "scopeDigest": canonical_digest(scope),
            "acceleratorDigest": canonical_digest(accelerator),
            "driverCudaDigest": canonical_digest(driver_cuda),
            "hostRuntimeDigest": _digest(
                binding["hostRuntimeDigest"], where="host_runtime"
            ),
            "storageRuntimeDigest": canonical_digest(storage),
        }
        if any(
            environment[field] != digest
            for field, digest in expected_components.items()
        ):
            raise IdentityError("environment_component_digest_mismatch")
        validated.append(binding)
    return validated


def select_environment(
    document: object,
    *,
    compatibility: dict[str, Any],
    valid_at: datetime,
) -> dict[str, Any]:
    """Select one explicit current binding and verify observable components."""

    member = {
        "poolRef": compatibility.get("pool_id"),
        "capacityType": compatibility.get("capacity_type"),
    }
    candidates = [
        binding
        for binding in validate_environment_qualifications(document)
        if binding["cacheTier"] == compatibility.get("cache_tier")
        and binding["startupScenario"] == compatibility.get("capacity_state")
        and member in binding["members"]
    ]
    if len(candidates) != 1:
        raise IdentityError("environment_binding_not_unique")
    binding = candidates[0]
    if valid_at >= _utc_timestamp(binding["validUntil"], where="environment_binding"):
        raise IdentityError("environment_binding_expired")

    scope = _exact_keys(
        binding["scope"], {"projectId", "region", "clusterContext"}, where="scope"
    )
    accelerator = _exact_keys(
        binding["accelerator"],
        {"acceleratorClass", "gpuProduct", "computeCapability", "memoryBytes"},
        where="accelerator",
    )
    driver_cuda = _exact_keys(
        binding["driverCuda"], {"driverVersion", "cudaVersion"}, where="driver_cuda"
    )
    storage = _exact_keys(
        binding["storageRuntime"], {"storageClass", "storageMode"}, where="storage"
    )
    observed_scope = {
        "projectId": compatibility.get("project_id"),
        "region": compatibility.get("region"),
        "clusterContext": compatibility.get("cluster_context"),
    }
    observed_accelerator = {
        "acceleratorClass": compatibility.get("accelerator_class"),
        "gpuProduct": compatibility.get("gpu_product"),
        "computeCapability": compatibility.get("gpu_compute_capability"),
        "memoryBytes": compatibility.get("gpu_memory_bytes"),
    }
    observed_driver_cuda = {
        "driverVersion": compatibility.get("driver_version"),
        "cudaVersion": compatibility.get("cuda_version"),
    }
    observed_storage = {
        "storageClass": compatibility.get("storage_class"),
        "storageMode": compatibility.get("storage_mode"),
    }
    if (scope, accelerator, driver_cuda, storage) != (
        observed_scope,
        observed_accelerator,
        observed_driver_cuda,
        observed_storage,
    ):
        raise IdentityError("environment_observation_mismatch")
    return binding["environment"]


def build_runtime_evidence_identity(
    *,
    compatibility: dict[str, Any],
    model_deployment: dict[str, Any],
    environment_qualifications: object,
    measurement_contract: object,
    valid_at: datetime,
) -> tuple[dict[str, Any], str]:
    """Build the exact v2 identity after verifying both external contracts."""

    desired = model_deployment.get("spec")
    if not isinstance(desired, dict):
        raise IdentityError("model_deployment_spec_invalid")
    artifact = desired.get("artifact")
    runtime_spec = desired.get("runtime")
    placement_spec = desired.get("placement")
    if not all(
        isinstance(value, dict) for value in (artifact, runtime_spec, placement_spec)
    ):
        raise IdentityError("model_deployment_runtime_invalid")
    assert (
        isinstance(artifact, dict)
        and isinstance(runtime_spec, dict)
        and isinstance(placement_spec, dict)
    )

    measurement = validate_measurement_contract(measurement_contract)
    observed_measurement = {
        "payloadDigest": f"sha256:{compatibility.get('payload_digest')}",
        "protocol": compatibility.get("interface_protocol"),
        "endpointPath": compatibility.get("endpoint_path"),
        "streaming": compatibility.get("streaming"),
        "semanticValidatorDigest": f"sha256:{compatibility.get('semantic_validator_digest')}",
        "benchmarkClientDigest": f"sha256:{compatibility.get('benchmark_client_digest')}",
        "clientPlacement": compatibility.get("client_placement"),
    }
    if any(
        measurement[field] != observed
        for field, observed in observed_measurement.items()
    ):
        raise IdentityError("measurement_contract_observation_mismatch")
    environment = select_environment(
        environment_qualifications,
        compatibility=compatibility,
        valid_at=valid_at,
    )

    argv_digest = _digest(
        f"sha256:{compatibility.get('runtime_argv_digest')}", where="argv"
    )
    environment_digest = _digest(
        f"sha256:{compatibility.get('runtime_environment_digest')}",
        where="runtime_environment",
    )
    render_contract_digest = canonical_digest(
        {
            "schema": "fs2-serve.nebius.ai/runtime-render-contract/v1",
            "runtimeImage": compatibility.get("runtime_image_ref"),
            "templateDigest": compatibility.get("runtime_template_digest"),
            "argvDigest": argv_digest,
            "environmentDigest": environment_digest,
        }
    )
    runtime = {
        "schema": RUNTIME_CONTRACT_SCHEMA,
        "modelRef": compatibility.get("model_id"),
        "sourceRevision": artifact.get("revision"),
        "modelContentDigest": compatibility.get("model_content_digest"),
        "artifactManifestDigest": compatibility.get("artifact_manifest_digest"),
        "runtimeProfile": runtime_spec.get("profile"),
        "runtimeImage": compatibility.get("runtime_image_ref"),
        "templateDigest": compatibility.get("runtime_template_digest"),
        "renderContractDigest": render_contract_digest,
        "argvDigest": argv_digest,
        "environmentDigest": environment_digest,
    }
    if any(not isinstance(value, str) or not value for value in runtime.values()):
        raise IdentityError("runtime_contract_value_missing")
    runtime["runtimeContractDigest"] = canonical_digest(runtime)

    storage_contract_digest = canonical_digest(
        {
            "schema": STORAGE_SCHEMA,
            "storageClass": compatibility.get("storage_class"),
            "storageMode": compatibility.get("storage_mode"),
        }
    )
    mechanism = compatibility.get("mechanism")
    if not isinstance(mechanism, str) or not mechanism:
        raise IdentityError("mechanism_invalid")
    if mechanism == "modelexpress":
        mechanism_config_digest = _digest(
            compatibility.get("mechanism_config_digest"),
            where="mechanism_config",
        )
    else:
        if compatibility.get("mechanism_config_digest") is not None:
            raise IdentityError("mechanism_config_unexpected")
        mechanism_config_digest = canonical_digest(
            {
                "schema": MECHANISM_SCHEMA,
                "mechanism": mechanism,
                "storageContractDigest": storage_contract_digest,
            }
        )
    snapshot = compatibility.get("snapshot_digest")
    if snapshot is not None:
        _digest(snapshot, where="snapshot")
    topology = placement_spec.get("topologyPolicy")
    if topology not in {"Any", "SingleNode", "HighBandwidthDomain"}:
        raise IdentityError("topology_policy_invalid")

    identity = {
        "schema": RUNTIME_IDENTITY_SCHEMA,
        "runtime": runtime,
        "environment": environment,
        "placement": {
            "acceleratorClass": compatibility.get("accelerator_class"),
            "acceleratorsPerReplica": compatibility.get("gpu_count"),
            "topologyPolicy": topology,
            "startupScenario": compatibility.get("capacity_state"),
        },
        "cache": {
            "tier": compatibility.get("cache_tier"),
            "mechanism": mechanism,
            "mechanismConfigDigest": mechanism_config_digest,
            "snapshotDigest": snapshot,
            "storageContractDigest": storage_contract_digest,
        },
        "measurement": measurement,
    }
    return identity, canonical_digest(identity)
