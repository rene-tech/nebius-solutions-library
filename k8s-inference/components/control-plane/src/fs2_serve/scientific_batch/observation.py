"""Controller-only diagnostics, separate from qualified runtime recipe types."""

from dataclasses import dataclass

from .models import LifecyclePhase, WorkloadObservation


@dataclass(frozen=True, slots=True)
class DiagnosedWorkloadObservation(WorkloadObservation):
    """A pending scheduler explanation is not a terminal workload failure."""

    pending_code: str | None = None

    def __post_init__(self) -> None:
        WorkloadObservation.__post_init__(self)
        if self.pending_code is not None and (
            LifecyclePhase.NODE_PENDING not in self.phases
            or self.pending_code not in {
                "UnschedulableInsufficientCpu", "UnschedulableInsufficientMemory",
                "UnschedulableInsufficientGpu", "UnschedulableNodeAffinity", "NodeProvisioning",
            }
        ):
            raise ValueError("pending reason must be a bounded node-pending observation")
