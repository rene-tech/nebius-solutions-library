"""Projection from a schema-validated catalog workload profile into internal plans.

This module is not a JSON-schema implementation. The catalog consumer remains
responsible for loading and validating the public scientific workload profile.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from jsonschema import Draft202012Validator

from .models import (
    CheckpointMode,
    ExecutionMode,
    PreemptionMode,
    ResourceClass,
    ScientificBatchPlan,
    ScientificStagePlacement,
    ScientificStagePlan,
    ScientificStageResources,
    ScientificStorageRequirement,
    StorageAccessMode,
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


def _number(value: object, path: str) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise CatalogProfileAdapterError(f"{path} must be a number")
    return float(value)


def _string_tuple(value: object, path: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CatalogProfileAdapterError(f"{path} must be an array of strings")
    return tuple(value)


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
        try:
            mode = ExecutionMode(_string(stage.get("admission_mode"), f"{path}.admission_mode"))
            resource_class = ResourceClass(_string(stage.get("resource_class"), f"{path}.resource_class"))
            checkpoint_mode = CheckpointMode(_string(stage.get("checkpoint_mode"), f"{path}.checkpoint_mode"))
            preemption_mode = PreemptionMode(_string(stage.get("preemption_mode"), f"{path}.preemption_mode"))
        except ValueError as error:
            raise CatalogProfileAdapterError(f"{path} contains an unsupported catalog enum") from error
        minimum = _integer(stage.get("min_parallelism"), f"{path}.min_parallelism")
        maximum = _integer(stage.get("max_parallelism"), f"{path}.max_parallelism")
        expansion = selected.pop(stage_id, None)
        resource_value = stage.get("resources")
        resources = None
        if resource_value is not None:
            resource_map = _mapping(resource_value, f"{path}.resources")
            limits = _mapping(resource_map.get("limits"), f"{path}.resources.limits")
            resources = ScientificStageResources(
                cpu_millis=_integer(resource_map.get("cpu_millis"), f"{path}.resources.cpu_millis"),
                memory_bytes=_integer(resource_map.get("memory_bytes"), f"{path}.resources.memory_bytes"),
                ephemeral_storage_bytes=int(
                    _number(
                        resource_map.get("ephemeral_storage_request_gib"),
                        f"{path}.resources.ephemeral_storage_request_gib",
                    )
                    * 1024**3
                ),
                gpu_count=_integer(resource_map.get("gpu_count"), f"{path}.resources.gpu_count"),
                cpu_limit_millis=_integer(limits.get("cpu_millis"), f"{path}.resources.limits.cpu_millis"),
                memory_limit_bytes=_integer(limits.get("memory_bytes"), f"{path}.resources.limits.memory_bytes"),
                ephemeral_storage_limit_bytes=_integer(
                    limits.get("ephemeral_storage_bytes"),
                    f"{path}.resources.limits.ephemeral_storage_bytes",
                ),
            )
        placement_map = _mapping(stage.get("placement"), f"{path}.placement")
        accelerator_value = placement_map.get("accelerator_requirement")
        if accelerator_value is None:
            accelerator_class = None
            accelerator_resource_name = None
            accelerator_count = 0
        else:
            accelerator = _mapping(
                accelerator_value, f"{path}.placement.accelerator_requirement"
            )
            accelerator_class = _string(
                accelerator.get("class"),
                f"{path}.placement.accelerator_requirement.class",
            )
            accelerator_resource_name = _string(
                accelerator.get("resource_name"),
                f"{path}.placement.accelerator_requirement.resource_name",
            )
            accelerator_count = _integer(
                accelerator.get("count"),
                f"{path}.placement.accelerator_requirement.count",
            )
        labels = _mapping(
            placement_map.get("required_node_labels"),
            f"{path}.placement.required_node_labels",
        )
        placement = ScientificStagePlacement(
            accelerator_class=accelerator_class,
            accelerator_resource_name=accelerator_resource_name,
            accelerator_count=accelerator_count,
            compatible_pool_ids=_string_tuple(
                placement_map.get("compatible_pool_ids"),
                f"{path}.placement.compatible_pool_ids",
            ),
            required_node_labels=tuple(
                sorted(
                    (
                        _string(key, f"{path}.placement.required_node_labels key"),
                        _string(value, f"{path}.placement.required_node_labels.{key}"),
                    )
                    for key, value in labels.items()
                )
            ),
        )

        if expansion is not None and not expansion.enabled:
            if expansion.gang_size is not None or expansion.depends_on is not None:
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
                    resources=resources,
                    placement=placement,
                )
            )
        except ValueError as error:
            raise CatalogProfileAdapterError(f"{path} cannot form an internal stage plan: {error}") from error

    if selected:
        raise CatalogProfileAdapterError(f"expansions reference unknown catalog stages: {sorted(selected)}")
    active_stage_ids = {stage.stage_id for stage in stages}
    raw_storage = workload.get("storage")
    if not isinstance(raw_storage, list):
        raise CatalogProfileAdapterError("profile.workload.storage must be an array")
    storage: list[ScientificStorageRequirement] = []
    for index, raw_requirement in enumerate(raw_storage):
        path = f"profile.workload.storage[{index}]"
        requirement = _mapping(raw_requirement, path)
        read_only = requirement.get("read_only")
        if not isinstance(read_only, bool):
            raise CatalogProfileAdapterError(f"{path}.read_only must be a boolean")
        bound_stages = tuple(
            stage_id
            for stage_id in _string_tuple(requirement.get("stages"), f"{path}.stages")
            if stage_id in active_stage_ids
        )
        if not bound_stages:
            continue
        try:
            storage.append(
                ScientificStorageRequirement(
                    storage_id=_string(requirement.get("id"), f"{path}.id"),
                    purpose=_string(requirement.get("purpose"), f"{path}.purpose"),
                    minimum_bytes=_integer(requirement.get("minimum_bytes"), f"{path}.minimum_bytes"),
                    access_mode=StorageAccessMode(
                        _string(requirement.get("access_mode"), f"{path}.access_mode")
                    ),
                    read_only=read_only,
                    stages=bound_stages,
                )
            )
        except (ValueError, TypeError) as error:
            raise CatalogProfileAdapterError(f"{path} cannot form an internal storage requirement: {error}") from error
    try:
        return ScientificBatchPlan(stages=tuple(stages), storage=tuple(storage))
    except ValueError as error:
        raise CatalogProfileAdapterError(f"catalog workload cannot form an internal stage plan: {error}") from error


def validate_scientific_run_request(
    profile: Mapping[str, object],
    request: Mapping[str, object],
    request_schema: Mapping[str, object],
) -> None:
    """Fail closed against both the canonical envelope and model parameters."""

    Draft202012Validator(request_schema).validate(request)
    interface = _mapping(profile.get("interface"), "profile.interface")
    operations = _string_tuple(interface.get("operations"), "profile.interface.operations")
    service_classes = _string_tuple(interface.get("service_classes"), "profile.interface.service_classes")
    if request.get("operation") not in operations:
        raise CatalogProfileAdapterError("request.operation is not supported by this model variant")
    if request.get("service_class") not in service_classes:
        raise CatalogProfileAdapterError("request.service_class is not supported by this model variant")
    parameter_schema = _mapping(
        interface.get("parameter_schema_definition"),
        "profile.interface.parameter_schema_definition",
    )
    parameters = _mapping(request.get("parameters"), "request.parameters")
    Draft202012Validator(parameter_schema).validate(parameters)
