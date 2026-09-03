"""Attempt-scoped HMAC capabilities for artifact companion containers."""

from __future__ import annotations

import base64
import hmac
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from ..crypto import KeyedHasher
from .models import WorkloadResource

CAPABILITY_SCHEMA = "fs2-serve.nebius.ai/scientific-workload-capability/v1"
_CONTEXT = "fs2-scientific-workload-capability/v1"


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


@dataclass(frozen=True, slots=True)
class CapabilityArtifact:
    logical_artifact_id: str
    artifact_id: UUID
    digest: str
    size_bytes: int
    media_type: str
    compression: str | None


@dataclass(frozen=True, slots=True)
class ScientificWorkloadCapability:
    operation_id: UUID
    batch_id: UUID
    workload_id: UUID
    attempt_id: UUID
    attempt_number: int
    tenant_id: str
    model_id: str
    variant_id: str
    stage_id: str
    shard_id: str
    collector_id: str
    validator_id: str
    logical_output_id: str
    artifacts: tuple[CapabilityArtifact, ...]
    access_profile: str
    access_receipt_digest: str | None

    def __post_init__(self) -> None:
        if not 1 <= self.attempt_number <= 10 or not self.tenant_id:
            raise ValueError("scientific workload capability identity is invalid")
        if len({item.logical_artifact_id for item in self.artifacts}) != len(self.artifacts):
            raise ValueError("scientific workload capability artifacts are duplicated")

    def value(self) -> dict[str, Any]:
        return {
            "schema": CAPABILITY_SCHEMA,
            "operation_id": str(self.operation_id),
            "batch_id": str(self.batch_id),
            "workload_id": str(self.workload_id),
            "attempt_id": str(self.attempt_id),
            "attempt_number": self.attempt_number,
            "tenant_id": self.tenant_id,
            "model_id": self.model_id,
            "variant_id": self.variant_id,
            "stage_id": self.stage_id,
            "shard_id": self.shard_id,
            "collector_id": self.collector_id,
            "validator_id": self.validator_id,
            "logical_output_id": self.logical_output_id,
            "artifacts": [
                {
                    "logical_artifact_id": item.logical_artifact_id,
                    "artifact_id": str(item.artifact_id),
                    "digest": item.digest,
                    "size_bytes": item.size_bytes,
                    "media_type": item.media_type,
                    "compression": item.compression,
                }
                for item in self.artifacts
            ],
            "access": {
                "profile": self.access_profile,
                "receipt_digest": self.access_receipt_digest,
            },
        }


class ScientificWorkloadCapabilityAuthority:
    """Issue deterministic capabilities and reject any modified claim."""

    def __init__(self, hasher: KeyedHasher) -> None:
        self.hasher = hasher

    def issue(self, resource: WorkloadResource) -> str:
        if resource.invocation is None:
            raise ValueError("scientific workload capability requires an invocation")
        claims = ScientificWorkloadCapability(
            operation_id=resource.operation_id,
            batch_id=resource.batch_id,
            workload_id=resource.workload_id,
            attempt_id=resource.attempt_id,
            attempt_number=resource.attempt_number,
            tenant_id=resource.tenant_id,
            model_id=resource.model_id,
            variant_id=resource.variant_id,
            stage_id=resource.stage_id,
            shard_id=resource.shard_id or "gang",
            collector_id=resource.invocation.collector_id,
            validator_id=resource.invocation.validator_id,
            logical_output_id=resource.invocation.produces,
            artifacts=tuple(
                CapabilityArtifact(
                    logical_artifact_id=item.logical_artifact_id,
                    artifact_id=item.artifact_id,
                    digest=item.digest,
                    size_bytes=item.size_bytes,
                    media_type=item.media_type,
                    compression=item.compression,
                )
                for item in resource.materializations
            ),
            access_profile=resource.access_context.profile,
            access_receipt_digest=resource.access_context.receipt_digest,
        )
        payload = json.dumps(claims.value(), sort_keys=True, separators=(",", ":")).encode()
        key_id, digest = self.hasher.digest(payload, context=_CONTEXT)
        return f"{key_id}.{_encode(payload)}.{digest}"

    def verify(self, token: str) -> ScientificWorkloadCapability:
        if not 1 <= len(token) <= 16_384:
            raise ValueError("scientific workload capability is invalid")
        try:
            key_id, encoded, supplied = token.split(".")
            payload = _decode(encoded)
            expected = self.hasher.digest_for(key_id, payload, context=_CONTEXT)
            if not hmac.compare_digest(supplied, expected):
                raise ValueError("scientific workload capability is invalid")
            value = json.loads(payload)
        except (UnicodeError, ValueError, json.JSONDecodeError):
            raise ValueError("scientific workload capability is invalid") from None
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "schema",
                "operation_id",
                "batch_id",
                "workload_id",
                "attempt_id",
                "attempt_number",
                "tenant_id",
                "model_id",
                "variant_id",
                "stage_id",
                "shard_id",
                "collector_id",
                "validator_id",
                "logical_output_id",
                "artifacts",
                "access",
            }
            or value.get("schema") != CAPABILITY_SCHEMA
        ):
            raise ValueError("scientific workload capability fields differ")
        artifacts = value["artifacts"]
        access = value["access"]
        if (
            not isinstance(artifacts, list)
            or not isinstance(access, dict)
            or set(access)
            != {
                "profile",
                "receipt_digest",
            }
        ):
            raise ValueError("scientific workload capability fields differ")
        try:
            bindings = tuple(
                CapabilityArtifact(
                    logical_artifact_id=str(item["logical_artifact_id"]),
                    artifact_id=UUID(str(item["artifact_id"])),
                    digest=str(item["digest"]),
                    size_bytes=int(item["size_bytes"]),
                    media_type=str(item["media_type"]),
                    compression=None if item["compression"] is None else str(item["compression"]),
                )
                for item in artifacts
                if isinstance(item, dict)
                and set(item)
                == {"logical_artifact_id", "artifact_id", "digest", "size_bytes", "media_type", "compression"}
            )
            if len(bindings) != len(artifacts):
                raise ValueError
            return ScientificWorkloadCapability(
                operation_id=UUID(str(value["operation_id"])),
                batch_id=UUID(str(value["batch_id"])),
                workload_id=UUID(str(value["workload_id"])),
                attempt_id=UUID(str(value["attempt_id"])),
                attempt_number=int(value["attempt_number"]),
                tenant_id=str(value["tenant_id"]),
                model_id=str(value["model_id"]),
                variant_id=str(value["variant_id"]),
                stage_id=str(value["stage_id"]),
                shard_id=str(value["shard_id"]),
                collector_id=str(value["collector_id"]),
                validator_id=str(value["validator_id"]),
                logical_output_id=str(value["logical_output_id"]),
                artifacts=bindings,
                access_profile=str(access["profile"]),
                access_receipt_digest=(None if access["receipt_digest"] is None else str(access["receipt_digest"])),
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError("scientific workload capability values are invalid") from None
