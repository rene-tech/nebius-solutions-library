"""Canonical controller adapter for the open FreeBindCraft candidate."""

from __future__ import annotations

from collections.abc import Mapping

from ..models import AdapterExecutionPlan, RuntimeArtifactMount
from .simple_candidates import SingleStageSpec, compile_single_stage

MODEL_ID = "freebindcraft"
VARIANT_ID = "upstream-v1-0-5"


def _parameter_argv(parameters: Mapping[str, object]) -> tuple[str, ...]:
    chains = parameters["target_chains"]
    assert isinstance(chains, list)
    return (
        "--binder-length",
        str(parameters["binder_length"]),
        "--scoring-mode",
        str(parameters["scoring_mode"]),
        "--target-chains",
        ",".join(str(chain) for chain in chains),
        "--trajectories",
        str(parameters["trajectories"]),
    )


SPEC = SingleStageSpec(
    MODEL_ID,
    VARIANT_ID,
    "cytokineking/FreeBindCraft",
    "28c43fc48942eebd7918f504e9812c5c17bb3411",
    "fs2-serve.nebius.ai/freebindcraft-upstream-v1-0-5-parameters/v1",
    "design-score",
    ("/opt/fs2/bin/freebindcraft-batch", "run-trajectory", "--request", "{REQUEST}", "--output", "{OUTPUT}"),
    (RuntimeArtifactMount("freebindcraft-alphafold2-params", "/models/alphafold2"),),
    (("FS2_NETWORK_MODE", "offline"),),
    _parameter_argv,
)


def compile_run(profile: Mapping[str, object], request: object, *, operation_id: str) -> AdapterExecutionPlan:
    return compile_single_stage(SPEC, profile, request, operation_id=operation_id)
