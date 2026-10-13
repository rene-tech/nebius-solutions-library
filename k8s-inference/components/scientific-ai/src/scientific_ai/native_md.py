"""Native MD seam over the existing S2 private storage/lifecycle contract.

GromacsApplication's engine-independent upload, snapshot, fencing and execution
delivery flow are reused. Native filenames, validators and execution come from
the selected existing worker. This class never admits or schedules work.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from fs2_gromacs.files import atomic_json, inventory
from jsonschema import ValidationError as NativeValidationError
from pydantic import Field, model_validator

from .contracts import Attempt, Contract, FileRecord, Identifier, RuntimeProfile, Snapshot, StorageGrant
from .delivery import publish_marker
from .gromacs import DeliveryPending, GromacsApplication
from .native_engines import EngineName, engine_for, work_complete
from .serverless import Lifecycle
from .storage import Artifacts


class NativeMDRun(Contract):
    """Protected native workflow, never ordinary S2 job metadata or public auth."""

    schema_version: Literal[1] = 1
    engine: EngineName
    job_id: Identifier
    input_bundle: FileRecord
    parameters: dict = Field(repr=False)

    @model_validator(mode="after")
    def native_contract(self) -> NativeMDRun:
        try:
            normalized = engine_for(self.engine).normalize(self.parameters)
        except (ValueError, NativeValidationError):
            raise ValueError("invalid native MD workflow; check the engine request schema") from None
        if self.input_bundle.path != "input.tar.gz":
            raise ValueError("native MD requires the existing gzip-tar input bundle")
        if len(normalized["jobs"]) != 1 or normalized["jobs"][0]["id"] != self.job_id:
            raise ValueError("this adapter executes exactly one explicit native job per S2 operation")
        if normalized["output_destination"] != "customer-bucket":
            raise ValueError("native MD artifacts must use the job-authorized customer storage path")
        if self.input_bundle.size_bytes > normalized["max_output_bytes"]:
            raise ValueError("input bundle exceeds the native execution byte budget")
        return self


class NativeMDApplication(GromacsApplication):
    """One private AMBER, NAMD or LAMMPS attempt, with no batch fan-out."""

    def __init__(
        self,
        *,
        run: NativeMDRun,
        attempt: Attempt,
        grant: StorageGrant,
        profile: RuntimeProfile,
        artifacts: Artifacts,
        lifecycle: Lifecycle,
        workspace: Path,
    ):
        grant.authorize(attempt)
        profile.authorize(attempt)
        lifecycle.require_current(attempt)
        self.engine = engine_for(run.engine)
        self.engine.authorize_shape(profile)
        self.run_spec, self.attempt, self.profile = run, attempt, profile
        self.artifacts, self.lifecycle, self.workspace = artifacts, lifecycle, workspace
        self.worker = self.engine.worker(
            run.parameters,
            job_id=run.job_id,
            attempt=attempt,
            workspace=workspace,
            checkpoint_publisher=self.publish_checkpoint,
        )
        self.checkpoint_ref: str | None = None
        self.final_checkpoint_error: str | None = None

    def _native_work_complete(self) -> bool:
        return work_complete(self.worker)

    def _validate_snapshot(self, snapshot: Snapshot) -> None:
        super()._validate_snapshot(snapshot)
        # No compatibility evidence exists for different native runtime images
        # on S2. A matching compatibility label cannot waive this check.
        if snapshot.image != self.attempt.image:
            raise ValueError("native MD checkpoint recovery requires the exact runtime image")
        self.engine.validate_state(self.worker, snapshot.state)
        if snapshot.kind == "result" and snapshot.result.get("files") != [
            record.model_dump() for record in snapshot.files
        ]:
            raise ValueError("native result and checkpoint inventories differ")

    def prepare(self, checkpoint_ref: str | None = None) -> Snapshot | None:
        self.lifecycle.require_current(self.attempt)
        self.workspace.mkdir(mode=0o700, parents=True, exist_ok=False)
        if checkpoint_ref is None:
            self.artifacts.download(self.run_spec.input_bundle, self.workspace / "input.tar.gz")
            return None
        snapshot = self.artifacts.load(checkpoint_ref)
        self._validate_snapshot(snapshot)
        self.worker.data.mkdir(mode=0o700)
        for record in snapshot.files:
            self.lifecycle.require_current(self.attempt)
            self.artifacts.download(record, self.worker.data / record.path)
        atomic_json(self.worker.meta / self.engine.state_file, snapshot.state)
        self.worker.committed_generation = snapshot.state["generation"]
        self.checkpoint_ref = checkpoint_ref
        return snapshot

    def finish_delivery(self, result: dict | None = None) -> dict:
        """Use the same S2 CPU-finalization receipt without executing native work."""
        result = result if result is not None else json.loads((self.workspace / "result.json").read_bytes())
        state_path = self.worker.meta / self.engine.state_file
        if not state_path.is_file() or state_path.is_symlink():
            raise ValueError("verified native state is required for result delivery")
        state = json.loads(state_path.read_bytes())
        if (state.get("recipe_sha256"), state.get("operation_id"), state.get("job_id")) != (
            self.worker.recipe,
            self.attempt.root_operation_id,
            self.run_spec.job_id,
        ):
            raise ValueError("retained workspace belongs to a different native execution")
        self.engine.validate_state(self.worker, state)
        self.worker.state = state
        if (
            result.get("schema") != f"fs2-serve.nebius.ai/{self.engine.name}-workflow-result/v1"
            or result.get("status") != "succeeded"
            or result.get("recipe_sha256") != self.worker.recipe
            or result.get("operation_id") != self.attempt.root_operation_id
            or result.get("job_id") != self.run_spec.job_id
            or result.get("execution_image") != self.attempt.image
            or result.get("completed_steps") != state["completed_steps"]
            or result.get("commands") != state["commands"]
            or result.get("error_code")
            or not result.get("inventory_complete")
            or not self._native_work_complete()
        ):
            raise ValueError("only verified successful native computation can enter finalization")
        actual = inventory(
            self.worker.data,
            max_bytes=self.worker.request["max_output_bytes"],
            digest_cache=self.artifacts.digests,
        )
        if actual != result.get("files"):
            raise ValueError("retained native output inventory changed before delivery")
        publish_marker(self, result)
        atomic_json(self.workspace / "delivery.json", {"compute": "succeeded", "export": "pending"})
        try:
            self.lifecycle.require_current(self.attempt)
            records = tuple(FileRecord.model_validate(item) for item in result["files"])
            self._upload(records)
            reference = self.artifacts.commit(self._snapshot(records, result=result))
            atomic_json(
                self.workspace / "delivery.json",
                {"compute": "succeeded", "export": "awaiting-ack", "reference": reference},
            )
            self.lifecycle.finalize(self.attempt, reference)
        except Exception:
            raise DeliveryPending("durable native result delivery is pending; retain recovery data") from None
        receipt = {"compute": "succeeded", "export": "committed", "reference": reference}
        atomic_json(self.workspace / "delivery.json", receipt)
        return receipt
