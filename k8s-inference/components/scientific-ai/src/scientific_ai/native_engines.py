"""Small adapters around the existing AMBER, NAMD and LAMMPS workers.

The workers retain command construction, scientific checks and restart semantics.
Only their synchronous, closed-workspace checkpoint boundary is adapted here.
Runtime packages are loaded lazily so the existing GROMACS wheel still imports.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Literal

from fs2_gromacs.files import atomic_json, inventory

from .contracts import Attempt, RuntimeProfile

EngineName = Literal["amber", "namd", "lammps"]


class NativeEngineUnavailable(RuntimeError):
    """The selected native runtime was not included in this application image."""


def work_complete(worker) -> bool:
    """The native worker marks completion only after its scientific validators."""
    expected = [step["id"] for step in worker.job["steps"]]
    commands = worker.state.get("commands", [])
    return (
        worker.child is None
        and not worker.stopped
        and worker.state.get("completed_steps") == expected
        and worker.state.get("active_step") is None
        and bool(commands)
        and all(command.get("exit_code") == 0 for command in commands)
        and {command.get("step_id") for command in commands} == set(expected)
    )


class _ApplicationCheckpoints:
    """Mixin: call the unchanged worker, then synchronously await S2 publication."""

    def checkpoint(self) -> None:
        # NAMD's original checkpoint method relies on its caller for this guard.
        # Enforce it at the application boundary for all engines.
        if self.child is not None or self.stopped:
            raise RuntimeError("only a stopped coherent native workspace can be checkpointed")
        super().checkpoint()
        marker = json.loads((self.meta / "checkpoint-ready.json").read_bytes())
        self.checkpoint_publisher(marker)
        # Local generation/state files alone are never proof of durable delivery.
        self.committed_generation = marker["state"]["generation"]

    def run(self) -> dict:
        result = super().run()
        try:
            if self.child is not None:
                raise ValueError("native process is still active")
            actual = inventory(self.data, max_bytes=self.request["max_output_bytes"])
            if actual != result.get("files") or result.get("inventory_error"):
                raise ValueError("native output inventory is incomplete")
        except (OSError, ValueError):
            result.update(
                status="failed",
                error="native output inventory is incomplete",
                error_code="native-inventory-invalid",
                inventory_complete=False,
            )
        else:
            result["inventory_complete"] = True
        if result["status"] == "succeeded" and not work_complete(self):
            result.update(
                status="failed",
                error="native workflow did not complete every validated stage",
                error_code="native-work-incomplete",
            )
        result.update(
            execution_image=self.execution_image,
            native_committed_generation=self.committed_generation,
        )
        atomic_json(self.root / "result.json", result)
        return result


@dataclass(frozen=True)
class NativeEngine:
    name: EngineName
    continuation: str

    @property
    def state_file(self) -> str:
        return f"{self.name}-state.json"

    @property
    def state_schema(self) -> str:
        return f"fs2-serve.nebius.ai/{self.name}-checkpoint/v1"

    def _module(self, name: str):
        try:
            return import_module(f"fs2_{self.name}.{name}")
        except ModuleNotFoundError as error:
            if error.name == f"fs2_{self.name}" or error.name.startswith(f"fs2_{self.name}."):
                raise NativeEngineUnavailable(f"{self.name} native runtime is not packaged") from None
            raise

    def normalize(self, parameters: dict) -> dict:
        return self._module("contracts").normalize(parameters)

    def authorize_shape(self, profile: RuntimeProfile) -> None:
        # These are the source workers' existing shape bounds, not a new S2
        # qualification claim. S2 profiles still need exact-image GPU evidence.
        if set(profile.gpu_preference) - {"h100", "l40s"}:
            raise ValueError(f"{self.name} adapter supports only the existing H100/L40S shapes")

    def worker(
        self,
        parameters: dict,
        *,
        job_id: str,
        attempt: Attempt,
        workspace: Path,
        checkpoint_publisher: Callable[[dict], None],
    ):
        native = self._module("worker").Workflow

        class ApplicationWorkflow(_ApplicationCheckpoints, native):
            pass

        worker = ApplicationWorkflow(
            parameters,
            job_id=job_id,
            operation_id=attempt.root_operation_id,
            workspace=workspace,
            checkpoint_mode="local",
        )
        worker.checkpoint_publisher = checkpoint_publisher
        worker.committed_generation = 0
        worker.execution_image = attempt.image
        return worker

    def validate_state(self, worker, state: dict) -> None:
        """Reject malformed progress before restoring any private files."""
        generation, elapsed = state.get("generation"), state.get("elapsed_seconds")
        completed, commands = state.get("completed_steps"), state.get("commands")
        expected = [step["id"] for step in worker.job["steps"]]
        if (
            state.get("schema") != self.state_schema
            or type(generation) is not int
            or generation < 1
            or type(elapsed) not in (int, float)
            or not math.isfinite(elapsed)
            or elapsed < 0
            or not isinstance(completed, list)
            or completed != expected[: len(completed)]
            or not isinstance(commands, list)
            or any(not isinstance(item, dict) or item.get("exit_code") != 0 for item in commands)
            or not set(completed) <= {item.get("step_id") for item in commands}
        ):
            raise ValueError("checkpoint native state or completed-stage prefix is invalid")
        active = state.get("active_step")
        if active is not None:
            if (
                self.name == "amber"
                or not isinstance(active, dict)
                or len(completed) >= len(expected)
                or active.get("id") != expected[len(completed)]
            ):
                raise ValueError("checkpoint active stage differs from the native workflow")
            step = worker.job["steps"][len(completed)]
            if self.name == "namd":
                progress = active.get("current_step")
                restart = active.get("restart")
                valid = (
                    step["mode"] == "dynamics"
                    and type(progress) is int
                    and step["first_step"] < progress < step["first_step"] + step["steps"]
                    and isinstance(restart, dict)
                    and {"coordinates", "velocities", "cell"} <= restart.keys()
                )
            else:
                continuation = step.get("continuation", {})
                progress, segment = active.get("progress"), active.get("segment")
                valid = (
                    bool(continuation)
                    and type(progress) is int
                    and 0 <= progress < continuation["target_step"]
                    and type(segment) is int
                    and segment >= 1
                    and active.get("restart_file") == continuation["restart_file"]
                    and active.get("continuation_input") == continuation["input"]
                )
            if not valid:
                raise ValueError("checkpoint lacks a valid native continuation boundary")


AMBER = NativeEngine("amber", "replay the active stage from the preceding committed full workspace")
NAMD = NativeEngine("namd", "native coordinates/velocities/cell and Colvars state for managed dynamics")
LAMMPS = NativeEngine("lammps", "explicit continuation script with independently checked native restart step")
ENGINES = {engine.name: engine for engine in (AMBER, NAMD, LAMMPS)}


def engine_for(name: EngineName) -> NativeEngine:
    try:
        return ENGINES[name]
    except KeyError:
        raise ValueError("unknown native molecular-dynamics engine") from None
