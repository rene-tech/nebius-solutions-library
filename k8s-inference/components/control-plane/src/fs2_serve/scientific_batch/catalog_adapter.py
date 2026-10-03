"""Projection from a schema-validated catalog workload profile into internal plans.

This module is not a JSON-schema implementation. The catalog consumer remains
responsible for loading and validating the public scientific workload profile.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from .models import (
    CheckpointMode,
    ExecutionMode,
    PreemptionMode,
    ResourceClass,
    ScientificBatchPlan,
    ScientificStagePlan,
    StageExecutionShape,
    StagePlacementClass,
    StageResourceEnvelope,
)


class CatalogProfileAdapterError(ValueError):
    """A validated catalog value cannot be projected into a run plan."""


@dataclass(frozen=True, slots=True)
class ScientificStageExpansion:
    """Run-specific bounded expansion selected from catalog parallelism limits."""

    shard_ids: tuple[str, ...] = ("main",)
    gang_size: int | None = None
    enabled: bool = True
    depends_on: tuple[str, ...] | None = None
    execution_shape_id: str | None = None


def _mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CatalogProfileAdapterError(f"{path} must be an object from a validated catalog profile")
    if not all(isinstance(key, str) for key in value):
        raise CatalogProfileAdapterError(f"{path} has a non-string key")
    return cast(Mapping[str, object], value)


def _string(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise CatalogProfileAdapterError(f"{path} must be a string")
    return value


def _integer(value: object, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise CatalogProfileAdapterError(f"{path} must be an integer")
    return value


def _string_tuple(value: object, path: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CatalogProfileAdapterError(f"{path} must be an array of strings")
    return tuple(value)


def _stage_contract(
    stage: Mapping[str, object], path: str
) -> tuple[StagePlacementClass | None, StageResourceEnvelope | None]:
    raw_placement = stage.get("placement")
    raw_resources = stage.get("resources")
    if raw_placement is None and raw_resources is None:
        return None, None
    placement = _mapping(raw_placement, f"{path}.placement")
    resources = _mapping(raw_resources, f"{path}.resources")
    limits = _mapping(resources.get("limits"), f"{path}.resources.limits")
    try:
        placement_class = StagePlacementClass(_string(placement.get("class"), f"{path}.placement.class"))
        envelope = StageResourceEnvelope(
            cpu_millis=_integer(resources.get("cpu_millis"), f"{path}.resources.cpu_millis"),
            memory_bytes=_integer(resources.get("memory_bytes"), f"{path}.resources.memory_bytes"),
            ephemeral_storage_bytes=_integer(
                resources.get("ephemeral_storage_bytes"), f"{path}.resources.ephemeral_storage_bytes"
            ),
            limit_cpu_millis=_integer(limits.get("cpu_millis"), f"{path}.resources.limits.cpu_millis"),
            limit_memory_bytes=_integer(limits.get("memory_bytes"), f"{path}.resources.limits.memory_bytes"),
            limit_ephemeral_storage_bytes=_integer(
                limits.get("ephemeral_storage_bytes"), f"{path}.resources.limits.ephemeral_storage_bytes"
            ),
        )
    except ValueError as error:
        raise CatalogProfileAdapterError(f"{path} contains an unsupported placement contract") from error
    return placement_class, envelope


def select_stage_shape(stage: Mapping[str, object], shape_id: str | None) -> Mapping[str, object]:
    """Select a catalog shape; never accept request-supplied execution fields."""
    if shape_id is None:
        return stage
    shapes = stage.get("execution_shapes")
    if not isinstance(shapes, list) or not 1 <= len(shapes) <= 32:
        raise CatalogProfileAdapterError("stage has no bounded execution shapes")
    matches = [item for item in shapes if isinstance(item, Mapping) and item.get("id") == shape_id]
    if len(matches) != 1:
        raise CatalogProfileAdapterError("execution shape is absent or duplicated in the catalog stage")
    shape = _mapping(matches[0], "execution shape")
    if set(shape) != {"id", "admission_mode", "min_parallelism", "max_parallelism", "placement", "resources"}:
        raise CatalogProfileAdapterError("execution shape fields differ from the catalog contract")
    return {**stage, **{key: value for key, value in shape.items() if key != "id"}}


def stage_execution_shape(stage: Mapping[str, object], shape_id: str | None) -> StageExecutionShape | None:
    if shape_id is None:
        return None
    placement = _mapping(stage.get("placement"), "execution shape placement")
    accelerator = _mapping(placement.get("accelerator"), "execution shape accelerator")
    return StageExecutionShape(
        shape_id=shape_id,
        accelerator_resource_name=_string(accelerator.get("resource_name"), "execution shape accelerator resource"),
        accelerator_count=_integer(accelerator.get("count"), "execution shape accelerator count"),
        pool_ids=_string_tuple(accelerator.get("pool_ids"), "execution shape pools"),
    )


def scientific_plan_from_catalog_profile(
    profile: Mapping[str, object],
    *,
    expansions: Mapping[str, ScientificStageExpansion] | None = None,
) -> ScientificBatchPlan:
    """Project the canonical profile's workload subset into controller state.

    ``expansions`` is run-specific state produced after resolving the immutable
    input manifest. It selects shard identities or a gang size inside the
    operator-owned catalog bounds; callers cannot supply execution fields.
    """

    workload = _mapping(profile.get("workload"), "profile.workload")
    retry = _mapping(workload.get("retry"), "profile.workload.retry")
    max_attempts = _integer(retry.get("max_attempts"), "profile.workload.retry.max_attempts")
    raw_stages = workload.get("stages")
    if not isinstance(raw_stages, list):
        raise CatalogProfileAdapterError("profile.workload.stages must be an array")

    selected = dict(expansions or {})
    stages: list[ScientificStagePlan] = []
    for index, raw_stage in enumerate(raw_stages):
        path = f"profile.workload.stages[{index}]"
        stage = _mapping(raw_stage, path)
        stage_id = _string(stage.get("id"), f"{path}.id")
        expansion = selected.pop(stage_id, None)
        shape_id = None if expansion is None else expansion.execution_shape_id
        stage = select_stage_shape(stage, shape_id)
        try:
            mode = ExecutionMode(_string(stage.get("admission_mode"), f"{path}.admission_mode"))
            resource_class = ResourceClass(_string(stage.get("resource_class"), f"{path}.resource_class"))
            checkpoint_mode = CheckpointMode(_string(stage.get("checkpoint_mode"), f"{path}.checkpoint_mode"))
            preemption_mode = PreemptionMode(_string(stage.get("preemption_mode"), f"{path}.preemption_mode"))
        except ValueError as error:
            raise CatalogProfileAdapterError(f"{path} contains an unsupported catalog enum") from error
        minimum = _integer(stage.get("min_parallelism"), f"{path}.min_parallelism")
        maximum = _integer(stage.get("max_parallelism"), f"{path}.max_parallelism")

        if expansion is not None and not expansion.enabled:
            if expansion.gang_size is not None or expansion.depends_on is not None or shape_id is not None:
                raise CatalogProfileAdapterError(f"{path} disabled expansion cannot supply execution fields")
            continue

        if mode is ExecutionMode.FANOUT:
            if expansion is None:
                if minimum != 1:
                    raise CatalogProfileAdapterError(f"{path} requires a run-specific shard expansion")
                expansion = ScientificStageExpansion()
            if expansion.gang_size is not None:
                raise CatalogProfileAdapterError(f"{path} is independent-jobs and cannot select a gang size")
            shards = expansion.shard_ids
            gang_size = None
        else:
            if expansion is None:
                if minimum != maximum:
                    raise CatalogProfileAdapterError(f"{path} requires a run-specific gang size")
                expansion = ScientificStageExpansion(shard_ids=("gang",), gang_size=minimum)
            if expansion.shard_ids != ("gang",) or expansion.gang_size is None:
                raise CatalogProfileAdapterError(f"{path} gang-jobset expansion must use logical shard 'gang'")
            shards = ("gang",)
            gang_size = expansion.gang_size

        try:
            placement_class, resources = _stage_contract(stage, path)
            stages.append(
                ScientificStagePlan(
                    stage_id=stage_id,
                    depends_on=(
                        expansion.depends_on
                        if expansion is not None and expansion.depends_on is not None
                        else _string_tuple(stage.get("needs"), f"{path}.needs")
                    ),
                    mode=mode,
                    shards=shards,
                    max_attempts=max_attempts,
                    gang_size=gang_size,
                    resource_class=resource_class,
                    min_parallelism=minimum,
                    max_parallelism=maximum,
                    checkpoint_mode=checkpoint_mode,
                    preemption_mode=preemption_mode,
                    placement_class=placement_class,
                    resources=resources,
                    execution_shape=stage_execution_shape(stage, shape_id),
                )
            )
        except ValueError as error:
            raise CatalogProfileAdapterError(f"{path} cannot form an internal stage plan: {error}") from error

    if selected:
        raise CatalogProfileAdapterError(f"expansions reference unknown catalog stages: {sorted(selected)}")
    try:
        return ScientificBatchPlan(stages=tuple(stages))
    except ValueError as error:
        raise CatalogProfileAdapterError(f"catalog workload cannot form an internal stage plan: {error}") from error
