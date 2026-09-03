"""Native academic AlphaFold 3 adapter with a relocatable CPU/GPU handoff."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..catalog_adapter import ScientificStageExpansion
from ..models import (
    AdapterExecutionPlan,
    ArtifactAccessContext,
    ArtifactMaterialization,
    MaterializationMode,
    ScientificInputArtifact,
    StageInvocation,
)
from . import CollectedStageOutput, StageExecutionContract
from .common import (
    ScientificAdapterError,
    assert_artifact_requirement,
    assert_profile_identity,
    bind_compiler_input,
    bounded_int,
    build_execution_plan,
    logical_stage_artifact,
    parse_public_request,
    raw_sha256,
    run_workspace,
    runtime_artifact_mount,
    strict_object,
)
from .secondary_structure import collect_confidence_envelope, collect_handoff

MODEL_ID = "alphafold3"
VARIANT_ID = "upstream-v3-0-4"
SOURCE_REPOSITORY = "google-deepmind/alphafold3"
SOURCE_REVISION = "85c4d20505fd5cef05eac22b534d4e793971ae69"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/alphafold3-upstream-v3-0-4-parameters/v1"
PARAMETERS_ARTIFACT = "alphafold3-parameters"
REFERENCE_ARTIFACT = "alphafold3-public-databases-v3.0"
REFERENCE_REVISION = "v3.0-paper-snapshot-2022-09-28"
PARAMETERS_SHA256 = "74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff"
VALIDATOR_ID = "alphafold3-upstream-v3-0-4"
ADMISSION_BLOCKER = "LicenseAcceptancePending"
STAGE_EXECUTION_CONTRACTS: Mapping[str, StageExecutionContract] = {
    "data-pipeline": StageExecutionContract(
        "alphafold3-data-collector-v1", "alphafold3-data-validator-v1", (REFERENCE_ARTIFACT,)
    ),
    "inference": StageExecutionContract(
        "alphafold3-result-collector-v1", VALIDATOR_ID, (PARAMETERS_ARTIFACT,)
    ),
}


@dataclass(frozen=True, slots=True)
class Parameters:
    input_mode: str
    seeds: tuple[int, ...]
    samples: int
    raw_input_sha256: str | None

    @classmethod
    def parse(cls, value: object) -> Parameters:
        item = strict_object(
            value,
            required=frozenset({"input_mode", "model_seeds", "num_diffusion_samples"}),
            optional=frozenset({"raw_input_sha256"}),
            label="AlphaFold 3 parameters",
        )
        if item["input_mode"] not in {"raw", "enriched"}:
            raise ScientificAdapterError("input_mode must be raw or enriched")
        raw = item["model_seeds"]
        if not isinstance(raw, list) or not 1 <= len(raw) <= 16:
            raise ScientificAdapterError("model_seeds must contain 1..16 values")
        seeds = tuple(bounded_int(seed, minimum=0, maximum=2**31 - 1, label="model seed") for seed in raw)
        if len(set(seeds)) != len(seeds):
            raise ScientificAdapterError("model_seeds must be unique")
        raw_input_digest = item.get("raw_input_sha256")
        if raw_input_digest is not None and (
            not isinstance(raw_input_digest, str)
            or len(raw_input_digest) != 64
            or any(character not in "0123456789abcdef" for character in raw_input_digest)
        ):
            raise ScientificAdapterError("raw_input_sha256 must be a lowercase SHA-256")
        if item["input_mode"] == "enriched" and raw_input_digest is None:
            raise ScientificAdapterError("enriched input requires its original raw_input_sha256 provenance")
        return cls(
            str(item["input_mode"]),
            seeds,
            bounded_int(item["num_diffusion_samples"], minimum=1, maximum=16, label="num_diffusion_samples"),
            raw_input_digest,
        )


def compile_run(
    profile: Mapping[str, object],
    request_value: object,
    *,
    operation_id: str,
    variant_id: str,
    access_context: ArtifactAccessContext,
    input_artifacts: tuple[ScientificInputArtifact, ...],
) -> AdapterExecutionPlan:
    request = parse_public_request(request_value, maximum_input_bytes=64 * 1024 * 1024)
    parameters = Parameters.parse(request.parameters)
    media = (
        frozenset({"application/json"})
        if parameters.input_mode == "raw"
        else frozenset({"application/x-tar", "application/zstd"})
    )
    compressions = frozenset({None, "none"}) if parameters.input_mode == "raw" else frozenset({"zstd"})
    input_artifact = bind_compiler_input(
        request,
        variant_id=variant_id,
        expected_variant_id=VARIANT_ID,
        input_artifacts=input_artifacts,
        maximum_bytes=64 * 1024 * 1024,
        allowed_media_types=media,
        allowed_compressions=compressions,
    )
    assert_profile_identity(
        profile,
        model_id=MODEL_ID,
        variant_id=VARIANT_ID,
        repository=SOURCE_REPOSITORY,
        revision=SOURCE_REVISION,
        parameter_schema=PARAMETER_SCHEMA,
        request=request,
    )
    parameters_artifact = assert_artifact_requirement(
        profile, artifact_id=PARAMETERS_ARTIFACT, content_sha256=PARAMETERS_SHA256, required_file="af3.bin.zst"
    )
    reference_artifact = assert_artifact_requirement(profile, artifact_id=REFERENCE_ARTIFACT, content_sha256=None)
    deployment_access = profile.get("access")
    reference_content_sha256 = reference_artifact.get("content_digest_sha256")
    reference_manifest_sha256 = reference_artifact.get("localization_manifest_sha256")
    reference_source = reference_artifact.get("source")
    if (
        not isinstance(reference_content_sha256, str)
        or len(reference_content_sha256) != 64
        or not isinstance(reference_manifest_sha256, str)
        or len(reference_manifest_sha256) != 64
        or reference_content_sha256 == reference_manifest_sha256
        or not isinstance(reference_source, Mapping)
        or reference_source.get("release_id") != REFERENCE_REVISION
    ):
        raise ScientificAdapterError("AlphaFold 3 reference bundle manifest is not promoted")
    if (
        not isinstance(deployment_access, Mapping)
        or deployment_access.get("profile") != "academic"
        or deployment_access.get("credentials_embedded") is not False
        or deployment_access.get("materialization") != "restricted-quarantine-poc-authorized"
        or deployment_access.get("operational_activation") != "user-authorized-academic-poc"
        or deployment_access.get("license_gate_scope") != "production-promotion-only"
        or deployment_access.get("receipt_digest") is not None
        or parameters_artifact.get("handling") != "restricted-quarantine"
        or parameters_artifact.get("supply_state") != "poc-staged-quarantine"
        or reference_artifact.get("handling") != "restricted-quarantine"
    ):
        raise ScientificAdapterError(ADMISSION_BLOCKER)
    data_root = run_workspace(MODEL_ID, operation_id, "data-pipeline-main")
    inference_root = run_workspace(MODEL_ID, operation_id, "inference-main")
    processed = logical_stage_artifact(operation_id, "data-pipeline", "main")
    result = logical_stage_artifact(operation_id, "inference", "main")
    seeds = ",".join(str(seed) for seed in parameters.seeds)
    expected_raw_sha256 = (
        raw_sha256(input_artifact.digest) if parameters.input_mode == "raw" else str(parameters.raw_input_sha256)
    )
    data_localization_marker = f"{data_root}/.fs2/runtime-localization.json"
    inference_localization_marker = f"{inference_root}/.fs2/runtime-localization.json"
    reference_mount = runtime_artifact_mount(
        profile,
        artifact_id=REFERENCE_ARTIFACT,
        mount_path="/databases",
        expected_manifest_sha256=reference_manifest_sha256,
    )
    parameters_mount = runtime_artifact_mount(
        profile,
        artifact_id=PARAMETERS_ARTIFACT,
        mount_path="/models/af3.bin.zst",
        sub_path="alphafold3/af3.bin.zst",
    )
    data = StageInvocation(
        stage_id="data-pipeline",
        shard_id="main",
        argv=(
            "/usr/local/bin/fs2-run-alphafold3",
            "data",
            "--input-json",
            f"{data_root}/input.json",
            "--output-dir",
            f"{data_root}/data-output",
            "--processed-json",
            f"{data_root}/processed.json",
            "--provenance-marker",
            f"{data_root}/provenance.json",
            "--handoff-tar",
            f"{data_root}/handoff.tar.zst",
            "--output-artifact-id",
            processed,
            "--db-dir",
            "/databases",
            "--db-ready-marker",
            "/databases/.fs2-manifest-sha256",
            "--reference-artifact-id",
            REFERENCE_ARTIFACT,
            "--reference-revision",
            REFERENCE_REVISION,
            "--expected-db-content-sha256",
            reference_content_sha256,
            "--expected-db-manifest-sha256",
            reference_manifest_sha256,
            "--raw-input-sha256",
            expected_raw_sha256,
            "--model-seeds",
            seeds,
            "--num-diffusion-samples",
            str(parameters.samples),
            "--runtime-localization-marker",
            data_localization_marker,
        ),
        environment=(("FS2_NETWORK_MODE", "offline"),),
        working_directory=data_root,
        consumes=(input_artifact.logical_artifact_id,),
        produces=processed,
        collector_id="alphafold3-data-collector-v1",
        validator_id="alphafold3-data-validator-v1",
        handoff_name="processed-input",
        max_output_artifacts=1,
        max_output_bytes=64 * 1024 * 1024,
        materializations=(
            ArtifactMaterialization(
                input_artifact.logical_artifact_id, f"{data_root}/input.json", MaterializationMode.COPY_FILE
            ),
        ),
        runtime_artifacts=(REFERENCE_ARTIFACT,),
        runtime_mounts=(reference_mount,),
    )
    inference_input = processed if parameters.input_mode == "raw" else input_artifact.logical_artifact_id
    inference_args = [
        "/usr/local/bin/fs2-run-alphafold3",
        "inference",
        "--processed-json",
        f"{inference_root}/input/processed.json",
        "--provenance-marker",
        f"{inference_root}/input/provenance.json",
        "--input-artifact-id",
        inference_input,
        "--expected-reference-artifact-id",
        REFERENCE_ARTIFACT,
        "--expected-reference-revision",
        REFERENCE_REVISION,
        "--expected-reference-content-sha256",
        reference_content_sha256,
        "--expected-reference-manifest-sha256",
        reference_manifest_sha256,
        "--expected-model-seeds",
        seeds,
        "--expected-raw-input-sha256",
        expected_raw_sha256,
        "--output-dir",
        f"{inference_root}/outputs",
        "--model-dir",
        "/models",
        "--num-diffusion-samples",
        str(parameters.samples),
        "--model-seeds",
        seeds,
        "--runtime-localization-marker",
        inference_localization_marker,
    ]
    inference = StageInvocation(
        stage_id="inference",
        shard_id="main",
        argv=tuple(inference_args),
        environment=(
            ("FS2_NETWORK_MODE", "offline"),
            ("HF_HUB_OFFLINE", "1"),
            ("TRANSFORMERS_OFFLINE", "1"),
            ("XLA_PYTHON_CLIENT_PREALLOCATE", "true"),
        ),
        working_directory=inference_root,
        consumes=(inference_input,),
        produces=result,
        collector_id="alphafold3-result-collector-v1",
        validator_id=VALIDATOR_ID,
        handoff_name=None,
        max_output_artifacts=len(parameters.seeds) * parameters.samples + 1,
        max_output_bytes=8 * 1024 * 1024 * 1024,
        materializations=(
            ArtifactMaterialization(
                inference_input, f"{inference_root}/input", MaterializationMode.EXTRACT_TAR, compression="zstd"
            ),
        ),
        runtime_artifacts=(PARAMETERS_ARTIFACT,),
        runtime_mounts=(parameters_mount,),
    )
    invocations: tuple[StageInvocation, ...]
    required: tuple[str, ...]
    if parameters.input_mode == "raw":
        invocations = (data, inference)
        expansions = None
        required = (PARAMETERS_ARTIFACT, REFERENCE_ARTIFACT)
    else:
        invocations = (inference,)
        expansions = {
            "data-pipeline": ScientificStageExpansion(enabled=False),
            "inference": ScientificStageExpansion(depends_on=()),
        }
        required = (PARAMETERS_ARTIFACT,)
    return build_execution_plan(
        model_id=MODEL_ID,
        variant_id=VARIANT_ID,
        source_revision=SOURCE_REVISION,
        request=request,
        profile=profile,
        expansions=expansions,
        invocations=invocations,
        required_model_artifacts=required,
    )


def collect_data(invocation: StageInvocation, workspace: Path) -> CollectedStageOutput:
    return collect_handoff(
        invocation,
        workspace,
        filename="handoff.tar.zst",
        semantic_type="alphafold3-processed-input/v1",
        media_type="application/x-tar",
        compression="zstd",
    )


def collect_result(invocation: StageInvocation, workspace: Path) -> CollectedStageOutput:
    seeds = tuple(int(value) for value in invocation.argv[invocation.argv.index("--model-seeds") + 1].split(","))
    samples = int(invocation.argv[invocation.argv.index("--num-diffusion-samples") + 1])
    return collect_confidence_envelope(
        invocation,
        workspace,
        expected_runtime_id=MODEL_ID,
        expected_model_revision=SOURCE_REVISION,
        expected_seeds=seeds,
        expected_samples_per_seed=samples,
    )
