"""Canonical BindCraft/PyRosetta adapter for the authorized academic PoC."""

from __future__ import annotations

from collections.abc import Mapping

from ..catalog_adapter import ScientificStageExpansion
from ..models import (
    AdapterExecutionPlan,
    ArtifactMaterialization,
    MaterializationMode,
    RuntimeArtifactMount,
    StageInvocation,
)
from .common import (
    ScientificAdapterError,
    assert_artifact_requirement,
    assert_profile_identity,
    bounded_int,
    build_execution_plan,
    logical_stage_artifact,
    parse_public_request,
    run_workspace,
    strict_object,
)

MODEL_ID = "bindcraft"
VARIANT_ID = "upstream-pyrosetta"
SOURCE_REPOSITORY = "martinpacesa/BindCraft"
SOURCE_REVISION = "7cd4ace1b7407adf66a50dfefa47de2270f5e4a9"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/bindcraft-upstream-pyrosetta-parameters/v1"
AF2_ARTIFACT = "bindcraft-alphafold2-params"
MPNN_ARTIFACT = "bindcraft-proteinmpnn-weights"
PYROSETTA_SOURCE_ARTIFACT = "bindcraft-pyrosetta"
PYROSETTA_RUNTIME_ARTIFACT = "bindcraft-pyrosetta-installed-tree"
PYROSETTA_WHEEL_SHA256 = "4383d8d1a14fd3aff52983de936908791cc77bc6ac418e3bc53bb963a42c5242"
PYROSETTA_WHEEL = "pyrosetta-2026.29+releasequarterly.80a0635615-cp310-cp310-linux_x86_64.whl"


def compile_run(
    profile: Mapping[str, object],
    request_value: object,
    *,
    operation_id: str,
) -> AdapterExecutionPlan:
    request = parse_public_request(request_value, maximum_input_bytes=256 * 1024 * 1024)
    assert_profile_identity(
        profile,
        model_id=MODEL_ID,
        variant_id=VARIANT_ID,
        repository=SOURCE_REPOSITORY,
        revision=SOURCE_REVISION,
        parameter_schema=PARAMETER_SCHEMA,
        request=request,
    )
    parameters = strict_object(
        request.parameters,
        required=frozenset({"binder_length", "target_chains", "trajectories"}),
        label="BindCraft parameters",
    )
    trajectories = bounded_int(parameters["trajectories"], minimum=1, maximum=64, label="trajectories")
    binder_length = bounded_int(parameters["binder_length"], minimum=20, maximum=500, label="binder_length")
    chains = parameters["target_chains"]
    if (
        not isinstance(chains, list)
        or not 1 <= len(chains) <= 26
        or len(set(chains)) != len(chains)
        or any(
            not isinstance(chain, str)
            or len(chain) != 1
            or not chain.isascii()
            or not chain.isupper()
            for chain in chains
        )
    ):
        raise ScientificAdapterError("target_chains must be unique uppercase chain IDs")
    assert_artifact_requirement(profile, artifact_id=AF2_ARTIFACT, content_sha256=None)
    assert_artifact_requirement(profile, artifact_id=MPNN_ARTIFACT, content_sha256=None)
    assert_artifact_requirement(
        profile,
        artifact_id=PYROSETTA_SOURCE_ARTIFACT,
        content_sha256=None,
        required_file=PYROSETTA_WHEEL,
        required_file_sha256=PYROSETTA_WHEEL_SHA256,
        required_file_size_bytes=1_667_097_173,
    )
    assert_artifact_requirement(
        profile,
        artifact_id=PYROSETTA_RUNTIME_ARTIFACT,
        content_sha256=None,
        required_file="pyrosetta/__init__.py",
    )

    shard_ids = tuple(f"trajectory-{index:03d}" for index in range(trajectories))
    expansions = {
        "design": ScientificStageExpansion(shard_ids=shard_ids),
        "relax-score": ScientificStageExpansion(shard_ids=("main",)),
    }
    runtime_artifacts = (AF2_ARTIFACT, MPNN_ARTIFACT, PYROSETTA_RUNTIME_ARTIFACT)
    runtime_mounts = (
        RuntimeArtifactMount(AF2_ARTIFACT, "/models/alphafold2"),
        RuntimeArtifactMount(MPNN_ARTIFACT, "/models/proteinmpnn"),
        RuntimeArtifactMount(
            PYROSETTA_RUNTIME_ARTIFACT,
            "/opt/fs2/academic/pyrosetta",
            supplemental_groups=(65532,),
        ),
    )
    environment = (
        ("FS2_NETWORK_MODE", "offline"),
        ("PYTHONPATH", "/opt/fs2/academic/pyrosetta"),
    )
    design_outputs: list[str] = []
    invocations: list[StageInvocation] = []
    for index, shard_id in enumerate(shard_ids):
        workspace = run_workspace(MODEL_ID, operation_id, shard_id)
        output = logical_stage_artifact(operation_id, "design", shard_id)
        design_outputs.append(output)
        invocations.append(
            StageInvocation(
                "design",
                shard_id,
                (
                    "/opt/fs2/bin/bindcraft-batch",
                    "run-trajectory",
                    "--request",
                    f"{workspace}/input/request.json",
                    "--output",
                    f"{workspace}/outputs",
                    "--trajectory-index",
                    str(index),
                    "--binder-length",
                    str(binder_length),
                ),
                environment,
                workspace,
                (request.input_manifest.artifact_id,),
                output,
                (
                    ArtifactMaterialization(
                        request.input_manifest.artifact_id,
                        f"{workspace}/input/request.json",
                        MaterializationMode.COPY_FILE,
                        compression=request.input_manifest.compression,
                    ),
                ),
                runtime_artifacts,
                runtime_mounts,
            )
        )
    aggregate_workspace = run_workspace(MODEL_ID, operation_id, "main")
    invocations.append(
        StageInvocation(
            "relax-score",
            "main",
            (
                "/opt/fs2/bin/bindcraft-batch",
                "aggregate",
                "--shards",
                aggregate_workspace,
                "--output",
                f"{aggregate_workspace}/outputs",
            ),
            environment,
            aggregate_workspace,
            tuple(design_outputs),
            logical_stage_artifact(operation_id, "relax-score", "main"),
            tuple(
                ArtifactMaterialization(
                    output,
                    aggregate_workspace,
                    MaterializationMode.OVERLAY_TAR,
                    compression="zstd",
                )
                for output in design_outputs
            ),
            runtime_artifacts,
            runtime_mounts,
        )
    )
    return build_execution_plan(
        model_id=MODEL_ID,
        variant_id=VARIANT_ID,
        source_revision=SOURCE_REVISION,
        request=request,
        profile=profile,
        expansions=expansions,
        invocations=tuple(invocations),
        required_model_artifacts=runtime_artifacts,
    )
