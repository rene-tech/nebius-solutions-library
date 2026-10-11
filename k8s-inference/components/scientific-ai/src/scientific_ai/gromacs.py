"""GROMACS application I/O around the existing native workflow implementation.

One object is one execution attempt, never shared across requests. Native
checkpoints are portable only between explicitly qualified runtime builds.
Serverless owns replacement decisions, active epochs and durable operation state.
"""

from __future__ import annotations

import json
from pathlib import Path

from fs2_gromacs.files import atomic_json
from fs2_gromacs.worker import Workflow

from .contracts import Attempt, FileRecord, GromacsRun, RuntimeProfile, Snapshot, StorageGrant
from .serverless import Lifecycle
from .storage import Artifacts


class DeliveryPending(RuntimeError):
    """Keep approved scratch: compute succeeded but durable delivery is incomplete."""


class GromacsApplication:
    def __init__(
        self,
        *,
        run: GromacsRun,
        attempt: Attempt,
        grant: StorageGrant,
        profile: RuntimeProfile,
        artifacts: Artifacts,
        lifecycle: Lifecycle,
        workspace: Path,
        gmx: str,
    ):
        grant.authorize(attempt)
        profile.authorize(attempt)
        lifecycle.require_current(attempt)
        self.run_spec, self.attempt, self.profile = run, attempt, profile
        self.artifacts, self.lifecycle, self.workspace = artifacts, lifecycle, workspace
        self.worker = Workflow(
            run.parameters,
            job_id=run.job_id,
            operation_id=attempt.root_operation_id,
            workspace=workspace,
            gmx=gmx,
            engine_id=attempt.image,
            checkpoint_compatibility_id=profile.checkpoint_compatibility_id,
            checkpoint_mode="application",
            checkpoint_publisher=self.publish_checkpoint,
        )
        self.checkpoint_ref: str | None = None

    def _snapshot(self, records: tuple[FileRecord, ...], *, result: dict | None = None) -> Snapshot:
        return Snapshot(
            kind="result" if result is not None else "checkpoint",
            owner=self.attempt.owner,
            root_operation_id=self.attempt.root_operation_id,
            attempt_id=self.attempt.attempt_id,
            epoch=self.attempt.epoch,
            runtime_revision=self.profile.revision,
            compatibility_id=self.profile.checkpoint_compatibility_id,
            image=self.attempt.image,
            job_id=self.run_spec.job_id,
            input_sha256=self.run_spec.input_bundle.sha256,
            state=self.worker.state,
            files=records,
            result=result,
        )

    def _upload(self, records: tuple[FileRecord, ...]) -> None:
        if sum(record.size_bytes for record in records) > self.worker.request["max_output_bytes"]:
            raise ValueError("workspace exceeds the execution byte budget")
        root = self.worker.data.resolve()
        for record in records:
            path = root / record.path
            if root not in path.resolve().parents or any(
                parent.is_symlink() for parent in path.parents if parent != root and root in parent.parents
            ):
                raise ValueError("artifact leaves its private execution workspace")
            self.lifecycle.require_current(self.attempt)
            self.artifacts.upload(path, record)

    def publish_checkpoint(self, marker: dict) -> None:
        records = tuple(FileRecord.model_validate(item) for item in marker["files"])
        self._upload(records)
        reference = self.artifacts.commit(self._snapshot(records))
        # Atomic epoch check + durable pointer publication is an S2 obligation.
        # An old attempt may upload orphan objects but cannot promote a head.
        self.lifecycle.checkpoint(self.attempt, reference, marker["state"]["generation"])
        self.checkpoint_ref = reference

    def _validate_snapshot(self, snapshot: Snapshot) -> None:
        if (
            snapshot.owner != self.attempt.owner
            or snapshot.root_operation_id != self.attempt.root_operation_id
            or snapshot.job_id != self.run_spec.job_id
            or snapshot.runtime_revision != self.profile.revision
            or snapshot.compatibility_id != self.profile.checkpoint_compatibility_id
            or snapshot.input_sha256 != self.run_spec.input_bundle.sha256
            or snapshot.image not in self.profile.images.values()
            or snapshot.epoch > self.attempt.epoch
            or (snapshot.epoch == self.attempt.epoch and snapshot.attempt_id != self.attempt.attempt_id)
            or snapshot.state.get("recipe_sha256") != self.worker.recipe
            or snapshot.state.get("operation_id") != self.attempt.root_operation_id
            or snapshot.state.get("job_id") != self.run_spec.job_id
        ):
            raise ValueError("checkpoint identity or scientific recipe differs from this execution")
        if sum(file.size_bytes for file in snapshot.files) > self.worker.request["max_output_bytes"]:
            raise ValueError("checkpoint exceeds the execution byte budget")

    def prepare(self, checkpoint_ref: str | None = None) -> Snapshot | None:
        """Materialize only into a fresh private workspace; never reuse a pod cache."""
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
        atomic_json(self.worker.meta / "gromacs-state.json", snapshot.state)
        self.checkpoint_ref = checkpoint_ref
        return snapshot

    def execute(self, checkpoint_ref: str | None = None) -> dict:
        previous = self.prepare(checkpoint_ref)
        if previous is not None and previous.kind == "result":
            # Recovery after export/callback loss must not run MD again.
            self.worker.state = previous.state
            atomic_json(self.workspace / "result.json", previous.result)
            result = previous.result
        else:
            result = self.worker.run()
        if result["status"] != "succeeded":
            atomic_json(
                self.workspace / "delivery.json",
                {
                    "compute": result["status"],
                    "export": "not-finalized",
                    "checkpoint_ref": self.checkpoint_ref,
                },
            )
            return {"compute": result["status"], "export": "not-finalized"}
        return self.finish_delivery(result)

    def finish_delivery(self, result: dict | None = None) -> dict:
        """Retry export with retained approved scratch, without calling mdrun.

        Call only for the same owned workspace (or one restored via prepare).
        Cleanup belongs to S2's approved profile and must wait for this ack.
        """
        result = result if result is not None else json.loads((self.workspace / "result.json").read_text())
        saved_state = self.worker.meta / "gromacs-state.json"
        if saved_state.is_file():
            state = json.loads(saved_state.read_text())
            if (state.get("recipe_sha256"), state.get("operation_id"), state.get("job_id")) != (
                self.worker.recipe,
                self.attempt.root_operation_id,
                self.run_spec.job_id,
            ):
                raise ValueError("retained workspace belongs to a different execution")
            self.worker.state = state
        if (
            result.get("status") != "succeeded"
            or result.get("recipe_sha256") != self.worker.recipe
            or result.get("operation_id") != self.attempt.root_operation_id
            or result.get("job_id") != self.run_spec.job_id
            or not result.get("inventory_complete")
        ):
            raise ValueError("only verified successful computation can enter finalization")
        atomic_json(self.workspace / "delivery.json", {"compute": "succeeded", "export": "pending"})
        try:
            self.lifecycle.require_current(self.attempt)
            records = tuple(FileRecord.model_validate(item) for item in result["files"])
            self._upload(records)
            reference = self.artifacts.commit(self._snapshot(records, result=result))
            # Save the immutable reference before callback; losing the response
            # requires an idempotent ack retry, not another scientific run.
            atomic_json(
                self.workspace / "delivery.json",
                {
                    "compute": "succeeded",
                    "export": "awaiting-ack",
                    "reference": reference,
                },
            )
            self.lifecycle.finalize(self.attempt, reference)
        except Exception:
            raise DeliveryPending(
                "durable result delivery is pending; retain approved recovery data"
            ) from None
        receipt = {"compute": "succeeded", "export": "committed", "reference": reference}
        atomic_json(self.workspace / "delivery.json", receipt)
        return receipt
