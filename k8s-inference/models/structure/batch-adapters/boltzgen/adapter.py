"""Compatibility import for the controller-owned BoltzGen adapter."""

from fs2_serve.scientific_batch.adapters.boltzgen import (
    CHECKPOINTS,
    MODEL_ID,
    PARAMETER_SCHEMA,
    RELEASE_TAG,
    SOURCE_REPOSITORY,
    SOURCE_REVISION,
    VARIANT_ID,
    WEIGHTS_ARTIFACT_ID,
    WEIGHTS_REVISION,
    BoltzGenParameters,
    DesignBatch,
    compile_run,
    collect_output,
    validate_output,
)

__all__ = [
    "CHECKPOINTS",
    "MODEL_ID",
    "PARAMETER_SCHEMA",
    "RELEASE_TAG",
    "SOURCE_REPOSITORY",
    "SOURCE_REVISION",
    "VARIANT_ID",
    "WEIGHTS_ARTIFACT_ID",
    "WEIGHTS_REVISION",
    "BoltzGenParameters",
    "DesignBatch",
    "compile_run",
    "collect_output",
    "validate_output",
]
