"""Bind a CPU delivery retry to completed native work on S2's retained volume."""

import hashlib
import json
from pathlib import Path
from typing import Literal

from fs2_gromacs.files import atomic_json

from .contracts import Attempt, Contract, Digest, GromacsRun, Identifier, RuntimeProfile


def result_digest(result: dict) -> str:
    return hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class DeliveryMarker(Contract):
    schema_version: Literal[1] = 1
    compute_attempt: Attempt
    runtime_revision: Identifier
    input_sha256: Digest
    job_id: Identifier
    recipe_sha256: Digest
    workspace: Identifier
    result_sha256: Digest


def publish_marker(app, result: dict) -> None:
    """Called only after finish_delivery validates successful scientific computation."""
    if app.attempt.attempt_role != "run":
        return  # Keep the original compute identity across every delivery retry.
    marker = DeliveryMarker(
        compute_attempt=app.attempt,
        runtime_revision=app.profile.revision,
        input_sha256=app.run_spec.input_bundle.sha256,
        job_id=app.run_spec.job_id,
        recipe_sha256=app.worker.recipe,
        workspace=app.workspace.name,
        result_sha256=result_digest(result),
    )
    atomic_json(app.workspace / "result.json", result)
    atomic_json(app.workspace.parent / "DELIVERY.json", marker.model_dump(mode="json"))


def retained_workspace(
    work: Path,
    *,
    attempt: Attempt,
    run: GromacsRun,
    profile: RuntimeProfile,
    compute_pod: str,
    compute_epoch: int,
) -> Path:
    """No guessed directories, preparation, checkpoint replay or scientific command."""
    if attempt.attempt_role != "finalize":
        raise PermissionError("retained delivery requires an authenticated CPU finalizer")
    marker_path = work / "DELIVERY.json"
    if marker_path.is_symlink() or marker_path.stat().st_size > 65536:
        raise ValueError("invalid completed-workspace marker")
    marker = DeliveryMarker.model_validate_json(marker_path.read_bytes())
    source = marker.compute_attempt
    profile.authorize(source)
    if (
        source.attempt_role != "run"
        or source.attempt_id != compute_pod
        or source.epoch != compute_epoch
        or source.epoch >= attempt.epoch
        or source.owner != attempt.owner
        or source.root_operation_id != attempt.root_operation_id
        or source.cluster != attempt.cluster
        or source.image != attempt.image
        or marker.runtime_revision != profile.revision
        or marker.job_id != run.job_id
        or marker.input_sha256 != run.input_bundle.sha256
    ):
        raise PermissionError("completed workspace belongs to a different execution")
    workspace = work / marker.workspace
    if workspace.is_symlink() or workspace.resolve().parent != work.resolve():
        raise ValueError("completed workspace leaves the retained execution volume")
    result_path = workspace / "result.json"
    if result_path.is_symlink() or result_path.stat().st_size > 64 * 1024**2:
        raise ValueError("invalid retained scientific result")
    result = json.loads(result_path.read_bytes())
    if (
        result_digest(result) != marker.result_sha256
        or result.get("status") != "succeeded"
        or result.get("recipe_sha256") != marker.recipe_sha256
        or result.get("operation_id") != attempt.root_operation_id
        or result.get("job_id") != run.job_id
        or not result.get("inventory_complete")
    ):
        raise ValueError("retained computation is incomplete or its result changed")
    return workspace
