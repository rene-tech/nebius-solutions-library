"""Compatibility import for the controller-owned Proteina-Complexa adapter."""

from fs2_serve.scientific_batch.adapters.proteina_complexa import (
    MODEL_ID,
    PARAMETER_SCHEMA,
    SOURCE_REPOSITORY,
    SOURCE_REVISION,
    VARIANT_ID,
    VARIANTS,
    ProteinaParameters,
    compile_run,
    collect_output,
    validate_output,
)

__all__ = [
    "MODEL_ID",
    "PARAMETER_SCHEMA",
    "SOURCE_REPOSITORY",
    "SOURCE_REVISION",
    "VARIANT_ID",
    "VARIANTS",
    "ProteinaParameters",
    "compile_run",
    "collect_output",
    "validate_output",
]
