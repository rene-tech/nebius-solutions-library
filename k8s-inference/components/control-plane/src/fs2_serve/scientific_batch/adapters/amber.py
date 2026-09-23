"""Private AMBER26 integration; native scientific semantics live in fs2_amber."""

from fs2_amber import ENGINE_ID, MODEL_ID, PARAMETER_SCHEMA, RESULT_SCHEMA

from .native_md import MAX_INPUT_BYTES, NativeMDAdapter

VARIANT_ID = "pmemd-26-single-gpu-v1"
SOURCE_REPOSITORY = "fs2-platform/amber26-engine"
SOURCE_REVISION = ENGINE_ID.rsplit(":", 1)[1]
COLLECTOR_ID = VALIDATOR_ID = "amber-workflow-v1"
INPUT_ID = "amber-inputs"
REQUIRES_VERIFIED_INPUT_ARTIFACTS = True

ADAPTER = NativeMDAdapter(MODEL_ID, VARIANT_ID, SOURCE_REPOSITORY)
public_input_contract = ADAPTER.public_input_contract
compile_run = ADAPTER.compile_run
collect_companion_output = ADAPTER.collect_companion_output

__all__ = [
    "MODEL_ID", "PARAMETER_SCHEMA", "RESULT_SCHEMA", "VARIANT_ID",
    "SOURCE_REPOSITORY", "SOURCE_REVISION", "COLLECTOR_ID", "VALIDATOR_ID",
    "INPUT_ID", "MAX_INPUT_BYTES", "REQUIRES_VERIFIED_INPUT_ARTIFACTS",
    "public_input_contract", "compile_run", "collect_companion_output",
]
