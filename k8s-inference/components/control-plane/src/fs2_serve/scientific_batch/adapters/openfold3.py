"""OpenFold3 OpenBind-0 adapter, explicitly non-equivalent to native AF3."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

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

MODEL_ID = "openfold3"
VARIANT_ID = "upstream-openbind-v0-5-0"
SOURCE_REPOSITORY = "aqlaboratory/openfold-3"
SOURCE_REVISION = "c4771653c5d0a3ebb0b3af71b05efd64bc44ee86"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/openfold3-upstream-openbind-v0-5-0-parameters/v1"
MODEL_ARTIFACT = "openfold3-openbind-0"
REFERENCE_ARTIFACT = "openfold3-components-bcif"
VALIDATOR_ID = "openfold3-upstream-openbind-v0-5-0"
RELATIONSHIP = "independent-non-equivalent-alternative"
MODEL_MANIFEST_SHA256 = "f954e2f2e3d0bdba297ac8009f6d590b3e2c28ca2985742c9bbd8167f276f6b5"
MODEL_FILE_SHA256 = "bd43301c011d5f87580d3e8b548658869433e4488399feb03035ba248f8e29e4"
MODEL_SIZE_BYTES = 2_287_872_989
CCD_MANIFEST_SHA256 = "ff75f66793c11d7cb63531c758b210fa6fe33d5a39378bb0ab89094278e95e3b"
CCD_FILE_SHA256 = "473d845c8b250b188dbed9bf505ae206692a178a2a7c4869bf8f9de707ffcc0c"
STAGE_EXECUTION_CONTRACTS: Mapping[str, StageExecutionContract] = {
    "data-pipeline": StageExecutionContract(
        "openfold3-data-collector-v1", "openfold3-data-validator-v1", ()
    ),
    "inference": StageExecutionContract(
        "openfold3-result-collector-v1", VALIDATOR_ID, (MODEL_ARTIFACT, REFERENCE_ARTIFACT)
    ),
}


@dataclass(frozen=True, slots=True)
class Parameters:
    seeds: tuple[int, ...]
    msa_mode: str

    @classmethod
    def parse(cls, value: object) -> Parameters:
        item = strict_object(value, required=frozenset({"model_seeds", "msa_mode"}), label="OpenFold3 parameters")
        raw = item["model_seeds"]
        if not isinstance(raw, list) or not 1 <= len(raw) <= 16:
            raise ScientificAdapterError("model_seeds must contain 1..16 values")
        seeds = tuple(bounded_int(seed, minimum=0, maximum=2**31 - 1, label="model seed") for seed in raw)
        if len(set(seeds)) != len(seeds):
            raise ScientificAdapterError("model_seeds must be unique")
        if item["msa_mode"] != "none":
            raise ScientificAdapterError("OpenFold3 currently admits only the relocatable no-MSA lane")
        return cls(seeds=seeds, msa_mode="none")


def compile_run(
    profile: Mapping[str, object],
    request_value: object,
    *,
    operation_id: str,
    variant_id: str,
    access_context: ArtifactAccessContext,
    input_artifacts: tuple[ScientificInputArtifact, ...],
) -> AdapterExecutionPlan:
    request = parse_public_request(request_value, maximum_input_bytes=256 * 1024 * 1024)
    parameters = Parameters.parse(request.parameters)
    input_artifact = bind_compiler_input(
        request,
        variant_id=variant_id,
        expected_variant_id=VARIANT_ID,
        input_artifacts=input_artifacts,
        maximum_bytes=256 * 1024 * 1024,
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
    checkpoint = assert_artifact_requirement(
        profile,
        artifact_id=MODEL_ARTIFACT,
        content_sha256=MODEL_MANIFEST_SHA256,
        required_file="of3-ob-2025-06-30-174k.pt",
    )
    ccd = assert_artifact_requirement(
        profile, artifact_id=REFERENCE_ARTIFACT, content_sha256=CCD_MANIFEST_SHA256, required_file="components.bcif"
    )
    if checkpoint.get("total_size_bytes") != MODEL_SIZE_BYTES:
        raise ScientificAdapterError("OpenFold3 requires exactly one verified OpenBind checkpoint")
    checkpoint_files = checkpoint.get("file_manifest")
    ccd_files = ccd.get("file_manifest")
    if (
        not isinstance(checkpoint_files, list)
        or len(checkpoint_files) != 1
        or checkpoint_files[0].get("sha256") != MODEL_FILE_SHA256
    ):
        raise ScientificAdapterError("OpenFold3 checkpoint manifest is not the exact single-file artifact")
    if not isinstance(ccd_files, list) or len(ccd_files) != 1 or ccd_files[0].get("sha256") != CCD_FILE_SHA256:
        raise ScientificAdapterError("OpenFold3 CCD manifest is not the exact single-file artifact")
    data_root = run_workspace(MODEL_ID, operation_id, "data-pipeline-main")
    inference_root = run_workspace(MODEL_ID, operation_id, "inference-main")
    prepared = logical_stage_artifact(operation_id, "data-pipeline", "main")
    result = logical_stage_artifact(operation_id, "inference", "main")
    seeds = ",".join(str(seed) for seed in parameters.seeds)
    localization_marker = f"{inference_root}/.fs2/runtime-localization.json"
    inference_mounts = (
        runtime_artifact_mount(
            profile,
            artifact_id=MODEL_ARTIFACT,
            mount_path="/models/openfold3",
        ),
        runtime_artifact_mount(
            profile,
            artifact_id=REFERENCE_ARTIFACT,
            mount_path="/databases/openfold3",
        ),
    )
    invocations = (
        StageInvocation(
            stage_id="data-pipeline",
            shard_id="main",
            argv=(
                "/usr/local/bin/fs2-run-openfold3",
                "prepare",
                "--input-manifest",
                f"{data_root}/input.json",
                "--query-json",
                f"{data_root}/query.json",
                "--base-runner-yaml",
                "/opt/fs2/runtime/openfold3/runner-base.yaml",
                "--runner-yaml",
                f"{data_root}/runner.yaml",
                "--provenance-marker",
                f"{data_root}/provenance.json",
                "--handoff-tar",
                f"{data_root}/handoff.tar.zst",
                "--output-artifact-id",
                prepared,
                "--raw-input-sha256",
                raw_sha256(input_artifact.digest),
                "--msa-mode",
                "none",
                "--model-seeds",
                seeds,
                "--offline",
            ),
            environment=(("FS2_NETWORK_MODE", "offline"),),
            working_directory=data_root,
            consumes=(input_artifact.logical_artifact_id,),
            produces=prepared,
            collector_id="openfold3-data-collector-v1",
            validator_id="openfold3-data-validator-v1",
            handoff_name="prepared-input",
            max_output_artifacts=1,
            max_output_bytes=64 * 1024 * 1024,
            materializations=(
                ArtifactMaterialization(
                    input_artifact.logical_artifact_id, f"{data_root}/input.json", MaterializationMode.COPY_FILE
                ),
            ),
        ),
        StageInvocation(
            stage_id="inference",
            shard_id="main",
            argv=(
                "/usr/local/bin/fs2-run-openfold3",
                "predict",
                "--query-json",
                f"{inference_root}/input/query.json",
                "--provenance-marker",
                f"{inference_root}/input/provenance.json",
                "--input-artifact-id",
                prepared,
                "--expected-raw-input-sha256",
                raw_sha256(input_artifact.digest),
                "--output-dir",
                f"{inference_root}/outputs",
                "--checkpoint",
                "/models/openfold3/of3-ob-2025-06-30-174k.pt",
                "--ccd-path",
                "/databases/openfold3/components.bcif",
                "--runner-yaml",
                f"{inference_root}/input/runner.yaml",
                "--base-runner-yaml",
                "/opt/fs2/runtime/openfold3/runner-base.yaml",
                "--num-diffusion-samples",
                "1",
                "--num-model-seeds",
                str(len(parameters.seeds)),
                "--model-seeds",
                seeds,
                "--msa-mode",
                "none",
                "--use-templates",
                "false",
                "--runtime-localization-marker",
                localization_marker,
            ),
            environment=(("FS2_NETWORK_MODE", "offline"), ("HF_HUB_OFFLINE", "1"), ("TRANSFORMERS_OFFLINE", "1")),
            working_directory=inference_root,
            consumes=(prepared,),
            produces=result,
            collector_id="openfold3-result-collector-v1",
            validator_id=VALIDATOR_ID,
            handoff_name=None,
            max_output_artifacts=len(parameters.seeds) + 1,
            max_output_bytes=8 * 1024 * 1024 * 1024,
            materializations=(
                ArtifactMaterialization(
                    prepared, f"{inference_root}/input", MaterializationMode.EXTRACT_TAR, compression="zstd"
                ),
            ),
            runtime_artifacts=(MODEL_ARTIFACT, REFERENCE_ARTIFACT),
            runtime_mounts=inference_mounts,
        ),
    )
    return build_execution_plan(
        model_id=MODEL_ID,
        variant_id=VARIANT_ID,
        source_revision=SOURCE_REVISION,
        request=request,
        profile=profile,
        expansions=None,
        invocations=invocations,
        required_model_artifacts=(MODEL_ARTIFACT, REFERENCE_ARTIFACT),
    )


def collect_data(invocation: StageInvocation, workspace: Path) -> CollectedStageOutput:
    return collect_handoff(
        invocation,
        workspace,
        filename="handoff.tar.zst",
        semantic_type="openfold3-prepared-input/v1",
        media_type="application/x-tar",
        compression="zstd",
    )


def collect_result(invocation: StageInvocation, workspace: Path) -> CollectedStageOutput:
    seeds = tuple(int(value) for value in invocation.argv[invocation.argv.index("--model-seeds") + 1].split(","))
    return collect_confidence_envelope(
        invocation,
        workspace,
        expected_runtime_id=MODEL_ID,
        expected_model_revision=SOURCE_REVISION,
        expected_seeds=seeds,
        expected_samples_per_seed=1,
    )
