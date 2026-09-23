"""Shared platform adapter for engine-owned native molecular-dynamics recipes.

The worker owns scientific syntax, native checkpoints and result inventories.
This layer binds the pinned runtime to the existing queue, per-job GPU shards,
artifact transport and collector. It does not translate or tune the science.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

from fs2_gromacs.files import digest_file, inventory, media_type

from ..catalog_adapter import ScientificStageExpansion
from ..models import (
    AdapterExecutionPlan,
    ArtifactMaterialization,
    MaterializationMode,
    ScientificInputArtifact,
    StageInvocation,
    StageWorkspaceDocument,
)
from ..native_workflows import NativeWorkflow, workflow_for_collector
from .common import (
    ScientificAdapterError,
    assert_profile_identity,
    build_execution_plan,
    logical_stage_artifact,
    parse_public_request,
    run_workspace,
)
from .staged_workspace import completion_marker, contained_stable_file, wrap_stage_argv

if TYPE_CHECKING:
    from . import CollectedStageOutput

MAX_INPUT_BYTES = 4 * 1024**3


@dataclass(frozen=True, slots=True)
class NativeMDAdapter:
    model_id: str
    variant_id: str
    source_repository: str

    @property
    def workflow(self) -> NativeWorkflow:
        value = workflow_for_collector(self.collector_id)
        if value is None or value.model_id != self.model_id:
            raise ScientificAdapterError("native MD adapter has no registered workflow")
        return value

    @property
    def collector_id(self) -> str:
        return f"{self.model_id}-workflow-v1"

    @property
    def input_id(self) -> str:
        return f"{self.model_id}-inputs"

    def runtime(self) -> ModuleType:
        return import_module(self.workflow.runtime_package)

    def contracts(self) -> ModuleType:
        return import_module(f"{self.workflow.runtime_package}.contracts")

    def public_input_contract(self) -> dict[str, Any]:
        return {
            "exactly_one_entry": True,
            "manifest_media_type": "application/vnd.fs2.scientific-manifest+json",
            "manifest_compression": "none",
            "entry_name_rule": (
                f"Exactly one gzip tar bundle containing all relative-path {self.model_id.upper()} "
                "inputs, includes, force fields and restart dependencies."
            ),
            "entry": {
                "name": self.input_id,
                "semantic_type": f"{self.model_id}-input-bundle/v1",
                "media_type": "application/x-tar",
                "compression": "gzip",
                "allowed_compressions": ["gzip"],
                "maximum_bytes": MAX_INPUT_BYTES,
            },
        }

    def compile_run(
        self,
        profile: Mapping[str, object],
        request_value: object,
        *,
        operation_id: str,
        input_artifacts: tuple[ScientificInputArtifact, ...] | None = None,
    ) -> AdapterExecutionPlan:
        request = parse_public_request(request_value, maximum_input_bytes=MAX_INPUT_BYTES)
        contracts, runtime = self.contracts(), self.runtime()
        value = contracts.normalize(request.parameters)
        revision = runtime.ENGINE_ID.rsplit(":", 1)[1]
        assert_profile_identity(
            profile,
            model_id=self.model_id,
            repository=self.source_repository,
            revision=revision,
            parameter_schema=runtime.PARAMETER_SCHEMA,
            request=request,
            gpu_topology="single-gpu",
        )
        entries = input_artifacts or ()
        if len(entries) != 1:
            raise ScientificAdapterError(f"{self.model_id} needs one verified {self.input_id} bundle")
        entry = entries[0]
        if (entry.logical_artifact_id, entry.semantic_type, entry.media_type, entry.compression) != (
            self.input_id,
            f"{self.model_id}-input-bundle/v1",
            "application/x-tar",
            "gzip",
        ) or not 1 <= entry.size_bytes <= MAX_INPUT_BYTES:
            raise ScientificAdapterError(f"{self.model_id} bundle metadata differs from its typed input contract")
        invocations = []
        for job in value["jobs"]:
            workspace = run_workspace(self.model_id, operation_id, job["id"])
            command = (
                "python3",
                "-m",
                f"{self.workflow.runtime_package}.worker",
                "--request",
                f"{workspace}/.fs2/request.json",
                "--workspace",
                workspace,
                "--operation-id",
                operation_id,
                "--job-id",
                job["id"],
                "--checkpoint-mode",
                "companion",
            )
            invocations.append(
                StageInvocation(
                    stage_id="workflow",
                    shard_id=job["id"],
                    argv=wrap_stage_argv(workspace, command),
                    environment=(),
                    working_directory=workspace,
                    consumes=(self.input_id,),
                    produces=logical_stage_artifact(operation_id, "workflow", job["id"]),
                    collector_id=self.collector_id,
                    validator_id=self.collector_id,
                    max_output_artifacts=10000,
                    max_output_bytes=value["max_output_bytes"] + 16 * 1024**2,
                    materializations=(
                        ArtifactMaterialization(
                            self.input_id,
                            f"{workspace}/input.tar.gz",
                            MaterializationMode.COPY_FILE,
                            compression="gzip",
                        ),
                    ),
                    workspace_documents=(
                        StageWorkspaceDocument(".fs2/request.json", contracts.canonical(value).decode()),
                    ),
                )
            )
        return build_execution_plan(
            model_id=self.model_id,
            variant_id=self.variant_id,
            source_revision=revision,
            request=request,
            profile=profile,
            expansions={"workflow": ScientificStageExpansion(shard_ids=tuple(job["id"] for job in value["jobs"]))},
            invocations=tuple(invocations),
            required_model_artifacts=(),
        )

    def collect_companion_output(self, invocation: StageInvocation, workspace: Path) -> CollectedStageOutput:
        from . import CollectedArtifactFile, CollectedStageOutput

        if invocation.stage_id != "workflow" or invocation.collector_id != self.collector_id:
            raise ScientificAdapterError(f"{self.model_id} collector received another stage contract")
        completion_marker(invocation, workspace, label=f"{self.model_id} workflow")
        result_path, raw = contained_stable_file(
            workspace, "result.json", maximum_bytes=16 * 1024**2, label=f"{self.model_id} result"
        )
        _, request_raw = contained_stable_file(
            workspace, ".fs2/request.json", maximum_bytes=1024**2, label=f"{self.model_id} request"
        )
        contracts, runtime = self.contracts(), self.runtime()
        result, request = json.loads(raw), contracts.normalize(json.loads(request_raw))
        operation = invocation.argv[invocation.argv.index("--operation-id") + 1]
        job = next(job for job in request["jobs"] if job["id"] == invocation.shard_id)
        recipe = hashlib.sha256(
            contracts.canonical({"request": request, "job": job["id"], "image": runtime.ENGINE_ID})
        ).hexdigest()
        if (
            result.get("schema"),
            result.get("operation_id"),
            result.get("job_id"),
            result.get("status"),
            result.get("recipe_sha256"),
        ) != (runtime.RESULT_SCHEMA, operation, job["id"], "succeeded", recipe) or result.get("completed_steps") != [
            step["id"] for step in job["steps"]
        ]:
            raise ScientificAdapterError(f"{self.model_id} did not complete the exact frozen workflow")
        actual = inventory(workspace / "data", max_bytes=request["max_output_bytes"])
        if actual != result.get("files"):
            raise ScientificAdapterError(f"{self.model_id} output files differ from the completed inventory")
        files = [
            CollectedArtifactFile("result", f"{self.model_id}-workflow-result/v1", result_path, "application/json")
        ]
        for index, item in enumerate(actual):
            files.append(
                CollectedArtifactFile(
                    f"file-{index:05d}",
                    f"{self.model_id}-file/v1",
                    workspace / "data" / item["path"],
                    media_type(item["path"]),
                )
            )
        return CollectedStageOutput(
            tuple(files),
            {
                "validator_id": self.collector_id,
                "status": "passed",
                "command_count": len(result["commands"]),
                "completed_steps": result["completed_steps"],
                "file_count": len(actual),
                "result_sha256": digest_file(result_path),
                "scientific_convergence_claimed": False,
            },
        )
