"""Distinct ESMFold2-Fast adapter; MSA modes are rejected at admission."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ..models import (
    AdapterExecutionPlan,
    ArtifactMaterialization,
    MaterializationMode,
    RuntimeArtifactMount,
    StageInvocation,
)
from . import esmfold2
from .common import (
    ArtifactLoader,
    CollectedOutput,
    assert_artifact_requirement,
    assert_profile_identity,
    build_execution_plan,
    logical_stage_artifact,
    parse_public_request,
    stage_workspace,
)
from .secondary_structure import collect_structure_outputs, validate_structure_output

MODEL_ID = "esmfold2-fast"
VARIANT_ID = "biohub-v3-4-0"
SOURCE_REPOSITORY = esmfold2.SOURCE_REPOSITORY
SOURCE_REVISION = esmfold2.SOURCE_REVISION
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/esmfold2-fast-biohub-v3-4-0-parameters/v1"
MODEL_ARTIFACT = "esmfold2-fast"
MODEL_REVISION = "c6c7958d63f5f2f1f0fed0bb9462316f8ccceea6"
ESMC_ARTIFACT = "esmc-6b"
CCD_ARTIFACT = esmfold2.CCD_ARTIFACT
VALIDATOR_ID = "esmfold2-fast-biohub-v3-4-0"
MODEL_SHA256 = "19ceaffb5860acf160ea199599fb719b0566519e4cc2fa7a7aa5ef547942ad63"
CCD_SHA256 = esmfold2.CCD_SHA256


def compile_run(profile: Mapping[str, object], request_value: object, *, operation_id: str) -> AdapterExecutionPlan:
    request = parse_public_request(request_value, maximum_input_bytes=256 * 1024 * 1024)
    parameters = esmfold2.Parameters.parse(request.parameters, fast=True)
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
        profile, artifact_id=ESMC_ARTIFACT, content_sha256=esmfold2.ESMC_SHA256,
        required_file="model.safetensors.index.json"
    )
    assert_artifact_requirement(
        profile,
        artifact_id=CCD_ARTIFACT,
        content_sha256=CCD_SHA256,
        required_file="ccd.pkl",
        required_file_sha256=esmfold2.CCD_FILE_SHA256,
        required_file_size_bytes=417_306_584,
    )
    prepare_root, fold_root = stage_workspace("prepare-input", "main"), stage_workspace("fold", "main")
    prepared = logical_stage_artifact(operation_id, "prepare-input", "main")
    result = logical_stage_artifact(operation_id, "fold", "main")
    invocations = (
        StageInvocation(
            "prepare-input",
            "main",
            (
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
                "/usr/local/bin/fs2-run-esmfold2",
                "fold",
                "--request",
                f"{fold_root}/prepared-input.json",
                "--output",
                f"{fold_root}/outputs/prediction.cif",
                "--model-dir",
                f"/models/{MODEL_ARTIFACT}",
                "--esmc-dir",
                f"/models/{ESMC_ARTIFACT}",
                "--ccd-path",
                "/databases/esmfold2/ccd.pkl",
                "--hardware-mode",
                "h100",
                "--num-loops",
                str(esmfold2.PRODUCTION_NUM_LOOPS),
                "--num-sampling-steps",
                str(esmfold2.PRODUCTION_NUM_SAMPLING_STEPS),
                "--seed",
                str(parameters.seed),
                "--variant",
                MODEL_ID,
            ),
            (("HF_HUB_OFFLINE", "1"), ("TRANSFORMERS_OFFLINE", "1")),
            fold_root,
            (prepared,),
            result,
            (ArtifactMaterialization(prepared, f"{fold_root}/prepared-input.json", MaterializationMode.COPY_FILE),),
            (MODEL_ARTIFACT, ESMC_ARTIFACT, CCD_ARTIFACT),
            (
                RuntimeArtifactMount(MODEL_ARTIFACT, f"/models/{MODEL_ARTIFACT}", expected_content_sha256=MODEL_SHA256),
                RuntimeArtifactMount(
                    ESMC_ARTIFACT,
                    f"/models/{ESMC_ARTIFACT}",
                    expected_content_sha256=esmfold2.ESMC_SHA256,
                ),
                RuntimeArtifactMount(
                    CCD_ARTIFACT,
                    "/databases/esmfold2",
                    expected_content_sha256=CCD_SHA256,
                ),
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
        required_model_artifacts=(MODEL_ARTIFACT, ESMC_ARTIFACT, CCD_ARTIFACT),
    )


def collect_output(workspace: Path) -> CollectedOutput:
    return collect_structure_outputs(
        workspace,
        structure_globs=("outputs/*.cif", "outputs/*.mmcif"),
        confidence_globs=("outputs/confidence.json",),
        manifest_id="esmfold2-fast.results",
        runtime_id=MODEL_ID,
        model_revision=MODEL_REVISION,
        maximum_structures=1,
    )


def validate_output(manifest: object, *, artifact_loader: ArtifactLoader) -> Mapping[str, object]:
    return validate_structure_output(
        manifest,
        artifact_loader=artifact_loader,
        expected_structures=1,
        validator_id=VALIDATOR_ID,
        backend_id=MODEL_ID,
        model_revision=MODEL_REVISION,
    )
