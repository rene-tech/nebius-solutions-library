"""Canonical controller adapter for the source-qualified Mosaic candidate."""

from __future__ import annotations

from collections.abc import Mapping

from ..models import AdapterExecutionPlan, RuntimeArtifactMount
from .simple_candidates import SingleStageSpec, compile_single_stage

MODEL_ID = "mosaic"
VARIANT_ID = "escalante-20260801"


def _parameter_argv(parameters: Mapping[str, object]) -> tuple[str, ...]:
    return (
        "--objective-profile-id",
        str(parameters["objective_profile_id"]),
        "--iterations",
        str(parameters["iterations"]),
        "--seed",
        str(parameters["seed"]),
    )


SPEC = SingleStageSpec(
    MODEL_ID,
    VARIANT_ID,
    "escalante-bio/mosaic",
    "70fec525423f5f87156a1a957b4a4048f9f8e676",
    "fs2-serve.nebius.ai/mosaic-escalante-20260801-parameters/v1",
    "optimize",
    ("/opt/fs2/bin/mosaic-batch", "run-shard", "--request", "{REQUEST}", "--output", "{OUTPUT}"),
    (RuntimeArtifactMount("mosaic-component-closure", "/opt/fs2/artifacts/mosaic-component-closure"),),
    (("FS2_NETWORK_MODE", "offline"),),
    _parameter_argv,
)


def compile_run(profile: Mapping[str, object], request: object, *, operation_id: str) -> AdapterExecutionPlan:
    return compile_single_stage(SPEC, profile, request, operation_id=operation_id)
