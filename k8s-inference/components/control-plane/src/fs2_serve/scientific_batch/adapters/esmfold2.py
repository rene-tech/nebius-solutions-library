"""ESMFold2 full-checkpoint adapter using the reviewed offline wrapper CLI."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

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

MODEL_ID = "esmfold2"
VARIANT_ID = "biohub-v3-4-0"
SOURCE_REPOSITORY = "Biohub/esm"
SOURCE_REVISION = "827ec128e4cdaf80f7d6f95fb367a08980b34918"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/esmfold2-biohub-v3-4-0-parameters/v1"
MODEL_ARTIFACT = "esmfold2"
ESMC_ARTIFACT = "esmc-6b"
VALIDATOR_ID = "esmfold2-biohub-v3-4-0"
MODEL_SHA256 = "3bb081b48f0ccf70ee38b86a3c7e014554d4bf9e4148e709c2d97519e28c4b80"
ESMC_SHA256 = "8f21da30919b3e0d7af9ec6c4b9879542234d77d42ce061fef029397a4d39758"
PRODUCTION_NUM_LOOPS = 20
PRODUCTION_NUM_SAMPLING_STEPS = 200


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
        mode = item["mode"]
        allowed = {"single-sequence"} if fast else {"single-sequence", "precomputed-msa"}
        if mode not in allowed:
            raise ScientificAdapterError("ESMFold2-Fast rejects MSA inputs" if fast else "ESMFold2 mode is unsupported")
        return cls(
            sequence,
            str(mode),
            bounded_int(item.get("seed", 0), minimum=0, maximum=2**31 - 1, label="seed"),
        )


def _request(value: object) -> tuple[PublicRunRequest, Parameters]:
    request = parse_public_request(value, maximum_input_bytes=256 * 1024 * 1024)
    return request, Parameters.parse(request.parameters)


def compile_run(profile: Mapping[str, object], request_value: object, *, operation_id: str) -> AdapterExecutionPlan:
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
        profile, artifact_id=MODEL_ARTIFACT, content_sha256=MODEL_SHA256, required_file="ccd.pkl"
    )
    assert_artifact_requirement(
        profile, artifact_id=ESMC_ARTIFACT, content_sha256=ESMC_SHA256, required_file="model.safetensors.index.json"
    )
    prepare_root = stage_workspace("prepare-input", "main")
    fold_root = stage_workspace("fold", "main")
    prepared = logical_stage_artifact(operation_id, "prepare-input", "main")
    result = logical_stage_artifact(operation_id, "fold", "main")
    invocations = (
        StageInvocation(
            "prepare-input",
            "main",
            (
                "python3",
                "/opt/fs2/run_esmfold2.py",
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
            (("FS2_NETWORK_MODE", "offline"),),
            prepare_root,
            (request.input_manifest.artifact_id,),
            prepared,
            (
                ArtifactMaterialization(
                    request.input_manifest.artifact_id,
                    f"{prepare_root}/input-manifest.json",
                    MaterializationMode.COPY_FILE,
                ),
            ),
        ),
        StageInvocation(
            "fold",
            "main",
            (
                "python3",
                "/opt/fs2/run_esmfold2.py",
                "fold",
                "--input",
                f"{fold_root}/prepared-input.json",
                "--output-dir",
                f"{fold_root}/outputs",
                "--model-dir",
                f"/models/{MODEL_ARTIFACT}",
                "--esmc-dir",
                f"/models/{ESMC_ARTIFACT}",
                "--ccd-path",
                f"/models/{MODEL_ARTIFACT}/ccd.pkl",
                "--variant",
                MODEL_ID,
                "--num-loops",
                str(PRODUCTION_NUM_LOOPS),
                "--num-sampling-steps",
                str(PRODUCTION_NUM_SAMPLING_STEPS),
            ),
            (("HF_HUB_OFFLINE", "1"), ("TRANSFORMERS_OFFLINE", "1")),
            fold_root,
            (prepared,),
            result,
            (ArtifactMaterialization(prepared, f"{fold_root}/prepared-input.json", MaterializationMode.COPY_FILE),),
            (MODEL_ARTIFACT, ESMC_ARTIFACT),
            (
                RuntimeArtifactMount(MODEL_ARTIFACT, f"/models/{MODEL_ARTIFACT}", expected_content_sha256=MODEL_SHA256),
                RuntimeArtifactMount(ESMC_ARTIFACT, f"/models/{ESMC_ARTIFACT}", expected_content_sha256=ESMC_SHA256),
            ),
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
        required_model_artifacts=(MODEL_ARTIFACT, ESMC_ARTIFACT),
    )


def collect_output(workspace: Path) -> CollectedOutput:
    return collect_structure_outputs(
        workspace,
        structure_globs=("outputs/*.cif", "outputs/*.mmcif"),
        confidence_globs=("outputs/confidence.json",),
        manifest_id="esmfold2.results",
        maximum_structures=1,
    )


def validate_output(manifest: object, *, artifact_loader: ArtifactLoader) -> Mapping[str, object]:
    return validate_structure_output(
        manifest,
        artifact_loader=artifact_loader,
        expected_structures=1,
        validator_id=VALIDATOR_ID,
        backend_id=MODEL_ID,
    )
