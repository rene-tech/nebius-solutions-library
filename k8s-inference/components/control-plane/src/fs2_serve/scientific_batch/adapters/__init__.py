"""Allow-listed scientific adapter and collector registry.

Model-owned modules register only compilers and collectors here. The
controller owns dispatch, persistence, artifact transport, and Kubernetes
rendering.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

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


class CollectionPendingError(RuntimeError):
    """The model process has not atomically published its expected outputs yet."""


AdapterCompiler = Callable[..., AdapterExecutionPlan]
StageCollector = Callable[[StageInvocation, Path], CollectedStageOutput]

_COMPILERS: dict[str, AdapterCompiler] = {}
_COLLECTORS: dict[str, StageCollector] = {}


def register_adapter(
    *,
    model_id: str,
    compiler: AdapterCompiler,
    collectors: Mapping[str, StageCollector],
) -> None:
    if model_id in _COMPILERS or not collectors or any(key in _COLLECTORS for key in collectors):
        raise RuntimeError("scientific adapter or collector is already registered")
    _COMPILERS[model_id] = compiler
    _COLLECTORS.update(collectors)


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
    "collect_stage_output",
    "compile_adapter_run",
    "register_adapter",
]
