"""Pinned NVIDIA LAMMPS integration; scientific semantics live in fs2_lammps."""

from fs2_lammps import ENGINE_ID, MODEL_ID, PARAMETER_SCHEMA, RESULT_SCHEMA

from .native_md import MAX_INPUT_BYTES, NativeMDAdapter

VARIANT_ID = "nvidia-2025-07-22-single-gpu-v1"
SOURCE_REPOSITORY = "nvidia/lammps"
SOURCE_REVISION = ENGINE_ID.rsplit(":", 1)[1]
COLLECTOR_ID = VALIDATOR_ID = "lammps-workflow-v1"
INPUT_ID = "lammps-inputs"
REQUIRES_VERIFIED_INPUT_ARTIFACTS = True

ADAPTER = NativeMDAdapter(MODEL_ID, VARIANT_ID, SOURCE_REPOSITORY)
public_input_contract = ADAPTER.public_input_contract
compile_run = ADAPTER.compile_run
collect_companion_output = ADAPTER.collect_companion_output

__all__ = [
    "MODEL_ID",
    "PARAMETER_SCHEMA",
    "RESULT_SCHEMA",
    "VARIANT_ID",
    "SOURCE_REPOSITORY",
    "SOURCE_REVISION",
    "COLLECTOR_ID",
    "VALIDATOR_ID",
    "INPUT_ID",
    "MAX_INPUT_BYTES",
    "REQUIRES_VERIFIED_INPUT_ARTIFACTS",
    "public_input_contract",
    "compile_run",
    "collect_companion_output",
]
