"""Operator-owned execution mapping for canonical scientific profiles.

This closed internal file supplies container/runtime details intentionally
absent from the public request schema. The public profile's immutable runtime
image digest must match before a manifest can be rendered.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .models import WorkloadKind, WorkloadResource
from .profile_catalog import ScientificProfileCatalog

EXECUTION_SCHEMA = "fs2-serve.nebius.ai/scientific-execution-map/v1"


class ScientificExecutionMapError(RuntimeError):
    """The trusted execution map is absent or differs from the profile."""


def _object(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ScientificExecutionMapError(f"{label} is not an object")
    return cast(Mapping[str, Any], value)


def _strings(value: object, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= 64
        or not all(isinstance(item, str) and 1 <= len(item) <= 4096 for item in value)
    ):
        raise ScientificExecutionMapError(f"{label} must be a non-empty string array")
    return value


def _positive_integer(value: object, label: str, *, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
        raise ScientificExecutionMapError(f"{label} must be an integer between 1 and {maximum}")
    return value


def _bounded_string(value: object, label: str, *, maximum: int = 253) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ScientificExecutionMapError(f"{label} must be a bounded non-empty string")
    return value


def _service_account(value: object) -> str:
    result = _bounded_string(value, "scientific service account", maximum=63)
    if re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", result) is None:
        raise ScientificExecutionMapError("scientific service account is invalid")
    return result


@dataclass(frozen=True, slots=True)
class StageExecution:
    image: str
    command: tuple[str, ...]
    args: tuple[str, ...]
    service_account_name: str
    cpu: str
    memory: str
    ephemeral_storage: str
    active_deadline_seconds: int
    termination_grace_seconds: int
    environment: Mapping[str, str]


class FileScientificManifestRenderer:
    """Render fixed direct-argv Pods; only opaque controller IDs vary per run."""

    def __init__(self, *, path: Path, profiles: ScientificProfileCatalog) -> None:
        try:
            raw = path.read_bytes()
            if len(raw) > 4 * 1024 * 1024:
                raise ScientificExecutionMapError("scientific execution map exceeds the bound")
            value = json.loads(raw)
        except (OSError, RecursionError, ValueError) as error:
            raise ScientificExecutionMapError("scientific execution map is unavailable or invalid") from error
        root = _object(value, "scientific execution map")
        if set(root) != {"schema", "models"} or root["schema"] != EXECUTION_SCHEMA:
            raise ScientificExecutionMapError("scientific execution map schema is unsupported")
        models = root["models"]
        if not isinstance(models, list) or len(models) > 256:
            raise ScientificExecutionMapError("scientific execution models are not bounded")
        executions: dict[tuple[str, str], StageExecution] = {}
        for raw_model in models:
            model = _object(raw_model, "scientific execution model")
            if set(model) != {"model_id", "execution_identity_sha256", "stages"}:
                raise ScientificExecutionMapError("scientific execution model fields differ")
            model_id = model["model_id"]
            if not isinstance(model_id, str):
                raise ScientificExecutionMapError("scientific execution model ID is invalid")
            profile = profiles.get(model_id)
            identity = _object(profile.value["execution_identity"], "profile execution identity")
            if model["execution_identity_sha256"] != identity["execution_identity_sha256"]:
                raise ScientificExecutionMapError("execution map identity differs from the qualified profile")
            stages = model["stages"]
            if not isinstance(stages, list) or not stages:
                raise ScientificExecutionMapError("scientific execution stages are absent")
            for raw_stage in stages:
                stage = _object(raw_stage, "scientific execution stage")
                allowed = {
                    "stage_id",
                    "image",
                    "command",
                    "args",
                    "service_account_name",
                    "resources",
                    "active_deadline_seconds",
                    "termination_grace_seconds",
                    "environment",
                }
                if set(stage) != allowed:
                    raise ScientificExecutionMapError("scientific execution stage fields differ")
                stage_id = stage["stage_id"]
                if not isinstance(stage_id, str) or (model_id, stage_id) in executions:
                    raise ScientificExecutionMapError("scientific execution stage identity is invalid")
                image = stage["image"]
                image_digest = identity["runtime_image_digest"]
                if not isinstance(image, str) or len(image) > 1024 or not image.endswith(f"@{image_digest}"):
                    raise ScientificExecutionMapError("execution image is not the profile's immutable digest")
                command = _strings(stage["command"], "scientific command")
                if command[0] in {"sh", "/bin/sh", "bash", "/bin/bash"} or "-c" in command[:3]:
                    raise ScientificExecutionMapError("scientific command cannot invoke a shell")
                args = stage["args"]
                if (
                    not isinstance(args, list)
                    or len(args) > 256
                    or not all(isinstance(item, str) and len(item) <= 4096 for item in args)
                ):
                    raise ScientificExecutionMapError("scientific args must be a string array")
                resources = _object(stage["resources"], "scientific execution resources")
                if set(resources) != {"cpu", "memory", "ephemeral_storage"}:
                    raise ScientificExecutionMapError("scientific execution resource fields differ")
                cpu = _bounded_string(resources["cpu"], "scientific CPU quantity", maximum=32)
                memory = _bounded_string(resources["memory"], "scientific memory quantity", maximum=32)
                ephemeral_storage = _bounded_string(
                    resources["ephemeral_storage"], "scientific ephemeral-storage quantity", maximum=32
                )
                if re.fullmatch(r"[1-9][0-9]*(?:m)?", cpu) is None:
                    raise ScientificExecutionMapError("scientific CPU quantity is invalid")
                if any(
                    re.fullmatch(r"[1-9][0-9]*(?:Ki|Mi|Gi|Ti)", item) is None for item in (memory, ephemeral_storage)
                ):
                    raise ScientificExecutionMapError("scientific storage quantity is invalid")
                environment = _object(stage["environment"], "scientific execution environment")
                if len(environment) > 128 or not all(
                    isinstance(key, str)
                    and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,252}", key) is not None
                    and isinstance(value, str)
                    and len(value) <= 4096
                    for key, value in environment.items()
                ):
                    raise ScientificExecutionMapError("scientific execution environment is invalid")
                executions[(model_id, stage_id)] = StageExecution(
                    image=image,
                    command=tuple(command),
                    args=tuple(cast(list[str], args)),
                    service_account_name=_service_account(stage["service_account_name"]),
                    cpu=cpu,
                    memory=memory,
                    ephemeral_storage=ephemeral_storage,
                    active_deadline_seconds=_positive_integer(
                        stage["active_deadline_seconds"], "scientific active deadline", maximum=7 * 24 * 3600
                    ),
                    termination_grace_seconds=_positive_integer(
                        stage["termination_grace_seconds"],
                        "scientific termination grace",
                        maximum=24 * 3600,
                    ),
                    environment=cast(Mapping[str, str], environment),
                )
        expected: set[tuple[str, str]] = set()
        for profile in profiles.list():
            workload = _object(profile.value["workload"], "scientific profile workload")
            stages = workload.get("stages")
            if not isinstance(stages, list):
                raise ScientificExecutionMapError("scientific profile stages are invalid")
            for raw_stage in stages:
                stage = _object(raw_stage, "scientific profile stage")
                stage_id = stage.get("id")
                if not isinstance(stage_id, str):
                    raise ScientificExecutionMapError("scientific profile stage ID is invalid")
                expected.add((profile.model_id, stage_id))
        if set(executions) != expected:
            raise ScientificExecutionMapError("execution map must cover every runnable profile stage exactly")
        self.executions = executions

    @staticmethod
    def _pod(resource: WorkloadResource, execution: StageExecution) -> dict[str, Any]:
        gpu_count = resource.scheduling.accelerator_count
        limits = {
            "cpu": execution.cpu,
            "memory": execution.memory,
            "ephemeral-storage": execution.ephemeral_storage,
        }
        if gpu_count:
            limits[resource.scheduling.accelerator_resource_name] = str(gpu_count)
        env = [
            {"name": key, "value": value}
            for key, value in sorted(
                {
                    **execution.environment,
                    "FS2_OPERATION_ID": str(resource.operation_id),
                    "FS2_BATCH_ID": str(resource.batch_id),
                    "FS2_WORKLOAD_ID": str(resource.workload_id),
                    "FS2_ATTEMPT_ID": str(resource.attempt_id),
                    "FS2_STAGE_ID": resource.stage_id,
                    "FS2_SHARD_ID": resource.shard_id or "gang",
                }.items()
            )
        ]
        return {
            "metadata": {},
            "spec": {
                "serviceAccountName": execution.service_account_name,
                "automountServiceAccountToken": False,
                "enableServiceLinks": False,
                "restartPolicy": "Never",
                "terminationGracePeriodSeconds": execution.termination_grace_seconds,
                "securityContext": {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [
                    {
                        "name": "scientific-stage",
                        "image": execution.image,
                        "imagePullPolicy": "IfNotPresent",
                        "command": list(execution.command),
                        "args": list(execution.args),
                        "env": env,
                        "resources": {"requests": copy.deepcopy(limits), "limits": limits},
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]},
                        },
                    }
                ],
            },
        }

    def render(self, resource: WorkloadResource) -> Mapping[str, Any]:
        execution = self.executions.get((resource.model_id, resource.stage_id))
        if execution is None:
            raise ScientificExecutionMapError("scientific stage has no qualified execution mapping")
        pod = self._pod(resource, execution)
        metadata = {"name": resource.name, "namespace": resource.namespace}
        if resource.kind is WorkloadKind.JOB:
            return {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": metadata,
                "spec": {
                    "activeDeadlineSeconds": execution.active_deadline_seconds,
                    "template": pod,
                },
            }
        assert resource.gang_size is not None
        return {
            "apiVersion": "jobset.x-k8s.io/v1alpha2",
            "kind": "JobSet",
            "metadata": metadata,
            "spec": {
                "failurePolicy": {"maxRestarts": 0},
                "replicatedJobs": [
                    {
                        "name": "gang",
                        "replicas": resource.gang_size,
                        "template": {
                            "spec": {
                                "backoffLimit": 0,
                                "activeDeadlineSeconds": execution.active_deadline_seconds,
                                "template": pod,
                            }
                        },
                    }
                ],
            },
        }
