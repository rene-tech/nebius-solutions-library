"""Versioned identity contracts for customer-facing fast-start evidence.

The models in this module deliberately contain only immutable runtime inputs.
Mutable serving policy (replica floors, queueing, access policy, and automatic
level selection) is not part of the identity and therefore cannot invalidate a
benchmark that still describes the exact executable runtime.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import AwareDatetime, Field, model_validator

from .models import KubernetesModel

SHA256_DIGEST_PATTERN = r"^sha256:[a-f0-9]{64}$"
IMAGE_DIGEST_PATTERN = r"^[^\s@]+@sha256:[a-f0-9]{64}$"
RUNTIME_IDENTITY_SCHEMA = "fs2-serve.nebius.ai/runtime-evidence-identity/v2"
RUNTIME_CONTRACT_SCHEMA = "fs2-serve.nebius.ai/runtime-contract/v1"
ENVIRONMENT_QUALIFICATION_SCHEMA = "fs2-serve.nebius.ai/runtime-environment-qualification/v1"
MEASUREMENT_CONTRACT_SCHEMA = "fs2-serve.nebius.ai/fast-start-measurement-contract/v1"
MECHANISM_CONTRACT_SCHEMA = "fs2-serve.nebius.ai/fast-start-mechanism-contract/v1"

CacheTierValue = Literal["Disabled", "ObjectStore", "SharedFilesystem", "NodeLocal"]
TopologyPolicyValue = Literal["Any", "SingleNode", "HighBandwidthDomain"]
StartupScenario = Literal[
    "prepared-node-zero-pod",
    "fresh-node-zero-pod",
    "preemption-replacement",
    "durable-cache-loss-fallback",
]


class EvidenceIdentityState(StrEnum):
    LEGACY_UNBOUND = "LegacyUnbound"
    BOUND = "Bound"


def canonical_json(value: object) -> bytes:
    """Return the cross-language canonical representation used by Terraform."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def canonical_digest(value: object) -> str:
    return f"sha256:{hashlib.sha256(canonical_json(value)).hexdigest()}"


class RuntimeContractIdentity(KubernetesModel):
    """Exact model executable identity, excluding mutable serving policy."""

    schema_id: Literal["fs2-serve.nebius.ai/runtime-contract/v1"] = Field(
        default="fs2-serve.nebius.ai/runtime-contract/v1",
        alias="schema",
    )
    model_ref: str = Field(min_length=1, max_length=128)
    source_revision: str = Field(min_length=1, max_length=256)
    model_content_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    artifact_manifest_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    runtime_profile: str = Field(min_length=1, max_length=128)
    runtime_image: str = Field(min_length=73, max_length=768, pattern=IMAGE_DIGEST_PATTERN)
    template_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    render_contract_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    argv_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    environment_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    runtime_contract_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)

    @model_validator(mode="after")
    def self_digest_matches(self) -> RuntimeContractIdentity:
        payload = self.model_dump(mode="json", by_alias=True, exclude={"runtime_contract_digest"})
        if self.runtime_contract_digest != canonical_digest(payload):
            raise ValueError("runtimeContractDigest does not match the canonical runtime-only contract")
        return self


class RuntimeEnvironmentIdentity(KubernetesModel):
    """Reviewed runtime environment represented by stable component digests."""

    schema_id: Literal["fs2-serve.nebius.ai/runtime-environment-qualification/v1"] = Field(
        default="fs2-serve.nebius.ai/runtime-environment-qualification/v1",
        alias="schema",
    )
    qualification_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    scope_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    accelerator_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    driver_cuda_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    host_runtime_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    storage_runtime_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)

    @model_validator(mode="after")
    def self_digest_matches(self) -> RuntimeEnvironmentIdentity:
        payload = self.model_dump(mode="json", by_alias=True, exclude={"qualification_digest"})
        if self.qualification_digest != canonical_digest(payload):
            raise ValueError("qualificationDigest does not match the canonical environment qualification")
        return self


class RuntimePlacementIdentity(KubernetesModel):
    accelerator_class: str = Field(min_length=1, max_length=128)
    accelerators_per_replica: int = Field(ge=1, le=64)
    topology_policy: TopologyPolicyValue
    startup_scenario: StartupScenario


class RuntimeCacheIdentity(KubernetesModel):
    tier: CacheTierValue
    mechanism: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9-]*$")
    mechanism_config_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    snapshot_digest: str | None = Field(default=None, pattern=SHA256_DIGEST_PATTERN)
    storage_contract_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)


class RuntimeMeasurementIdentity(KubernetesModel):
    schema_id: Literal["fs2-serve.nebius.ai/fast-start-measurement-contract/v1"] = Field(
        default="fs2-serve.nebius.ai/fast-start-measurement-contract/v1",
        alias="schema",
    )
    basis: Literal["CapacityAvailableToSemanticReady"] = "CapacityAvailableToSemanticReady"
    payload_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    protocol: str = Field(min_length=1, max_length=128)
    endpoint_path: str = Field(min_length=1, max_length=512, pattern=r"^/")
    streaming: bool
    semantic_validator_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    benchmark_client_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    client_placement: Literal["same-pod", "same-node", "in-cluster", "same-region", "cross-region", "external"]
    contract_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)

    @model_validator(mode="after")
    def self_digest_matches(self) -> RuntimeMeasurementIdentity:
        payload = self.model_dump(mode="json", by_alias=True, exclude={"contract_digest"})
        if self.contract_digest != canonical_digest(payload):
            raise ValueError("contractDigest does not match the canonical measurement contract")
        return self


class RuntimeEvidenceIdentity(KubernetesModel):
    """Complete equality identity used to admit benchmark evidence."""

    schema_id: Literal["fs2-serve.nebius.ai/runtime-evidence-identity/v2"] = Field(
        default="fs2-serve.nebius.ai/runtime-evidence-identity/v2",
        alias="schema",
    )
    runtime: RuntimeContractIdentity
    environment: RuntimeEnvironmentIdentity
    placement: RuntimePlacementIdentity
    cache: RuntimeCacheIdentity
    measurement: RuntimeMeasurementIdentity

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", by_alias=True))


class EnvironmentMember(KubernetesModel):
    pool_ref: str = Field(min_length=1, max_length=128)
    capacity_type: Literal["regular", "preemptible"]


class FastStartEnvironmentBinding(KubernetesModel):
    """Current reviewed environment for one cache tier and startup scenario."""

    environment: RuntimeEnvironmentIdentity
    members: list[EnvironmentMember] = Field(min_length=1, max_length=32)
    cache_tier: CacheTierValue
    startup_scenario: StartupScenario
    valid_until: AwareDatetime

    @model_validator(mode="after")
    def exact_members(self) -> FastStartEnvironmentBinding:
        identities = {(member.pool_ref, member.capacity_type) for member in self.members}
        if len(identities) != len(self.members):
            raise ValueError("environment qualification members must be unique")
        return self

    def includes(self, *, pool_ref: str, capacity_type: str) -> bool:
        return any(member.pool_ref == pool_ref and member.capacity_type == capacity_type for member in self.members)

    def current_at(self, value: datetime) -> bool:
        return value < self.valid_until


class FastStartRuntimeContract(KubernetesModel):
    """Terraform-owned expected identity components for one exact pool render."""

    pool_ref: str = Field(min_length=1, max_length=128)
    runtime: RuntimeContractIdentity
    storage_contract_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    measurement: RuntimeMeasurementIdentity


class FastStartIdentityMismatch(KubernetesModel):
    """Bounded non-secret explanation for an incompatible retained receipt."""

    code: Literal["LegacyUnbound", "MissingExpectedValue", "ValueMismatch", "Expired"]
    field: str = Field(min_length=1, max_length=160, pattern=r"^\$")
    expected_digest: str | None = Field(default=None, pattern=SHA256_DIGEST_PATTERN)
    observed_digest: str | None = Field(default=None, pattern=SHA256_DIGEST_PATTERN)


def mechanism_config_digest(*, mechanism: str, storage_contract_digest: str) -> str:
    """Canonical config identity for non-ModelExpress mechanisms.

    ModelExpress uses its separately reviewed ``configDigest`` because network
    transport and coordinator settings are material to restore performance.
    """

    return canonical_digest(
        {
            "schema": MECHANISM_CONTRACT_SCHEMA,
            "mechanism": mechanism,
            "storageContractDigest": storage_contract_digest,
        }
    )


def identity_mismatches(
    expected: RuntimeEvidenceIdentity,
    observed: RuntimeEvidenceIdentity,
) -> list[FastStartIdentityMismatch]:
    """Compare every leaf and return stable JSON-path mismatch diagnostics."""

    expected_value = expected.model_dump(mode="json", by_alias=True)
    observed_value = observed.model_dump(mode="json", by_alias=True)
    mismatches: list[FastStartIdentityMismatch] = []

    def visit(path: str, wanted: Any, actual: Any) -> None:
        if isinstance(wanted, dict) and isinstance(actual, dict):
            for key in sorted(set(wanted) | set(actual)):
                child = f"{path}.{key}"
                if key not in wanted or key not in actual:
                    mismatches.append(FastStartIdentityMismatch(code="MissingExpectedValue", field=child))
                else:
                    visit(child, wanted[key], actual[key])
            return
        if wanted == actual:
            return
        expected_digest = wanted if isinstance(wanted, str) and wanted.startswith("sha256:") else None
        observed_digest = actual if isinstance(actual, str) and actual.startswith("sha256:") else None
        mismatches.append(
            FastStartIdentityMismatch(
                code="ValueMismatch",
                field=path,
                expected_digest=expected_digest,
                observed_digest=observed_digest,
            )
        )

    visit("$", expected_value, observed_value)
    return mismatches
