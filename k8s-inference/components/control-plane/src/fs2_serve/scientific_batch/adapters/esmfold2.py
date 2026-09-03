"""ESMFold2 full-checkpoint adapter using the reviewed offline wrapper CLI."""

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
    run_workspace,
    runtime_artifact_mount,
    strict_object,
)
from .secondary_structure import collect_confidence_envelope, collect_handoff

MODEL_ID = "esmfold2"
VARIANT_ID = "biohub-v3-4-0"
SOURCE_REPOSITORY = "Biohub/esm"
SOURCE_REVISION = "827ec128e4cdaf80f7d6f95fb367a08980b34918"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/esmfold2-biohub-v3-4-0-parameters/v1"
MODEL_ARTIFACT = "esmfold2-trunk"
ESMC_ARTIFACT = "esmc-6b"
CCD_ARTIFACT = "esmfold2-ccd"
VALIDATOR_ID = "esmfold2-biohub-v3-4-0"
MODEL_SHA256 = "136a3580c01cc055ae5a1278bae056e5150a5441ddb89dfbafb9f4e88d763a0c"
MODEL_REVISION = "8fc3ff471022fdce52c77030685eb775de0c00a3"
ESMC_SHA256 = "8f21da30919b3e0d7af9ec6c4b9879542234d77d42ce061fef029397a4d39758"
CCD_MANIFEST_SHA256 = "b1c2fe19204c57f7a7cca6ab4cb0cb420b99312fff424ef2e405fc8234b7616e"
CCD_FILE_SHA256 = "9ff44b1927c6b9198e38ffe0928706827a09a350c15530beeeabebfa88038fc5"
PRODUCTION_NUM_LOOPS = 20
PRODUCTION_NUM_SAMPLING_STEPS = 200
STAGE_EXECUTION_CONTRACTS: Mapping[str, StageExecutionContract] = {
    "prepare-input": StageExecutionContract(
        "esmfold2-prepare-collector-v1", "esmfold2-prepare-validator-v1", ()
    ),
    "fold": StageExecutionContract(
        "esmfold2-result-collector-v1", VALIDATOR_ID, (MODEL_ARTIFACT, ESMC_ARTIFACT, CCD_ARTIFACT)
    ),
}


@dataclass(frozen=True, slots=True)
class Parameters:
    sequence: str
    mode: str
    seed: int

    @classmethod
    def parse(cls, value: object, *, fast: bool = False) -> Parameters:
        item = strict_object(
            value,
            required=frozenset({"sequence", "mode"}),
            optional=frozenset({"seed"}),
            label="ESMFold2 parameters",
        )
        sequence = item["sequence"]
        if (
            not isinstance(sequence, str)
            or not 1 <= len(sequence) <= 4096
            or set(sequence) - set("ACDEFGHIKLMNPQRSTVWYX")
        ):
            raise ScientificAdapterError("sequence must contain 1..4096 supported amino-acid symbols")
        allowed = {"single-sequence"} if fast else {"single-sequence", "precomputed-msa"}
        if item["mode"] not in allowed:
            raise ScientificAdapterError("ESMFold2-Fast rejects MSA inputs" if fast else "ESMFold2 mode is unsupported")
        return cls(
            sequence=sequence,
            mode=str(item["mode"]),
            seed=bounded_int(item.get("seed", 0), minimum=0, maximum=2**31 - 1, label="seed"),
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
    assert_artifact_requirement(
        profile, artifact_id=MODEL_ARTIFACT, content_sha256=MODEL_SHA256, required_file="model.safetensors"
    )
    assert_artifact_requirement(
        profile, artifact_id=ESMC_ARTIFACT, content_sha256=ESMC_SHA256, required_file="model.safetensors.index.json"
    )
    assert_artifact_requirement(
        profile,
        artifact_id=CCD_ARTIFACT,
        content_sha256=CCD_MANIFEST_SHA256,
        required_file="ccd.pkl",
    )
    prepare_root = run_workspace(MODEL_ID, operation_id, "prepare-input-main")
    fold_root = run_workspace(MODEL_ID, operation_id, "fold-main")
    prepared = logical_stage_artifact(operation_id, "prepare-input", "main")
    result = logical_stage_artifact(operation_id, "fold", "main")
    localization_marker = f"{fold_root}/.fs2/runtime-localization.json"
    fold_mounts = (
        runtime_artifact_mount(
            profile,
            artifact_id=MODEL_ARTIFACT,
            mount_path="/models/esmfold2",
        ),
        runtime_artifact_mount(
            profile,
            artifact_id=ESMC_ARTIFACT,
            mount_path="/models/esmc-6b",
        ),
        runtime_artifact_mount(
            profile,
            artifact_id=CCD_ARTIFACT,
            mount_path="/databases/esmfold2",
        ),
    )
    invocations = (
        StageInvocation(
            stage_id="prepare-input",
            shard_id="main",
            argv=(
                "/usr/local/bin/fs2-run-esmfold2",
                "prepare-input",
                "--input-manifest",
                f"{prepare_root}/input-manifest.json",
                "--output",
                f"{prepare_root}/prepared-input.json",
                "--sequence",
                parameters.sequence,
                "--mode",
                parameters.mode,
                "--seed",
                str(parameters.seed),
            ),
            environment=(("FS2_NETWORK_MODE", "offline"),),
            working_directory=prepare_root,
            consumes=(input_artifact.logical_artifact_id,),
            produces=prepared,
            collector_id="esmfold2-prepare-collector-v1",
            validator_id="esmfold2-prepare-validator-v1",
            handoff_name="prepared-input",
            max_output_artifacts=1,
            max_output_bytes=16 * 1024 * 1024,
            materializations=(
                ArtifactMaterialization(
                    input_artifact.logical_artifact_id,
                    f"{prepare_root}/input-manifest.json",
                    MaterializationMode.COPY_FILE,
                ),
            ),
        ),
        StageInvocation(
            stage_id="fold",
            shard_id="main",
            argv=(
                "/usr/local/bin/fs2-run-esmfold2",
                "fold",
                "--input",
                f"{fold_root}/prepared-input.json",
                "--output-dir",
                f"{fold_root}/outputs",
                "--model-dir",
                "/models/esmfold2",
                "--esmc-dir",
                "/models/esmc-6b",
                "--ccd-path",
                "/databases/esmfold2/ccd.pkl",
                "--variant",
                MODEL_ID,
                "--hardware-mode",
                "h100",
                "--esmc-precision",
                "bf16",
                "--num-loops",
                str(PRODUCTION_NUM_LOOPS),
                "--num-sampling-steps",
                str(PRODUCTION_NUM_SAMPLING_STEPS),
                "--seed",
                str(parameters.seed),
                "--complex-id",
                "fs2-result",
                "--runtime-localization-marker",
                localization_marker,
            ),
            environment=(
                ("FS2_NETWORK_MODE", "offline"),
                ("FS2_MODEL_DIR", "/models/esmfold2"),
                ("FS2_ESMC_MODEL_DIR", "/models/esmc-6b"),
                ("ESMCFOLD_CCD_PATH", "/databases/esmfold2/ccd.pkl"),
                ("HF_HUB_OFFLINE", "1"),
                ("TRANSFORMERS_OFFLINE", "1"),
            ),
            working_directory=fold_root,
            consumes=(prepared,),
            produces=result,
            collector_id="esmfold2-result-collector-v1",
            validator_id=VALIDATOR_ID,
            handoff_name=None,
            max_output_artifacts=2,
            max_output_bytes=2 * 1024 * 1024 * 1024,
            materializations=(
                ArtifactMaterialization(prepared, f"{fold_root}/prepared-input.json", MaterializationMode.COPY_FILE),
            ),
            runtime_artifacts=(MODEL_ARTIFACT, ESMC_ARTIFACT, CCD_ARTIFACT),
            runtime_mounts=fold_mounts,
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
        required_model_artifacts=(MODEL_ARTIFACT, ESMC_ARTIFACT, CCD_ARTIFACT),
    )


def collect_prepare(invocation: StageInvocation, workspace: Path) -> CollectedStageOutput:
    return collect_handoff(
        invocation,
        workspace,
        filename="prepared-input.json",
        semantic_type="esmfold2-prepared-input/v1",
        media_type="application/json",
    )


def collect_result(invocation: StageInvocation, workspace: Path) -> CollectedStageOutput:
    seed = int(invocation.argv[invocation.argv.index("--seed") + 1])
    return collect_confidence_envelope(
        invocation,
        workspace,
        expected_runtime_id=MODEL_ID,
        expected_model_revision=MODEL_REVISION,
        expected_seeds=(seed,),
        expected_samples_per_seed=1,
    )
