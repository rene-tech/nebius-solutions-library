"""Canonical controller adapter for the distinct upstream RFdiffusion variant."""

from __future__ import annotations

import json
from collections.abc import Mapping

from ..models import AdapterExecutionPlan, RuntimeArtifactMount
from .simple_candidates import SingleStageSpec, compile_single_stage

MODEL_ID = "rfdiffusion"
VARIANT_ID = "upstream-v1-1-0"
ARTIFACT_SHA256 = "a6f25ea4df457270825f01fd0477bf2b9dcc17f817b59ab06ad9d640de8e1540"


def _parameter_argv(parameters: Mapping[str, object]) -> tuple[str, ...]:
    return (
        "--contigs-json",
        json.dumps(parameters["contigs"], sort_keys=True, separators=(",", ":")),
        "--design-count",
        str(parameters["design_count"]),
        "--hotspot-residues-json",
        json.dumps(parameters.get("hotspot_residues", []), sort_keys=True, separators=(",", ":")),
        "--seed",
        str(parameters["seed"]),
    )


SPEC = SingleStageSpec(
    MODEL_ID,
    VARIANT_ID,
    "RosettaCommons/RFdiffusion",
    "9273ef67335acaf91df0150473a274759229cdf6",
    "fs2-serve.nebius.ai/rfdiffusion-upstream-v1-1-0-parameters/v1",
    "design",
    (
        "/opt/fs2/bin/rfdiffusion-batch",
        "run-shard",
        "--request",
        "{REQUEST}",
        "--output",
        "{OUTPUT}",
        "--checkpoint",
        "/models/rfdiffusion/Base_ckpt.pt",
    ),
    (
        RuntimeArtifactMount(
            "rfdiffusion-v1-1-0-checkpoints",
            "/models/rfdiffusion",
            expected_content_sha256=ARTIFACT_SHA256,
        ),
    ),
    (("FS2_NETWORK_MODE", "offline"),),
    _parameter_argv,
)


def compile_run(profile: Mapping[str, object], request: object, *, operation_id: str) -> AdapterExecutionPlan:
    return compile_single_stage(SPEC, profile, request, operation_id=operation_id)
