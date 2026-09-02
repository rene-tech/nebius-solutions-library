"""OpenFold3 OpenBind-0 adapter, explicitly non-equivalent to native AF3."""

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

MODEL_ID = "openfold3"
VARIANT_ID = "upstream-openbind-v0-5-0"
SOURCE_REPOSITORY = "aqlaboratory/openfold-3"
SOURCE_REVISION = "c4771653c5d0a3ebb0b3af71b05efd64bc44ee86"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/openfold3-upstream-openbind-v0-5-0-parameters/v1"
MODEL_ARTIFACT = "openfold3-openbind-0"
REFERENCE_ARTIFACT = "openfold3-components-bcif"
BASE_RUNNER_CONFIG = "/opt/fs2/runtime/openfold3/runner-base.yaml"
VALIDATOR_ID = "openfold3-upstream-openbind-v0-5-0"
RELATIONSHIP = "independent-non-equivalent-alternative"
MODEL_SHA256 = "bd43301c011d5f87580d3e8b548658869433e4488399feb03035ba248f8e29e4"
MODEL_CONTENT_SHA256 = "f954e2f2e3d0bdba297ac8009f6d590b3e2c28ca2985742c9bbd8167f276f6b5"
REFERENCE_CONTENT_SHA256 = "ff75f66793c11d7cb63531c758b210fa6fe33d5a39378bb0ab89094278e95e3b"
REFERENCE_FILE_SHA256 = "473d845c8b250b188dbed9bf505ae206692a178a2a7c4869bf8f9de707ffcc0c"


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
        if len(set(seeds)) != len(seeds) or item["msa_mode"] != "none":
            raise ScientificAdapterError("OpenFold3 seeds or MSA mode are invalid")
        return cls(seeds, str(item["msa_mode"]))


def compile_run(profile: Mapping[str, object], request_value: object, *, operation_id: str) -> AdapterExecutionPlan:
    request = parse_public_request(request_value, maximum_input_bytes=256 * 1024 * 1024)
    parameters = Parameters.parse(request.parameters)
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
        artifact_id=MODEL_ARTIFACT,
        content_sha256=MODEL_CONTENT_SHA256,
        required_file="of3-ob-2025-06-30-174k.pt",
        required_file_sha256=MODEL_SHA256,
        required_file_size_bytes=2_287_872_989,
    )
    assert_artifact_requirement(
        profile,
        artifact_id=REFERENCE_ARTIFACT,
        content_sha256=REFERENCE_CONTENT_SHA256,
        required_file="components.bcif",
        required_file_sha256=REFERENCE_FILE_SHA256,
        required_file_size_bytes=63_393_643,
    )
    data_root, inference_root = stage_workspace("data-pipeline", "main"), stage_workspace("inference", "main")
    prepared, result = (
        logical_stage_artifact(operation_id, "data-pipeline", "main"),
        logical_stage_artifact(operation_id, "inference", "main"),
    )
    seed_csv = ",".join(str(seed) for seed in parameters.seeds)
    invocations = (
        StageInvocation(
            "data-pipeline",
            "main",
            (
                "/usr/local/bin/fs2-run-openfold3",
                "prepare",
                "--input-manifest",
                f"{data_root}/input.json",
                "--query-json",
                f"{data_root}/prepared/query.json",
                "--base-runner-yaml",
                BASE_RUNNER_CONFIG,
                "--runner-yaml",
                f"{data_root}/prepared/runner.yaml",
                "--provenance-marker",
                f"{data_root}/prepared/provenance.json",
                "--handoff-tar",
                f"{data_root}/prepared.tar.zst",
                "--output-artifact-id",
                prepared,
                "--raw-input-sha256",
                request.input_manifest.sha256,
                "--msa-mode",
                parameters.msa_mode,
                "--model-seeds",
                seed_csv,
                "--offline",
            ),
            (("FS2_NETWORK_MODE", "offline"),),
            data_root,
            (request.input_manifest.artifact_id,),
            prepared,
            (
                ArtifactMaterialization(
                    request.input_manifest.artifact_id, f"{data_root}/input.json", MaterializationMode.COPY_FILE
                ),
            ),
            (),
            (),
        ),
        StageInvocation(
            "inference",
            "main",
            (
                "/usr/local/bin/fs2-run-openfold3",
                "predict",
                "--query-json",
                f"{inference_root}/input/query.json",
                "--provenance-marker",
                f"{inference_root}/input/provenance.json",
                "--input-artifact-id",
                prepared,
                "--expected-raw-input-sha256",
                request.input_manifest.sha256,
                "--output-dir",
                f"{inference_root}/outputs",
                "--checkpoint",
                "/models/openfold3/of3-ob-2025-06-30-174k.pt",
                "--ccd-path",
                "/databases/openfold3/components.bcif",
                "--runner-yaml",
                f"{inference_root}/runner.yaml",
                "--base-runner-yaml",
                BASE_RUNNER_CONFIG,
                "--num-diffusion-samples",
                "1",
                "--num-model-seeds",
                str(len(parameters.seeds)),
                "--model-seeds",
                seed_csv,
                "--msa-mode",
                parameters.msa_mode,
                "--use-templates",
                "false",
            ),
            (("HF_HUB_OFFLINE", "1"),),
            inference_root,
            (prepared,),
            result,
            (
                ArtifactMaterialization(
                    prepared,
                    f"{inference_root}/input",
                    MaterializationMode.EXTRACT_TAR,
                    compression="zstd",
                    expected_members=("query.json", "provenance.json"),
                ),
            ),
            (MODEL_ARTIFACT, REFERENCE_ARTIFACT),
            (
                RuntimeArtifactMount(
                    MODEL_ARTIFACT,
                    "/models/openfold3",
                    expected_content_sha256=MODEL_CONTENT_SHA256,
                ),
                RuntimeArtifactMount(
                    REFERENCE_ARTIFACT,
                    "/databases/openfold3",
                    expected_content_sha256=REFERENCE_CONTENT_SHA256,
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
        required_model_artifacts=(MODEL_ARTIFACT, REFERENCE_ARTIFACT),
    )


def collect_output(workspace: Path) -> CollectedOutput:
    return collect_structure_outputs(
        workspace,
        structure_globs=("outputs/**/*_model.cif",),
        confidence_globs=("outputs/confidence.json",),
        manifest_id="openfold3.results",
        runtime_id=MODEL_ID,
        model_revision=SOURCE_REVISION,
        maximum_structures=16,
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
