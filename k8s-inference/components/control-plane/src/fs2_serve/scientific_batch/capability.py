"""Attempt-scoped HMAC capabilities for artifact companion containers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from ..crypto import KeyedHasher
from ..scientific_cpu import run_scientific_cpu
from .codec import _retained_metadata_bytes
from .models import StageInvocation, VerifiedInputManifest, WorkloadResource

CAPABILITY_SCHEMA = "fs2-serve.nebius.ai/scientific-workload-capability/v1"
COMPACT_CAPABILITY_SCHEMA = "fs2-serve.nebius.ai/scientific-workload-capability/v2"
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
    artifacts_digest: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.attempt_number <= 10 or not self.tenant_id:
            raise ValueError("scientific workload capability identity is invalid")
        if len({item.logical_artifact_id for item in self.artifacts}) != len(self.artifacts):
            raise ValueError("scientific workload capability artifacts are duplicated")

    def value(self) -> dict[str, Any]:
        value = {
            "schema": COMPACT_CAPABILITY_SCHEMA if self.artifacts_digest is not None else CAPABILITY_SCHEMA,
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
        if self.artifacts_digest is not None:
            value["artifacts_digest"] = self.artifacts_digest
        return value


def capability_artifacts_digest(artifacts: tuple[CapabilityArtifact, ...]) -> str:
    values = [
        {
            "logical_artifact_id": item.logical_artifact_id,
            "artifact_id": str(item.artifact_id),
            "digest": item.digest,
            "size_bytes": item.size_bytes,
            "media_type": item.media_type,
            "compression": item.compression,
        }
        for item in artifacts
    ]
    return "sha256:" + hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


_Bindings = tuple[tuple[CapabilityArtifact, ...], str]
_RetainedBindings = tuple[VerifiedInputManifest, StageInvocation, _Bindings]


class _ImmutableBindingsCache:
    """Only a frozen graph's derived file identities, never an authorization."""

    def __init__(self, *, max_bytes: int = 64 * 1024**2, max_entries: int = 4) -> None:
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self.retained_bytes = 0
        self.entries: OrderedDict[tuple[int, int], tuple[_RetainedBindings, int]] = OrderedDict()
        self.lock = threading.Lock()

    def get(self, manifest: VerifiedInputManifest, invocation: StageInvocation) -> _Bindings | None:
        key = (id(manifest), id(invocation))
        with self.lock:
            entry = self.entries.get(key)
            if entry is None:
                return None
            # Strong references keep both frozen source objects alive, so a
            # recycled object ID can never turn a different graph into a hit.
            value, _ = entry
            if value[0] is not manifest or value[1] is not invocation:
                return None
            self.entries.move_to_end(key)
            return value[2]

    def put(self, manifest: VerifiedInputManifest, invocation: StageInvocation, bindings: _Bindings) -> None:
        key = (id(manifest), id(invocation))
        value = (manifest, invocation, bindings)
        try:
            weight = _retained_metadata_bytes((key, value))
        except ValueError:
            return  # Future mutable types bypass the optimization, never validation.
        if weight > self.max_bytes or self.max_entries < 1:
            return
        with self.lock:
            previous = self.entries.pop(key, None)
            if previous is not None:
                self.retained_bytes -= previous[1]
            while self.entries and (
                len(self.entries) >= self.max_entries or self.retained_bytes + weight > self.max_bytes
            ):
                _, (_, removed) = self.entries.popitem(last=False)
                self.retained_bytes -= removed
            self.entries[key] = (value, weight)
            self.retained_bytes += weight


_IMMUTABLE_BINDINGS = _ImmutableBindingsCache()


def _build_input_bindings(manifest: VerifiedInputManifest, invocation: StageInvocation) -> _Bindings:
    cached = _IMMUTABLE_BINDINGS.get(manifest, invocation)
    if cached is not None:
        return cached
    sources = {item.logical_artifact_id: item for item in manifest.entries}
    bindings = tuple(
        CapabilityArtifact(
            logical_artifact_id=item.artifact_id,
            artifact_id=(source := sources[item.artifact_id]).artifact_id,
            digest=source.digest,
            size_bytes=source.size_bytes,
            media_type=source.media_type,
            compression=source.compression,
        )
        for item in invocation.materializations
    )
    value = (bindings, capability_artifacts_digest(bindings))
    _IMMUTABLE_BINDINGS.put(manifest, invocation, value)
    return value


async def immutable_input_bindings(manifest: VerifiedInputManifest, invocation: StageInvocation) -> _Bindings:
    cached = _IMMUTABLE_BINDINGS.get(manifest, invocation)
    return cached if cached is not None else await run_scientific_cpu(_build_input_bindings, manifest, invocation)


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
        if len(payload) > 4096 and resource.model_id in {"gromacs", "gromacs-mpi"}:
            # Long native continuations already have an immutable input manifest
            # in durable batch state. Bind its exact ordered artifact identities
            # instead of copying hundreds of file records into every HTTP header.
            claims = replace(claims, artifacts_digest=capability_artifacts_digest(claims.artifacts), artifacts=())
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
        compact = isinstance(value, dict) and value.get("schema") == COMPACT_CAPABILITY_SCHEMA
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
            | ({"artifacts_digest"} if compact else set())
            or value.get("schema") not in {CAPABILITY_SCHEMA, COMPACT_CAPABILITY_SCHEMA}
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
        if compact and (
            artifacts != []
            or value["model_id"] not in {"gromacs", "gromacs-mpi"}
            or not isinstance(value["artifacts_digest"], str)
            or not value["artifacts_digest"].startswith("sha256:")
            or len(value["artifacts_digest"]) != 71
        ):
            raise ValueError("scientific workload capability artifact binding is invalid")
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
                artifacts_digest=value["artifacts_digest"] if compact else None,
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError("scientific workload capability values are invalid") from None
