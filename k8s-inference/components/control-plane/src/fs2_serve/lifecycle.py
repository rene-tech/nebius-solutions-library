"""Append-only workload lifecycle facts and deterministic GPU accounting.

OpenTelemetry carries request causality while this module keeps the durable,
payload-free facts needed for accounting.  The reconciler intentionally works
from immutable start/end signals instead of inferring GPU use from request
latency.  It partitions every scheduler-occupied GPU rank exactly once, using
an explicit precedence when independently observed phase intervals overlap.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from asyncio import Lock
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import asyncpg
from pydantic import AwareDatetime, Field, JsonValue, field_validator, model_validator

from .models import StrictModel

_SHA256_RE = r"^(?:sha256:)?[a-f0-9]{64}$"
_TRACE_ID_RE = r"^[a-f0-9]{32}$"
_SPAN_ID_RE = r"^[a-f0-9]{16}$"
_EVENT_KEY_RE = r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,511}$"
_GPU_UUID_RE = r"^(?:GPU|MIG)-[A-Za-z0-9_.:/-]{1,123}$"
_MEDIA_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}$")
_PAYLOAD_SHAPE_KEYS = frozenset(
    {
        "availability",
        "bytes",
        "characters",
        "content_type",
        "field_count",
        "fields",
        "fields_truncated",
        "items",
        "json",
        "json_shape",
        "length",
        "name_sha256",
        "reason",
        "sample_truncated",
        "shape",
        "truncated",
        "type",
        "utf8_bytes",
    }
)
_PAYLOAD_SHAPE_NUMBERS = frozenset({"bytes", "characters", "field_count", "length", "utf8_bytes"})
_PAYLOAD_SHAPE_BOOLEANS = frozenset({"fields_truncated", "sample_truncated", "truncated"})
_PAYLOAD_SHAPE_LISTS = frozenset({"fields", "items"})
_PAYLOAD_SHAPE_TYPES = frozenset(
    {
        "NoneType",
        "array",
        "bool",
        "boolean",
        "dict",
        "float",
        "int",
        "list",
        "null",
        "number",
        "object",
        "str",
        "string",
    }
)
_SAFE_DETAIL_KEYS = frozenset(
    {
        "artifact_digest",
        "capacity_type",
        "checkpoint_digest",
        "image_digest",
        "outcome",
        "reason_code",
        "resource_flavor",
        "resource_version",
        "service_class",
        "source_event_uid",
    }
)


class WorkloadTelemetryKind(StrEnum):
    ONLINE = "online"
    SCIENTIFIC_BATCH = "scientific_batch"


class LifecycleClock(StrEnum):
    LIFECYCLE = "lifecycle"
    QUOTA_RESERVED = "quota_reserved"
    SCHEDULER_OCCUPIED = "scheduler_occupied"
    DEVICE_ALLOCATED = "device_allocated"
    PHASE = "phase"


class LifecycleEdge(StrEnum):
    START = "start"
    END = "end"
    INSTANT = "instant"


class LifecycleSource(StrEnum):
    APPLICATION = "application"
    CONTROLLER = "controller"
    KUEUE = "kueue"
    KUBERNETES = "kubernetes"
    KUBELET = "kubelet"
    DCGM = "dcgm"
    DERIVED = "derived"


class MeasurementQuality(StrEnum):
    MEASURED = "measured"
    APPLICATION_OBSERVED = "application_observed"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


class LifecyclePhase(StrEnum):
    RECEIVE = "receive"
    ENQUEUE = "enqueue"
    ADMISSION_WAIT = "admission_wait"
    ADMIT = "admit"
    NODE_REQUEST = "node_request"
    NODE_READY = "node_ready"
    IMAGE_PULL = "image_pull"
    ARTIFACT_LOAD = "artifact_load"
    RESTORE = "restore"
    COMPILE = "compile"
    CONTAINER_READY = "container_ready"
    RUNTIME_READY = "runtime_ready"
    WARMUP = "warmup"
    GPU_ALLOCATION = "gpu_allocation"
    ACTIVE_COMPUTE = "active_compute"
    WORKFLOW_WAIT = "workflow_wait"
    RESIDENT_IDLE = "resident_idle"
    COOLDOWN_GRACE = "cooldown_grace"
    CHECKPOINT_DRAIN = "checkpoint_drain"
    PREEMPTION = "preemption"
    RETRY = "retry"
    TEARDOWN = "teardown"
    RELEASE = "release"
    UNCLASSIFIED = "unclassified"


# Useful work wins over broad idle windows; shutdown work wins over grace.
# Ties are resolved by the enum value, so reconciliation is deterministic.
_PHASE_PRECEDENCE: Mapping[LifecyclePhase, int] = {
    LifecyclePhase.ACTIVE_COMPUTE: 100,
    LifecyclePhase.CHECKPOINT_DRAIN: 90,
    LifecyclePhase.RESTORE: 80,
    LifecyclePhase.COMPILE: 75,
    LifecyclePhase.ARTIFACT_LOAD: 70,
    LifecyclePhase.IMAGE_PULL: 65,
    LifecyclePhase.WARMUP: 60,
    LifecyclePhase.WORKFLOW_WAIT: 50,
    LifecyclePhase.RESIDENT_IDLE: 40,
    LifecyclePhase.COOLDOWN_GRACE: 30,
    LifecyclePhase.TEARDOWN: 20,
    LifecyclePhase.UNCLASSIFIED: 0,
}
ACCOUNTING_PHASES = tuple(_PHASE_PRECEDENCE)


class ArtifactReference(StrictModel):
    role: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    uri: str = Field(min_length=1, max_length=2048)
    digest: str = Field(pattern=_SHA256_RE)
    size_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def reject_secret_bearing_uri(self) -> ArtifactReference:
        parsed = urlsplit(self.uri)
        if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError("artifact URI must not contain credentials, query parameters, or fragments")
        if parsed.scheme not in {"file", "gs", "hf", "https", "oci", "s3"}:
            raise ValueError("artifact URI scheme is not allowed")
        return self


def _validate_payload_shape(value: JsonValue, *, key: str | None = None, depth: int = 0) -> None:
    """Accept only the value-free shape grammar emitted by this module."""

    # One source nesting level expands into object -> fields -> item -> shape,
    # so the four-level extractor can legitimately use roughly 16 validation
    # levels even though customer data itself is already truncated.
    if depth > 32:
        raise ValueError("payload shape exceeds the bounded nesting depth")
    if isinstance(value, dict):
        unknown = set(value) - _PAYLOAD_SHAPE_KEYS
        if unknown:
            raise ValueError("payload shape contains a non-approved field")
        for child_key, child in value.items():
            _validate_payload_shape(child, key=child_key, depth=depth + 1)
        return
    if isinstance(value, list):
        if key not in _PAYLOAD_SHAPE_LISTS or len(value) > 64:
            raise ValueError("payload shape contains an invalid bounded list")
        for child in value:
            _validate_payload_shape(child, depth=depth + 1)
        return
    if isinstance(value, bool):
        if key not in _PAYLOAD_SHAPE_BOOLEANS:
            raise ValueError("payload shape contains an invalid boolean")
        return
    if isinstance(value, int):
        if key not in _PAYLOAD_SHAPE_NUMBERS or not 0 <= value <= 2**63 - 1:
            raise ValueError("payload shape contains an invalid count")
        return
    if not isinstance(value, str):
        raise ValueError("payload shape contains an unsupported scalar")
    if key == "content_type" and _MEDIA_TYPE_RE.fullmatch(value):
        return
    if key == "name_sha256" and re.fullmatch(r"[a-f0-9]{64}", value):
        return
    if key == "type" and value in _PAYLOAD_SHAPE_TYPES:
        return
    if (key, value) in {
        ("availability", "unavailable"),
        ("json", "invalid"),
        ("reason", "no_response_payload"),
    }:
        return
    raise ValueError("payload shape contains a non-shape string")


class ReproducibilityMetadata(StrictModel):
    input_shape: dict[str, JsonValue] = Field(default_factory=dict)
    parameter_digest: str | None = Field(default=None, pattern=_SHA256_RE)
    artifact_references: list[ArtifactReference] = Field(default_factory=list, max_length=128)

    @field_validator("input_shape")
    @classmethod
    def input_shape_is_value_free(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _validate_payload_shape(value)
        return value


class LifecycleSubject(StrictModel):
    subject_id: UUID
    workload_kind: WorkloadTelemetryKind
    operation_id: UUID | None = None
    request_id: UUID
    batch_id: UUID | None = None
    workload_id: UUID
    attempt_id: UUID | None = None
    tenant_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    principal_id: str = Field(min_length=1, max_length=200)
    api_key_id: UUID | None = None
    api_key_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    model_id: str = Field(min_length=1, max_length=128)
    model_revision: str = Field(min_length=1, max_length=256)
    protocol: str = Field(min_length=1, max_length=64)
    trace_id: str | None = Field(default=None, pattern=_TRACE_ID_RE)
    parent_span_id: str | None = Field(default=None, pattern=_SPAN_ID_RE)
    accepted_at: AwareDatetime
    reproducibility: ReproducibilityMetadata = Field(default_factory=ReproducibilityMetadata)

    @model_validator(mode="after")
    def validate_kind(self) -> LifecycleSubject:
        if self.workload_kind is WorkloadTelemetryKind.ONLINE:
            if self.operation_id is None or self.batch_id is not None:
                raise ValueError("online telemetry requires operation_id and forbids batch_id")
        elif self.operation_id is None or self.batch_id is None or self.attempt_id is None:
            raise ValueError("scientific batch telemetry requires operation, batch, and attempt IDs")
        if (self.api_key_id is None) != (self.api_key_fingerprint is None):
            raise ValueError("API-key ID and non-secret fingerprint must be supplied together")
        if self.api_key_id is not None and self.api_key_fingerprint != api_key_id_hash(self.api_key_id):
            raise ValueError("API-key fingerprint is not derived from the opaque key ID")
        return self


class LifecycleCorrelation(StrictModel):
    correlation_key: str = Field(min_length=1, max_length=512, pattern=_EVENT_KEY_RE)
    subject_id: UUID
    observed_at: AwareDatetime
    source: LifecycleSource
    attempt: int = Field(default=0, ge=0, le=1024)
    cluster: str | None = Field(default=None, max_length=128)
    namespace: str | None = Field(default=None, max_length=63)
    queue_name: str | None = Field(default=None, max_length=253)
    kueue_workload_name: str | None = Field(default=None, max_length=253)
    kueue_workload_uid: str | None = Field(default=None, max_length=128)
    job_name: str | None = Field(default=None, max_length=253)
    job_uid: str | None = Field(default=None, max_length=128)
    pod_name: str | None = Field(default=None, max_length=253)
    pod_uid: str | None = Field(default=None, max_length=128)
    node_name: str | None = Field(default=None, max_length=253)
    node_uid: str | None = Field(default=None, max_length=128)
    gpu_uuid: str | None = Field(default=None, pattern=_GPU_UUID_RE)
    gpu_rank: int | None = Field(default=None, ge=0, le=1023)

    @model_validator(mode="after")
    def validate_gpu_coordinate(self) -> LifecycleCorrelation:
        if (self.gpu_uuid is None) != (self.gpu_rank is None):
            raise ValueError("GPU UUID and rank must be supplied together")
        if self.gpu_uuid is not None and self.pod_uid is None:
            raise ValueError("GPU correlations require an exact Pod UID")
        if not any(
            (
                self.queue_name,
                self.kueue_workload_uid,
                self.job_uid,
                self.pod_uid,
                self.node_uid,
                self.gpu_uuid,
            )
        ):
            raise ValueError("correlation must bind at least one queue or runtime identity")
        return self


class LifecycleSignal(StrictModel):
    sequence: int | None = Field(default=None, ge=1)
    event_key: str = Field(min_length=1, max_length=512, pattern=_EVENT_KEY_RE)
    subject_id: UUID
    occurred_at: AwareDatetime
    observed_at: AwareDatetime
    source: LifecycleSource
    source_resolution_seconds: float = Field(default=0, ge=0, le=300)
    quality: MeasurementQuality
    phase: LifecyclePhase
    edge: LifecycleEdge
    clock: LifecycleClock
    interval_key: str | None = Field(default=None, min_length=1, max_length=512, pattern=_EVENT_KEY_RE)
    attempt: int = Field(default=0, ge=0, le=1024)
    gpu_count: int = Field(default=0, ge=0, le=1024)
    cluster: str | None = Field(default=None, max_length=128)
    namespace: str | None = Field(default=None, max_length=63)
    queue_name: str | None = Field(default=None, max_length=253)
    kueue_workload_name: str | None = Field(default=None, max_length=253)
    kueue_workload_uid: str | None = Field(default=None, max_length=128)
    job_name: str | None = Field(default=None, max_length=253)
    job_uid: str | None = Field(default=None, max_length=128)
    pod_name: str | None = Field(default=None, max_length=253)
    pod_uid: str | None = Field(default=None, max_length=128)
    node_name: str | None = Field(default=None, max_length=253)
    node_uid: str | None = Field(default=None, max_length=128)
    gpu_uuid: str | None = Field(default=None, pattern=_GPU_UUID_RE)
    gpu_rank: int | None = Field(default=None, ge=0, le=1023)
    detail: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_interval(self) -> LifecycleSignal:
        if self.observed_at < self.occurred_at:
            raise ValueError("lifecycle signal cannot be observed before it occurs")
        if self.edge is LifecycleEdge.INSTANT:
            if self.interval_key is not None:
                raise ValueError("instant lifecycle signals cannot name an interval")
        elif self.interval_key is None:
            raise ValueError("start/end lifecycle signals require interval_key")
        if (self.gpu_uuid is None) != (self.gpu_rank is None):
            raise ValueError("GPU UUID and rank must be supplied together")
        if self.clock is LifecycleClock.DEVICE_ALLOCATED:
            if self.pod_uid is None or self.gpu_uuid is None or self.gpu_count != 1:
                raise ValueError("device allocation signals require an exact Pod UID and one GPU UUID/rank")
        if self.gpu_uuid is not None and self.gpu_count not in {0, 1}:
            raise ValueError("a GPU coordinate identifies exactly one device")
        if self.gpu_uuid is not None and self.gpu_count != 1:
            raise ValueError("a GPU-coordinate signal must account for one device")
        if (
            self.clock
            in {
                LifecycleClock.QUOTA_RESERVED,
                LifecycleClock.SCHEDULER_OCCUPIED,
            }
            and self.edge is not LifecycleEdge.INSTANT
            and self.gpu_count < 1
        ):
            raise ValueError("quota/scheduler intervals require a positive GPU count")
        if (
            self.clock is LifecycleClock.QUOTA_RESERVED
            and self.edge is not LifecycleEdge.INSTANT
            and self.kueue_workload_uid is None
        ):
            raise ValueError("quota reservation intervals require an exact Kueue Workload UID")
        if (
            self.clock is LifecycleClock.SCHEDULER_OCCUPIED
            and self.edge is not LifecycleEdge.INSTANT
            and self.pod_uid is None
        ):
            raise ValueError("scheduler occupancy intervals require an exact Pod UID")
        if self.clock is LifecycleClock.PHASE and self.phase not in ACCOUNTING_PHASES:
            raise ValueError("phase intervals must use an accounting phase")
        expected_clock_phases = {
            LifecycleClock.QUOTA_RESERVED: LifecyclePhase.ADMIT,
            LifecycleClock.SCHEDULER_OCCUPIED: LifecyclePhase.GPU_ALLOCATION,
            LifecycleClock.DEVICE_ALLOCATED: LifecyclePhase.GPU_ALLOCATION,
        }
        expected_phase = expected_clock_phases.get(self.clock)
        if expected_phase is not None and self.phase is not expected_phase:
            raise ValueError(f"{self.clock.value} signals must use the {expected_phase.value} phase")
        unknown_detail = set(self.detail) - _SAFE_DETAIL_KEYS
        if unknown_detail:
            raise ValueError("lifecycle detail contains a non-approved field")
        for value in self.detail.values():
            if not isinstance(value, str) or not 1 <= len(value) <= 512:
                raise ValueError("lifecycle detail values must be bounded non-secret strings")
        return self


class LifecycleRollup(StrictModel):
    rollup_id: UUID
    subject_id: UUID
    generated_at: AwareDatetime
    event_watermark: int = Field(ge=0)
    events_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    terminal: bool
    outcome: str | None = Field(default=None, max_length=64)
    quota_reserved_gpu_seconds: float = Field(ge=0)
    scheduler_occupied_gpu_seconds: float = Field(ge=0)
    device_allocated_gpu_seconds: float = Field(ge=0)
    active_gpu_seconds: float = Field(ge=0)
    occupied_idle_gpu_seconds: float = Field(ge=0)
    phase_gpu_seconds: dict[str, float]
    reconciliation_delta_seconds: float
    device_scheduler_delta_seconds: float
    tolerance_seconds: float = Field(ge=0)
    reconciled: bool
    quality: MeasurementQuality
    data_gaps: list[str] = Field(max_length=128)
    output_shape: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("output_shape")
    @classmethod
    def output_shape_is_value_free(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _validate_payload_shape(value)
        return value


class LifecycleWorkloadSummary(StrictModel):
    subject: LifecycleSubject
    rollup: LifecycleRollup | None = None


class LifecycleWorkloadDetail(LifecycleWorkloadSummary):
    correlations: list[LifecycleCorrelation] = Field(max_length=10_000)
    signals: list[LifecycleSignal] = Field(max_length=100_000)
    payloads_exposed: bool = False


class LifecycleAdminList(StrictModel):
    items: list[LifecycleWorkloadSummary] = Field(max_length=200)
    total: int = Field(ge=0)


class LifecycleMetricRow(StrictModel):
    tenant_id: str
    model_id: str
    phase: str
    quality: MeasurementQuality
    seconds: float = Field(ge=0)


class LifecycleRollupMetricRow(StrictModel):
    tenant_id: str
    model_id: str
    quality: MeasurementQuality
    reconciled: bool
    workloads: int = Field(ge=0)
    quota_reserved_gpu_seconds: float = Field(ge=0)
    scheduler_occupied_gpu_seconds: float = Field(ge=0)
    device_allocated_gpu_seconds: float = Field(ge=0)
    absolute_reconciliation_delta_seconds: float = Field(ge=0)
    unclassified_gpu_seconds: float = Field(ge=0)


class LifecycleRepository(Protocol):
    async def register_subject(self, subject: LifecycleSubject) -> LifecycleSubject: ...

    async def append_correlations(self, correlations: Sequence[LifecycleCorrelation]) -> None: ...

    async def append_signals(self, signals: Sequence[LifecycleSignal]) -> None: ...

    async def reconcile(
        self,
        subject_id: UUID,
        *,
        terminal: bool,
        outcome: str | None,
        output_shape: Mapping[str, JsonValue] | None = None,
    ) -> LifecycleRollup | None: ...

    async def list_workloads(
        self,
        *,
        tenant_id: str | None,
        model_id: str | None,
        operation_id: UUID | None,
        limit: int,
    ) -> LifecycleAdminList: ...

    async def get_workload(
        self,
        subject_id: UUID,
        *,
        tenant_id: str | None,
    ) -> LifecycleWorkloadDetail | None: ...

    async def metric_rows(self) -> list[LifecycleMetricRow]: ...

    async def rollup_metric_rows(self) -> list[LifecycleRollupMetricRow]: ...


@dataclass(frozen=True, slots=True)
class _Interval:
    key: str
    start: datetime
    end: datetime
    clock: LifecycleClock
    phase: LifecyclePhase
    quality: MeasurementQuality
    resolution: float
    gpu_count: int
    pod_uid: str | None
    gpu_uuid: str | None
    gpu_rank: int | None
    kueue_uid: str | None


def api_key_id_hash(api_key_id: UUID) -> str:
    """Return a stable non-secret correlation hash, never a credential hash."""

    return hashlib.sha256(f"fs2-api-key-id/v1:{api_key_id}".encode()).hexdigest()


def trace_identity(traceparent: str | None) -> tuple[str | None, str | None]:
    """Return bounded W3C IDs while rejecting zero or malformed identities."""

    if traceparent is None:
        return None, None
    parts = traceparent.lower().split("-")
    if len(parts) != 4 or parts[0] != "00" or len(parts[1]) != 32 or len(parts[2]) != 16 or len(parts[3]) != 2:
        return None, None
    if not all(character in "0123456789abcdef" for part in parts for character in part):
        return None, None
    if parts[1] == "0" * 32 or parts[2] == "0" * 16:
        return None, None
    return parts[1], parts[2]


def _bounded_json_shape(value: JsonValue, *, depth: int = 0) -> JsonValue:
    if depth >= 4:
        return {"type": type(value).__name__, "truncated": True}
    if isinstance(value, dict):
        # Caller-controlled field names can themselves contain payload or
        # credentials. Retain only stable name hashes and value shapes.
        names = sorted(str(key) for key in value)[:64]
        return {
            "type": "object",
            "field_count": len(value),
            "fields": [
                {
                    "name_sha256": hashlib.sha256(name.encode("utf-8")).hexdigest(),
                    "shape": _bounded_json_shape(value[name], depth=depth + 1),
                }
                for name in names
            ],
            "fields_truncated": len(value) > len(names),
        }
    if isinstance(value, list):
        sample = value[:8]
        return {
            "type": "array",
            "length": len(value),
            "items": [_bounded_json_shape(item, depth=depth + 1) for item in sample],
            "sample_truncated": len(value) > len(sample),
        }
    if isinstance(value, str):
        return {"type": "string", "utf8_bytes": len(value.encode("utf-8")), "characters": len(value)}
    if value is None:
        return {"type": "null"}
    return {"type": "boolean" if isinstance(value, bool) else "number"}


def reproducibility_metadata(body: bytes, content_type: str) -> ReproducibilityMetadata:
    """Extract shapes and explicitly named reproducibility references only.

    No prompt, sequence, image, credential, or arbitrary customer field value is
    retained. Parameter values are represented solely by one canonical digest.
    Artifact URIs/digests are accepted only from an explicit ``artifacts`` list.
    """

    media_type = content_type.split(";", 1)[0].strip().lower()
    if not _MEDIA_TYPE_RE.fullmatch(media_type):
        media_type = "application/octet-stream"
    base: dict[str, JsonValue] = {"content_type": media_type, "bytes": len(body)}
    if "json" not in content_type.lower():
        return ReproducibilityMetadata(input_shape=base)
    try:
        parsed = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return ReproducibilityMetadata(input_shape={**base, "json": "invalid"})
    if not isinstance(parsed, dict | list):
        return ReproducibilityMetadata(input_shape={**base, "json_shape": _bounded_json_shape(cast(JsonValue, parsed))})

    shape = {**base, "json_shape": _bounded_json_shape(cast(JsonValue, parsed))}
    parameter_digest: str | None = None
    artifacts: list[ArtifactReference] = []
    if isinstance(parsed, dict):
        parameters = parsed.get("parameters")
        if isinstance(parameters, dict):
            encoded = json.dumps(parameters, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            parameter_digest = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
        candidate_artifacts = parsed.get("artifacts")
        if isinstance(candidate_artifacts, list):
            for item in candidate_artifacts[:128]:
                if not isinstance(item, dict):
                    continue
                selected = {
                    "role": item.get("role"),
                    "uri": item.get("uri"),
                    "digest": item.get("digest"),
                    "size_bytes": item.get("size_bytes"),
                }
                try:
                    artifacts.append(ArtifactReference.model_validate(selected))
                except ValueError:
                    continue
    return ReproducibilityMetadata(
        input_shape=shape,
        parameter_digest=parameter_digest,
        artifact_references=artifacts,
    )


def payload_shape(body: bytes | None, content_type: str | None) -> dict[str, JsonValue]:
    if body is None:
        return {"availability": "unavailable", "reason": "no_response_payload"}
    return reproducibility_metadata(body, content_type or "application/octet-stream").input_shape


def _events_digest(signals: Sequence[LifecycleSignal]) -> str:
    values = [signal.model_dump(mode="json", exclude_none=True) for signal in signals]
    body = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(body).hexdigest()


def _paired_intervals(signals: Sequence[LifecycleSignal]) -> tuple[list[_Interval], list[str]]:
    groups: dict[str, list[LifecycleSignal]] = defaultdict(list)
    for signal in signals:
        if signal.interval_key is not None:
            groups[signal.interval_key].append(signal)
    intervals: list[_Interval] = []
    gaps: list[str] = []
    for key, values in sorted(groups.items()):
        starts = [value for value in values if value.edge is LifecycleEdge.START]
        ends = [value for value in values if value.edge is LifecycleEdge.END]
        if len(starts) != 1 or len(ends) != 1:
            gaps.append(f"incomplete_interval:{key[:96]}")
            continue
        start, end = starts[0], ends[0]

        def identity(signal: LifecycleSignal) -> tuple[object, ...]:
            return (
                signal.subject_id,
                signal.clock,
                signal.phase,
                signal.attempt,
                signal.gpu_count,
                signal.pod_uid,
                signal.gpu_uuid,
                signal.gpu_rank,
                signal.kueue_workload_uid,
            )

        if identity(start) != identity(end) or end.occurred_at < start.occurred_at:
            gaps.append(f"invalid_interval:{key[:96]}")
            continue
        intervals.append(
            _Interval(
                key=key,
                start=start.occurred_at.astimezone(UTC),
                end=end.occurred_at.astimezone(UTC),
                clock=start.clock,
                phase=start.phase,
                quality=_worst_quality((start.quality, end.quality)),
                resolution=max(start.source_resolution_seconds, end.source_resolution_seconds),
                gpu_count=start.gpu_count,
                pod_uid=start.pod_uid,
                gpu_uuid=start.gpu_uuid,
                gpu_rank=start.gpu_rank,
                kueue_uid=start.kueue_workload_uid,
            )
        )
    return intervals, gaps


def _merge_seconds(values: Iterable[tuple[datetime, datetime]]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    total = 0.0
    start, end = ordered[0]
    for next_start, next_end in ordered[1:]:
        if next_start <= end:
            end = max(end, next_end)
            continue
        total += (end - start).total_seconds()
        start, end = next_start, next_end
    return total + (end - start).total_seconds()


def _clock_lanes(interval: _Interval) -> tuple[str, ...]:
    if interval.clock is LifecycleClock.DEVICE_ALLOCATED:
        if interval.pod_uid is None or interval.gpu_uuid is None or interval.gpu_rank is None:
            return ()
        # The device UUID is the physical accounting lane. Pod/rank remain in
        # the facts for attribution, but overlapping DCGM and kubelet evidence
        # for the same device must not claim its seconds twice.
        return (f"device:{interval.gpu_uuid}",)
    if interval.clock is LifecycleClock.QUOTA_RESERVED:
        owner = interval.kueue_uid or f"unknown:{interval.key}"
        return tuple(f"quota:{owner}:{rank}" for rank in range(interval.gpu_count))
    if interval.clock is LifecycleClock.SCHEDULER_OCCUPIED:
        owner = interval.pod_uid or f"unknown:{interval.key}"
        return tuple(f"pod:{owner}:{rank}" for rank in range(interval.gpu_count))
    return ()


def _clock_total(intervals: Sequence[_Interval], clock: LifecycleClock) -> tuple[float, int]:
    lanes: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    for interval in intervals:
        if interval.clock is not clock:
            continue
        for lane in _clock_lanes(interval):
            lanes[lane].append((interval.start, interval.end))
    return sum(_merge_seconds(values) for values in lanes.values()), len(lanes)


def _phase_applies(interval: _Interval, pod_uid: str, rank: int) -> bool:
    if interval.pod_uid is not None and interval.pod_uid != pod_uid:
        return False
    return interval.gpu_rank is None or interval.gpu_rank == rank


def _phase_partition(intervals: Sequence[_Interval]) -> tuple[dict[str, float], int]:
    occupied = [item for item in intervals if item.clock is LifecycleClock.SCHEDULER_OCCUPIED]
    fallback = False
    if not occupied:
        occupied = [item for item in intervals if item.clock is LifecycleClock.DEVICE_ALLOCATED]
        fallback = True
    phases = [item for item in intervals if item.clock is LifecycleClock.PHASE]
    totals = {phase.value: 0.0 for phase in ACCOUNTING_PHASES}
    lanes: dict[tuple[str, int], list[tuple[datetime, datetime]]] = defaultdict(list)
    for interval in occupied:
        pod_uid = interval.pod_uid or f"unknown:{interval.key}"
        ranks: tuple[int, ...]
        if fallback:
            assert interval.gpu_rank is not None
            ranks = (interval.gpu_rank,)
        else:
            ranks = tuple(range(interval.gpu_count))
        for rank in ranks:
            lanes[(pod_uid, rank)].append((interval.start, interval.end))

    for (pod_uid, rank), raw_windows in lanes.items():
        windows = sorted(raw_windows)
        # Merge occupancy first so duplicate controller/collector windows do not
        # double-count one Pod rank.
        merged: list[tuple[datetime, datetime]] = []
        for start, end in windows:
            if not merged or start > merged[-1][1]:
                merged.append((start, end))
            else:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        for window_start, window_end in merged:
            relevant = [
                phase
                for phase in phases
                if _phase_applies(phase, pod_uid, rank) and phase.start < window_end and phase.end > window_start
            ]
            boundaries = {window_start, window_end}
            for phase in relevant:
                boundaries.add(max(window_start, phase.start))
                boundaries.add(min(window_end, phase.end))
            ordered = sorted(boundaries)
            for start, end in zip(ordered, ordered[1:], strict=False):
                if end <= start:
                    continue
                active = [phase.phase for phase in relevant if phase.start < end and phase.end > start]
                selected = max(
                    active,
                    key=lambda phase: (_PHASE_PRECEDENCE.get(phase, -1), phase.value),
                    default=LifecyclePhase.UNCLASSIFIED,
                )
                totals[selected.value] += (end - start).total_seconds()
    return totals, len(lanes)


def _worst_quality(values: Iterable[MeasurementQuality]) -> MeasurementQuality:
    rank = {
        MeasurementQuality.MEASURED: 0,
        MeasurementQuality.APPLICATION_OBSERVED: 1,
        MeasurementQuality.ESTIMATED: 2,
        MeasurementQuality.UNAVAILABLE: 3,
    }
    sequence = tuple(values)
    return max(sequence, key=rank.__getitem__) if sequence else MeasurementQuality.UNAVAILABLE


def reconcile_lifecycle(
    subject: LifecycleSubject,
    signals: Sequence[LifecycleSignal],
    *,
    terminal: bool,
    outcome: str | None,
    output_shape: Mapping[str, JsonValue] | None = None,
    generated_at: datetime | None = None,
) -> LifecycleRollup:
    """Reduce append-only facts into one immutable accounting revision."""

    ordered = tuple(sorted(signals, key=lambda item: (item.sequence or 0, item.occurred_at, item.event_key)))
    if any(signal.subject_id != subject.subject_id for signal in ordered):
        raise ValueError("lifecycle signal belongs to another subject")
    intervals, gaps = _paired_intervals(ordered)
    quota, _ = _clock_total(intervals, LifecycleClock.QUOTA_RESERVED)
    scheduler, scheduler_lanes = _clock_total(intervals, LifecycleClock.SCHEDULER_OCCUPIED)
    device, device_lanes = _clock_total(intervals, LifecycleClock.DEVICE_ALLOCATED)
    phase_totals, accounting_lanes = _phase_partition(intervals)
    accounted = math.fsum(phase_totals.values())
    accounting_clock = scheduler if scheduler_lanes else device
    reconciliation_delta = accounting_clock - accounted
    device_scheduler_delta = scheduler - device
    resolution = max((item.resolution for item in intervals), default=0.0)
    # Each independently sampled interval has two edges. The absolute
    # reconciliation budget is therefore two source-resolution buckets per
    # occupied rank, plus one percent for long-running clock drift.
    tolerance = max(0.001, accounting_clock * 0.01, 2 * resolution * max(1, accounting_lanes))

    if subject.trace_id is None:
        gaps.append("trace_context_missing")
    if scheduler > 0 and device_lanes == 0:
        gaps.append("device_allocation_clock_missing")
    if device > 0 and scheduler_lanes == 0:
        gaps.append("scheduler_occupancy_clock_missing")
    if device - scheduler > tolerance and scheduler > 0:
        gaps.append("device_allocation_exceeds_scheduler_occupancy")
    if accounting_clock > 0 and phase_totals[LifecyclePhase.UNCLASSIFIED.value] > tolerance:
        gaps.append("phase_classification_incomplete")
    if terminal and not any(signal.phase is LifecyclePhase.RELEASE for signal in ordered):
        gaps.append("release_event_missing")
    gaps = sorted(set(gaps))
    core_reconciliation_gaps = {
        gap
        for gap in gaps
        if gap.startswith(("incomplete_interval:", "invalid_interval:"))
        or gap
        in {
            "device_allocation_clock_missing",
            "device_allocation_exceeds_scheduler_occupancy",
            "release_event_missing",
            "scheduler_occupancy_clock_missing",
        }
    }
    # A zero-duration unavailable interval is an explicit schema placeholder,
    # not missing elapsed accounting.  It must not lower the quality of real
    # measured intervals.  Any unavailable interval with positive duration is
    # still retained and remains fail-closed.
    quality = _worst_quality(
        item.quality
        for item in intervals
        if item.quality is not MeasurementQuality.UNAVAILABLE or item.end > item.start
    )
    active = phase_totals[LifecyclePhase.ACTIVE_COMPUTE.value]
    return LifecycleRollup(
        rollup_id=uuid4(),
        subject_id=subject.subject_id,
        generated_at=(generated_at or datetime.now(UTC)).astimezone(UTC),
        event_watermark=max((item.sequence or 0 for item in ordered), default=0),
        events_sha256=_events_digest(ordered),
        terminal=terminal,
        outcome=outcome,
        quota_reserved_gpu_seconds=quota,
        scheduler_occupied_gpu_seconds=scheduler,
        device_allocated_gpu_seconds=device,
        active_gpu_seconds=active,
        occupied_idle_gpu_seconds=max(0.0, accounting_clock - active),
        phase_gpu_seconds={key: round(value, 9) for key, value in phase_totals.items()},
        reconciliation_delta_seconds=round(reconciliation_delta, 9),
        device_scheduler_delta_seconds=round(device_scheduler_delta, 9),
        tolerance_seconds=round(tolerance, 9),
        reconciled=abs(reconciliation_delta) <= tolerance and not core_reconciliation_gaps,
        quality=quality,
        data_gaps=gaps,
        output_shape=dict(output_shape or {}),
    )


def _json_value(value: object, label: str) -> object:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            raise RuntimeError(f"stored {label} is invalid") from None
    return value


def _subject_from_row(row: Mapping[str, Any]) -> LifecycleSubject:
    return LifecycleSubject(
        subject_id=row["subject_id"],
        workload_kind=row["workload_kind"],
        operation_id=row["operation_id"],
        request_id=row["request_id"],
        batch_id=row["batch_id"],
        workload_id=row["workload_id"],
        attempt_id=row["attempt_id"],
        tenant_id=row["tenant_id"],
        principal_id=row["principal_id"],
        api_key_id=row["api_key_id"],
        api_key_fingerprint=row["api_key_fingerprint"],
        model_id=row["model_id"],
        model_revision=row["model_revision"],
        protocol=row["protocol"],
        trace_id=row["trace_id"],
        parent_span_id=row["parent_span_id"],
        accepted_at=row["accepted_at"],
        reproducibility=ReproducibilityMetadata(
            input_shape=cast(dict[str, JsonValue], _json_value(row["input_shape"], "input shape")),
            parameter_digest=row["parameter_digest"],
            artifact_references=[
                ArtifactReference.model_validate(item)
                for item in cast(
                    list[dict[str, JsonValue]],
                    _json_value(row["artifact_references"], "artifacts"),
                )
            ],
        ),
    )


def _signal_from_row(row: Mapping[str, Any]) -> LifecycleSignal:
    return LifecycleSignal(
        sequence=row["id"],
        event_key=row["event_key"],
        subject_id=row["subject_id"],
        occurred_at=row["occurred_at"],
        observed_at=row["observed_at"],
        source=row["source"],
        source_resolution_seconds=row["source_resolution_seconds"],
        quality=row["quality"],
        phase=row["phase"],
        edge=row["edge"],
        clock=row["clock"],
        interval_key=row["interval_key"],
        attempt=row["attempt"],
        gpu_count=row["gpu_count"],
        cluster=row["cluster"],
        namespace=row["namespace"],
        queue_name=row["queue_name"],
        kueue_workload_name=row["kueue_workload_name"],
        kueue_workload_uid=row["kueue_workload_uid"],
        job_name=row["job_name"],
        job_uid=row["job_uid"],
        pod_name=row["pod_name"],
        pod_uid=row["pod_uid"],
        node_name=row["node_name"],
        node_uid=row["node_uid"],
        gpu_uuid=row["gpu_uuid"],
        gpu_rank=row["gpu_rank"],
        detail=cast(dict[str, JsonValue], _json_value(row["detail"], "signal detail")),
    )


def _correlation_from_row(row: Mapping[str, Any]) -> LifecycleCorrelation:
    return LifecycleCorrelation(
        correlation_key=row["correlation_key"],
        subject_id=row["subject_id"],
        observed_at=row["observed_at"],
        source=row["source"],
        attempt=row["attempt"],
        cluster=row["cluster"],
        namespace=row["namespace"],
        queue_name=row["queue_name"],
        kueue_workload_name=row["kueue_workload_name"],
        kueue_workload_uid=row["kueue_workload_uid"],
        job_name=row["job_name"],
        job_uid=row["job_uid"],
        pod_name=row["pod_name"],
        pod_uid=row["pod_uid"],
        node_name=row["node_name"],
        node_uid=row["node_uid"],
        gpu_uuid=row["gpu_uuid"],
        gpu_rank=row["gpu_rank"],
    )


def _rollup_from_row(row: Mapping[str, Any]) -> LifecycleRollup:
    return LifecycleRollup(
        rollup_id=row["rollup_id"],
        subject_id=row["subject_id"],
        generated_at=row["generated_at"],
        event_watermark=row["event_watermark"],
        events_sha256=row["events_sha256"],
        terminal=row["terminal"],
        outcome=row["outcome"],
        quota_reserved_gpu_seconds=row["quota_reserved_gpu_seconds"],
        scheduler_occupied_gpu_seconds=row["scheduler_occupied_gpu_seconds"],
        device_allocated_gpu_seconds=row["device_allocated_gpu_seconds"],
        active_gpu_seconds=row["active_gpu_seconds"],
        occupied_idle_gpu_seconds=row["occupied_idle_gpu_seconds"],
        phase_gpu_seconds=cast(dict[str, float], _json_value(row["phase_gpu_seconds"], "phase accounting")),
        reconciliation_delta_seconds=row["reconciliation_delta_seconds"],
        device_scheduler_delta_seconds=row["device_scheduler_delta_seconds"],
        tolerance_seconds=row["tolerance_seconds"],
        reconciled=row["reconciled"],
        quality=row["quality"],
        data_gaps=list(row["data_gaps"]),
        output_shape=cast(dict[str, JsonValue], _json_value(row["output_shape"], "output shape")),
    )


class PostgresLifecycleRepository:
    """Durable writer/reader over the migration-owned append-only tables."""

    def __init__(self, pool: asyncpg.Pool[Any]) -> None:
        self.pool = pool

    async def register_subject(self, subject: LifecycleSubject) -> LifecycleSubject:
        reproducibility = subject.reproducibility
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                """
                INSERT INTO fs2_telemetry_subjects(
                    subject_id,workload_kind,operation_id,request_id,batch_id,workload_id,attempt_id,
                    tenant_id,principal_id,api_key_id,api_key_fingerprint,model_id,model_revision,protocol,
                    trace_id,parent_span_id,accepted_at,input_shape,parameter_digest,artifact_references
                ) VALUES(
                    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18::jsonb,$19,$20::jsonb
                ) ON CONFLICT (subject_id) DO NOTHING
                """,
                subject.subject_id,
                str(subject.workload_kind),
                subject.operation_id,
                subject.request_id,
                subject.batch_id,
                subject.workload_id,
                subject.attempt_id,
                subject.tenant_id,
                subject.principal_id,
                subject.api_key_id,
                subject.api_key_fingerprint,
                subject.model_id,
                subject.model_revision,
                subject.protocol,
                subject.trace_id,
                subject.parent_span_id,
                subject.accepted_at,
                json.dumps(reproducibility.input_shape, sort_keys=True, separators=(",", ":")),
                reproducibility.parameter_digest,
                json.dumps(
                    [item.model_dump(mode="json") for item in reproducibility.artifact_references],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            row = await connection.fetchrow(
                "SELECT * FROM fs2_telemetry_subjects WHERE subject_id=$1",
                subject.subject_id,
            )
            assert row is not None
            persisted = _subject_from_row(row)
            if persisted != subject:
                raise ValueError("telemetry subject identity is already bound to different facts")
            return persisted

    async def append_correlations(self, correlations: Sequence[LifecycleCorrelation]) -> None:
        if not correlations:
            return
        async with self.pool.acquire() as connection, connection.transaction():
            for value in correlations:
                await connection.execute(
                    """
                    INSERT INTO fs2_telemetry_correlations(
                        correlation_key,subject_id,observed_at,source,attempt,cluster,namespace,queue_name,
                        kueue_workload_name,kueue_workload_uid,job_name,job_uid,pod_name,pod_uid,
                        node_name,node_uid,gpu_uuid,gpu_rank
                    ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
                    ON CONFLICT (correlation_key) DO NOTHING
                    """,
                    value.correlation_key,
                    value.subject_id,
                    value.observed_at,
                    str(value.source),
                    value.attempt,
                    value.cluster,
                    value.namespace,
                    value.queue_name,
                    value.kueue_workload_name,
                    value.kueue_workload_uid,
                    value.job_name,
                    value.job_uid,
                    value.pod_name,
                    value.pod_uid,
                    value.node_name,
                    value.node_uid,
                    value.gpu_uuid,
                    value.gpu_rank,
                )
                row = await connection.fetchrow(
                    "SELECT * FROM fs2_telemetry_correlations WHERE correlation_key=$1",
                    value.correlation_key,
                )
                if row is None or _correlation_from_row(row) != value:
                    raise ValueError("telemetry correlation key is already bound to different facts")

    async def append_signals(self, signals: Sequence[LifecycleSignal]) -> None:
        if not signals:
            return
        async with self.pool.acquire() as connection, connection.transaction():
            for value in signals:
                if value.sequence is not None:
                    raise ValueError("persisted lifecycle sequence is database-assigned")
                await connection.execute(
                    """
                    INSERT INTO fs2_lifecycle_signals(
                        event_key,subject_id,occurred_at,observed_at,source,source_resolution_seconds,
                        quality,phase,edge,clock,interval_key,attempt,gpu_count,cluster,namespace,queue_name,
                        kueue_workload_name,kueue_workload_uid,job_name,job_uid,pod_name,pod_uid,node_name,
                        node_uid,gpu_uuid,gpu_rank,detail
                    ) VALUES(
                        $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,
                        $21,$22,$23,$24,$25,$26,$27::jsonb
                    ) ON CONFLICT (event_key) DO NOTHING
                    """,
                    value.event_key,
                    value.subject_id,
                    value.occurred_at,
                    value.observed_at,
                    str(value.source),
                    value.source_resolution_seconds,
                    str(value.quality),
                    str(value.phase),
                    str(value.edge),
                    str(value.clock),
                    value.interval_key,
                    value.attempt,
                    value.gpu_count,
                    value.cluster,
                    value.namespace,
                    value.queue_name,
                    value.kueue_workload_name,
                    value.kueue_workload_uid,
                    value.job_name,
                    value.job_uid,
                    value.pod_name,
                    value.pod_uid,
                    value.node_name,
                    value.node_uid,
                    value.gpu_uuid,
                    value.gpu_rank,
                    json.dumps(value.detail, sort_keys=True, separators=(",", ":")),
                )
                row = await connection.fetchrow(
                    "SELECT * FROM fs2_lifecycle_signals WHERE event_key=$1",
                    value.event_key,
                )
                if row is None:
                    raise RuntimeError("persisted lifecycle signal is unavailable")
                persisted = _signal_from_row(row).model_copy(update={"sequence": None})
                if persisted != value:
                    raise ValueError("lifecycle event key is already bound to different facts")

    async def _load(self, subject_id: UUID) -> tuple[LifecycleSubject, list[LifecycleSignal]] | None:
        async with self.pool.acquire() as connection:
            subject_row = await connection.fetchrow(
                "SELECT * FROM fs2_telemetry_subjects WHERE subject_id=$1", subject_id
            )
            if subject_row is None:
                return None
            rows = await connection.fetch(
                "SELECT * FROM fs2_lifecycle_signals WHERE subject_id=$1 ORDER BY id", subject_id
            )
        return _subject_from_row(subject_row), [_signal_from_row(row) for row in rows]

    async def reconcile(
        self,
        subject_id: UUID,
        *,
        terminal: bool,
        outcome: str | None,
        output_shape: Mapping[str, JsonValue] | None = None,
    ) -> LifecycleRollup | None:
        loaded = await self._load(subject_id)
        if loaded is None:
            return None
        subject, signals = loaded
        rollup = reconcile_lifecycle(
            subject,
            signals,
            terminal=terminal,
            outcome=outcome,
            output_shape=output_shape,
        )
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                """
                INSERT INTO fs2_lifecycle_rollups(
                    rollup_id,subject_id,generated_at,event_watermark,events_sha256,terminal,outcome,
                    quota_reserved_gpu_seconds,scheduler_occupied_gpu_seconds,device_allocated_gpu_seconds,
                    active_gpu_seconds,occupied_idle_gpu_seconds,phase_gpu_seconds,
                    reconciliation_delta_seconds,device_scheduler_delta_seconds,tolerance_seconds,
                    reconciled,quality,data_gaps,output_shape
                ) VALUES(
                    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14,$15,$16,$17,$18,$19,$20::jsonb
                ) ON CONFLICT (subject_id,events_sha256,terminal,outcome) DO NOTHING
                """,
                rollup.rollup_id,
                rollup.subject_id,
                rollup.generated_at,
                rollup.event_watermark,
                rollup.events_sha256,
                rollup.terminal,
                rollup.outcome,
                rollup.quota_reserved_gpu_seconds,
                rollup.scheduler_occupied_gpu_seconds,
                rollup.device_allocated_gpu_seconds,
                rollup.active_gpu_seconds,
                rollup.occupied_idle_gpu_seconds,
                json.dumps(rollup.phase_gpu_seconds, sort_keys=True, separators=(",", ":")),
                rollup.reconciliation_delta_seconds,
                rollup.device_scheduler_delta_seconds,
                rollup.tolerance_seconds,
                rollup.reconciled,
                str(rollup.quality),
                rollup.data_gaps,
                json.dumps(rollup.output_shape, sort_keys=True, separators=(",", ":")),
            )
            row = await connection.fetchrow(
                """
                SELECT * FROM fs2_lifecycle_rollups
                WHERE subject_id=$1 AND events_sha256=$2 AND terminal=$3
                  AND outcome IS NOT DISTINCT FROM $4::text
                """,
                rollup.subject_id,
                rollup.events_sha256,
                rollup.terminal,
                rollup.outcome,
            )
            assert row is not None
            return _rollup_from_row(row)

    async def list_workloads(
        self,
        *,
        tenant_id: str | None,
        model_id: str | None,
        operation_id: UUID | None,
        limit: int,
    ) -> LifecycleAdminList:
        if not 1 <= limit <= 200:
            raise ValueError("lifecycle query limit is outside the bound")
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT subject.*,rollup.rollup_id,rollup.generated_at,rollup.event_watermark,
                       rollup.events_sha256,rollup.terminal,rollup.outcome,
                       rollup.quota_reserved_gpu_seconds,rollup.scheduler_occupied_gpu_seconds,
                       rollup.device_allocated_gpu_seconds,rollup.active_gpu_seconds,
                       rollup.occupied_idle_gpu_seconds,rollup.phase_gpu_seconds,
                       rollup.reconciliation_delta_seconds,rollup.device_scheduler_delta_seconds,
                       rollup.tolerance_seconds,rollup.reconciled,rollup.quality,rollup.data_gaps,
                       rollup.output_shape
                FROM fs2_telemetry_subjects subject
                LEFT JOIN fs2_reporting_lifecycle_latest rollup USING(subject_id)
                WHERE ($1::text IS NULL OR subject.tenant_id=$1)
                  AND ($2::text IS NULL OR subject.model_id=$2)
                  AND ($3::uuid IS NULL OR subject.operation_id=$3)
                ORDER BY subject.accepted_at DESC,subject.subject_id DESC
                LIMIT $4
                """,
                tenant_id,
                model_id,
                operation_id,
                limit,
            )
            total = await connection.fetchval(
                """
                SELECT count(*) FROM fs2_telemetry_subjects subject
                WHERE ($1::text IS NULL OR subject.tenant_id=$1)
                  AND ($2::text IS NULL OR subject.model_id=$2)
                  AND ($3::uuid IS NULL OR subject.operation_id=$3)
                """,
                tenant_id,
                model_id,
                operation_id,
            )
        items = []
        for row in rows:
            rollup = _rollup_from_row(row) if row["rollup_id"] is not None else None
            items.append(LifecycleWorkloadSummary(subject=_subject_from_row(row), rollup=rollup))
        return LifecycleAdminList(items=items, total=int(total or 0))

    async def get_workload(
        self,
        subject_id: UUID,
        *,
        tenant_id: str | None,
    ) -> LifecycleWorkloadDetail | None:
        async with self.pool.acquire() as connection:
            subject_row = await connection.fetchrow(
                """
                SELECT subject.*,rollup.rollup_id,rollup.generated_at,rollup.event_watermark,
                       rollup.events_sha256,rollup.terminal,rollup.outcome,
                       rollup.quota_reserved_gpu_seconds,rollup.scheduler_occupied_gpu_seconds,
                       rollup.device_allocated_gpu_seconds,rollup.active_gpu_seconds,
                       rollup.occupied_idle_gpu_seconds,rollup.phase_gpu_seconds,
                       rollup.reconciliation_delta_seconds,rollup.device_scheduler_delta_seconds,
                       rollup.tolerance_seconds,rollup.reconciled,rollup.quality,rollup.data_gaps,
                       rollup.output_shape
                FROM fs2_telemetry_subjects subject
                LEFT JOIN fs2_reporting_lifecycle_latest rollup USING(subject_id)
                WHERE subject.subject_id=$1 AND ($2::text IS NULL OR subject.tenant_id=$2)
                """,
                subject_id,
                tenant_id,
            )
            if subject_row is None:
                return None
            correlation_rows = await connection.fetch(
                """
                SELECT * FROM fs2_telemetry_correlations
                WHERE subject_id=$1 ORDER BY observed_at,correlation_key LIMIT 10001
                """,
                subject_id,
            )
            signal_rows = await connection.fetch(
                "SELECT * FROM fs2_lifecycle_signals WHERE subject_id=$1 ORDER BY id LIMIT 100001",
                subject_id,
            )
        if len(correlation_rows) > 10_000 or len(signal_rows) > 100_000:
            raise RuntimeError("lifecycle detail exceeds the bounded admin projection")
        return LifecycleWorkloadDetail(
            subject=_subject_from_row(subject_row),
            rollup=_rollup_from_row(subject_row) if subject_row["rollup_id"] is not None else None,
            correlations=[_correlation_from_row(row) for row in correlation_rows],
            signals=[_signal_from_row(row) for row in signal_rows],
        )

    async def metric_rows(self) -> list[LifecycleMetricRow]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT tenant_id,model_id,phase,quality,sum(gpu_seconds)::double precision AS seconds
                FROM fs2_reporting_gpu_phase_usage
                GROUP BY tenant_id,model_id,phase,quality
                ORDER BY tenant_id,model_id,phase,quality
                """
            )
        return [LifecycleMetricRow.model_validate(dict(row)) for row in rows]

    async def rollup_metric_rows(self) -> list[LifecycleRollupMetricRow]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT subject.tenant_id,subject.model_id,rollup.quality,rollup.reconciled,
                       count(*)::bigint AS workloads,
                       sum(rollup.quota_reserved_gpu_seconds)::double precision
                           AS quota_reserved_gpu_seconds,
                       sum(rollup.scheduler_occupied_gpu_seconds)::double precision
                           AS scheduler_occupied_gpu_seconds,
                       sum(rollup.device_allocated_gpu_seconds)::double precision
                           AS device_allocated_gpu_seconds,
                       sum(abs(rollup.reconciliation_delta_seconds))::double precision
                           AS absolute_reconciliation_delta_seconds,
                       sum(COALESCE((rollup.phase_gpu_seconds->>'unclassified')::double precision,0))
                           ::double precision AS unclassified_gpu_seconds
                FROM fs2_reporting_lifecycle_latest rollup
                JOIN fs2_telemetry_subjects subject USING(subject_id)
                WHERE rollup.terminal
                GROUP BY subject.tenant_id,subject.model_id,rollup.quality,rollup.reconciled
                ORDER BY subject.tenant_id,subject.model_id,rollup.quality,rollup.reconciled
                """
            )
        return [LifecycleRollupMetricRow.model_validate(dict(row)) for row in rows]


class NullLifecycleRepository:
    async def register_subject(self, subject: LifecycleSubject) -> LifecycleSubject:
        return subject

    async def append_correlations(self, correlations: Sequence[LifecycleCorrelation]) -> None:
        del correlations

    async def append_signals(self, signals: Sequence[LifecycleSignal]) -> None:
        del signals

    async def reconcile(
        self,
        subject_id: UUID,
        *,
        terminal: bool,
        outcome: str | None,
        output_shape: Mapping[str, JsonValue] | None = None,
    ) -> LifecycleRollup | None:
        del subject_id, terminal, outcome, output_shape
        return None

    async def list_workloads(
        self,
        *,
        tenant_id: str | None,
        model_id: str | None,
        operation_id: UUID | None,
        limit: int,
    ) -> LifecycleAdminList:
        del tenant_id, model_id, operation_id, limit
        return LifecycleAdminList(items=[], total=0)

    async def metric_rows(self) -> list[LifecycleMetricRow]:
        return []

    async def rollup_metric_rows(self) -> list[LifecycleRollupMetricRow]:
        return []

    async def get_workload(
        self,
        subject_id: UUID,
        *,
        tenant_id: str | None,
    ) -> LifecycleWorkloadDetail | None:
        del subject_id, tenant_id
        return None


class MemoryLifecycleRepository:
    """Deterministic in-memory implementation for contract and API tests."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._subjects: dict[UUID, LifecycleSubject] = {}
        self._correlations: dict[str, LifecycleCorrelation] = {}
        self._signals: dict[str, LifecycleSignal] = {}
        self._rollups: dict[UUID, list[LifecycleRollup]] = defaultdict(list)
        self._next_sequence = 1

    async def register_subject(self, subject: LifecycleSubject) -> LifecycleSubject:
        async with self._lock:
            existing = self._subjects.setdefault(subject.subject_id, subject)
            if existing != subject:
                raise ValueError("telemetry subject identity is already bound to different facts")
            return existing

    async def append_correlations(self, correlations: Sequence[LifecycleCorrelation]) -> None:
        async with self._lock:
            for value in correlations:
                existing = self._correlations.setdefault(value.correlation_key, value)
                if existing != value:
                    raise ValueError("telemetry correlation key is already bound to different facts")

    async def append_signals(self, signals: Sequence[LifecycleSignal]) -> None:
        async with self._lock:
            for value in signals:
                if value.sequence is not None:
                    raise ValueError("persisted lifecycle sequence is repository-assigned")
                existing = self._signals.get(value.event_key)
                if existing is not None:
                    if existing.model_copy(update={"sequence": None}) != value:
                        raise ValueError("lifecycle event key is already bound to different facts")
                    continue
                self._signals[value.event_key] = value.model_copy(update={"sequence": self._next_sequence})
                self._next_sequence += 1

    async def reconcile(
        self,
        subject_id: UUID,
        *,
        terminal: bool,
        outcome: str | None,
        output_shape: Mapping[str, JsonValue] | None = None,
    ) -> LifecycleRollup | None:
        async with self._lock:
            subject = self._subjects.get(subject_id)
            if subject is None:
                return None
            signals = [value for value in self._signals.values() if value.subject_id == subject_id]
            candidate = reconcile_lifecycle(
                subject,
                signals,
                terminal=terminal,
                outcome=outcome,
                output_shape=output_shape,
            )
            for existing in self._rollups[subject_id]:
                if (
                    existing.events_sha256,
                    existing.terminal,
                    existing.outcome,
                ) == (candidate.events_sha256, candidate.terminal, candidate.outcome):
                    return existing
            self._rollups[subject_id].append(candidate)
            return candidate

    def _latest(self, subject_id: UUID) -> LifecycleRollup | None:
        values = self._rollups.get(subject_id, [])
        if not values:
            return None
        return max(values, key=lambda item: (item.event_watermark, item.generated_at, str(item.rollup_id)))

    async def list_workloads(
        self,
        *,
        tenant_id: str | None,
        model_id: str | None,
        operation_id: UUID | None,
        limit: int,
    ) -> LifecycleAdminList:
        if not 1 <= limit <= 200:
            raise ValueError("lifecycle query limit is outside the bound")
        async with self._lock:
            subjects = [
                subject
                for subject in self._subjects.values()
                if (tenant_id is None or subject.tenant_id == tenant_id)
                and (model_id is None or subject.model_id == model_id)
                and (operation_id is None or subject.operation_id == operation_id)
            ]
            subjects.sort(key=lambda item: (item.accepted_at, str(item.subject_id)), reverse=True)
            return LifecycleAdminList(
                items=[
                    LifecycleWorkloadSummary(subject=subject, rollup=self._latest(subject.subject_id))
                    for subject in subjects[:limit]
                ],
                total=len(subjects),
            )

    async def get_workload(
        self,
        subject_id: UUID,
        *,
        tenant_id: str | None,
    ) -> LifecycleWorkloadDetail | None:
        async with self._lock:
            subject = self._subjects.get(subject_id)
            if subject is None or (tenant_id is not None and subject.tenant_id != tenant_id):
                return None
            return LifecycleWorkloadDetail(
                subject=subject,
                rollup=self._latest(subject_id),
                correlations=sorted(
                    (value for value in self._correlations.values() if value.subject_id == subject_id),
                    key=lambda item: (item.observed_at, item.correlation_key),
                ),
                signals=sorted(
                    (value for value in self._signals.values() if value.subject_id == subject_id),
                    key=lambda item: item.sequence or 0,
                ),
            )

    async def metric_rows(self) -> list[LifecycleMetricRow]:
        async with self._lock:
            totals: dict[tuple[str, str, str, MeasurementQuality], float] = defaultdict(float)
            for subject in self._subjects.values():
                rollup = self._latest(subject.subject_id)
                if rollup is None or not rollup.terminal:
                    continue
                for phase, seconds in rollup.phase_gpu_seconds.items():
                    totals[(subject.tenant_id, subject.model_id, phase, rollup.quality)] += seconds
            return [
                LifecycleMetricRow(
                    tenant_id=tenant,
                    model_id=model,
                    phase=phase,
                    quality=quality,
                    seconds=seconds,
                )
                for (tenant, model, phase, quality), seconds in sorted(totals.items(), key=lambda item: item[0])
            ]

    async def rollup_metric_rows(self) -> list[LifecycleRollupMetricRow]:
        async with self._lock:
            totals: dict[tuple[str, str, MeasurementQuality, bool], dict[str, float]] = defaultdict(
                lambda: defaultdict(float)
            )
            for subject in self._subjects.values():
                rollup = self._latest(subject.subject_id)
                if rollup is None or not rollup.terminal:
                    continue
                key = (subject.tenant_id, subject.model_id, rollup.quality, rollup.reconciled)
                values = totals[key]
                values["workloads"] += 1
                values["quota"] += rollup.quota_reserved_gpu_seconds
                values["scheduler"] += rollup.scheduler_occupied_gpu_seconds
                values["device"] += rollup.device_allocated_gpu_seconds
                values["delta"] += abs(rollup.reconciliation_delta_seconds)
                values["unclassified"] += rollup.phase_gpu_seconds.get(LifecyclePhase.UNCLASSIFIED.value, 0)
            return [
                LifecycleRollupMetricRow(
                    tenant_id=tenant,
                    model_id=model,
                    quality=quality,
                    reconciled=reconciled,
                    workloads=int(values["workloads"]),
                    quota_reserved_gpu_seconds=values["quota"],
                    scheduler_occupied_gpu_seconds=values["scheduler"],
                    device_allocated_gpu_seconds=values["device"],
                    absolute_reconciliation_delta_seconds=values["delta"],
                    unclassified_gpu_seconds=values["unclassified"],
                )
                for (tenant, model, quality, reconciled), values in sorted(totals.items(), key=lambda item: item[0])
            ]
