"""Shared compiler for one-stage, candidate-only scientific adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from ..models import (
    AdapterExecutionPlan,
    ArtifactMaterialization,
    MaterializationMode,
    RuntimeArtifactMount,
    StageInvocation,
)
from .common import (
    ScientificAdapterError,
    assert_artifact_requirement,
    assert_profile_identity,
    build_execution_plan,
    logical_stage_artifact,
    parse_public_request,
    run_workspace,
)


@dataclass(frozen=True, slots=True)
class SingleStageSpec:
    model_id: str
    variant_id: str
    repository: str
    revision: str
    parameter_schema: str
    stage_id: str
    argv: tuple[str, ...]
    artifacts: tuple[RuntimeArtifactMount, ...]
    environment: tuple[tuple[str, str], ...] = ()
    parameter_argv: Callable[[Mapping[str, object]], tuple[str, ...]] | None = None


def compile_single_stage(
    spec: SingleStageSpec,
    profile: Mapping[str, object],
    request_value: object,
    *,
    operation_id: str,
) -> AdapterExecutionPlan:
    request = parse_public_request(request_value, maximum_input_bytes=256 * 1024 * 1024)
    assert_profile_identity(
        profile,
        model_id=spec.model_id,
        variant_id=spec.variant_id,
        repository=spec.repository,
        revision=spec.revision,
        parameter_schema=spec.parameter_schema,
        request=request,
    )
    interface = profile.get("interface")
    if not isinstance(interface, Mapping):
        raise ScientificAdapterError("catalog profile interface is invalid")
    schema = interface.get("parameter_schema_definition")
    if not isinstance(schema, Mapping):
        raise ScientificAdapterError("catalog parameter schema is invalid")
    try:
        Draft202012Validator(schema).validate(request.parameters)
    except ValidationError as error:
        raise ScientificAdapterError(f"parameters do not match the catalog schema: {error.message}") from error
    for mount in spec.artifacts:
        assert_artifact_requirement(
            profile,
            artifact_id=mount.artifact_id,
            content_sha256=mount.expected_content_sha256,
        )

    parameters = request.parameters
    parameter_argv = spec.parameter_argv(parameters) if spec.parameter_argv is not None else ()
    workspace = run_workspace(spec.model_id, operation_id, "main")
    request_path = f"{workspace}/input/request.json"
    output_path = f"{workspace}/outputs"
    argv = tuple(
        value.replace("{REQUEST}", request_path).replace("{OUTPUT}", output_path)
        for value in (*spec.argv, *parameter_argv)
    )
    output = logical_stage_artifact(operation_id, spec.stage_id, "main")
    invocation = StageInvocation(
        stage_id=spec.stage_id,
        shard_id="main",
        argv=argv,
        environment=spec.environment,
        working_directory=workspace,
        consumes=(request.input_manifest.artifact_id,),
        produces=output,
        materializations=(
            ArtifactMaterialization(
                request.input_manifest.artifact_id,
                request_path,
                MaterializationMode.COPY_FILE,
                compression=request.input_manifest.compression,
            ),
        ),
        runtime_artifacts=tuple(mount.artifact_id for mount in spec.artifacts),
        runtime_mounts=spec.artifacts,
    )
    return build_execution_plan(
        model_id=spec.model_id,
        variant_id=spec.variant_id,
        source_revision=spec.revision,
        request=request,
        profile=profile,
        expansions=None,
        invocations=(invocation,),
        required_model_artifacts=tuple(mount.artifact_id for mount in spec.artifacts),
    )
