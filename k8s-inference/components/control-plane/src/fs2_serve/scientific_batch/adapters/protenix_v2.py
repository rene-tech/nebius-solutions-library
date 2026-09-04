"""Fail-closed Protenix v2 adapter for one canonical offline artifact tree."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..models import (
    AdapterExecutionPlan,
    ArtifactMaterialization,
    MaterializationMode,
    RuntimeArtifactMount,
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
    PUBLIC_ARTIFACT_SUPPLEMENTAL_GROUP,
    collect_confidence_envelope,
    collect_confidence_stage,
    collect_handoff,
    collect_handoff_stage,
)
from .verified_input import verified_manifest_entry

if TYPE_CHECKING:
    from . import CollectedStageOutput

MODEL_ID = "protenix-v2"
VARIANT_ID = "upstream-v2-0-0"
SOURCE_REPOSITORY = "bytedance/Protenix"
SOURCE_REVISION = "2475421477ab414b571149ad4a875c390ff8a35d"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/protenix-v2-upstream-v2-0-0-parameters/v1"
INPUT_ARTIFACT_ID = "protenix-input"
INPUT_SEMANTIC_TYPE = "protenix-input-json/v1"
INPUT_MEDIA_TYPE = "application/json"
MAX_INPUT_BYTES = 256 * 1024 * 1024
REQUIRES_VERIFIED_INPUT_ARTIFACTS = True
MODEL_ARTIFACT = "protenix-v2"
VALIDATOR_ID = "protenix-v2-upstream-v2-0-0"
WEIGHTS_REPOSITORY = "TMF001/protenix-v2-weights"
WEIGHTS_REVISION = "653edab28103133512575365130916e3fd23ecc3"
OUTPUT_MODEL_REVISION = f"{WEIGHTS_REPOSITORY}@{WEIGHTS_REVISION}"
# Acquisition bytes, the localization recipe, and the resulting runtime tree
# are deliberately separate identities.  A source payload digest must never
# be mistaken for the tree mounted into a model Pod.
SOURCE_PAYLOAD_SHA256 = "8e14bb809d37db806159b7d277577abc692aec81d8899fbc84915d23ebe12eca"
LOCALIZATION_MANIFEST_SHA256 = "a093d28ecfc8374f143cc32ff713b0e6ad1124c095dbbca5af6e51b4f7dcc6b7"
LOCALIZED_TREE_CONTENT_SHA256 = "5e1c3b548af40752bb15f9f2ba06590e20e2b165e3fe9ab3fa99af9977574d48"
WEIGHTS_SHA256 = "8f931f9774a396b67033d0e58628e1834f4a1448165e04254b40a780b0c0d599"
WEIGHTS_SIZE_BYTES = 1_859_785_497
COMPOSITE_SIZE_BYTES = 2_514_897_184
COMPOSITE_ARTIFACT_REVISION = (
    "code-2475421477ab414b571149ad4a875c390ff8a35d_"
    "checkpoint-653edab28103133512575365130916e3fd23ecc3_"
    "common-2026-01-29"
)
MANDATORY_FILES = (
    ".fs2-manifest-sha256",
    "checkpoint/protenix-v2.pt",
    "common/clusters-by-entity-40.txt",
    "common/components.cif",
    "common/components.cif.rdkit_mol.pkl",
    "common/obsolete_release_date.csv",
    "manifest.json",
)
PREPARE_COLLECTOR_ID = "protenix-v2-prep-collector-v1"
PREPARE_VALIDATOR_ID = "protenix-v2-prep-validator-v1"
RESULT_COLLECTOR_ID = "protenix-v2-result-collector-v1"
STAGE_EXECUTION_CONTRACTS: Mapping[str, Mapping[str, object]] = {
    "prepare-data": {
        "collector_id": PREPARE_COLLECTOR_ID,
        "validator_id": PREPARE_VALIDATOR_ID,
        "runtime_artifacts": (MODEL_ARTIFACT,),
    },
    "sample-structure": {
        "collector_id": RESULT_COLLECTOR_ID,
        "validator_id": VALIDATOR_ID,
        "runtime_artifacts": (MODEL_ARTIFACT,),
    },
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
        label="Protenix v2",
    )
    raw_input_sha256 = model_input.digest.removeprefix("sha256:")
    raw_input_artifact_id = str(model_input.artifact_id)
    assert_profile_identity(
        profile,
        model_id=MODEL_ID,
        repository=SOURCE_REPOSITORY,
        revision=SOURCE_REVISION,
        parameter_schema=PARAMETER_SCHEMA,
        request=request,
    )
    prep_root = run_workspace(MODEL_ID, operation_id, "prepare-data-main")
    pred_root = run_workspace(MODEL_ID, operation_id, "sample-structure-main")
    prepared = logical_stage_artifact(operation_id, "prepare-data", "main")
    result = logical_stage_artifact(operation_id, "sample-structure", "main")
    seeds = ",".join(str(seed) for seed in parameters.seeds)
    prep_localization_marker = f"{prep_root}/.fs2/runtime-localization.json"
    pred_localization_marker = f"{pred_root}/.fs2/runtime-localization.json"
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
                "--raw-input-artifact-id",
                raw_input_artifact_id,
                "--raw-input-sha256",
                raw_input_sha256,
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
                ("TRITON_CACHE_DIR", "/cache/protenix/triton"),
                ("CUEQ_TRITON_CACHE_DIR", "/cache/protenix/cueq-triton"),
                ("TORCH_EXTENSIONS_DIR", "/cache/protenix/torch-extensions"),
                ("XDG_CACHE_HOME", "/cache/protenix/xdg"),
                ("FS2_SCIENTIFIC_COLLECTOR_ID", PREPARE_COLLECTOR_ID),
                ("FS2_SCIENTIFIC_VALIDATOR_ID", PREPARE_VALIDATOR_ID),
            ),
            working_directory=prep_root,
            consumes=(model_input.logical_artifact_id,),
            produces=prepared,
            collector_id=PREPARE_COLLECTOR_ID,
            validator_id=PREPARE_VALIDATOR_ID,
            handoff_name="processed-input",
            max_output_artifacts=1,
            max_output_bytes=64 * 1024 * 1024,
            materializations=(
                ArtifactMaterialization(
                    model_input.logical_artifact_id,
                    f"{prep_root}/input.json",
                    MaterializationMode.COPY_FILE,
                    compression=model_input.compression,
                ),
            ),
            runtime_artifacts=(MODEL_ARTIFACT,),
            runtime_mounts=(
                RuntimeArtifactMount(
                    artifact_id=MODEL_ARTIFACT,
                    mount_path="/models/protenix-v2",
                    read_only=True,
                    supplemental_groups=(PUBLIC_ARTIFACT_SUPPLEMENTAL_GROUP,),
                ),
            ),
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
                "--expected-raw-input-artifact-id",
                raw_input_artifact_id,
                "--expected-raw-input-sha256",
                raw_input_sha256,
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
                ("TRITON_CACHE_DIR", "/cache/protenix/triton"),
                ("CUEQ_TRITON_CACHE_DIR", "/cache/protenix/cueq-triton"),
                ("TORCH_EXTENSIONS_DIR", "/cache/protenix/torch-extensions"),
                ("XDG_CACHE_HOME", "/cache/protenix/xdg"),
                ("FS2_SCIENTIFIC_COLLECTOR_ID", RESULT_COLLECTOR_ID),
                ("FS2_SCIENTIFIC_VALIDATOR_ID", VALIDATOR_ID),
            ),
            working_directory=pred_root,
            consumes=(prepared,),
            produces=result,
            collector_id=RESULT_COLLECTOR_ID,
            validator_id=VALIDATOR_ID,
            max_output_artifacts=len(parameters.seeds) * parameters.sample_count * 2 + 1,
            max_output_bytes=8 * 1024 * 1024 * 1024,
            materializations=(
                ArtifactMaterialization(
                    prepared, f"{pred_root}/input", MaterializationMode.EXTRACT_TAR, compression="zstd"
                ),
            ),
            runtime_artifacts=(MODEL_ARTIFACT,),
            runtime_mounts=(
                RuntimeArtifactMount(
                    artifact_id=MODEL_ARTIFACT,
                    mount_path="/models/protenix-v2",
                    read_only=True,
                    supplemental_groups=(PUBLIC_ARTIFACT_SUPPLEMENTAL_GROUP,),
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
        required_model_artifacts=(MODEL_ARTIFACT,),
    )


def collect_prep(workspace: Path) -> CollectedOutput:
    return collect_handoff(
        workspace,
        filename="handoff.tar.zst",
        name="processed-input",
        semantic_type="protenix-processed-input/v1",
        media_type="application/x-tar",
        compression="zstd",
        maximum_bytes=64 * 1024 * 1024,
    )


def collect_result(request_value: object, workspace: Path) -> CollectedOutput:
    request = parse_public_request(request_value, maximum_input_bytes=256 * 1024 * 1024)
    parameters = Parameters.parse(request.parameters)
    return collect_confidence_envelope(
        workspace,
        validator_id=VALIDATOR_ID,
        expected_runtime_id=MODEL_ID,
        expected_model_revision=OUTPUT_MODEL_REVISION,
        expected_seeds=parameters.seeds,
        expected_samples_per_seed=parameters.sample_count,
        maximum_total_bytes=8 * 1024 * 1024 * 1024,
    )


def collect_stage_output(collector_id: str, request_value: object, workspace: Path) -> CollectedOutput:
    """Dispatch only collector identities frozen into this adapter's plan."""

    if collector_id == PREPARE_COLLECTOR_ID:
        return collect_prep(workspace)
    if collector_id == RESULT_COLLECTOR_ID:
        return collect_result(request_value, workspace)
    raise ScientificAdapterError(f"unsupported Protenix v2 collector identity {collector_id!r}")


def _argument(invocation: StageInvocation, name: str) -> str:
    if invocation.argv.count(name) != 1 or invocation.argv.index(name) + 1 >= len(invocation.argv):
        raise ScientificAdapterError(f"Protenix v2 invocation has no exact {name} argument")
    return invocation.argv[invocation.argv.index(name) + 1]


def collect_companion_output(invocation: StageInvocation, workspace: Path) -> CollectedStageOutput:
    """Collect from the immutable invocation used by the production companion."""

    if invocation.collector_id == PREPARE_COLLECTOR_ID:
        if invocation.stage_id != "prepare-data" or invocation.validator_id != PREPARE_VALIDATOR_ID:
            raise ScientificAdapterError("Protenix v2 prep collector received another stage contract")
        return collect_handoff_stage(
            workspace,
            filename="handoff.tar.zst",
            name="processed-input",
            semantic_type="protenix-processed-input/v1",
            media_type="application/x-tar",
            compression="zstd",
            maximum_bytes=64 * 1024 * 1024,
            validator_id=invocation.validator_id,
            expected_provenance={
                "artifact_id": invocation.produces,
                "raw_input_artifact_id": _argument(invocation, "--raw-input-artifact-id"),
                "raw_input_sha256": _argument(invocation, "--raw-input-sha256"),
                "msa_mode": _argument(invocation, "--msa-mode"),
                "composite_artifact_id": MODEL_ARTIFACT,
                "composite_artifact_revision": COMPOSITE_ARTIFACT_REVISION,
                "localized_content_digest_sha256": LOCALIZED_TREE_CONTENT_SHA256,
                "composite_manifest_sha256": LOCALIZATION_MANIFEST_SHA256,
                "source_revision": SOURCE_REVISION,
            },
        )
    if invocation.collector_id == RESULT_COLLECTOR_ID:
        if invocation.stage_id != "sample-structure" or invocation.validator_id != VALIDATOR_ID:
            raise ScientificAdapterError("Protenix v2 result collector received another stage contract")
        raw_seeds = _argument(invocation, "--seeds").split(",")
        try:
            seeds = tuple(int(value) for value in raw_seeds)
            samples = int(_argument(invocation, "--sample-count"))
        except ValueError as error:
            raise ScientificAdapterError("Protenix v2 invocation seed/sample envelope is invalid") from error
        if not 1 <= len(seeds) <= 16 or len(set(seeds)) != len(seeds):
            raise ScientificAdapterError("Protenix v2 invocation seeds are invalid")
        for seed in seeds:
            bounded_int(seed, minimum=0, maximum=2**31 - 1, label="model seed")
        bounded_int(samples, minimum=1, maximum=16, label="sample_count")
        return collect_confidence_stage(
            workspace,
            validator_id=invocation.validator_id,
            expected_runtime_id=MODEL_ID,
            expected_model_revision=OUTPUT_MODEL_REVISION,
            expected_seeds=seeds,
            expected_samples_per_seed=samples,
            expected_input_artifact_id=_argument(invocation, "--expected-raw-input-artifact-id"),
            expected_raw_input_sha256=_argument(invocation, "--expected-raw-input-sha256"),
            maximum_total_bytes=invocation.max_output_bytes,
        )
    raise ScientificAdapterError(f"unsupported Protenix v2 collector identity {invocation.collector_id!r}")
