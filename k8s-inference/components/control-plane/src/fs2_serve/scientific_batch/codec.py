"""Closed JSON codec for durable internal scientific-batch state.

This is deliberately not an API schema.  It serializes the controller's
``Scientific*`` records for PostgreSQL and rejects any missing or extra field
when reopening them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from .models import (
    AdapterExecutionPlan,
    ArtifactAccessContext,
    ArtifactMaterialization,
    AttemptOutcome,
    BatchStatus,
    CheckpointMode,
    ExecutionMode,
    FailureKind,
    LifecyclePhase,
    MaterializationMode,
    PreemptionMode,
    ResourceClass,
    RuntimeArtifactAggregateTree,
    RuntimeArtifactFile,
    RuntimeArtifactLocalization,
    RuntimeArtifactMount,
    SchedulingAdmission,
    SchedulingSnapshot,
    ScientificAttemptState,
    ScientificBatchPlan,
    ScientificBatchState,
    ScientificInputArtifact,
    ScientificStagePlan,
    ScientificStageState,
    ServiceClass,
    StageExecutionBinding,
    StageInvocation,
    StagePlacementClass,
    StageResourceEnvelope,
    StageSchedulingDecision,
    StageStatus,
    StageToleration,
    StageVolumeBinding,
    VerifiedInputManifest,
    WorkloadKind,
    WorkloadRef,
)

STATE_SCHEMA = "fs2-serve.nebius.ai/scientific-batch-state/v8"
PREVIOUS_STATE_SCHEMA = "fs2-serve.nebius.ai/scientific-batch-state/v7"
LEGACY_STATE_SCHEMA = "fs2-serve.nebius.ai/scientific-batch-state/v6"
MAX_STATE_BYTES = 4 * 1024 * 1024


def _object(value: object, keys: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"stored {label} is not an object")
    result = cast(Mapping[str, Any], value)
    if set(result) != keys:
        raise ValueError(f"stored {label} fields differ")
    return result


def _items(value: object, label: str, *, maximum: int = 4096) -> list[Any]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"stored {label} is not a bounded array")
    return value


def _datetime(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"stored {label} is not a timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"stored {label} timestamp is naive")
    return parsed


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"stored {label} is not an integer")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"stored {label} is not a boolean")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"stored {label} is not a string")
    return value


def _optional_string(value: object, label: str) -> str | None:
    return None if value is None else _string(value, label)


def _uuid(value: object, label: str) -> UUID:
    try:
        return UUID(_string(value, label))
    except ValueError as error:
        raise ValueError(f"stored {label} is not a UUID") from error


def _string_items(value: object, label: str, *, maximum: int) -> tuple[str, ...]:
    return tuple(_string(item, label) for item in _items(value, label, maximum=maximum))


def state_to_value(state: ScientificBatchState) -> dict[str, Any]:
    """Return canonical, payload-free internal state."""

    return {
        "schema_version": STATE_SCHEMA,
        "operation_id": str(state.operation_id),
        "batch_id": str(state.batch_id),
        "workload_id": str(state.workload_id),
        "tenant_id": state.tenant_id,
        "model_id": state.model_id,
        "variant_id": state.variant_id,
        "input_artifact_id": str(state.input_artifact_id),
        "access_context": {
            "profile": state.access_context.profile,
            "receipt_digest": state.access_context.receipt_digest,
            "tenant_id": state.access_context.tenant_id,
        },
        "input_manifest": (
            None
            if state.input_manifest is None
            else {
                "manifest_id": state.input_manifest.manifest_id,
                "manifest_artifact_id": str(state.input_manifest.manifest_artifact_id),
                "manifest_digest": state.input_manifest.manifest_digest,
                "entries": [
                    {
                        "logical_artifact_id": item.logical_artifact_id,
                        "semantic_type": item.semantic_type,
                        "artifact_id": str(item.artifact_id),
                        "digest": item.digest,
                        "size_bytes": item.size_bytes,
                        "media_type": item.media_type,
                        "compression": item.compression,
                    }
                    for item in state.input_manifest.entries
                ],
            }
        ),
        "runtime_artifacts": [
            {
                "logical_artifact_id": item.logical_artifact_id,
                "mount_path": item.mount_path,
                "content_digest": item.content_digest,
                "files": [
                    {"path": file.path, "digest": file.digest, "size_bytes": file.size_bytes} for file in item.files
                ],
                "localization_receipt_digest": item.localization_receipt_digest,
                "aggregate_tree": (
                    None
                    if item.aggregate_tree is None
                    else {
                        "tree_digest": item.aggregate_tree.tree_digest,
                        "manifest_digest": item.aggregate_tree.manifest_digest,
                        "manifest_algorithm": item.aggregate_tree.manifest_algorithm,
                        "file_count": item.aggregate_tree.file_count,
                        "expanded_bytes": item.aggregate_tree.expanded_bytes,
                        "canonical_path": item.aggregate_tree.canonical_path,
                    }
                ),
            }
            for item in state.runtime_artifacts
        ],
        "adapter_execution": (
            None
            if state.execution_plan is None
            else {
                "model_id": state.execution_plan.model_id,
                "variant_id": state.execution_plan.variant_id,
                "source_revision": state.execution_plan.source_revision,
                "request_sha256": state.execution_plan.request_sha256,
                "required_model_artifacts": list(state.execution_plan.required_model_artifacts),
                "execution_map_sha256": state.execution_plan.execution_map_sha256,
                "stage_bindings": [
                    {
                        "stage_id": binding.stage_id,
                        "image": binding.image,
                        "collector_id": binding.collector_id,
                        "validator_id": binding.validator_id,
                        "mounts": [
                            {
                                "name": mount.name,
                                "kind": mount.kind,
                                "claim_name": mount.claim_name,
                                "host_path": mount.host_path,
                                "mount_path": mount.mount_path,
                                "sub_path": mount.sub_path,
                                "read_only": mount.read_only,
                            }
                            for mount in binding.mounts
                        ],
                        "service_account_name": binding.service_account_name,
                        "cpu": binding.cpu,
                        "memory": binding.memory,
                        "ephemeral_storage": binding.ephemeral_storage,
                        "active_deadline_seconds": binding.active_deadline_seconds,
                        "termination_grace_seconds": binding.termination_grace_seconds,
                        "environment": [list(item) for item in binding.environment],
                        "required_node_labels": [list(item) for item in binding.required_node_labels],
                    }
                    for binding in state.execution_plan.stage_bindings
                ],
                "invocations": [
                    {
                        "stage_id": invocation.stage_id,
                        "shard_id": invocation.shard_id,
                        "argv": list(invocation.argv),
                        "environment": [list(item) for item in invocation.environment],
                        "working_directory": invocation.working_directory,
                        "consumes": list(invocation.consumes),
                        "produces": invocation.produces,
                        "collector_id": invocation.collector_id,
                        "validator_id": invocation.validator_id,
                        "handoff_name": invocation.handoff_name,
                        "max_output_artifacts": invocation.max_output_artifacts,
                        "max_output_bytes": invocation.max_output_bytes,
                        "runtime_artifacts": list(invocation.runtime_artifacts),
                        "runtime_mounts": [
                            {
                                "artifact_id": item.artifact_id,
                                "mount_path": item.mount_path,
                                "sub_path": item.sub_path,
                                "read_only": item.read_only,
                                "expected_content_sha256": item.expected_content_sha256,
                                "expected_manifest_sha256": item.expected_manifest_sha256,
                                "authorization_receipt_sha256": item.authorization_receipt_sha256,
                                "readiness_receipt_sha256": item.readiness_receipt_sha256,
                                "supplemental_groups": list(item.supplemental_groups),
                            }
                            for item in invocation.runtime_mounts
                        ],
                        "materializations": [
                            {
                                "artifact_id": item.artifact_id,
                                "destination": item.destination,
                                "mode": item.mode.value,
                                "compression": item.compression,
                                "yaml_name": item.yaml_name,
                                "reuse_prefix": item.reuse_prefix,
                            }
                            for item in invocation.materializations
                        ],
                    }
                    for invocation in state.execution_plan.invocations
                ],
            }
        ),
        "plan": {
            "stages": [
                {
                    "stage_id": stage.stage_id,
                    "depends_on": list(stage.depends_on),
                    "mode": stage.mode.value,
                    "shards": list(stage.shards),
                    "max_attempts": stage.max_attempts,
                    "gang_size": stage.gang_size,
                    "resource_class": stage.resource_class.value,
                    "min_parallelism": stage.min_parallelism,
                    "max_parallelism": stage.max_parallelism,
                    "checkpoint_mode": stage.checkpoint_mode.value,
                    "preemption_mode": stage.preemption_mode.value,
                    "placement_class": None if stage.placement_class is None else stage.placement_class.value,
                    "resources": (
                        None
                        if stage.resources is None
                        else {
                            "cpu_millis": stage.resources.cpu_millis,
                            "memory_bytes": stage.resources.memory_bytes,
                            "ephemeral_storage_bytes": stage.resources.ephemeral_storage_bytes,
                            "limit_cpu_millis": stage.resources.limit_cpu_millis,
                            "limit_memory_bytes": stage.resources.limit_memory_bytes,
                            "limit_ephemeral_storage_bytes": stage.resources.limit_ephemeral_storage_bytes,
                        }
                    ),
                }
                for stage in state.plan.stages
            ]
        },
        "scheduling": {
            "policy_revision": state.scheduling.policy_revision,
            "captured_at": state.scheduling.captured_at.isoformat(),
            "service_class": state.scheduling.service_class.value,
            "tenant_queue": state.scheduling.tenant_queue,
            "model_lane": state.scheduling.model_lane,
            "workload_namespace": state.scheduling.workload_namespace,
            "route_namespace": state.scheduling.route_namespace,
            "raw_contract_sha256": state.scheduling.raw_contract_sha256,
            "stages": [
                {
                    "stage_id": stage.stage_id,
                    "resource_class": stage.resource_class.value,
                    "resolved_cluster_queue": stage.resolved_cluster_queue,
                    "resolved_local_queue": stage.resolved_local_queue,
                    "workload_priority_class": stage.workload_priority_class,
                    "workload_priority_value": stage.workload_priority_value,
                    "resolved_pool_preference": list(stage.resolved_pool_preference),
                    "accelerator_resource_name": stage.accelerator_resource_name,
                    "accelerator_count": stage.accelerator_count,
                    "max_queue_seconds": stage.max_queue_seconds,
                    "max_execution_seconds": stage.max_execution_seconds,
                    "checkpoint_mode": stage.checkpoint_mode.value,
                    "preemption_mode": stage.preemption_mode.value,
                    "placement_class": None if stage.placement_class is None else stage.placement_class.value,
                    "workload_namespace": stage.workload_namespace,
                    "route_namespace": stage.route_namespace,
                    "requested_resource_flavor": stage.requested_resource_flavor,
                    "node_selector": [list(item) for item in stage.node_selector],
                    "tolerations": [
                        {
                            "key": item.key,
                            "operator": item.operator,
                            "value": item.value,
                            "effect": item.effect,
                        }
                        for item in stage.tolerations
                    ],
                }
                for stage in state.scheduling.stages
            ],
        },
        "stages": [
            {
                "stage_id": stage.stage_id,
                "status": stage.status.value,
                "failure_code": stage.failure_code,
                "attempts": [
                    {
                        "attempt_id": str(attempt.attempt_id),
                        "stage_id": attempt.stage_id,
                        "shard_id": attempt.shard_id,
                        "attempt_number": attempt.attempt_number,
                        "started_at": attempt.started_at.isoformat() if attempt.started_at is not None else None,
                        "completed_at": attempt.completed_at.isoformat() if attempt.completed_at is not None else None,
                        "workload": {
                            "namespace": attempt.workload.namespace,
                            "route_namespace": attempt.workload.route_namespace,
                            "name": attempt.workload.name,
                            "kind": attempt.workload.kind.value,
                            "uid": attempt.workload.uid,
                        },
                        "outcome": attempt.outcome.value,
                        "last_phase": attempt.last_phase.value,
                        "deletion_requested": attempt.deletion_requested,
                        "resource_released": attempt.resource_released,
                        "scheduling_admission": (
                            None
                            if attempt.scheduling_admission is None
                            else {
                                "resolved_pool_id": attempt.scheduling_admission.resolved_pool_id,
                                "admitted_resource_flavor": attempt.scheduling_admission.admitted_resource_flavor,
                                "accelerator_resource_name": attempt.scheduling_admission.accelerator_resource_name,
                                "accelerator_count": attempt.scheduling_admission.accelerator_count,
                                "admitted_at": attempt.scheduling_admission.admitted_at.isoformat(),
                            }
                        ),
                        "kueue_workload_uid": attempt.kueue_workload_uid,
                        "pod_uids": list(attempt.pod_uids),
                        "failure_kind": attempt.failure_kind.value if attempt.failure_kind else None,
                        "failure_code": attempt.failure_code,
                    }
                    for attempt in stage.attempts
                ],
            }
            for stage in state.stages
        ],
        "status": state.status.value,
        "revision": state.revision,
        "cancel_requested": state.cancel_requested,
        "failure_code": state.failure_code,
        "result_published": state.result_published,
    }


def state_to_json(state: ScientificBatchState) -> str:
    value = json.dumps(state_to_value(state), sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(value.encode("utf-8")) > MAX_STATE_BYTES:
        raise ValueError("scientific-batch state exceeds the durable bound")
    return value


def state_from_value(raw: object) -> ScientificBatchState:
    """Reopen and validate one exact internal state value."""

    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > MAX_STATE_BYTES:
            raise ValueError("stored scientific-batch state exceeds the durable bound")
        raw = json.loads(raw)
    value = _object(
        raw,
        {
            "schema_version",
            "operation_id",
            "batch_id",
            "workload_id",
            "tenant_id",
            "model_id",
            "variant_id",
            "input_artifact_id",
            "access_context",
            "input_manifest",
            "runtime_artifacts",
            "adapter_execution",
            "plan",
            "scheduling",
            "stages",
            "status",
            "revision",
            "cancel_requested",
            "failure_code",
            "result_published",
        },
        "scientific-batch state",
    )
    schema_version = value["schema_version"]
    if schema_version not in {LEGACY_STATE_SCHEMA, PREVIOUS_STATE_SCHEMA, STATE_SCHEMA}:
        raise ValueError("stored scientific-batch state schema is unsupported")
    legacy_v6 = schema_version == LEGACY_STATE_SCHEMA
    legacy_before_v8 = schema_version in {LEGACY_STATE_SCHEMA, PREVIOUS_STATE_SCHEMA}

    plan_value = _object(value["plan"], {"stages"}, "scientific-batch plan")
    plan_stages: list[ScientificStagePlan] = []
    stage_plan_keys = {
        "stage_id",
        "depends_on",
        "mode",
        "shards",
        "max_attempts",
        "gang_size",
        "resource_class",
        "min_parallelism",
        "max_parallelism",
        "checkpoint_mode",
        "preemption_mode",
    }
    if not legacy_before_v8:
        stage_plan_keys.update({"placement_class", "resources"})
    for raw_stage in _items(plan_value["stages"], "scientific-batch plan stages", maximum=64):
        stage = _object(raw_stage, stage_plan_keys, "scientific-batch plan stage")
        resources = None
        placement_class = None
        if not legacy_before_v8:
            placement_class = (
                None
                if stage["placement_class"] is None
                else StagePlacementClass(_string(stage["placement_class"], "stage placement class"))
            )
            if stage["resources"] is not None:
                resource_value = _object(
                    stage["resources"],
                    {
                        "cpu_millis",
                        "memory_bytes",
                        "ephemeral_storage_bytes",
                        "limit_cpu_millis",
                        "limit_memory_bytes",
                        "limit_ephemeral_storage_bytes",
                    },
                    "stage resources",
                )
                resources = StageResourceEnvelope(
                    cpu_millis=_integer(resource_value["cpu_millis"], "stage CPU request"),
                    memory_bytes=_integer(resource_value["memory_bytes"], "stage memory request"),
                    ephemeral_storage_bytes=_integer(
                        resource_value["ephemeral_storage_bytes"], "stage ephemeral storage request"
                    ),
                    limit_cpu_millis=_integer(resource_value["limit_cpu_millis"], "stage CPU limit"),
                    limit_memory_bytes=_integer(resource_value["limit_memory_bytes"], "stage memory limit"),
                    limit_ephemeral_storage_bytes=_integer(
                        resource_value["limit_ephemeral_storage_bytes"], "stage ephemeral storage limit"
                    ),
                )
        plan_stages.append(
            ScientificStagePlan(
                stage_id=_string(stage["stage_id"], "stage ID"),
                depends_on=_string_items(stage["depends_on"], "stage dependency", maximum=32),
                mode=ExecutionMode(_string(stage["mode"], "stage mode")),
                shards=_string_items(stage["shards"], "stage shard", maximum=1024),
                max_attempts=_integer(stage["max_attempts"], "stage max attempts"),
                gang_size=None if stage["gang_size"] is None else _integer(stage["gang_size"], "gang size"),
                resource_class=ResourceClass(_string(stage["resource_class"], "stage resource class")),
                min_parallelism=_integer(stage["min_parallelism"], "stage minimum parallelism"),
                max_parallelism=_integer(stage["max_parallelism"], "stage maximum parallelism"),
                checkpoint_mode=CheckpointMode(_string(stage["checkpoint_mode"], "stage checkpoint mode")),
                preemption_mode=PreemptionMode(_string(stage["preemption_mode"], "stage preemption mode")),
                placement_class=placement_class,
                resources=resources,
            )
        )
    plan = ScientificBatchPlan(tuple(plan_stages))

    access_value = _object(
        value["access_context"], {"profile", "receipt_digest", "tenant_id"}, "artifact access context"
    )
    access_context = ArtifactAccessContext(
        profile=_string(access_value["profile"], "artifact access profile"),
        receipt_digest=_optional_string(access_value["receipt_digest"], "artifact access receipt"),
        tenant_id=_optional_string(access_value["tenant_id"], "artifact access tenant"),
    )

    input_manifest = None
    if value["input_manifest"] is not None:
        manifest = _object(
            value["input_manifest"],
            {"manifest_id", "manifest_artifact_id", "manifest_digest", "entries"},
            "verified input manifest",
        )
        entries: list[ScientificInputArtifact] = []
        for raw_entry in _items(manifest["entries"], "verified input entries", maximum=10_000):
            entry = _object(
                raw_entry,
                {
                    "logical_artifact_id",
                    "semantic_type",
                    "artifact_id",
                    "digest",
                    "size_bytes",
                    "media_type",
                    "compression",
                },
                "verified input entry",
            )
            entries.append(
                ScientificInputArtifact(
                    logical_artifact_id=_string(entry["logical_artifact_id"], "input logical artifact ID"),
                    semantic_type=_string(entry["semantic_type"], "input semantic type"),
                    artifact_id=_uuid(entry["artifact_id"], "input artifact ID"),
                    digest=_string(entry["digest"], "input artifact digest"),
                    size_bytes=_integer(entry["size_bytes"], "input artifact size"),
                    media_type=_string(entry["media_type"], "input artifact media type"),
                    compression=_optional_string(entry["compression"], "input artifact compression"),
                )
            )
        input_manifest = VerifiedInputManifest(
            manifest_id=_string(manifest["manifest_id"], "input manifest ID"),
            manifest_artifact_id=_uuid(manifest["manifest_artifact_id"], "input manifest artifact ID"),
            manifest_digest=_string(manifest["manifest_digest"], "input manifest digest"),
            entries=tuple(entries),
        )

    runtime_artifacts: list[RuntimeArtifactLocalization] = []
    for raw_artifact in _items(value["runtime_artifacts"], "runtime artifact localizations", maximum=64):
        artifact_fields = {
            "logical_artifact_id",
            "mount_path",
            "content_digest",
            "files",
            "localization_receipt_digest",
        }
        if not legacy_before_v8:
            artifact_fields.add("aggregate_tree")
        artifact = _object(raw_artifact, artifact_fields, "runtime artifact localization")
        aggregate_tree = None
        if not legacy_before_v8 and artifact["aggregate_tree"] is not None:
            tree = _object(
                artifact["aggregate_tree"],
                {
                    "tree_digest",
                    "manifest_digest",
                    "manifest_algorithm",
                    "file_count",
                    "expanded_bytes",
                    "canonical_path",
                },
                "runtime artifact aggregate tree",
            )
            aggregate_tree = RuntimeArtifactAggregateTree(
                tree_digest=_string(tree["tree_digest"], "runtime artifact tree digest"),
                manifest_digest=_string(tree["manifest_digest"], "runtime artifact tree manifest digest"),
                manifest_algorithm=_string(tree["manifest_algorithm"], "runtime artifact tree manifest algorithm"),
                file_count=_integer(tree["file_count"], "runtime artifact tree file count"),
                expanded_bytes=_integer(tree["expanded_bytes"], "runtime artifact tree bytes"),
                canonical_path=_string(tree["canonical_path"], "runtime artifact tree path"),
            )
        runtime_artifacts.append(
            RuntimeArtifactLocalization(
                logical_artifact_id=_string(artifact["logical_artifact_id"], "runtime artifact ID"),
                mount_path=_string(artifact["mount_path"], "runtime artifact mount path"),
                content_digest=_string(artifact["content_digest"], "runtime artifact content digest"),
                files=tuple(
                    RuntimeArtifactFile(
                        path=_string(file["path"], "runtime artifact file path"),
                        digest=_string(file["digest"], "runtime artifact file digest"),
                        size_bytes=_integer(file["size_bytes"], "runtime artifact file size"),
                    )
                    for file in (
                        _object(raw_file, {"path", "digest", "size_bytes"}, "runtime artifact file")
                        for raw_file in _items(artifact["files"], "runtime artifact files", maximum=4096)
                    )
                ),
                localization_receipt_digest=_string(
                    artifact["localization_receipt_digest"], "runtime artifact localization receipt"
                ),
                aggregate_tree=aggregate_tree,
            )
        )

    adapter_execution = None
    if value["adapter_execution"] is not None:
        execution_fields = {
            "model_id",
            "variant_id",
            "source_revision",
            "request_sha256",
            "required_model_artifacts",
            "invocations",
        }
        if not legacy_before_v8:
            execution_fields.update({"execution_map_sha256", "stage_bindings"})
        execution = _object(value["adapter_execution"], execution_fields, "adapter execution")
        invocations: list[StageInvocation] = []
        for raw_invocation in _items(execution["invocations"], "adapter invocations"):
            invocation = _object(
                raw_invocation,
                {
                    "stage_id",
                    "shard_id",
                    "argv",
                    "environment",
                    "working_directory",
                    "consumes",
                    "produces",
                    "collector_id",
                    "validator_id",
                    "handoff_name",
                    "max_output_artifacts",
                    "max_output_bytes",
                    "runtime_artifacts",
                    "runtime_mounts",
                    "materializations",
                },
                "adapter invocation",
            )
            environment: list[tuple[str, str]] = []
            for raw_item in _items(invocation["environment"], "adapter environment", maximum=128):
                items = _items(raw_item, "adapter environment item", maximum=2)
                if len(items) != 2:
                    raise ValueError("stored adapter environment item fields differ")
                environment.append((_string(items[0], "environment key"), _string(items[1], "environment value")))
            materializations: list[ArtifactMaterialization] = []
            for raw_materialization in _items(invocation["materializations"], "artifact materializations", maximum=64):
                materialization = _object(
                    raw_materialization,
                    {"artifact_id", "destination", "mode", "compression", "yaml_name", "reuse_prefix"},
                    "artifact materialization",
                )
                materializations.append(
                    ArtifactMaterialization(
                        artifact_id=_string(materialization["artifact_id"], "logical artifact ID"),
                        destination=_string(materialization["destination"], "materialization destination"),
                        mode=MaterializationMode(_string(materialization["mode"], "materialization mode")),
                        compression=_optional_string(materialization["compression"], "artifact compression"),
                        yaml_name=_optional_string(materialization["yaml_name"], "BoltzGen YAML name"),
                        reuse_prefix=_optional_string(materialization["reuse_prefix"], "BoltzGen reuse prefix"),
                    )
                )
            runtime_mounts: list[RuntimeArtifactMount] = []
            for raw_mount in _items(invocation["runtime_mounts"], "runtime artifact mounts", maximum=64):
                runtime_mount_fields = {
                    "artifact_id",
                    "mount_path",
                    "sub_path",
                    "read_only",
                    "expected_content_sha256",
                    "authorization_receipt_sha256",
                    "readiness_receipt_sha256",
                    "supplemental_groups",
                }
                if not legacy_v6:
                    runtime_mount_fields.add("expected_manifest_sha256")
                mount = _object(
                    raw_mount,
                    runtime_mount_fields,
                    "runtime artifact mount",
                )
                runtime_mounts.append(
                    RuntimeArtifactMount(
                        artifact_id=_string(mount["artifact_id"], "runtime mount artifact ID"),
                        mount_path=_string(mount["mount_path"], "runtime mount path"),
                        sub_path=_optional_string(mount["sub_path"], "runtime mount sub-path"),
                        read_only=_boolean(mount["read_only"], "runtime mount read-only"),
                        expected_content_sha256=_optional_string(
                            mount["expected_content_sha256"], "runtime mount content digest"
                        ),
                        expected_manifest_sha256=(
                            None
                            if legacy_v6
                            else _optional_string(mount["expected_manifest_sha256"], "runtime mount manifest digest")
                        ),
                        authorization_receipt_sha256=_optional_string(
                            mount["authorization_receipt_sha256"], "runtime mount authorization receipt"
                        ),
                        readiness_receipt_sha256=_optional_string(
                            mount["readiness_receipt_sha256"], "runtime mount readiness receipt"
                        ),
                        supplemental_groups=tuple(
                            _integer(item, "runtime mount supplemental group")
                            for item in _items(
                                mount["supplemental_groups"], "runtime mount supplemental groups", maximum=32
                            )
                        ),
                    )
                )
            invocations.append(
                StageInvocation(
                    stage_id=_string(invocation["stage_id"], "invocation stage ID"),
                    shard_id=_string(invocation["shard_id"], "invocation shard ID"),
                    argv=_string_items(invocation["argv"], "invocation argv", maximum=64),
                    environment=tuple(environment),
                    working_directory=_string(invocation["working_directory"], "invocation working directory"),
                    consumes=_string_items(invocation["consumes"], "logical input", maximum=64),
                    produces=_string(invocation["produces"], "logical output"),
                    collector_id=_string(invocation["collector_id"], "collector ID"),
                    validator_id=_string(invocation["validator_id"], "validator ID"),
                    handoff_name=_optional_string(invocation["handoff_name"], "handoff entry name"),
                    max_output_artifacts=_integer(invocation["max_output_artifacts"], "maximum output artifacts"),
                    max_output_bytes=_integer(invocation["max_output_bytes"], "maximum output bytes"),
                    materializations=tuple(materializations),
                    runtime_artifacts=_string_items(invocation["runtime_artifacts"], "runtime artifact", maximum=64),
                    runtime_mounts=tuple(runtime_mounts),
                )
            )
        stage_bindings: list[StageExecutionBinding] = []
        if not legacy_before_v8:
            for raw_binding in _items(execution["stage_bindings"], "stage execution bindings", maximum=64):
                binding = _object(
                    raw_binding,
                    {
                        "stage_id",
                        "image",
                        "collector_id",
                        "validator_id",
                        "mounts",
                        "service_account_name",
                        "cpu",
                        "memory",
                        "ephemeral_storage",
                        "active_deadline_seconds",
                        "termination_grace_seconds",
                        "environment",
                        "required_node_labels",
                    },
                    "stage execution binding",
                )
                mounts = tuple(
                    StageVolumeBinding(
                        name=_string(mount["name"], "stage volume name"),
                        kind=_string(mount["kind"], "stage volume kind"),
                        claim_name=_optional_string(mount["claim_name"], "stage volume claim"),
                        host_path=_optional_string(mount["host_path"], "stage volume host path"),
                        mount_path=_string(mount["mount_path"], "stage volume mount path"),
                        sub_path=_optional_string(mount["sub_path"], "stage volume sub-path"),
                        read_only=_boolean(mount["read_only"], "stage volume read-only"),
                    )
                    for mount in (
                        _object(
                            raw_mount,
                            {
                                "name",
                                "kind",
                                "claim_name",
                                "host_path",
                                "mount_path",
                                "sub_path",
                                "read_only",
                            },
                            "stage volume binding",
                        )
                        for raw_mount in _items(binding["mounts"], "stage volume bindings", maximum=64)
                    )
                )

                def pairs(raw: object, label: str) -> tuple[tuple[str, str], ...]:
                    result: list[tuple[str, str]] = []
                    for raw_item in _items(raw, label, maximum=128):
                        item = _items(raw_item, label, maximum=2)
                        if len(item) != 2:
                            raise ValueError(f"stored {label} item fields differ")
                        result.append((_string(item[0], label), _string(item[1], label)))
                    return tuple(result)

                stage_bindings.append(
                    StageExecutionBinding(
                        stage_id=_string(binding["stage_id"], "stage execution binding ID"),
                        image=_string(binding["image"], "stage execution image"),
                        collector_id=_string(binding["collector_id"], "stage execution collector"),
                        validator_id=_string(binding["validator_id"], "stage execution validator"),
                        mounts=mounts,
                        service_account_name=_string(
                            binding["service_account_name"], "stage execution service account"
                        ),
                        cpu=_string(binding["cpu"], "stage execution CPU"),
                        memory=_string(binding["memory"], "stage execution memory"),
                        ephemeral_storage=_string(binding["ephemeral_storage"], "stage execution ephemeral storage"),
                        active_deadline_seconds=_integer(binding["active_deadline_seconds"], "stage active deadline"),
                        termination_grace_seconds=_integer(
                            binding["termination_grace_seconds"], "stage termination grace"
                        ),
                        environment=pairs(binding["environment"], "stage execution environment"),
                        required_node_labels=pairs(binding["required_node_labels"], "stage execution node label"),
                    )
                )
        adapter_execution = AdapterExecutionPlan(
            model_id=_string(execution["model_id"], "adapter model ID"),
            variant_id=_string(execution["variant_id"], "adapter variant ID"),
            source_revision=_string(execution["source_revision"], "adapter source revision"),
            request_sha256=_string(execution["request_sha256"], "adapter request digest"),
            controller_plan=plan,
            invocations=tuple(invocations),
            required_model_artifacts=_string_items(
                execution["required_model_artifacts"], "required runtime artifact", maximum=64
            ),
            execution_map_sha256=(
                None
                if legacy_before_v8
                else _optional_string(execution["execution_map_sha256"], "execution map digest")
            ),
            stage_bindings=tuple(stage_bindings),
        )

    scheduling_fields = {
        "policy_revision",
        "captured_at",
        "service_class",
        "tenant_queue",
        "model_lane",
        "stages",
    }
    if not legacy_v6:
        scheduling_fields.update({"workload_namespace", "route_namespace"})
    if not legacy_before_v8:
        scheduling_fields.add("raw_contract_sha256")
    scheduling_value = _object(
        value["scheduling"],
        scheduling_fields,
        "scientific-batch scheduling snapshot",
    )
    scheduling_keys = {
        "stage_id",
        "resolved_cluster_queue",
        "resolved_local_queue",
        "workload_priority_class",
        "workload_priority_value",
        "resolved_pool_preference",
        "accelerator_resource_name",
        "accelerator_count",
        "max_queue_seconds",
        "max_execution_seconds",
        "checkpoint_mode",
        "preemption_mode",
    }
    scheduling_keys.add("admitted_resource_flavor" if legacy_v6 else "resource_class")
    if not legacy_before_v8:
        scheduling_keys.update(
            {
                "placement_class",
                "workload_namespace",
                "route_namespace",
                "requested_resource_flavor",
                "node_selector",
                "tolerations",
            }
        )
    decisions: list[StageSchedulingDecision] = []
    for raw_decision in _items(scheduling_value["stages"], "scheduling stages", maximum=64):
        decision = _object(raw_decision, scheduling_keys, "scheduling stage")
        stage_id = _string(decision["stage_id"], "scheduling stage ID")
        if legacy_v6:
            _optional_string(decision["admitted_resource_flavor"], "legacy admitted ResourceFlavor")
        node_selector: tuple[tuple[str, str], ...] = ()
        tolerations: tuple[StageToleration, ...] = ()
        if not legacy_before_v8:
            selector_items: list[tuple[str, str]] = []
            for raw_selector in _items(decision["node_selector"], "stage node selector", maximum=32):
                selector = _items(raw_selector, "stage node selector item", maximum=2)
                if len(selector) != 2:
                    raise ValueError("stored stage node selector item fields differ")
                selector_items.append(
                    (_string(selector[0], "stage node selector key"), _string(selector[1], "stage node selector value"))
                )
            node_selector = tuple(selector_items)
            tolerations = tuple(
                StageToleration(
                    key=_string(item["key"], "stage toleration key"),
                    operator=_string(item["operator"], "stage toleration operator"),
                    value=_optional_string(item["value"], "stage toleration value"),
                    effect=_string(item["effect"], "stage toleration effect"),
                )
                for item in (
                    _object(raw_item, {"key", "operator", "value", "effect"}, "stage toleration")
                    for raw_item in _items(decision["tolerations"], "stage tolerations", maximum=32)
                )
            )
        decisions.append(
            StageSchedulingDecision(
                stage_id=stage_id,
                resource_class=(
                    plan.stage(stage_id).resource_class
                    if legacy_v6
                    else ResourceClass(_string(decision["resource_class"], "scheduling resource class"))
                ),
                resolved_cluster_queue=_string(decision["resolved_cluster_queue"], "cluster queue"),
                resolved_local_queue=_string(decision["resolved_local_queue"], "local queue"),
                workload_priority_class=_string(decision["workload_priority_class"], "priority class"),
                workload_priority_value=_integer(decision["workload_priority_value"], "workload priority"),
                resolved_pool_preference=_string_items(
                    decision["resolved_pool_preference"], "pool preference", maximum=256
                ),
                accelerator_resource_name=_optional_string(
                    decision["accelerator_resource_name"], "accelerator resource"
                ),
                accelerator_count=_integer(decision["accelerator_count"], "accelerator count"),
                max_queue_seconds=(
                    None
                    if decision["max_queue_seconds"] is None
                    else _integer(decision["max_queue_seconds"], "maximum queue seconds")
                ),
                max_execution_seconds=(
                    None
                    if decision["max_execution_seconds"] is None
                    else _integer(decision["max_execution_seconds"], "maximum execution seconds")
                ),
                checkpoint_mode=CheckpointMode(_string(decision["checkpoint_mode"], "checkpoint mode")),
                preemption_mode=PreemptionMode(_string(decision["preemption_mode"], "preemption mode")),
                placement_class=(
                    None
                    if legacy_before_v8 or decision["placement_class"] is None
                    else StagePlacementClass(_string(decision["placement_class"], "stage placement class"))
                ),
                workload_namespace=(
                    None
                    if legacy_before_v8
                    else _optional_string(decision["workload_namespace"], "stage workload namespace")
                ),
                route_namespace=(
                    None if legacy_before_v8 else _optional_string(decision["route_namespace"], "stage route namespace")
                ),
                requested_resource_flavor=(
                    None
                    if legacy_before_v8
                    else _optional_string(decision["requested_resource_flavor"], "requested resource flavor")
                ),
                node_selector=node_selector,
                tolerations=tolerations,
            )
        )
    legacy_namespaces: set[str] = set()
    if legacy_v6:
        raw_stage_states = value["stages"]
        if isinstance(raw_stage_states, list):
            for raw_stage_state in raw_stage_states:
                if not isinstance(raw_stage_state, Mapping):
                    continue
                raw_attempts = raw_stage_state.get("attempts")
                if not isinstance(raw_attempts, list):
                    continue
                for raw_attempt in raw_attempts:
                    if not isinstance(raw_attempt, Mapping):
                        continue
                    raw_workload = raw_attempt.get("workload")
                    if isinstance(raw_workload, Mapping) and isinstance(raw_workload.get("namespace"), str):
                        legacy_namespaces.add(cast(str, raw_workload["namespace"]))
        if len(legacy_namespaces) > 1:
            raise ValueError("stored v6 scientific-batch state spans multiple workload namespaces")
    workload_namespace = (
        next(iter(legacy_namespaces), "fs2-models")
        if legacy_v6
        else _string(scheduling_value["workload_namespace"], "workload namespace")
    )
    route_namespace = (
        workload_namespace if legacy_v6 else _string(scheduling_value["route_namespace"], "route namespace")
    )
    scheduling = SchedulingSnapshot(
        policy_revision=_string(scheduling_value["policy_revision"], "scheduling policy revision"),
        captured_at=_datetime(scheduling_value["captured_at"], "scheduling capture"),
        service_class=ServiceClass(_string(scheduling_value["service_class"], "service class")),
        tenant_queue=_string(scheduling_value["tenant_queue"], "tenant queue"),
        model_lane=_string(scheduling_value["model_lane"], "model lane"),
        workload_namespace=workload_namespace,
        route_namespace=route_namespace,
        stages=tuple(decisions),
        raw_contract_sha256=(
            None
            if legacy_before_v8
            else _optional_string(scheduling_value["raw_contract_sha256"], "raw scheduling contract digest")
        ),
    )

    stage_state_keys = {"stage_id", "status", "failure_code", "attempts"}
    attempt_keys = {
        "attempt_id",
        "stage_id",
        "shard_id",
        "attempt_number",
        "started_at",
        "completed_at",
        "workload",
        "outcome",
        "last_phase",
        "resource_released",
        "scheduling_admission",
        "kueue_workload_uid",
        "pod_uids",
        "failure_kind",
        "failure_code",
    }
    if not legacy_v6:
        attempt_keys.add("deletion_requested")
    workload_keys = {"namespace", "name", "kind", "uid"}
    if not legacy_v6:
        workload_keys.add("route_namespace")
    stage_states: list[ScientificStageState] = []
    for raw_stage_state in _items(value["stages"], "scientific-batch stages", maximum=64):
        stage_state = _object(raw_stage_state, stage_state_keys, "scientific-batch stage")
        attempts: list[ScientificAttemptState] = []
        for raw_attempt in _items(stage_state["attempts"], "scientific attempts"):
            attempt = _object(raw_attempt, attempt_keys, "scientific attempt")
            workload = _object(attempt["workload"], workload_keys, "scientific attempt workload")
            admission = None
            if attempt["scheduling_admission"] is not None:
                admission_value = _object(
                    attempt["scheduling_admission"],
                    {
                        "resolved_pool_id",
                        "admitted_resource_flavor",
                        "accelerator_resource_name",
                        "accelerator_count",
                        "admitted_at",
                    },
                    "scientific attempt scheduling admission",
                )
                admission = SchedulingAdmission(
                    resolved_pool_id=_optional_string(admission_value["resolved_pool_id"], "resolved pool ID"),
                    admitted_resource_flavor=_optional_string(
                        admission_value["admitted_resource_flavor"], "admitted ResourceFlavor"
                    ),
                    accelerator_resource_name=_optional_string(
                        admission_value["accelerator_resource_name"], "admitted accelerator resource"
                    ),
                    accelerator_count=_integer(admission_value["accelerator_count"], "admitted accelerator count"),
                    admitted_at=_datetime(admission_value["admitted_at"], "Kueue admission time"),
                )
            attempts.append(
                ScientificAttemptState(
                    attempt_id=_uuid(attempt["attempt_id"], "attempt ID"),
                    stage_id=_string(attempt["stage_id"], "attempt stage ID"),
                    shard_id=_optional_string(attempt["shard_id"], "attempt shard ID"),
                    attempt_number=_integer(attempt["attempt_number"], "attempt number"),
                    started_at=(
                        None if attempt["started_at"] is None else _datetime(attempt["started_at"], "attempt start")
                    ),
                    completed_at=(
                        None
                        if attempt["completed_at"] is None
                        else _datetime(attempt["completed_at"], "attempt completion")
                    ),
                    workload=WorkloadRef(
                        namespace=_string(workload["namespace"], "workload namespace"),
                        name=_string(workload["name"], "workload name"),
                        kind=WorkloadKind(_string(workload["kind"], "workload kind")),
                        uid=_optional_string(workload["uid"], "workload UID"),
                        route_namespace=(
                            _string(workload["namespace"], "workload namespace")
                            if legacy_v6
                            else _string(workload["route_namespace"], "workload route namespace")
                        ),
                    ),
                    outcome=AttemptOutcome(_string(attempt["outcome"], "attempt outcome")),
                    last_phase=LifecyclePhase(_string(attempt["last_phase"], "attempt phase")),
                    deletion_requested=(
                        False if legacy_v6 else _boolean(attempt["deletion_requested"], "attempt deletion request")
                    ),
                    resource_released=_boolean(attempt["resource_released"], "attempt resource release"),
                    scheduling_admission=admission,
                    kueue_workload_uid=_optional_string(attempt["kueue_workload_uid"], "Kueue Workload UID"),
                    pod_uids=_string_items(attempt["pod_uids"], "Pod UID", maximum=64),
                    failure_kind=(
                        None
                        if attempt["failure_kind"] is None
                        else FailureKind(_string(attempt["failure_kind"], "attempt failure kind"))
                    ),
                    failure_code=_optional_string(attempt["failure_code"], "attempt failure code"),
                )
            )
        stage_states.append(
            ScientificStageState(
                stage_id=_string(stage_state["stage_id"], "stage state ID"),
                status=StageStatus(_string(stage_state["status"], "stage status")),
                attempts=tuple(attempts),
                failure_code=_optional_string(stage_state["failure_code"], "stage failure code"),
            )
        )

    return ScientificBatchState(
        operation_id=_uuid(value["operation_id"], "operation ID"),
        batch_id=_uuid(value["batch_id"], "batch ID"),
        workload_id=_uuid(value["workload_id"], "workload ID"),
        tenant_id=_string(value["tenant_id"], "tenant ID"),
        model_id=_string(value["model_id"], "model ID"),
        variant_id=_string(value["variant_id"], "variant ID"),
        input_artifact_id=_uuid(value["input_artifact_id"], "input artifact ID"),
        plan=plan,
        scheduling=scheduling,
        stages=tuple(stage_states),
        execution_plan=adapter_execution,
        access_context=access_context,
        input_manifest=input_manifest,
        runtime_artifacts=tuple(runtime_artifacts),
        status=BatchStatus(_string(value["status"], "batch status")),
        revision=_integer(value["revision"], "revision"),
        cancel_requested=_boolean(value["cancel_requested"], "cancel request"),
        failure_code=_optional_string(value["failure_code"], "batch failure code"),
        result_published=_boolean(value["result_published"], "terminal result publication"),
    )
