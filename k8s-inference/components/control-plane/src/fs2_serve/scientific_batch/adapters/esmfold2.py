"""ESMFold2 full-checkpoint adapter using the reviewed offline wrapper CLI."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..models import (
    AdapterExecutionPlan,
    ArtifactMaterialization,
    MaterializationMode,
    ScientificInputArtifact,
    StageInvocation,
)
from .common import (
    CollectedOutput,
    ScientificAdapterError,
    assert_profile_identity,
    bounded_int,
    build_execution_plan,
    logical_stage_artifact,
    parse_public_request,
    run_workspace,
    strict_object,
)
from .secondary_structure import (
    collect_confidence_envelope,
    collect_confidence_stage,
    collect_handoff,
    collect_handoff_stage,
)
from .verified_input import verified_manifest_entry

if TYPE_CHECKING:
    from . import CollectedStageOutput

MODEL_ID = "esmfold2"
VARIANT_ID = "biohub-v3-4-0"
SOURCE_REPOSITORY = "Biohub/esm"
SOURCE_REVISION = "827ec128e4cdaf80f7d6f95fb367a08980b34918"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/esmfold2-biohub-v3-4-0-parameters/v1"
INPUT_ARTIFACT_ID = "esmfold2-input"
INPUT_SEMANTIC_TYPE = "esmfold2-input-json/v1"
INPUT_MEDIA_TYPE = "application/json"
MAX_INPUT_BYTES = 256 * 1024 * 1024
REQUIRES_VERIFIED_INPUT_ARTIFACTS = True
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
PREPARE_COLLECTOR_ID = "esmfold2-prepare-collector-v1"
PREPARE_VALIDATOR_ID = "esmfold2-prepare-validator-v1"
RESULT_COLLECTOR_ID = "esmfold2-result-collector-v1"
STAGE_EXECUTION_CONTRACTS: Mapping[str, Mapping[str, object]] = {
    "prepare-input": {
        "collector_id": PREPARE_COLLECTOR_ID,
        "validator_id": PREPARE_VALIDATOR_ID,
        "runtime_artifacts": (),
    },
    "fold": {
        "collector_id": RESULT_COLLECTOR_ID,
        "validator_id": VALIDATOR_ID,
        "runtime_artifacts": (MODEL_ARTIFACT, ESMC_ARTIFACT, CCD_ARTIFACT),
    },
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
    input_artifacts: tuple[ScientificInputArtifact, ...] | None = None,
) -> AdapterExecutionPlan:
    request = parse_public_request(request_value, maximum_input_bytes=MAX_INPUT_BYTES)
    parameters = Parameters.parse(request.parameters)
    model_input = verified_manifest_entry(
        request,
        input_artifacts,
        logical_artifact_id=INPUT_ARTIFACT_ID,
        semantic_type=INPUT_SEMANTIC_TYPE,
        media_type=INPUT_MEDIA_TYPE,
        compressions=frozenset({None, "none"}),
        maximum_bytes=MAX_INPUT_BYTES,
        label="ESMFold2",
    )
    assert_profile_identity(
        profile,
        model_id=MODEL_ID,
        repository=SOURCE_REPOSITORY,
        revision=SOURCE_REVISION,
        parameter_schema=PARAMETER_SCHEMA,
        request=request,
    )
    prepare_root = run_workspace(MODEL_ID, operation_id, "prepare-input-main")
    fold_root = run_workspace(MODEL_ID, operation_id, "fold-main")
    prepared = logical_stage_artifact(operation_id, "prepare-input", "main")
    result = logical_stage_artifact(operation_id, "fold", "main")
    localization_marker = f"{fold_root}/.fs2/runtime-localization.json"
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
            environment=(
                ("FS2_NETWORK_MODE", "offline"),
                ("FS2_SCIENTIFIC_COLLECTOR_ID", PREPARE_COLLECTOR_ID),
                ("FS2_SCIENTIFIC_VALIDATOR_ID", PREPARE_VALIDATOR_ID),
            ),
            working_directory=prepare_root,
            consumes=(model_input.logical_artifact_id,),
            produces=prepared,
            collector_id=PREPARE_COLLECTOR_ID,
            validator_id=PREPARE_VALIDATOR_ID,
            handoff_name="prepared-input",
            max_output_artifacts=1,
            max_output_bytes=16 * 1024 * 1024,
            materializations=(
                ArtifactMaterialization(
                    model_input.logical_artifact_id,
                    f"{prepare_root}/input-manifest.json",
                    MaterializationMode.COPY_FILE,
                    compression=model_input.compression,
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
                ("FS2_SCIENTIFIC_COLLECTOR_ID", RESULT_COLLECTOR_ID),
                ("FS2_SCIENTIFIC_VALIDATOR_ID", VALIDATOR_ID),
            ),
            working_directory=fold_root,
            consumes=(prepared,),
            produces=result,
            collector_id=RESULT_COLLECTOR_ID,
            validator_id=VALIDATOR_ID,
            max_output_artifacts=3,
            max_output_bytes=2 * 1024 * 1024 * 1024,
            materializations=(
                ArtifactMaterialization(prepared, f"{fold_root}/prepared-input.json", MaterializationMode.COPY_FILE),
            ),
            runtime_artifacts=(MODEL_ARTIFACT, ESMC_ARTIFACT, CCD_ARTIFACT),
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


def collect_prepare(workspace: Path) -> CollectedOutput:
    return collect_handoff(
        workspace,
        filename="prepared-input.json",
        name="prepared-input",
        semantic_type="esmfold2-prepared-input/v1",
        media_type="application/json",
        maximum_bytes=16 * 1024 * 1024,
    )


def collect_result(request_value: object, workspace: Path) -> CollectedOutput:
    request = parse_public_request(request_value, maximum_input_bytes=256 * 1024 * 1024)
    seed = Parameters.parse(request.parameters).seed
    return collect_confidence_envelope(
        workspace,
        validator_id=VALIDATOR_ID,
        expected_runtime_id=MODEL_ID,
        expected_model_revision=MODEL_REVISION,
        expected_seeds=(seed,),
        expected_samples_per_seed=1,
        maximum_total_bytes=2 * 1024 * 1024 * 1024,
    )


def collect_stage_output(collector_id: str, request_value: object, workspace: Path) -> CollectedOutput:
    """Dispatch only collector identities frozen into this adapter's plan."""

    if collector_id == PREPARE_COLLECTOR_ID:
        return collect_prepare(workspace)
    if collector_id == RESULT_COLLECTOR_ID:
        return collect_result(request_value, workspace)
    raise ScientificAdapterError(f"unsupported ESMFold2 collector identity {collector_id!r}")


def _argument(invocation: StageInvocation, name: str) -> str:
    if invocation.argv.count(name) != 1 or invocation.argv.index(name) + 1 >= len(invocation.argv):
        raise ScientificAdapterError(f"ESMFold2 invocation has no exact {name} argument")
    return invocation.argv[invocation.argv.index(name) + 1]


def collect_companion_output(invocation: StageInvocation, workspace: Path) -> CollectedStageOutput:
    """Collect from the immutable invocation used by the production companion."""

    if invocation.collector_id == PREPARE_COLLECTOR_ID:
        if invocation.stage_id != "prepare-input" or invocation.validator_id != PREPARE_VALIDATOR_ID:
            raise ScientificAdapterError("ESMFold2 prepare collector received another stage contract")
        return collect_handoff_stage(
            workspace,
            filename="prepared-input.json",
            name="prepared-input",
            semantic_type="esmfold2-prepared-input/v1",
            media_type="application/json",
            maximum_bytes=16 * 1024 * 1024,
            validator_id=invocation.validator_id,
        )
    if invocation.collector_id == RESULT_COLLECTOR_ID:
        if invocation.stage_id != "fold" or invocation.validator_id != VALIDATOR_ID:
            raise ScientificAdapterError("ESMFold2 result collector received another stage contract")
        try:
            seed = int(_argument(invocation, "--seed"))
        except ValueError as error:
            raise ScientificAdapterError("ESMFold2 invocation seed is invalid") from error
        bounded_int(seed, minimum=0, maximum=2**31 - 1, label="seed")
        if _argument(invocation, "--variant") != MODEL_ID:
            raise ScientificAdapterError("ESMFold2 invocation variant differs")
        return collect_confidence_stage(
            workspace,
            validator_id=invocation.validator_id,
            expected_runtime_id=MODEL_ID,
            expected_model_revision=MODEL_REVISION,
            expected_seeds=(seed,),
            expected_samples_per_seed=1,
            maximum_total_bytes=invocation.max_output_bytes,
        )
    raise ScientificAdapterError(f"unsupported ESMFold2 collector identity {invocation.collector_id!r}")
