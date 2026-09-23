"""Shared transport identities for explicitly registered native MD workflows.

Engines own scientific inputs and restart semantics. This registry only binds
their durable checkpoint/storage envelope; adding an entry never publishes an
App or supplies runtime qualification. GROMACS keeps its existing wire formats.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Any


@dataclass(frozen=True, slots=True)
class NativeWorkflow:
    model_id: str
    engine: str
    collector_id: str
    runtime_package: str
    parameter_schema: str

    @property
    def checkpoint_schema(self) -> str:
        return f"fs2-serve.nebius.ai/{self.engine}-checkpoint/v1"

    @property
    def checkpoint_media_type(self) -> str:
        return f"application/vnd.fs2.{self.engine}-checkpoint+json"

    @property
    def state_filename(self) -> str:
        return f"{self.engine}-state.json"

    @property
    def customer_checkpoint_schema(self) -> str:
        return f"fs2-serve.nebius.ai/{self.engine}-customer-checkpoint/v1"

    @property
    def storage_endpoint(self) -> str:
        # Older GROMACS images remain compatible during an additive rollout.
        family = "gromacs" if self.engine == "gromacs" else "native"
        return f"/internal/scientific-workloads/{family}/storage"

    def normalize(self, request: object) -> dict[str, Any]:
        # The module name is operator-defined below, never supplied by a caller.
        contracts = import_module(f"{self.runtime_package}.contracts")
        if self.engine == "gromacs":
            return contracts.normalize(request, mpi=self.model_id == "gromacs-mpi")
        return contracts.normalize(request)


WORKFLOWS = (
    NativeWorkflow(
        "gromacs",
        "gromacs",
        "gromacs-workflow-v1",
        "fs2_gromacs",
        "fs2-serve.nebius.ai/gromacs-workflow-request/v1",
    ),
    NativeWorkflow(
        "gromacs-mpi",
        "gromacs",
        "gromacs-mpi-workflow-v1",
        "fs2_gromacs",
        "fs2-serve.nebius.ai/gromacs-mpi-workflow-request/v1",
    ),
    *(
        NativeWorkflow(
            engine,
            engine,
            f"{engine}-workflow-v1",
            f"fs2_{engine}",
            f"fs2-serve.nebius.ai/{engine}-workflow-request/v1",
        )
        for engine in ("lammps", "namd", "amber")
    ),
)


def workflow_for_collector(collector_id: str) -> NativeWorkflow | None:
    return next((item for item in WORKFLOWS if item.collector_id == collector_id), None)


def workflow_for_schema(parameter_schema: str) -> NativeWorkflow | None:
    return next((item for item in WORKFLOWS if item.parameter_schema == parameter_schema), None)


def workflow_for_binding(model_id: str, stage_id: str, collector_id: str) -> NativeWorkflow | None:
    value = workflow_for_collector(collector_id)
    return value if value is not None and value.model_id == model_id and stage_id == "workflow" else None
