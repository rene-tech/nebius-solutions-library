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
REFERENCE_ARTIFACT = "openfold3-reference-databases"
RUNNER_CONFIG_ARTIFACT = "openfold3-offline-runner-config"
VALIDATOR_ID = "openfold3-upstream-openbind-v0-5-0"
RELATIONSHIP = "independent-non-equivalent-alternative"
MODEL_SHA256 = "bd43301c011d5f87580d3e8b548658869433e4488399feb03035ba248f8e29e4"


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
        if len(set(seeds)) != len(seeds) or item["msa_mode"] not in {"local", "precomputed"}:
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
        profile, artifact_id=MODEL_ARTIFACT, content_sha256=MODEL_SHA256,
        required_file="of3-ob-2025-06-30-174k.pt"
    )
    assert_artifact_requirement(profile, artifact_id=REFERENCE_ARTIFACT, content_sha256=None)
    assert_artifact_requirement(
        profile,
        artifact_id=RUNNER_CONFIG_ARTIFACT,
        content_sha256=None,
        required_file="runner.yaml",
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
                "python3",
                "/opt/fs2/prepare_openfold3.py",
                "--input",
                f"{data_root}/input.json",
                "--output-dir",
                f"{data_root}/prepared",
                "--msa-mode",
                parameters.msa_mode,
                "--database-dir",
                "/databases",
                "--seeds",
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
            (REFERENCE_ARTIFACT, RUNNER_CONFIG_ARTIFACT),
            (
                RuntimeArtifactMount(REFERENCE_ARTIFACT, "/databases/openfold3"),
                RuntimeArtifactMount(RUNNER_CONFIG_ARTIFACT, "/opt/fs2/artifacts/openfold3-offline-runner-config"),
            ),
        ),
        StageInvocation(
            "inference",
            "main",
            (
                "/usr/local/bin/fs2-run-openfold3",
                "--query-json",
                f"{inference_root}/query.json",
                "--output-dir",
                f"{inference_root}/outputs",
                "--checkpoint",
                "/models/openfold3/of3-ob-2025-06-30-174k.pt",
                "--ccd-path",
                "/databases/openfold3/components.bcif",
                "--runner-yaml",
                "/opt/fs2/artifacts/openfold3-offline-runner-config/runner.yaml",
                "--num-diffusion-samples",
                str(len(parameters.seeds)),
                "--num-model-seeds",
                str(len(parameters.seeds)),
            ),
            (("HF_HUB_OFFLINE", "1"),),
            inference_root,
            (prepared,),
            result,
            (ArtifactMaterialization(prepared, inference_root, MaterializationMode.EXTRACT_TAR, compression="zstd"),),
            (MODEL_ARTIFACT, REFERENCE_ARTIFACT, RUNNER_CONFIG_ARTIFACT),
            (
                RuntimeArtifactMount(MODEL_ARTIFACT, "/models/openfold3", expected_content_sha256=MODEL_SHA256),
                RuntimeArtifactMount(REFERENCE_ARTIFACT, "/databases/openfold3"),
                RuntimeArtifactMount(RUNNER_CONFIG_ARTIFACT, "/opt/fs2/artifacts/openfold3-offline-runner-config"),
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
        required_model_artifacts=(MODEL_ARTIFACT, REFERENCE_ARTIFACT, RUNNER_CONFIG_ARTIFACT),
    )


def collect_output(workspace: Path) -> CollectedOutput:
    return collect_structure_outputs(
        workspace,
        structure_globs=("outputs/**/_model.cif", "outputs/**/*_model.cif"),
        confidence_globs=("outputs/**/confidence.json", "outputs/**/*_confidences_aggregated.json"),
        manifest_id="openfold3.results",
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
    )
