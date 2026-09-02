"""Canonical controller adapter for upstream ProteinMPNN."""

from __future__ import annotations

from collections.abc import Mapping

from ..models import AdapterExecutionPlan, RuntimeArtifactMount
from .simple_candidates import SingleStageSpec, compile_single_stage

MODEL_ID = "proteinmpnn"
VARIANT_ID = "upstream-8907e667"
ARTIFACT_SHA256 = "43085c02e220bedf3a7edb089c290c0440a390c6993d3b6530be07ef1af515ae"


def _parameter_argv(parameters: Mapping[str, object]) -> tuple[str, ...]:
    return (
        "--num_seq_per_target",
        str(parameters["num_sequences"]),
        "--sampling_temp",
        str(parameters["sampling_temperature"]),
        "--seed",
        str(parameters["seed"]),
    )


SPEC = SingleStageSpec(
    MODEL_ID,
    VARIANT_ID,
    "dauparas/ProteinMPNN",
    "8907e6671bfbfc92303b5f79c4b5e6ce47cdef57",
    "fs2-serve.nebius.ai/proteinmpnn-upstream-8907e667-parameters/v1",
    "design",
    (
        "python3",
        "/opt/proteinmpnn/protein_mpnn_run.py",
        "--path_to_model_weights=/models/proteinmpnn",
        "--jsonl_path={REQUEST}",
        "--out_folder={OUTPUT}",
    ),
    (
        RuntimeArtifactMount(
            "proteinmpnn-vanilla-and-soluble",
            "/models/proteinmpnn",
            expected_content_sha256=ARTIFACT_SHA256,
        ),
    ),
    (("FS2_NETWORK_MODE", "offline"),),
    _parameter_argv,
)


def compile_run(profile: Mapping[str, object], request: object, *, operation_id: str) -> AdapterExecutionPlan:
    return compile_single_stage(SPEC, profile, request, operation_id=operation_id)
