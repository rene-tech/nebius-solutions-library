"""Per-node feasibility, separate from Kueue's aggregate pool quotas.

The model envelope is not the Pod envelope: the collector is concurrent and
init containers have Kubernetes maximum/sidecar semantics. Admission uses the
current companion request envelope; creation rechecks the actual rendered
manifest through the canonical PodSet arithmetic, including overhead and gangs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

from .models import StageResourceEnvelope, WorkloadKind
from .podset_envelope import ResourceVector, WorkloadEnvelope, envelope_from_manifest, parse_bytes, parse_cpu_millis

NODE_CAPACITY_SCHEMA = "fs2-serve.nebius.ai/accelerator-node-capacity/v1"


class PodPlacementError(ValueError):
    """A whole Pod cannot fit its declared accelerator node choices."""


class StageResourceQuantities(Protocol):
    @property
    def request_cpu(self) -> str: ...
    @property
    def request_memory(self) -> str: ...
    @property
    def request_ephemeral_storage(self) -> str: ...
    @property
    def limit_cpu(self) -> str: ...
    @property
    def limit_memory(self) -> str: ...
    @property
    def limit_ephemeral_storage(self) -> str: ...


def execution_resource_envelope(execution: StageResourceQuantities) -> StageResourceEnvelope:
    """Read exact requests from the validated immutable execution-map binding.

    Older GPU profiles leave placement resources in this operator-owned map.
    Reusing it avoids rewriting qualified profiles or inventing SKU defaults.
    """
    return StageResourceEnvelope(
        cpu_millis=parse_cpu_millis(execution.request_cpu),
        memory_bytes=parse_bytes(execution.request_memory, label="stage memory"),
        ephemeral_storage_bytes=parse_bytes(execution.request_ephemeral_storage, label="stage storage"),
        limit_cpu_millis=parse_cpu_millis(execution.limit_cpu),
        limit_memory_bytes=parse_bytes(execution.limit_memory, label="stage memory limit"),
        limit_ephemeral_storage_bytes=parse_bytes(execution.limit_ephemeral_storage, label="stage storage limit"),
    )


@dataclass(frozen=True, slots=True)
class AcceleratorNodeCapacity:
    cpu_millicores: int
    memory_mib: int
    accelerator_count: int
    ephemeral_storage_mib: int | None = None

    def fits(self, request: ResourceVector, accelerator_resource: str) -> bool:
        return (
            request.cpu_millis <= self.cpu_millicores
            and request.memory_bytes <= self.memory_mib * 1024**2
            and dict(request.accelerators).get(accelerator_resource, 0) <= self.accelerator_count
            and (
                self.ephemeral_storage_mib is None
                or request.ephemeral_storage_bytes <= self.ephemeral_storage_mib * 1024**2
            )
        )


def stage_pod_request(
    stage: StageResourceEnvelope, *, accelerator_resource: str, accelerator_count: int
) -> ResourceVector:
    """Preflight the canonical stage+collector Pod without rendering secrets.

    The materializer's 100m/256Mi is the largest current init request. The
    collector has the same request and runs alongside the scientific stage, so
    it dominates every current regular-init maximum. A renderer regression test
    binds these companion facts to the actual manifest; the creation-time check
    also prevents a future added sidecar or init from escaping the fit test.
    """
    requests = {
        "cpu": f"{stage.cpu_millis}m",
        "memory": str(stage.memory_bytes),
        "ephemeral-storage": str(stage.ephemeral_storage_bytes),
        accelerator_resource: str(accelerator_count),
    }
    companion = {"cpu": "100m", "memory": "256Mi"}
    manifest: dict[str, Any] = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": "scientific-stage", "resources": {"requests": requests, "limits": requests}},
                        {"name": "artifact-collector", "resources": {"requests": companion, "limits": companion}},
                    ],
                    "initContainers": [
                        {"name": "materialize-inputs", "resources": {"requests": companion, "limits": companion}}
                    ],
                }
            }
        }
    }
    return envelope_from_manifest(manifest, WorkloadKind.JOB).pod_sets[0].per_replica_requests


class AcceleratorPodPlacement:
    """Use declared allocatable *per node*, never aggregate quota or GPU SKU."""

    def __init__(self, contract: Mapping[str, Any]) -> None:
        raw = contract.get("accelerator_node_capacity")
        # A rolling upgrade may still read the preceding immutable contract.
        # Its absence is explicitly legacy/unverified, not guessed from quota.
        # New workloads-stage Terraform requires and publishes measured facts.
        self.legacy_unverified = raw is None
        self.capacities: dict[str, AcceleratorNodeCapacity] = {}
        if self.legacy_unverified:
            return
        if contract.get("accelerator_node_capacity_schema") != NODE_CAPACITY_SCHEMA or not isinstance(raw, Mapping):
            raise PodPlacementError("accelerator per-node capacity contract is invalid")
        for pool_id, value in raw.items():
            if not isinstance(pool_id, str) or not isinstance(value, Mapping):
                raise PodPlacementError("accelerator per-node capacity entry is invalid")
            fields: dict[str, int | None] = {}
            for field in ("cpu_millicores", "memory_mib", "accelerator_count", "ephemeral_storage_mib"):
                amount = value.get(field)
                if field == "ephemeral_storage_mib" and amount is None:
                    fields[field] = None
                elif (
                    not isinstance(amount, int)
                    or isinstance(amount, bool)
                    or amount < (0 if field == "ephemeral_storage_mib" else 1)
                ):
                    raise PodPlacementError(f"accelerator pool {pool_id} has invalid {field}")
                else:
                    fields[field] = amount
            self.capacities[pool_id] = AcceleratorNodeCapacity(
                cpu_millicores=cast(int, fields["cpu_millicores"]),
                memory_mib=cast(int, fields["memory_mib"]),
                accelerator_count=cast(int, fields["accelerator_count"]),
                ephemeral_storage_mib=fields["ephemeral_storage_mib"],
            )

    def eligible_pools(
        self, pools: tuple[str, ...], request: ResourceVector, accelerator_resource: str
    ) -> tuple[str, ...]:
        if self.legacy_unverified:
            return pools
        return tuple(
            pool_id
            for pool_id in pools
            if pool_id in self.capacities and self.capacities[pool_id].fits(request, accelerator_resource)
        )

    def validate_rendered(self, envelope: WorkloadEnvelope, pools: tuple[str, ...], accelerator_resource: str) -> None:
        """Every frozen eligible pool must fit each Pod, not the gang sum."""
        for pod_set in envelope.pod_sets:
            if self.eligible_pools(pools, pod_set.per_replica_requests, accelerator_resource) != pools:
                raise PodPlacementError(
                    f"rendered PodSet {pod_set.name} exceeds a frozen accelerator pool's per-node capacity"
                )
