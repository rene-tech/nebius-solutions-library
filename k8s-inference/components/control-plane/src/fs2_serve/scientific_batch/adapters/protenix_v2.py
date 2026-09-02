"""Fail-closed Protenix v2 adapter with exact upstream prep/pred commands."""

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

MODEL_ID = "protenix-v2"
VARIANT_ID = "upstream-v2-0-0"
SOURCE_REPOSITORY = "bytedance/Protenix"
SOURCE_REVISION = "2475421477ab414b571149ad4a875c390ff8a35d"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/protenix-v2-upstream-v2-0-0-parameters/v1"
MODEL_ARTIFACT = "protenix-v2"
REFERENCE_ARTIFACT = "protenix-v2-inference-data-2026-01-29"
VALIDATOR_ID = "protenix-v2-upstream-v2-0-0"
WEIGHTS_REPOSITORY = "TMF001/protenix-v2-weights"
WEIGHTS_REVISION = "653edab28103133512575365130916e3fd23ecc3"
WEIGHTS_SHA256 = "8f931f9774a396b67033d0e58628e1834f4a1448165e04254b40a780b0c0d599"
WEIGHTS_SIZE_BYTES = 1_859_785_497
MANDATORY_COMMON_FILES = (
    "common/components.cif",
    "common/components.cif.rdkit_mol.pkl",
    "common/clusters-by-entity-40.txt",
    "common/obsolete_release_date.csv",
)


@dataclass(frozen=True, slots=True)
class Parameters:
    msa_mode: str
    sample_count: int
    seed: int

    @classmethod
    def parse(cls, value: object) -> Parameters:
        item = strict_object(
            value,
            required=frozenset({"checkpoint", "msa_mode", "sample_count", "seed"}),
            label="Protenix v2 parameters",
        )
        if item["checkpoint"] != MODEL_ARTIFACT or item["msa_mode"] not in {"none", "precomputed"}:
            raise ScientificAdapterError("Protenix requires the exact v2 checkpoint and a supported MSA mode")
        return cls(
            str(item["msa_mode"]),
            bounded_int(item["sample_count"], minimum=1, maximum=16, label="sample_count"),
            bounded_int(item["seed"], minimum=0, maximum=2**31 - 1, label="seed"),
        )


def _request(value: object) -> tuple[PublicRunRequest, Parameters]:
    request = parse_public_request(value, maximum_input_bytes=256 * 1024 * 1024)
    return request, Parameters.parse(request.parameters)


def _candidate_plan(profile: Mapping[str, object], request_value: object, *, operation_id: str) -> AdapterExecutionPlan:
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
    assert_admissible(profile)
    for filename in MANDATORY_COMMON_FILES:
        assert_artifact_requirement(
            profile,
            artifact_id=REFERENCE_ARTIFACT,
            content_sha256=None,
            required_file=filename,
        )
    prep_root, sample_root = stage_workspace("prepare-data", "main"), stage_workspace("sample-structure", "main")
    prepared, result = (
        logical_stage_artifact(operation_id, "prepare-data", "main"),
        logical_stage_artifact(operation_id, "sample-structure", "main"),
    )
    invocations = (
        StageInvocation(
            "prepare-data",
            "main",
            (
                "protenix",
                "prep",
                "-i",
                f"{prep_root}/input.json",
                "-o",
                f"{prep_root}/prepared",
            ),
            (("PROTENIX_ROOT_DIR", "/models/protenix-v2"), ("FS2_NETWORK_MODE", "offline")),
            prep_root,
            (request.input_manifest.artifact_id,),
            prepared,
            (
                ArtifactMaterialization(
                    request.input_manifest.artifact_id, f"{prep_root}/input.json", MaterializationMode.COPY_FILE
                ),
            ),
            (REFERENCE_ARTIFACT,),
            (RuntimeArtifactMount(REFERENCE_ARTIFACT, "/models/protenix-v2/common", sub_path="common"),),
        ),
        StageInvocation(
            "sample-structure",
            "main",
            (
                "protenix",
                "pred",
                "-i",
                f"{sample_root}/prepared.json",
                "-o",
                f"{sample_root}/outputs",
                "-s",
                str(parameters.seed),
                "-n",
                MODEL_ARTIFACT,
                "-e",
                str(parameters.sample_count),
                "--use_msa",
                "true" if parameters.msa_mode == "precomputed" else "false",
                "--use_template",
                "true" if parameters.msa_mode == "precomputed" else "false",
                "--use_default_params",
                "true",
            ),
            (("PROTENIX_ROOT_DIR", "/models/protenix-v2"), ("FS2_NETWORK_MODE", "offline")),
            sample_root,
            (prepared,),
            result,
            (ArtifactMaterialization(prepared, f"{sample_root}/prepared.json", MaterializationMode.COPY_FILE),),
            (MODEL_ARTIFACT, REFERENCE_ARTIFACT),
            (
                RuntimeArtifactMount(
                    MODEL_ARTIFACT,
                    "/models/protenix-v2/checkpoint",
                    sub_path="checkpoint",
                    expected_content_sha256=WEIGHTS_SHA256,
                ),
                RuntimeArtifactMount(REFERENCE_ARTIFACT, "/models/protenix-v2/common", sub_path="common"),
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


def assert_admissible(profile: Mapping[str, object]) -> None:
    artifact = assert_artifact_requirement(
        profile,
        artifact_id=MODEL_ARTIFACT,
        content_sha256=WEIGHTS_SHA256,
        required_file="checkpoint/protenix-v2.pt",
    )
    source = artifact.get("source")
    if (
        artifact.get("total_size_bytes") != WEIGHTS_SIZE_BYTES
        or artifact.get("supply_state") != "third-party-mirror-verified"
        or not isinstance(source, Mapping)
        or source.get("repository") != WEIGHTS_REPOSITORY
        or source.get("revision") != WEIGHTS_REVISION
    ):
        raise ScientificAdapterError("Protenix v2 requires the verified immutable third-party mirror artifact")


def compile_run(profile: Mapping[str, object], request_value: object, *, operation_id: str) -> AdapterExecutionPlan:
    return _candidate_plan(profile, request_value, operation_id=operation_id)


def collect_output(workspace: Path) -> CollectedOutput:
    return collect_structure_outputs(
        workspace,
        structure_globs=("outputs/**/*.cif",),
        confidence_globs=("outputs/confidence.json",),
        manifest_id="protenix-v2.results",
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
