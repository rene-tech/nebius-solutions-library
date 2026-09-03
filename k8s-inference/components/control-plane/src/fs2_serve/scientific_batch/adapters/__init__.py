"""Allow-listed scientific adapter and collector registry.

Model-owned modules register only compilers and collectors here. The
controller owns dispatch, persistence, artifact transport, and Kubernetes
rendering.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from ..models import AdapterExecutionPlan, ArtifactAccessContext, ScientificInputArtifact, StageInvocation


@dataclass(frozen=True, slots=True)
class CollectedArtifactFile:
    name: str
    semantic_type: str
    path: Path
    media_type: str
    compression: str | None = None


@dataclass(frozen=True, slots=True)
class CollectedStageOutput:
    artifacts: tuple[CollectedArtifactFile, ...]
    validation: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class StageExecutionContract:
    """Static adapter facts used to build the trusted deployment execution map."""

    collector_id: str
    validator_id: str
    runtime_artifacts: tuple[str, ...]


class CollectionPendingError(RuntimeError):
    """The model process has not atomically published its expected outputs yet."""


AdapterCompiler = Callable[..., AdapterExecutionPlan]
StageCollector = Callable[[StageInvocation, Path], CollectedStageOutput]

_COMPILERS: dict[str, AdapterCompiler] = {}
_COLLECTORS: dict[str, StageCollector] = {}
_STAGE_CONTRACTS: dict[str, Mapping[str, StageExecutionContract]] = {}


def register_adapter(
    *,
    model_id: str,
    compiler: AdapterCompiler,
    collectors: Mapping[str, StageCollector],
    stage_contracts: Mapping[str, StageExecutionContract] | None = None,
) -> None:
    if (
        model_id in _COMPILERS
        or not collectors
        or any(key in _COLLECTORS for key in collectors)
        or (
            stage_contracts is not None
            and any(contract.collector_id not in collectors for contract in stage_contracts.values())
        )
    ):
        raise RuntimeError("scientific adapter or collector is already registered")
    _COMPILERS[model_id] = compiler
    _COLLECTORS.update(collectors)
    if stage_contracts is not None:
        _STAGE_CONTRACTS[model_id] = MappingProxyType(dict(stage_contracts))


def stage_execution_contracts(model_id: str) -> Mapping[str, StageExecutionContract]:
    """Return the packaged adapter's immutable stage and artifact closure."""

    try:
        return _STAGE_CONTRACTS[model_id]
    except KeyError as error:
        raise ValueError(f"no scientific adapter is registered for {model_id}") from error


def compile_adapter_run(
    model_id: str,
    profile: Mapping[str, object],
    request: object,
    *,
    operation_id: str,
    variant_id: str,
    access_context: ArtifactAccessContext,
    input_artifacts: tuple[ScientificInputArtifact, ...],
) -> AdapterExecutionPlan:
    try:
        compiler = _COMPILERS[model_id]
    except KeyError as error:
        raise ValueError(f"no scientific adapter is registered for {model_id}") from error
    return compiler(
        profile,
        request,
        operation_id=operation_id,
        variant_id=variant_id,
        access_context=access_context,
        input_artifacts=input_artifacts,
    )


def collect_stage_output(invocation: StageInvocation, workspace: Path) -> CollectedStageOutput:
    try:
        collector = _COLLECTORS[invocation.collector_id]
    except KeyError as error:
        raise ValueError(f"no scientific collector is registered for {invocation.collector_id}") from error
    output = collector(invocation, workspace)
    if not isinstance(output, CollectedStageOutput):
        raise TypeError("scientific collector returned another controller type")
    if output.validation.get("validator_id") != invocation.validator_id:
        raise ValueError("scientific validation identity differs from the stage invocation")
    return output


__all__ = [
    "AdapterCompiler",
    "CollectedArtifactFile",
    "CollectedStageOutput",
    "CollectionPendingError",
    "StageCollector",
    "StageExecutionContract",
    "collect_stage_output",
    "compile_adapter_run",
    "register_adapter",
    "stage_execution_contracts",
]


# Model modules are imported only after the registry types exist, avoiding a
# second scheduler protocol while keeping model-specific code in unique files.
from . import alphafold3, esmfold2, esmfold2_fast, openfold3, protenix_v2  # noqa: E402

register_adapter(
    model_id=esmfold2.MODEL_ID,
    compiler=esmfold2.compile_run,
    collectors={
        "esmfold2-prepare-collector-v1": esmfold2.collect_prepare,
        "esmfold2-result-collector-v1": esmfold2.collect_result,
    },
    stage_contracts=esmfold2.STAGE_EXECUTION_CONTRACTS,
)
register_adapter(
    model_id=esmfold2_fast.MODEL_ID,
    compiler=esmfold2_fast.compile_run,
    collectors={
        "esmfold2-fast-prepare-collector-v1": esmfold2_fast.collect_prepare,
        "esmfold2-fast-result-collector-v1": esmfold2_fast.collect_result,
    },
    stage_contracts=esmfold2_fast.STAGE_EXECUTION_CONTRACTS,
)
register_adapter(
    model_id=alphafold3.MODEL_ID,
    compiler=alphafold3.compile_run,
    collectors={
        "alphafold3-data-collector-v1": alphafold3.collect_data,
        "alphafold3-result-collector-v1": alphafold3.collect_result,
    },
    stage_contracts=alphafold3.STAGE_EXECUTION_CONTRACTS,
)
register_adapter(
    model_id=openfold3.MODEL_ID,
    compiler=openfold3.compile_run,
    collectors={
        "openfold3-data-collector-v1": openfold3.collect_data,
        "openfold3-result-collector-v1": openfold3.collect_result,
    },
    stage_contracts=openfold3.STAGE_EXECUTION_CONTRACTS,
)
register_adapter(
    model_id=protenix_v2.MODEL_ID,
    compiler=protenix_v2.compile_run,
    collectors={
        "protenix-v2-prep-collector-v1": protenix_v2.collect_prep,
        "protenix-v2-result-collector-v1": protenix_v2.collect_result,
    },
    stage_contracts=protenix_v2.STAGE_EXECUTION_CONTRACTS,
)
