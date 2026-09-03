"""Canonical PodSet resource envelope for Kueue-admitted scientific workloads.

Kueue budgets a *Workload*, and a Workload is a list of PodSets. Every quota
decision, every ``resourceUsage`` figure and every capacity claim this
controller makes therefore has to agree on one arithmetic:

per-replica request
    The **effective pod resource request** of a single Pod of that PodSet:
    the sum over the regular containers, raised to the maximum effective
    init-container request, plus Pod overhead. This mirrors Kubernetes'
    ``resourcehelper.PodRequests`` exactly, including native sidecars
    (init containers with ``restartPolicy: Always``), which are additive to
    the regular sum *and* accumulate into the init-container maxima.

PodSet count
    The number of Pods Kueue reserves for that PodSet: a batch/v1 Job's
    ``parallelism`` (bounded by ``completions``), and for a JobSet the
    ``replicas`` of the replicated Job multiplied by that Job's parallelism.
    A true-gang stage renders exactly one replicated Job whose ``replicas``
    is ``gang_size``, so ``gang_size`` enters the arithmetic here and only
    here.

aggregate request
    ``count * per-replica``, computed in exactly one place
    (:meth:`PodSetEnvelope.aggregate`). Kueue's
    ``status.admission.podSetAssignments[].resourceUsage`` is the aggregate
    for the PodSet, not the per-replica request, so a comparison that forgets
    the multiplication under-counts a gang by ``gang_size`` and one that
    applies it twice over-counts it by the same factor. Both are silent: the
    Workload is still admitted, only against the wrong quota. The frozen
    envelope carries the per-replica request, the count and the aggregate, and
    :func:`envelope_from_value` refuses to reopen a document whose aggregate
    is not exactly ``count * per-replica``.

The envelope is derived from the rendered manifest, frozen into the workload's
own annotations before creation (so it is covered by the manifest digest and
cannot drift), and recomputed from the live object on every observation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from .models import WorkloadKind

ENVELOPE_SCHEMA: Final = "fs2-serve.nebius.ai/podset-resource-envelope/v1"

CPU_RESOURCE: Final = "cpu"
MEMORY_RESOURCE: Final = "memory"
EPHEMERAL_STORAGE_RESOURCE: Final = "ephemeral-storage"
# The three core resources the controller freezes for every Pod. Kueue budgets
# them only when the deployment turns core admission on; ephemeral storage is
# always excluded from Kueue's quota because no ClusterQueue here budgets it.
# Whenever Kueue *does* report one of them it must match the frozen aggregate
# exactly, which is what makes the exclusion an operator choice rather than an
# accounting hole.
CORE_RESOURCES: Final = (CPU_RESOURCE, MEMORY_RESOURCE, EPHEMERAL_STORAGE_RESOURCE)
# Kueue names the single PodSet of a batch/v1 Job "main" and omits the name
# from that assignment; the comparison keys on this exact identity.
DEFAULT_JOB_POD_SET_NAME: Final = "main"

_MAX_POD_CPU_MILLIS: Final = 1_024_000
_MAX_POD_MEMORY_BYTES: Final = 1024**5
_MAX_POD_EPHEMERAL_BYTES: Final = 1024**5
_MAX_ACCELERATORS_PER_POD: Final = 1024
_MAX_POD_SET_COUNT: Final = 1024
_MAX_POD_SETS: Final = 16
_MAX_RESOURCE_NAME = 317

_BINARY_SUFFIXES: Final = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "Pi": 1024**5,
    "Ei": 1024**6,
}
_DECIMAL_SUFFIXES: Final = {
    "k": 1000,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
    "P": 1000**5,
    "E": 1000**6,
}


class PodSetEnvelopeError(ValueError):
    """The rendered, frozen or admitted resource envelope is not exact."""


def _decimal(raw: str, *, label: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation as error:
        raise PodSetEnvelopeError(f"{label} quantity {raw!r} is not a Kubernetes quantity") from error
    if not value.is_finite():
        raise PodSetEnvelopeError(f"{label} quantity {raw!r} is not finite")
    return value


def _quantity(value: object, *, label: str) -> Decimal:
    """Parse one Kubernetes quantity into its unsuffixed decimal magnitude."""

    if isinstance(value, bool) or not isinstance(value, str | int):
        raise PodSetEnvelopeError(f"{label} quantity is not a Kubernetes quantity")
    raw = str(value).strip()
    if not raw or len(raw) > 64:
        raise PodSetEnvelopeError(f"{label} quantity is empty or unbounded")
    for suffix, factor in _BINARY_SUFFIXES.items():
        if raw.endswith(suffix):
            return _decimal(raw[: -len(suffix)], label=label) * factor
    if raw.endswith("m"):
        return _decimal(raw[:-1], label=label) / 1000
    for suffix, factor in _DECIMAL_SUFFIXES.items():
        if raw.endswith(suffix):
            return _decimal(raw[: -len(suffix)], label=label) * factor
    return _decimal(raw, label=label)


def parse_cpu_millis(value: object, *, label: str = "cpu") -> int:
    """Parse a CPU quantity into exact milli-cores."""

    millis = _quantity(value, label=label) * 1000
    if millis != millis.to_integral_value() or millis < 0:
        raise PodSetEnvelopeError(f"{label} quantity is not an exact non-negative milli-core count")
    return int(millis)


def parse_bytes(value: object, *, label: str) -> int:
    """Parse a memory or storage quantity into exact bytes."""

    quantity = _quantity(value, label=label)
    if quantity != quantity.to_integral_value() or quantity < 0:
        raise PodSetEnvelopeError(f"{label} quantity is not an exact non-negative byte count")
    return int(quantity)


def parse_count(value: object, *, label: str) -> int:
    """Parse a whole-unit quantity (an accelerator count) into an integer."""

    quantity = _quantity(value, label=label)
    if quantity != quantity.to_integral_value() or quantity < 0:
        raise PodSetEnvelopeError(f"{label} quantity is not an exact non-negative count")
    return int(quantity)


def parse_resource_quantity(resource: str, value: object, *, label: str) -> int:
    """Normalize one resource quantity into this module's canonical unit."""

    if resource == CPU_RESOURCE:
        return parse_cpu_millis(value, label=f"{label} cpu")
    if resource in {MEMORY_RESOURCE, EPHEMERAL_STORAGE_RESOURCE}:
        return parse_bytes(value, label=f"{label} {resource}")
    return parse_count(value, label=f"{label} {resource}")


def _resource_name(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_RESOURCE_NAME:
        raise PodSetEnvelopeError(f"{label} is not a bounded resource name")
    return value


@dataclass(frozen=True, slots=True)
class ResourceVector:
    """One exact CPU/memory/ephemeral-storage/accelerator request or limit.

    CPU is milli-cores, memory and ephemeral storage are bytes, and every other
    (extended) resource is a whole-unit count keyed by its exact resource name.
    """

    cpu_millis: int = 0
    memory_bytes: int = 0
    ephemeral_storage_bytes: int = 0
    accelerators: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        for value, label in (
            (self.cpu_millis, "cpu millis"),
            (self.memory_bytes, "memory bytes"),
            (self.ephemeral_storage_bytes, "ephemeral storage bytes"),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise PodSetEnvelopeError(f"{label} must be a non-negative integer")
        names = [name for name, _ in self.accelerators]
        if len(set(names)) != len(names) or list(names) != sorted(names):
            raise PodSetEnvelopeError("accelerator resources must be unique and canonically ordered")
        for name, count in self.accelerators:
            _resource_name(name, label="accelerator resource")
            if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                raise PodSetEnvelopeError("accelerator counts must be positive integers")

    @classmethod
    def of(
        cls,
        *,
        cpu_millis: int = 0,
        memory_bytes: int = 0,
        ephemeral_storage_bytes: int = 0,
        accelerators: Mapping[str, int] | Iterable[tuple[str, int]] = (),
    ) -> ResourceVector:
        items = accelerators.items() if isinstance(accelerators, Mapping) else accelerators
        return cls(
            cpu_millis=cpu_millis,
            memory_bytes=memory_bytes,
            ephemeral_storage_bytes=ephemeral_storage_bytes,
            accelerators=tuple(sorted((name, count) for name, count in items if count)),
        )

    def accelerator(self, resource: str) -> int:
        return dict(self.accelerators).get(resource, 0)

    def resource(self, resource: str) -> int | None:
        """Return the requested quantity, or ``None`` when nothing is requested."""

        if resource == CPU_RESOURCE:
            return self.cpu_millis or None
        if resource == MEMORY_RESOURCE:
            return self.memory_bytes or None
        if resource == EPHEMERAL_STORAGE_RESOURCE:
            return self.ephemeral_storage_bytes or None
        return dict(self.accelerators).get(resource)

    @property
    def resources(self) -> tuple[str, ...]:
        return tuple(
            [name for name in CORE_RESOURCES if self.resource(name) is not None]
            + [name for name, _ in self.accelerators]
        )

    def __add__(self, other: ResourceVector) -> ResourceVector:
        merged = dict(self.accelerators)
        for name, count in other.accelerators:
            merged[name] = merged.get(name, 0) + count
        return ResourceVector.of(
            cpu_millis=self.cpu_millis + other.cpu_millis,
            memory_bytes=self.memory_bytes + other.memory_bytes,
            ephemeral_storage_bytes=self.ephemeral_storage_bytes + other.ephemeral_storage_bytes,
            accelerators=merged,
        )

    def raised_to(self, other: ResourceVector) -> ResourceVector:
        """Return the per-resource maximum of both vectors."""

        merged = dict(self.accelerators)
        for name, count in other.accelerators:
            merged[name] = max(merged.get(name, 0), count)
        return ResourceVector.of(
            cpu_millis=max(self.cpu_millis, other.cpu_millis),
            memory_bytes=max(self.memory_bytes, other.memory_bytes),
            ephemeral_storage_bytes=max(self.ephemeral_storage_bytes, other.ephemeral_storage_bytes),
            accelerators=merged,
        )

    def scaled(self, factor: int) -> ResourceVector:
        """Multiply every resource by ``factor`` exactly once."""

        if not isinstance(factor, int) or isinstance(factor, bool) or factor < 1:
            raise PodSetEnvelopeError("a PodSet replica count must be a positive integer")
        return ResourceVector.of(
            cpu_millis=self.cpu_millis * factor,
            memory_bytes=self.memory_bytes * factor,
            ephemeral_storage_bytes=self.ephemeral_storage_bytes * factor,
            accelerators={name: count * factor for name, count in self.accelerators},
        )

    def assert_pod_bounds(self) -> None:
        if self.cpu_millis > _MAX_POD_CPU_MILLIS:
            raise PodSetEnvelopeError("per-Pod CPU request exceeds the controller bound")
        if self.memory_bytes > _MAX_POD_MEMORY_BYTES:
            raise PodSetEnvelopeError("per-Pod memory request exceeds the controller bound")
        if self.ephemeral_storage_bytes > _MAX_POD_EPHEMERAL_BYTES:
            raise PodSetEnvelopeError("per-Pod ephemeral storage request exceeds the controller bound")
        for _, count in self.accelerators:
            if count > _MAX_ACCELERATORS_PER_POD:
                raise PodSetEnvelopeError("per-Pod accelerator request exceeds the controller bound")

    def to_value(self) -> dict[str, object]:
        return {
            "cpu_millis": self.cpu_millis,
            "memory_bytes": self.memory_bytes,
            "ephemeral_storage_bytes": self.ephemeral_storage_bytes,
            "accelerators": {name: count for name, count in self.accelerators},
        }

    @classmethod
    def from_value(cls, value: object, *, label: str) -> ResourceVector:
        body = _mapping(value, label=label)
        if set(body) != {"cpu_millis", "memory_bytes", "ephemeral_storage_bytes", "accelerators"}:
            raise PodSetEnvelopeError(f"{label} has an unexpected resource vector shape")
        accelerators = _mapping(body["accelerators"], label=f"{label} accelerators")
        return cls.of(
            cpu_millis=_integer(body["cpu_millis"], label=f"{label} cpu millis"),
            memory_bytes=_integer(body["memory_bytes"], label=f"{label} memory bytes"),
            ephemeral_storage_bytes=_integer(body["ephemeral_storage_bytes"], label=f"{label} ephemeral bytes"),
            accelerators={
                _resource_name(name, label=f"{label} accelerator"): _integer(count, label=f"{label} accelerator count")
                for name, count in accelerators.items()
            },
        )


ZERO_RESOURCES: Final = ResourceVector()


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise PodSetEnvelopeError(f"{label} is not an object")
    return {str(key): item for key, item in value.items()}


def _sequence(value: object, *, label: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise PodSetEnvelopeError(f"{label} is not a list")
    return value


def _integer(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise PodSetEnvelopeError(f"{label} is not an integer")
    return value


def _container_vector(container: Mapping[str, object], *, kind: str, label: str) -> ResourceVector:
    """Return one container's exact request or limit vector.

    Kubernetes defaults an omitted *request* to the container's limit for that
    same resource, per resource and not per block, so the effective request of
    a container that only declares limits is those limits. An omitted limit
    stays absent; it is not borrowed from the request.
    """

    resources = container.get("resources")
    if resources is None:
        return ZERO_RESOURCES
    declared = _mapping(resources, label=f"{label} resources")
    selected = _mapping(declared.get(kind, {}), label=f"{label} {kind}")
    fallback: Mapping[str, object] = {}
    if kind == "requests":
        fallback = _mapping(declared.get("limits", {}), label=f"{label} limits")
    names = {*selected, *fallback}
    quantities = {name: selected.get(name, fallback.get(name)) for name in names}
    accelerators: dict[str, int] = {}
    for name, quantity in quantities.items():
        if name in CORE_RESOURCES:
            continue
        accelerators[_resource_name(name, label=f"{label} resource")] = parse_count(quantity, label=f"{label} {name}")
    return ResourceVector.of(
        cpu_millis=(
            0 if quantities.get(CPU_RESOURCE) is None else parse_cpu_millis(quantities[CPU_RESOURCE], label=label)
        ),
        memory_bytes=(
            0
            if quantities.get(MEMORY_RESOURCE) is None
            else parse_bytes(quantities[MEMORY_RESOURCE], label=f"{label} memory")
        ),
        ephemeral_storage_bytes=(
            0
            if quantities.get(EPHEMERAL_STORAGE_RESOURCE) is None
            else parse_bytes(quantities[EPHEMERAL_STORAGE_RESOURCE], label=f"{label} ephemeral storage")
        ),
        accelerators=accelerators,
    )


def _effective_pod_vector(pod_spec: Mapping[str, object], *, kind: str, label: str) -> ResourceVector:
    """Compute the effective per-Pod request or limit of one Pod template.

    This is Kubernetes' own ``resourcehelper.PodRequests`` arithmetic: the sum
    over the regular containers, plus every native sidecar, raised to the
    maximum cumulative init-container request, plus Pod overhead. Kueue reads
    the same effective value when it builds the Workload's PodSet, so any other
    reading of "the Pod's resources" would disagree with the quota it charges.
    """

    total = ZERO_RESOURCES
    containers = _sequence(pod_spec.get("containers", []), label=f"{label} containers")
    if not containers:
        raise PodSetEnvelopeError(f"{label} declares no container")
    for index, raw in enumerate(containers):
        total = total + _container_vector(
            _mapping(raw, label=f"{label} container {index}"), kind=kind, label=f"{label} container {index}"
        )
    sidecars = ZERO_RESOURCES
    init_maximum = ZERO_RESOURCES
    for index, raw in enumerate(_sequence(pod_spec.get("initContainers", []), label=f"{label} initContainers")):
        container = _mapping(raw, label=f"{label} init container {index}")
        vector = _container_vector(container, kind=kind, label=f"{label} init container {index}")
        if container.get("restartPolicy") == "Always":
            # A native sidecar runs for the whole Pod lifetime, so it is
            # additive to the regular containers and to every later maximum.
            total = total + vector
            sidecars = sidecars + vector
            init_maximum = init_maximum.raised_to(sidecars)
            continue
        init_maximum = init_maximum.raised_to(sidecars + vector)
    effective = total.raised_to(init_maximum)
    overhead = pod_spec.get("overhead")
    if overhead is not None:
        effective = effective + _container_vector({"resources": {kind: overhead}}, kind=kind, label=f"{label} overhead")
    return effective


def _pod_count(job_spec: Mapping[str, object], *, replicas: int, label: str) -> int:
    """Return the Pod count Kueue reserves for one Job template.

    Kueue's batch/v1 Job and JobSet integrations both use ``parallelism``,
    bounded below by ``completions``, and the JobSet integration multiplies it
    by the replicated Job's ``replicas``. ``replicas`` is where a true-gang
    stage's ``gang_size`` enters, exactly once.
    """

    parallelism = job_spec.get("parallelism")
    completions = job_spec.get("completions")
    count = 1 if parallelism is None else _integer(parallelism, label=f"{label} parallelism")
    if completions is not None:
        bounded = _integer(completions, label=f"{label} completions")
        count = min(count, bounded)
    if count < 1:
        raise PodSetEnvelopeError(f"{label} reserves no Pod")
    return replicas * count


@dataclass(frozen=True, slots=True)
class PodSetEnvelope:
    """One Kueue PodSet's frozen per-replica and derived aggregate envelope.

    Only the per-replica request, the per-replica limit and the replica count
    are stored. The aggregate is a property, so the replica count is applied in
    exactly one place and can be neither forgotten nor applied twice.
    """

    name: str
    count: int
    per_replica_requests: ResourceVector
    per_replica_limits: ResourceVector

    def __post_init__(self) -> None:
        if not self.name or len(self.name) > 253:
            raise PodSetEnvelopeError("a PodSet name must be non-empty and bounded")
        if not isinstance(self.count, int) or isinstance(self.count, bool):
            raise PodSetEnvelopeError("a PodSet replica count must be an integer")
        if not 1 <= self.count <= _MAX_POD_SET_COUNT:
            raise PodSetEnvelopeError("a PodSet replica count is outside the controller bound")
        self.per_replica_requests.assert_pod_bounds()
        self.per_replica_limits.assert_pod_bounds()
        for request, limit, label in (
            (self.per_replica_requests.cpu_millis, self.per_replica_limits.cpu_millis, "cpu"),
            (self.per_replica_requests.memory_bytes, self.per_replica_limits.memory_bytes, "memory"),
            (
                self.per_replica_requests.ephemeral_storage_bytes,
                self.per_replica_limits.ephemeral_storage_bytes,
                "ephemeral storage",
            ),
        ):
            if limit and limit < request:
                raise PodSetEnvelopeError(f"a PodSet {label} limit cannot be smaller than its request")
        for name, count in self.per_replica_requests.accelerators:
            if self.per_replica_limits.accelerator(name) != count:
                raise PodSetEnvelopeError("an accelerator request and limit must be identical")

    @property
    def aggregate_requests(self) -> ResourceVector:
        """``count * per-replica`` requests: the figure Kueue budgets."""

        return self.per_replica_requests.scaled(self.count)

    @property
    def aggregate_limits(self) -> ResourceVector:
        return self.per_replica_limits.scaled(self.count)

    def to_value(self) -> dict[str, object]:
        return {
            "name": self.name,
            "count": self.count,
            "per_replica": {
                "requests": self.per_replica_requests.to_value(),
                "limits": self.per_replica_limits.to_value(),
            },
            "aggregate": {
                "requests": self.aggregate_requests.to_value(),
                "limits": self.aggregate_limits.to_value(),
            },
        }

    @classmethod
    def from_value(cls, value: object, *, label: str) -> PodSetEnvelope:
        body = _mapping(value, label=label)
        if set(body) != {"name", "count", "per_replica", "aggregate"}:
            raise PodSetEnvelopeError(f"{label} has an unexpected PodSet envelope shape")
        name = body["name"]
        if not isinstance(name, str):
            raise PodSetEnvelopeError(f"{label} PodSet name is not a string")
        per_replica = _mapping(body["per_replica"], label=f"{label} per-replica")
        aggregate = _mapping(body["aggregate"], label=f"{label} aggregate")
        if set(per_replica) != {"requests", "limits"} or set(aggregate) != {"requests", "limits"}:
            raise PodSetEnvelopeError(f"{label} PodSet envelope omits requests or limits")
        envelope = cls(
            name=name,
            count=_integer(body["count"], label=f"{label} PodSet count"),
            per_replica_requests=ResourceVector.from_value(
                per_replica["requests"], label=f"{label} per-replica requests"
            ),
            per_replica_limits=ResourceVector.from_value(per_replica["limits"], label=f"{label} per-replica limits"),
        )
        # A frozen document that multiplied the replica count twice, or not at
        # all, is refused here rather than compared against Kueue later.
        for supplied, derived, kind in (
            (aggregate["requests"], envelope.aggregate_requests, "requests"),
            (aggregate["limits"], envelope.aggregate_limits, "limits"),
        ):
            if ResourceVector.from_value(supplied, label=f"{label} aggregate {kind}") != derived:
                raise PodSetEnvelopeError(
                    f"{label} aggregate {kind} is not exactly {envelope.count} times its per-replica {kind}"
                )
        return envelope


@dataclass(frozen=True, slots=True)
class WorkloadEnvelope:
    """The frozen resource envelope of every PodSet in one Kueue Workload."""

    kind: WorkloadKind
    pod_sets: tuple[PodSetEnvelope, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.kind, WorkloadKind):
            raise PodSetEnvelopeError("a workload envelope requires a supported workload kind")
        if not self.pod_sets or len(self.pod_sets) > _MAX_POD_SETS:
            raise PodSetEnvelopeError("a workload envelope must carry between one and 16 PodSets")
        names = [item.name for item in self.pod_sets]
        if len(set(names)) != len(names):
            raise PodSetEnvelopeError("PodSet names must be unique within one workload")
        if self.kind is WorkloadKind.JOB and len(self.pod_sets) != 1:
            raise PodSetEnvelopeError("a batch/v1 Job has exactly one PodSet")

    def pod_set(self, name: str) -> PodSetEnvelope:
        for item in self.pod_sets:
            if item.name == name:
                return item
        raise PodSetEnvelopeError(f"the frozen envelope has no PodSet {name!r}")

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.pod_sets)

    @property
    def pod_count(self) -> int:
        return sum(item.count for item in self.pod_sets)

    @property
    def aggregate_requests(self) -> ResourceVector:
        total = ZERO_RESOURCES
        for item in self.pod_sets:
            total = total + item.aggregate_requests
        return total

    def accelerator_pod_sets(self, resource: str) -> tuple[PodSetEnvelope, ...]:
        return tuple(item for item in self.pod_sets if item.per_replica_requests.accelerator(resource))

    def to_value(self) -> dict[str, object]:
        return {
            "schema": ENVELOPE_SCHEMA,
            "kind": str(self.kind),
            "pod_sets": [item.to_value() for item in self.pod_sets],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_value(), sort_keys=True, separators=(",", ":"), allow_nan=False)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()


def envelope_from_value(value: object, *, kind: WorkloadKind | None = None) -> WorkloadEnvelope:
    """Reopen a frozen envelope document, re-deriving every aggregate."""

    body = _mapping(value, label="PodSet resource envelope")
    if body.get("schema") != ENVELOPE_SCHEMA:
        raise PodSetEnvelopeError("PodSet resource envelope schema is not supported")
    if set(body) != {"schema", "kind", "pod_sets"}:
        raise PodSetEnvelopeError("PodSet resource envelope has an unexpected shape")
    raw_kind = body["kind"]
    if not isinstance(raw_kind, str):
        raise PodSetEnvelopeError("PodSet resource envelope kind is not a string")
    try:
        parsed_kind = WorkloadKind(raw_kind)
    except ValueError as error:
        raise PodSetEnvelopeError("PodSet resource envelope kind is not a supported workload kind") from error
    if kind is not None and parsed_kind is not kind:
        raise PodSetEnvelopeError("PodSet resource envelope kind differs from the observed workload")
    pod_sets = _sequence(body["pod_sets"], label="PodSet resource envelope PodSets")
    return WorkloadEnvelope(
        kind=parsed_kind,
        pod_sets=tuple(
            PodSetEnvelope.from_value(item, label=f"PodSet resource envelope PodSet {index}")
            for index, item in enumerate(pod_sets)
        ),
    )


def envelope_from_json(raw: object, *, kind: WorkloadKind | None = None) -> WorkloadEnvelope:
    if not isinstance(raw, str) or not raw or len(raw) > 32_768:
        raise PodSetEnvelopeError("the frozen PodSet resource envelope document is absent or unbounded")
    try:
        value = json.loads(raw)
    except ValueError as error:
        raise PodSetEnvelopeError("the frozen PodSet resource envelope document is not JSON") from error
    return envelope_from_value(value, kind=kind)


def envelope_from_manifest(manifest: Mapping[str, object], kind: WorkloadKind) -> WorkloadEnvelope:
    """Derive the canonical envelope from a rendered Job or JobSet manifest."""

    spec = _mapping(manifest.get("spec"), label="workload spec")
    if kind is WorkloadKind.JOB:
        template = _mapping(spec.get("template"), label="Job Pod template")
        pod_spec = _mapping(template.get("spec"), label="Job Pod spec")
        return WorkloadEnvelope(
            kind=kind,
            pod_sets=(
                PodSetEnvelope(
                    # Kueue's batch/v1 Job integration always names the single
                    # PodSet "main"; the comparison keys on that exact name.
                    name=DEFAULT_JOB_POD_SET_NAME,
                    count=_pod_count(spec, replicas=1, label="Job"),
                    per_replica_requests=_effective_pod_vector(pod_spec, kind="requests", label="Job Pod"),
                    per_replica_limits=_effective_pod_vector(pod_spec, kind="limits", label="Job Pod"),
                ),
            ),
        )
    replicated = _sequence(spec.get("replicatedJobs"), label="JobSet replicatedJobs")
    if not replicated:
        raise PodSetEnvelopeError("a JobSet must declare at least one replicated Job")
    pod_sets: list[PodSetEnvelope] = []
    for index, raw in enumerate(replicated):
        job = _mapping(raw, label=f"JobSet replicated job {index}")
        name = job.get("name")
        if not isinstance(name, str) or not name:
            raise PodSetEnvelopeError("a JobSet replicated job requires a name")
        replicas = job.get("replicas")
        replica_count = 1 if replicas is None else _integer(replicas, label=f"replicated job {name} replicas")
        if replica_count < 1:
            raise PodSetEnvelopeError(f"replicated job {name!r} declares no replica")
        job_template = _mapping(job.get("template"), label=f"replicated job {name} template")
        job_spec = _mapping(job_template.get("spec"), label=f"replicated job {name} spec")
        pod_template = _mapping(job_spec.get("template"), label=f"replicated job {name} Pod template")
        pod_spec = _mapping(pod_template.get("spec"), label=f"replicated job {name} Pod spec")
        pod_sets.append(
            PodSetEnvelope(
                name=name,
                # replicas * parallelism, so gang_size is applied exactly once.
                count=_pod_count(job_spec, replicas=replica_count, label=f"replicated job {name}"),
                per_replica_requests=_effective_pod_vector(
                    pod_spec, kind="requests", label=f"replicated job {name} Pod"
                ),
                per_replica_limits=_effective_pod_vector(pod_spec, kind="limits", label=f"replicated job {name} Pod"),
            )
        )
    return WorkloadEnvelope(kind=kind, pod_sets=tuple(pod_sets))


@dataclass(frozen=True, slots=True)
class KueuePodSetUsage:
    """One ``status.admission.podSetAssignments`` entry, in canonical units."""

    name: str
    usage: tuple[tuple[str, int], ...]
    count: int | None = None

    @classmethod
    def from_admission(cls, value: object, *, index: int) -> KueuePodSetUsage:
        body = _mapping(value, label=f"Kueue PodSet assignment {index}")
        raw_name = body.get("name")
        # Kueue omits the name only for the single default PodSet of a Job.
        name = DEFAULT_JOB_POD_SET_NAME if raw_name is None else raw_name
        if not isinstance(name, str) or not name:
            raise PodSetEnvelopeError("a Kueue PodSet assignment name is invalid")
        raw_usage = _mapping(body.get("resourceUsage", {}), label=f"Kueue PodSet {name} resourceUsage")
        raw_count = body.get("count")
        return cls(
            name=name,
            usage=tuple(
                sorted(
                    (
                        _resource_name(resource, label=f"Kueue PodSet {name} resource"),
                        parse_resource_quantity(resource, quantity, label=f"Kueue PodSet {name}"),
                    )
                    for resource, quantity in raw_usage.items()
                )
            ),
            count=None if raw_count is None else _integer(raw_count, label=f"Kueue PodSet {name} count"),
        )

    def quantity(self, resource: str) -> int | None:
        return dict(self.usage).get(resource)


@dataclass(frozen=True, slots=True)
class KueueUsageComparison:
    """The exact per-replica and aggregate result of one admitted Workload."""

    accelerator_resource: str | None
    accelerator_per_replica: int
    accelerator_aggregate: int
    cpu_millis_per_replica: int
    memory_bytes_per_replica: int
    aggregate: ResourceVector
    compared: tuple[tuple[str, tuple[str, ...]], ...]


def compare_kueue_usage(
    envelope: WorkloadEnvelope,
    assignments: Iterable[object],
    *,
    accelerator_resource: str | None,
) -> KueueUsageComparison:
    """Compare Kueue's admitted ``resourceUsage`` with the frozen envelope.

    Every PodSet of the frozen envelope must appear exactly once, and every
    core or accelerator resource Kueue reports for it must equal that PodSet's
    aggregate (``count * per-replica``) to the byte, milli-core and device.
    Extended resources the envelope does not request (an RDMA device a
    ClusterQueue does not budget, for example) are ignored, and a core
    resource Kueue does not report is one the deployment excluded from quota.
    """

    usages = [KueuePodSetUsage.from_admission(item, index=index) for index, item in enumerate(assignments)]
    if not usages:
        raise PodSetEnvelopeError("Kueue admission has no PodSet assignment")
    if len({item.name for item in usages}) != len(usages):
        raise PodSetEnvelopeError("Kueue admitted the same PodSet twice")
    assigned = {item.name for item in usages}
    missing = tuple(name for name in envelope.names if name not in assigned)
    if missing:
        raise PodSetEnvelopeError(f"Kueue admission omits the frozen PodSet {missing[0]!r}")
    aggregate = ZERO_RESOURCES
    accelerator_aggregate = 0
    accelerator_per_replica = 0
    compared: list[tuple[str, tuple[str, ...]]] = []
    for item in usages:
        pod_set = envelope.pod_set(item.name)
        if item.count is not None and item.count != pod_set.count:
            # Partial admission would legitimately reduce this count, and this
            # controller never opts a scientific PodSet into it.
            raise PodSetEnvelopeError(
                f"Kueue admitted {item.count} Pods for PodSet {item.name!r} instead of the frozen {pod_set.count}"
            )
        matched: list[str] = []
        for resource, quantity in item.usage:
            expected = pod_set.aggregate_requests.resource(resource)
            if expected is None:
                if resource in CORE_RESOURCES or resource == accelerator_resource:
                    raise PodSetEnvelopeError(
                        f"Kueue charged PodSet {item.name!r} for {resource} that its Pods do not request"
                    )
                # An extended resource outside every ClusterQueue resource
                # group; the frozen envelope deliberately does not claim it.
                continue
            if quantity != expected:
                raise PodSetEnvelopeError(
                    f"Kueue admitted {resource}={quantity} for PodSet {item.name!r} instead of the frozen "
                    f"{expected} ({pod_set.count} x {pod_set.per_replica_requests.resource(resource)})"
                )
            matched.append(resource)
        for resource in pod_set.per_replica_requests.resources:
            if resource == accelerator_resource and item.quantity(resource) is None:
                raise PodSetEnvelopeError(
                    f"Kueue admitted PodSet {item.name!r} without the frozen accelerator {resource}"
                )
        compared.append((item.name, tuple(matched)))
        aggregate = aggregate + pod_set.aggregate_requests
        if accelerator_resource is not None:
            per_replica = pod_set.per_replica_requests.accelerator(accelerator_resource)
            if per_replica:
                if accelerator_per_replica and per_replica != accelerator_per_replica:
                    raise PodSetEnvelopeError("the frozen envelope requests different accelerator counts per PodSet")
                accelerator_per_replica = per_replica
                accelerator_aggregate += per_replica * pod_set.count
    primary = _primary_pod_set(envelope, accelerator_resource)
    return KueueUsageComparison(
        accelerator_resource=accelerator_resource,
        accelerator_per_replica=accelerator_per_replica,
        accelerator_aggregate=accelerator_aggregate,
        cpu_millis_per_replica=primary.per_replica_requests.cpu_millis,
        memory_bytes_per_replica=primary.per_replica_requests.memory_bytes,
        aggregate=aggregate,
        compared=tuple(compared),
    )


def _primary_pod_set(envelope: WorkloadEnvelope, accelerator_resource: str | None) -> PodSetEnvelope:
    """Return the PodSet whose per-replica core request the attempt records.

    A GPU stage's identity is the accelerator-bearing PodSet. A CPU stage is a
    single-PodSet Job, so any other shape has no unambiguous per-replica core
    request and must not be summarized into one.
    """

    if accelerator_resource is None:
        if len(envelope.pod_sets) != 1:
            raise PodSetEnvelopeError("a CPU workload envelope must have exactly one PodSet")
        return envelope.pod_sets[0]
    accelerator_pod_sets = envelope.accelerator_pod_sets(accelerator_resource)
    if not accelerator_pod_sets:
        raise PodSetEnvelopeError(f"the frozen envelope requests no {accelerator_resource}")
    if len({item.per_replica_requests for item in accelerator_pod_sets}) != 1:
        raise PodSetEnvelopeError("accelerator PodSets must share one per-replica resource envelope")
    return accelerator_pod_sets[0]
