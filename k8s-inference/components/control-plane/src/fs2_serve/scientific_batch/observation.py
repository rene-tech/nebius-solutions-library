"""Controller-only diagnostics, separate from qualified runtime recipe types."""

from dataclasses import dataclass
from datetime import datetime

from .models import LifecyclePhase, WorkloadObservation


@dataclass(frozen=True, slots=True)
class DiagnosedWorkloadObservation(WorkloadObservation):
    """A pending scheduler explanation is not a terminal workload failure."""

    pending_code: str | None = None
    pods_unstarted: bool = False
    pool_unavailable_since: datetime | None = None

    def __post_init__(self) -> None:
        WorkloadObservation.__post_init__(self)
        if self.pending_code is not None and (
            LifecyclePhase.NODE_PENDING not in self.phases
            or self.pending_code not in {
                "UnschedulableInsufficientCpu", "UnschedulableInsufficientMemory",
                "UnschedulableInsufficientGpu", "UnschedulableNodeAffinity", "NodeProvisioning",
                "AdmittedPoolUnavailable", "PoolScaleUpInProgress", "PoolHealthUnknown",
            }
        ):
            raise ValueError("pending reason must be a bounded node-pending observation")
        if self.pods_unstarted and (not self.pod_uids or LifecyclePhase.NODE_PENDING not in self.phases):
            raise ValueError("unstarted proof requires observed pending Pods")
        if self.pool_unavailable_since is not None and (
            not self.pods_unstarted or self.pool_unavailable_since.tzinfo is None
        ):
            raise ValueError("pool unavailability requires timestamped unstarted proof")
