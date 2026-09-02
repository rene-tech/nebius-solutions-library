"""Native academic AlphaFold 3 adapter with separate CPU and GPU execution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..catalog_adapter import ScientificStageExpansion
from ..models import (
    AdapterExecutionPlan,
    ArtifactMaterialization,
    MaterializationMode,
    RuntimeArtifactMount,
    StageInvocation,
)
from .common import (
    ArtifactLoader,
    CollectedOutput,
    PublicRunRequest,
    ScientificAdapterError,
    assert_artifact_requirement,
    assert_profile_identity,
    bounded_int,
    build_execution_plan,
    logical_stage_artifact,
    parse_public_request,
    stage_workspace,
    strict_object,
)
from .secondary_structure import collect_structure_outputs, validate_structure_output

MODEL_ID = "alphafold3"
VARIANT_ID = "upstream-v3-0-4"
SOURCE_REPOSITORY = "google-deepmind/alphafold3"
SOURCE_REVISION = "85c4d20505fd5cef05eac22b534d4e793971ae69"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/alphafold3-upstream-v3-0-4-parameters/v1"
PARAMETERS_ARTIFACT = "alphafold3-parameters"
REFERENCE_ARTIFACT = "alphafold3-public-databases-v3.0"
REFERENCE_READY_MARKER = "/databases/.fs2-manifest-sha256"
PARAMETERS_SHA256 = "74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff"
VALIDATOR_ID = "alphafold3-upstream-v3-0-4"


@dataclass(frozen=True, slots=True)
class Parameters:
    input_mode: str
    seeds: tuple[int, ...]
    samples: int

    @classmethod
    def parse(cls, value: object) -> Parameters:
        item = strict_object(
            value,
            required=frozenset({"model_seeds", "num_diffusion_samples"}),
            optional=frozenset({"input_mode"}),
            label="AlphaFold 3 parameters",
        )
        input_mode = item.get("input_mode", "raw")
        if input_mode not in {"raw", "enriched"}:
            raise ScientificAdapterError("input_mode must be raw or enriched")
        raw = item["model_seeds"]
        if not isinstance(raw, list) or not 1 <= len(raw) <= 16:
            raise ScientificAdapterError("model_seeds must contain 1..16 values")
        seeds = tuple(bounded_int(seed, minimum=0, maximum=2**31 - 1, label="model seed") for seed in raw)
        if len(set(seeds)) != len(seeds):
            raise ScientificAdapterError("model_seeds must be unique")
        return cls(
            str(input_mode),
            seeds,
            bounded_int(item["num_diffusion_samples"], minimum=1, maximum=16, label="num_diffusion_samples"),
        )


def _request(value: object) -> tuple[PublicRunRequest, Parameters]:
    request = parse_public_request(value, maximum_input_bytes=64 * 1024 * 1024)
    return request, Parameters.parse(request.parameters)


def _candidate_plan(
    profile: Mapping[str, object],
    request_value: object,
    *,
    operation_id: str,
) -> AdapterExecutionPlan:
    request, parameters = _request(request_value)
    assert_profile_identity(
        profile,
        model_id=MODEL_ID,
        variant_id=VARIANT_ID,
        repository=SOURCE_REPOSITORY,
        revision=SOURCE_REVISION,
        parameter_schema=PARAMETER_SCHEMA,
        request=request,
    )
    assert_artifact_requirement(
        profile,
        artifact_id=PARAMETERS_ARTIFACT,
        content_sha256=None,
        required_file="af3.bin.zst",
        required_file_sha256=PARAMETERS_SHA256,
        required_file_size_bytes=1_020_545_840,
    )
    assert_artifact_requirement(
        profile,
        artifact_id=REFERENCE_ARTIFACT,
        content_sha256=None,
        required_file=".fs2-manifest-sha256",
    )
    data_root = stage_workspace("data-pipeline", "main")
    inference_root = stage_workspace("inference", "main")
    enriched, result = (
        logical_stage_artifact(operation_id, "data-pipeline", "main"),
        logical_stage_artifact(operation_id, "inference", "main"),
    )
    common = ("/usr/local/bin/fs2-run-alphafold3",)
    seed_csv = ",".join(str(seed) for seed in parameters.seeds)
    data_pipeline = StageInvocation(
        "data-pipeline",
        "main",
        common
        + (
            "data",
            "--input-json",
            f"{data_root}/input.json",
            "--output-dir",
            f"{data_root}/outputs",
            "--processed-json",
            f"{data_root}/processed.json",
            "--provenance-marker",
            f"{data_root}/provenance.json",
            "--handoff-tar",
            f"{data_root}/handoff.tar.zst",
            "--output-artifact-id",
            enriched,
            "--db-dir",
            "/databases",
            "--db-ready-marker",
            REFERENCE_READY_MARKER,
            "--reference-artifact-id",
            REFERENCE_ARTIFACT,
            "--raw-input-sha256",
            request.input_manifest.sha256,
            "--model-seeds",
            seed_csv,
        ),
        (("FS2_NETWORK_MODE", "offline"),),
        data_root,
        (request.input_manifest.artifact_id,),
        enriched,
        (
            ArtifactMaterialization(
                request.input_manifest.artifact_id,
                f"{data_root}/input.json",
                MaterializationMode.COPY_FILE,
            ),
        ),
        (REFERENCE_ARTIFACT,),
        (
            RuntimeArtifactMount(
                REFERENCE_ARTIFACT,
                "/databases",
                supplemental_groups=(65532,),
            ),
        ),
    )
    inference_input = enriched if parameters.input_mode == "raw" else request.input_manifest.artifact_id
    provenance_expectations: tuple[str, ...] = (
        "--expected-reference-artifact-id",
        REFERENCE_ARTIFACT,
        "--expected-model-seeds",
        seed_csv,
    )
    if parameters.input_mode == "raw":
        provenance_expectations += (
            "--expected-raw-input-sha256",
            request.input_manifest.sha256,
        )
    inference = StageInvocation(
        "inference",
        "main",
        common
        + (
            "inference",
            "--processed-json",
            f"{inference_root}/input/processed.json",
            "--provenance-marker",
            f"{inference_root}/input/provenance.json",
            "--input-artifact-id",
            inference_input,
        )
        + provenance_expectations
        + (
            "--output-dir",
            f"{inference_root}/outputs",
            "--model-dir",
            "/models",
            "--num-diffusion-samples",
            str(parameters.samples),
            "--model-seeds",
            seed_csv,
        ),
        (("XLA_PYTHON_CLIENT_PREALLOCATE", "true"), ("FS2_NETWORK_MODE", "offline")),
        inference_root,
        (inference_input,),
        result,
        (
            ArtifactMaterialization(
                inference_input,
                f"{inference_root}/input",
                MaterializationMode.EXTRACT_TAR,
                compression=("zstd" if parameters.input_mode == "raw" else request.input_manifest.compression),
                expected_members=("processed.json", "provenance.json"),
            ),
        ),
        (PARAMETERS_ARTIFACT,),
        (
            RuntimeArtifactMount(
                PARAMETERS_ARTIFACT,
                "/models",
                supplemental_groups=(65532,),
            ),
        ),
    )
    invocations: tuple[StageInvocation, ...]
    required_artifacts: tuple[str, ...]
    if parameters.input_mode == "raw":
        invocations = (data_pipeline, inference)
        expansions = None
        required_artifacts = (REFERENCE_ARTIFACT, PARAMETERS_ARTIFACT)
    else:
        invocations = (inference,)
        expansions = {
            "data-pipeline": ScientificStageExpansion(enabled=False),
            "inference": ScientificStageExpansion(depends_on=()),
        }
        required_artifacts = (PARAMETERS_ARTIFACT,)
    return build_execution_plan(
        model_id=MODEL_ID,
        variant_id=VARIANT_ID,
        source_revision=SOURCE_REVISION,
        request=request,
        profile=profile,
        expansions=expansions,
        invocations=invocations,
        required_model_artifacts=required_artifacts,
    )


def compile_run(
    profile: Mapping[str, object],
    request_value: object,
    *,
    operation_id: str,
) -> AdapterExecutionPlan:
    return _candidate_plan(profile, request_value, operation_id=operation_id)


def collect_output(workspace: Path) -> CollectedOutput:
    return collect_structure_outputs(
        workspace,
        structure_globs=("outputs/*/seed-*_sample-*/*_model.cif",),
        confidence_globs=("outputs/confidence.json",),
        manifest_id="alphafold3.results",
        runtime_id=MODEL_ID,
        model_revision=SOURCE_REVISION,
        maximum_structures=256,
    )


def validate_output(
    manifest: object, *, artifact_loader: ArtifactLoader, expected_structures: int
) -> Mapping[str, object]:
    return validate_structure_output(
        manifest,
        artifact_loader=artifact_loader,
        expected_structures=expected_structures,
        validator_id=VALIDATOR_ID,
        backend_id=MODEL_ID,
        model_revision=SOURCE_REVISION,
    )
