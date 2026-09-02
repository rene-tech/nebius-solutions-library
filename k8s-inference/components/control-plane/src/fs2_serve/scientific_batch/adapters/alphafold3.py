"""Native academic AlphaFold 3 adapter with separate CPU and GPU execution."""

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
    canonical_digest,
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
REFERENCE_ARTIFACT = "alphafold3-reference-databases"
PARAMETERS_SHA256 = "74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff"
VALIDATOR_ID = "alphafold3-upstream-v3-0-4"
ADMISSION_BLOCKER = "LicenseAcceptancePending"


@dataclass(frozen=True, slots=True)
class Parameters:
    seeds: tuple[int, ...]
    samples: int

    @classmethod
    def parse(cls, value: object) -> Parameters:
        item = strict_object(
            value, required=frozenset({"model_seeds", "num_diffusion_samples"}), label="AlphaFold 3 parameters"
        )
        raw = item["model_seeds"]
        if not isinstance(raw, list) or not 1 <= len(raw) <= 16:
            raise ScientificAdapterError("model_seeds must contain 1..16 values")
        seeds = tuple(bounded_int(seed, minimum=0, maximum=2**31 - 1, label="model seed") for seed in raw)
        if len(set(seeds)) != len(seeds):
            raise ScientificAdapterError("model_seeds must be unique")
        return cls(
            seeds, bounded_int(item["num_diffusion_samples"], minimum=1, maximum=16, label="num_diffusion_samples")
        )


def _request(value: object) -> tuple[PublicRunRequest, Parameters]:
    request = parse_public_request(value, maximum_input_bytes=64 * 1024 * 1024)
    return request, Parameters.parse(request.parameters)


def _candidate_plan(
    profile: Mapping[str, object],
    request_value: object,
    *,
    operation_id: str,
    authorization_receipt_sha256: str | None = None,
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
        profile, artifact_id=PARAMETERS_ARTIFACT, content_sha256=PARAMETERS_SHA256, required_file="af3.bin.zst"
    )
    assert_artifact_requirement(
        profile, artifact_id=REFERENCE_ARTIFACT, content_sha256=None
    )
    data_root, inference_root = stage_workspace("data-pipeline", "main"), stage_workspace("inference", "main")
    enriched, result = (
        logical_stage_artifact(operation_id, "data-pipeline", "main"),
        logical_stage_artifact(operation_id, "inference", "main"),
    )
    common = ("/opt/alphafold3-venv/bin/python", "/opt/alphafold3/run_alphafold.py")
    invocations = (
        StageInvocation(
            "data-pipeline",
            "main",
            common
            + (
                f"--json_path={data_root}/input.json",
                f"--output_dir={data_root}/outputs",
                "--db_dir=/databases",
                "--run_data_pipeline",
                "--norun_inference",
                "--force_output_dir",
            ),
            (),
            data_root,
            (request.input_manifest.artifact_id,),
            enriched,
            (
                ArtifactMaterialization(
                    request.input_manifest.artifact_id, f"{data_root}/input.json", MaterializationMode.COPY_FILE
                ),
            ),
            (REFERENCE_ARTIFACT,),
            (
                RuntimeArtifactMount(
                    REFERENCE_ARTIFACT,
                    "/databases",
                    expected_content_sha256=None,
                    authorization_receipt_sha256=authorization_receipt_sha256,
                    supplemental_groups=(65532,),
                ),
            ),
        ),
        StageInvocation(
            "inference",
            "main",
            common
            + (
                f"--json_path={inference_root}/enriched-input.json",
                f"--output_dir={inference_root}/outputs",
                "--model_dir=/opt/fs2/academic/alphafold3",
                "--norun_data_pipeline",
                "--run_inference",
                "--force_output_dir",
                f"--num_diffusion_samples={parameters.samples}",
            ),
            (("XLA_PYTHON_CLIENT_PREALLOCATE", "true"),),
            inference_root,
            (enriched,),
            result,
            (
                ArtifactMaterialization(
                    enriched, f"{inference_root}/enriched-input.json", MaterializationMode.COPY_FILE
                ),
            ),
            (PARAMETERS_ARTIFACT,),
            (
                RuntimeArtifactMount(
                    PARAMETERS_ARTIFACT,
                    "/opt/fs2/academic/alphafold3",
                    expected_content_sha256=PARAMETERS_SHA256,
                    authorization_receipt_sha256=authorization_receipt_sha256,
                    supplemental_groups=(65532,),
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
        required_model_artifacts=(PARAMETERS_ARTIFACT, REFERENCE_ARTIFACT),
    )


def assert_admissible(*, tenant_id: str, receipt: object) -> str:
    item = strict_object(
        receipt,
        required=frozenset({"schema", "tenant_id", "scope", "parameters_sha256", "authorization_id"}),
        label="AlphaFold 3 access receipt",
    )
    if (
        item["schema"] != "fs2-serve.nebius.ai/academic-asset-access-receipt/v1"
        or item["tenant_id"] != tenant_id
        or item["scope"] != "technical-poc"
        or item["parameters_sha256"] != PARAMETERS_SHA256
    ):
        raise ScientificAdapterError(ADMISSION_BLOCKER)
    authorization_id = item["authorization_id"]
    if not isinstance(authorization_id, str) or not authorization_id:
        raise ScientificAdapterError(ADMISSION_BLOCKER)
    return canonical_digest(item)


def compile_run(
    profile: Mapping[str, object],
    request_value: object,
    *,
    operation_id: str,
    tenant_id: str | None = None,
    access_receipt: object | None = None,
) -> AdapterExecutionPlan:
    if tenant_id is None or access_receipt is None:
        raise ScientificAdapterError(ADMISSION_BLOCKER)
    authorization_digest = assert_admissible(tenant_id=tenant_id, receipt=access_receipt)
    return _candidate_plan(
        profile,
        request_value,
        operation_id=operation_id,
        authorization_receipt_sha256=authorization_digest,
    )


def collect_output(workspace: Path) -> CollectedOutput:
    return collect_structure_outputs(
        workspace,
        structure_globs=("outputs/**/*_model.cif",),
        confidence_globs=("outputs/**/confidence.json", "outputs/**/summary_confidences.json"),
        manifest_id="alphafold3.results",
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
        backend_id="alphafold3-native",
    )
