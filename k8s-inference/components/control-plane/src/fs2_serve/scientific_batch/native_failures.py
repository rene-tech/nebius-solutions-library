"""Preserve bounded native failure evidence without certifying scientific output."""

from __future__ import annotations

import hashlib
import json
import re
from importlib import import_module
from pathlib import Path

from fs2_gromacs.contracts import relative_path

from .adapters import CollectedArtifactFile, CollectedStageOutput
from .adapters.staged_workspace import (
    ScientificAdapterError,
    atomic_publish,
    contained_stable_file,
    unwrapped_stage_argv,
)
from .models import StageInvocation
from .native_workflows import NativeWorkflow

FAILURE_SCHEMA = "fs2-serve.nebius.ai/scientific-stage-failure/v1"
DIAGNOSTIC_ROLE = "failed-attempt-diagnostics"
MAX_DIAGNOSTIC_BYTES = 64 * 1024**2
MAX_DIAGNOSTIC_FILES = 512


def collect_failed_diagnostics(
    invocation: StageInvocation, workspace: Path, workflow: NativeWorkflow
) -> CollectedStageOutput | None:
    marker = workspace / ".fs2/stage-failed.json"
    if not marker.exists() and not marker.is_symlink():
        return None
    _, marker_raw = contained_stable_file(
        workspace, ".fs2/stage-failed.json", maximum_bytes=16384, label="native failure marker"
    )
    value = json.loads(marker_raw)
    command = unwrapped_stage_argv(invocation, label="native failure")
    exit_code = value.get("exit_code")
    expected = {
        "schema": FAILURE_SCHEMA,
        "status": "failed",
        "stage_id": invocation.stage_id,
        "shard_id": invocation.shard_id,
        "logical_output_id": invocation.produces,
        "collector_id": invocation.collector_id,
        "validator_id": invocation.validator_id,
        "argv_sha256": hashlib.sha256(json.dumps(command, separators=(",", ":")).encode()).hexdigest(),
        "exit_code": exit_code,
    }
    if value != expected or type(exit_code) is not int or not 1 <= exit_code <= 255:
        raise ScientificAdapterError("native failure marker differs from the frozen invocation")
    result_path, raw = contained_stable_file(
        workspace, "result.json", maximum_bytes=16 * 1024**2, label="failed native result"
    )
    _, request_raw = contained_stable_file(
        workspace, ".fs2/request.json", maximum_bytes=1024**2, label="failed native request"
    )
    result, request = json.loads(raw), workflow.normalize(json.loads(request_raw))
    runtime = import_module(workflow.runtime_package)
    contracts = import_module(workflow.runtime_package + ".contracts")
    engine_id = (
        (runtime.MPI_ENGINE if workflow.model_id == "gromacs-mpi" else runtime.NVIDIA_IMAGE)
        if workflow.engine == "gromacs"
        else runtime.ENGINE_ID
    )
    operation = invocation.argv[invocation.argv.index("--operation-id") + 1]
    job = next(job for job in request["jobs"] if job["id"] == invocation.shard_id)
    recipe = hashlib.sha256(contracts.canonical({"request": request, "job": job["id"], "image": engine_id})).hexdigest()
    if (
        result.get("schema"),
        result.get("operation_id"),
        result.get("job_id"),
        result.get("recipe_sha256"),
        result.get("engine_id"),
    ) != (runtime.RESULT_SCHEMA, operation, invocation.shard_id, recipe, engine_id) or result.get("status") not in {
        "failed",
        "interrupted",
    }:
        raise ScientificAdapterError("failed native result differs from the frozen workflow")
    completed = result.get("completed_steps")
    steps = [step["id"] for step in job["steps"]]
    if not isinstance(completed, list) or completed != steps[: len(completed)]:
        raise ScientificAdapterError("failed native completed steps are not a frozen workflow prefix")
    inventory = result.get("files")
    if not isinstance(inventory, list) or len(inventory) > 9998:
        raise ScientificAdapterError("failed native file inventory exceeds its bound")
    indexed = {}
    for item in inventory:
        name = relative_path(item["path"])
        if (
            name in indexed
            or type(item.get("size_bytes")) is not int
            or item["size_bytes"] < 0
            or not re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", ""))
        ):
            raise ScientificAdapterError("failed native inventory is invalid")
        indexed[name] = item
    if sum(item["size_bytes"] for item in inventory) > request["max_output_bytes"]:
        raise ScientificAdapterError("failed native inventory exceeds its byte budget")
    # Capture stdout/stderr from the actual command first, then native text
    # logs such as PMEMD mdout. Never label a partial trajectory/restart valid.
    command_logs = [
        item["log"]
        for item in reversed(result.get("commands", []))
        if isinstance(item, dict) and isinstance(item.get("log"), str)
    ]
    candidates = list(
        dict.fromkeys(
            [
                *command_logs,
                *(
                    item["path"]
                    for item in inventory
                    if Path(item["path"]).suffix.lower() in {".log", ".mdout", ".out", ".err", ".stderr", ".stdout"}
                ),
            ]
        )
    )
    files = [
        CollectedArtifactFile("failed-result", f"{workflow.engine}-failed-result/v1", result_path, "application/json")
    ]
    preserved, omitted = [], []
    maximum = min(MAX_DIAGNOSTIC_BYTES, invocation.max_output_bytes - len(raw) - 1024**2)
    total = 0
    for name in candidates:
        item = indexed.get(name)
        if item is None:
            continue
        if (
            item["size_bytes"] == 0
            or total + item["size_bytes"] > maximum
            or len(files) >= min(MAX_DIAGNOSTIC_FILES, invocation.max_output_artifacts - 1)
        ):
            omitted.append({**item, "reason": "empty file or bounded diagnostic byte/file budget"})
            continue
        path, content = contained_stable_file(
            workspace, "data/" + name, maximum_bytes=maximum - total, label="failed native log"
        )
        if len(content) != item["size_bytes"] or hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise ScientificAdapterError("failed native log differs from its stopped result inventory")
        artifact_name = f"failed-log-{len(preserved):05d}"
        files.append(CollectedArtifactFile(artifact_name, f"{workflow.engine}-failed-log/v1", path, "text/plain"))
        preserved.append({**item, "artifact_name": artifact_name})
        total += len(content)
    evidence = {
        "schema": "fs2-serve.nebius.ai/native-failed-diagnostics/v1",
        "artifact_role": DIAGNOSTIC_ROLE,
        "status": "failed",
        "validator_id": invocation.validator_id,
        "collector_id": invocation.collector_id,
        "stage_id": invocation.stage_id,
        "shard_id": invocation.shard_id,
        "logical_output_id": invocation.produces,
        "operation_id": operation,
        "job_id": job["id"],
        "recipe_sha256": recipe,
        "engine_id": engine_id,
        "native_status": result["status"],
        "exit_code": exit_code,
        "failure_marker_sha256": hashlib.sha256(marker_raw).hexdigest(),
        "result_sha256": hashlib.sha256(raw).hexdigest(),
        "preserved_logs": preserved,
        "omitted_logs": omitted,
        "partial_scientific_outputs_included": False,
        "scientific_validation_passed": False,
        "checkpoint_generation_created": False,
    }
    receipt = workspace / ".fs2/failed-diagnostics.json"
    atomic_publish(
        receipt,
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode(),
        workspace=workspace,
        label="failed native diagnostic inventory",
    )
    files.append(
        CollectedArtifactFile("failed-diagnostics", "native-failed-diagnostics/v1", receipt, "application/json")
    )
    return CollectedStageOutput(tuple(files), evidence)
