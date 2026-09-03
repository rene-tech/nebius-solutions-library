"""Fail-closed Protenix v2 adapter for one canonical offline artifact tree."""

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

MODEL_ID = "protenix-v2"
VARIANT_ID = "upstream-v2-0-0"
SOURCE_REPOSITORY = "bytedance/Protenix"
SOURCE_REVISION = "2475421477ab414b571149ad4a875c390ff8a35d"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/protenix-v2-upstream-v2-0-0-parameters/v1"
MODEL_ARTIFACT = "protenix-v2"
VALIDATOR_ID = "protenix-v2-upstream-v2-0-0"
WEIGHTS_REPOSITORY = "TMF001/protenix-v2-weights"
WEIGHTS_REVISION = "653edab28103133512575365130916e3fd23ecc3"
OUTPUT_MODEL_REVISION = f"{WEIGHTS_REPOSITORY}@{WEIGHTS_REVISION}"
COMPOSITE_MANIFEST_SHA256 = "5e1c3b548af40752bb15f9f2ba06590e20e2b165e3fe9ab3fa99af9977574d48"
LOCALIZATION_MANIFEST_SHA256 = "a093d28ecfc8374f143cc32ff713b0e6ad1124c095dbbca5af6e51b4f7dcc6b7"
WEIGHTS_SHA256 = "8f931f9774a396b67033d0e58628e1834f4a1448165e04254b40a780b0c0d599"
WEIGHTS_SIZE_BYTES = 1_859_785_497
COMPOSITE_SIZE_BYTES = 2_514_897_184
MANDATORY_FILES = (
    ".fs2-manifest-sha256",
    "checkpoint/protenix-v2.pt",
    "common/clusters-by-entity-40.txt",
    "common/components.cif",
    "common/components.cif.rdkit_mol.pkl",
    "common/obsolete_release_date.csv",
    "manifest.json",
)
STAGE_EXECUTION_CONTRACTS: Mapping[str, StageExecutionContract] = {
    "prepare-data": StageExecutionContract(
        "protenix-v2-prep-collector-v1", "protenix-v2-prep-validator-v1", (MODEL_ARTIFACT,)
    ),
    "sample-structure": StageExecutionContract(
        "protenix-v2-result-collector-v1", VALIDATOR_ID, (MODEL_ARTIFACT,)
    ),
}


@dataclass(frozen=True, slots=True)
class Parameters:
    msa_mode: str
    sample_count: int
    seeds: tuple[int, ...]

    @classmethod
    def parse(cls, value: object) -> Parameters:
        item = strict_object(
            value,
            required=frozenset({"checkpoint", "msa_mode", "sample_count", "model_seeds"}),
            label="Protenix v2 parameters",
        )
        if item["checkpoint"] != MODEL_ARTIFACT:
            raise ScientificAdapterError("Protenix v2 requires the exact verified v2 checkpoint")
        if item["msa_mode"] != "none":
            raise ScientificAdapterError("Protenix v2 currently admits only the relocatable no-MSA lane")
        raw = item["model_seeds"]
        if not isinstance(raw, list) or not 1 <= len(raw) <= 16:
            raise ScientificAdapterError("model_seeds must contain 1..16 values")
        seeds = tuple(bounded_int(seed, minimum=0, maximum=2**31 - 1, label="model seed") for seed in raw)
        if len(set(seeds)) != len(seeds):
            raise ScientificAdapterError("model_seeds must be unique")
        return cls(
            msa_mode="none",
            sample_count=bounded_int(item["sample_count"], minimum=1, maximum=16, label="sample_count"),
            seeds=seeds,
        )


def assert_admissible(profile: Mapping[str, object]) -> None:
    artifact = assert_artifact_requirement(
        profile, artifact_id=MODEL_ARTIFACT, content_sha256=COMPOSITE_MANIFEST_SHA256
    )
    source, file_manifest = artifact.get("source"), artifact.get("file_manifest")
    if (
        artifact.get("total_size_bytes") != COMPOSITE_SIZE_BYTES
        or artifact.get("supply_state") != "third-party-mirror-verified"
        or not isinstance(source, Mapping)
        or source.get("repository") != WEIGHTS_REPOSITORY
        or source.get("revision") != WEIGHTS_REVISION
        or not isinstance(file_manifest, list)
        or {item.get("path") for item in file_manifest if isinstance(item, Mapping)} != set(MANDATORY_FILES)
    ):
        raise ScientificAdapterError("Protenix v2 requires the exact seven-file composite artifact")
    weight = next(
        item for item in file_manifest if isinstance(item, Mapping) and item.get("path") == "checkpoint/protenix-v2.pt"
    )
    if weight.get("sha256") != WEIGHTS_SHA256 or weight.get("size_bytes") != WEIGHTS_SIZE_BYTES:
        raise ScientificAdapterError("Protenix v2 composite does not bind the verified v2 checkpoint bytes")


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
    assert_admissible(profile)
    prep_root = run_workspace(MODEL_ID, operation_id, "prepare-data-main")
    pred_root = run_workspace(MODEL_ID, operation_id, "sample-structure-main")
    prepared = logical_stage_artifact(operation_id, "prepare-data", "main")
    result = logical_stage_artifact(operation_id, "sample-structure", "main")
    seeds = ",".join(str(seed) for seed in parameters.seeds)
    prep_localization_marker = f"{prep_root}/.fs2/runtime-localization.json"
    pred_localization_marker = f"{pred_root}/.fs2/runtime-localization.json"
    model_mount = runtime_artifact_mount(
        profile,
        artifact_id=MODEL_ARTIFACT,
        mount_path="/models/protenix-v2",
        expected_manifest_sha256=LOCALIZATION_MANIFEST_SHA256,
    )
    invocations = (
        StageInvocation(
            stage_id="prepare-data",
            shard_id="main",
            argv=(
                "/usr/local/bin/fs2-run-protenix",
                "prep",
                "--input",
                f"{prep_root}/input.json",
                "--output-dir",
                f"{prep_root}/prepared-output",
                "--processed-json",
                f"{prep_root}/processed.json",
                "--provenance-marker",
                f"{prep_root}/provenance.json",
                "--handoff-tar",
                f"{prep_root}/handoff.tar.zst",
                "--output-artifact-id",
                prepared,
                "--msa-mode",
                "none",
                "--reference-root",
                "/models/protenix-v2",
                "--reference-manifest",
                "/models/protenix-v2/manifest.json",
                "--runtime-localization-marker",
                prep_localization_marker,
            ),
            environment=(
                ("PROTENIX_ROOT_DIR", "/models/protenix-v2"),
                ("FS2_NETWORK_MODE", "offline"),
                ("HF_HUB_OFFLINE", "1"),
                ("TRANSFORMERS_OFFLINE", "1"),
            ),
            working_directory=prep_root,
            consumes=(input_artifact.logical_artifact_id,),
            produces=prepared,
            collector_id="protenix-v2-prep-collector-v1",
            validator_id="protenix-v2-prep-validator-v1",
            handoff_name="processed-input",
            max_output_artifacts=1,
            max_output_bytes=64 * 1024 * 1024,
            materializations=(
                ArtifactMaterialization(
                    input_artifact.logical_artifact_id, f"{prep_root}/input.json", MaterializationMode.COPY_FILE
                ),
            ),
            runtime_artifacts=(MODEL_ARTIFACT,),
            runtime_mounts=(model_mount,),
        ),
        StageInvocation(
            stage_id="sample-structure",
            shard_id="main",
            argv=(
                "/usr/local/bin/fs2-run-protenix",
                "pred",
                "--input",
                f"{pred_root}/input/processed.json",
                "--input-marker",
                f"{pred_root}/input/provenance.json",
                "--input-artifact-id",
                prepared,
                "--output-dir",
                f"{pred_root}/outputs",
                "--checkpoint",
                "/models/protenix-v2/checkpoint/protenix-v2.pt",
                "--common-dir",
                "/models/protenix-v2/common",
                "--msa-mode",
                "none",
                "--seeds",
                seeds,
                "--sample-count",
                str(parameters.sample_count),
                "--disable-templates",
                "--disable-rna-msa",
                "--runtime-localization-marker",
                pred_localization_marker,
            ),
            environment=(
                ("PROTENIX_ROOT_DIR", "/models/protenix-v2"),
                ("FS2_NETWORK_MODE", "offline"),
                ("HF_HUB_OFFLINE", "1"),
                ("TRANSFORMERS_OFFLINE", "1"),
            ),
            working_directory=pred_root,
            consumes=(prepared,),
            produces=result,
            collector_id="protenix-v2-result-collector-v1",
            validator_id=VALIDATOR_ID,
            handoff_name=None,
            max_output_artifacts=len(parameters.seeds) * parameters.sample_count + 1,
            max_output_bytes=8 * 1024 * 1024 * 1024,
            materializations=(
                ArtifactMaterialization(
                    prepared, f"{pred_root}/input", MaterializationMode.EXTRACT_TAR, compression="zstd"
                ),
            ),
            runtime_artifacts=(MODEL_ARTIFACT,),
            runtime_mounts=(model_mount,),
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
        required_model_artifacts=(MODEL_ARTIFACT,),
    )


def collect_prep(invocation: StageInvocation, workspace: Path) -> CollectedStageOutput:
    return collect_handoff(
        invocation,
        workspace,
        filename="handoff.tar.zst",
        semantic_type="protenix-processed-input/v1",
        media_type="application/x-tar",
        compression="zstd",
    )


def collect_result(invocation: StageInvocation, workspace: Path) -> CollectedStageOutput:
    seeds = tuple(int(value) for value in invocation.argv[invocation.argv.index("--seeds") + 1].split(","))
    samples = int(invocation.argv[invocation.argv.index("--sample-count") + 1])
    return collect_confidence_envelope(
        invocation,
        workspace,
        expected_runtime_id=MODEL_ID,
        expected_model_revision=OUTPUT_MODEL_REVISION,
        expected_seeds=seeds,
        expected_samples_per_seed=samples,
    )
