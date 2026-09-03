"""Operator-owned execution mapping for canonical scientific profiles.

This closed internal file supplies container/runtime details intentionally
absent from the public request schema. The public profile's immutable runtime
image digest must match before a manifest can be rendered.
"""

from __future__ import annotations

import copy
import importlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, cast
from uuid import UUID

from .capability import ScientificWorkloadCapabilityAuthority
from .catalog_adapter import CatalogProfileAdapterError
from .companion import RUNTIME_LOCALIZATION_SCHEMA
from .models import (
    AdapterExecutionPlan,
    ArtifactAccessContext,
    RuntimeArtifactFile,
    RuntimeArtifactLocalization,
    RuntimeArtifactMount,
    ScientificInputArtifact,
    StageInvocation,
    WorkloadKind,
    WorkloadResource,
)
from .profile_catalog import ScientificProfileCatalog, ScientificWorkloadProfile

EXECUTION_SCHEMA = "fs2-serve.nebius.ai/scientific-execution-map/v3"
MOUNT_KINDS = {"artifact-workspace", "reference", "private"}
REFERENCE_DATASETS_HOST_PATH = "/mnt/fs2-reference-data/data/datasets"
REFERENCE_DATA_STORAGE_LABEL = "storage.fs2.nebius/reference-data"
REFERENCE_DATA_GID = 1000
ACADEMIC_NAMESPACE = "fs2-academic-poc"
ACADEMIC_SERVICE_ACCOUNT = "fs2-academic-runner"
ACADEMIC_RUNTIME_PVC = "academic-assets-runtime-rwx"
ACADEMIC_RUNTIME_GID = 65532
BINDCRAFT_IMAGE_DIGEST = "sha256:9ec7eb93208ffd5ec88669e9a6714d8d1e9bffcea1bd5130ab81271095736aa1"
BINDCRAFT_AF2_ARTIFACT = "bindcraft-alphafold2-params"
BINDCRAFT_MPNN_ARTIFACT = "bindcraft-proteinmpnn-weights"
BINDCRAFT_PYROSETTA_ARTIFACT = "bindcraft-pyrosetta-installed-tree"
BINDCRAFT_PYROSETTA_DIGEST = "a93d68e198c81cbb87926e012dff6b50a73e99d9a41261e65f73d264c792aa8d"
BINDCRAFT_PYROSETTA_PATH = "/opt/fs2/academic/pyrosetta-bindcraft/site-packages"
BINDCRAFT_MPNN_PROJECTIONS = {
    "/opt/conda/lib/python3.10/site-packages/colabdesign/mpnn/weights": "vanilla_model_weights",
    "/opt/conda/lib/python3.10/site-packages/colabdesign/mpnn/weights_soluble": "soluble_model_weights",
}


class ScientificExecutionMapError(CatalogProfileAdapterError):
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
class StageMount:
    name: str
    kind: str
    claim_name: str | None
    host_path: str | None
    mount_path: str
    sub_path: str | None
    read_only: bool


@dataclass(frozen=True, slots=True)
class StageExecution:
    image: str
    collector_id: str
    validator_id: str
    mounts: tuple[StageMount, ...]
    service_account_name: str
    cpu: str
    memory: str
    ephemeral_storage: str
    active_deadline_seconds: int
    termination_grace_seconds: int
    environment: Mapping[str, str]
    required_node_labels: Mapping[str, str]


def _invocation_json(invocation: StageInvocation) -> str:
    return json.dumps(
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
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class FileScientificManifestRenderer:
    """Render fixed direct-argv Pods; only opaque controller IDs vary per run."""

    def __init__(
        self,
        *,
        path: Path,
        profiles: ScientificProfileCatalog,
        tools_image: str | None = None,
        internal_api_url: str | None = None,
        capability_authority: ScientificWorkloadCapabilityAuthority | None = None,
    ) -> None:
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
        runtime_artifacts: dict[tuple[str, str], RuntimeArtifactLocalization] = {}
        variants: dict[str, str] = {}
        workload_namespaces: dict[str, str] = {}
        plan_adapters: dict[str, tuple[str, str]] = {}
        for raw_model in models:
            model = _object(raw_model, "scientific execution model")
            if set(model) not in (
                {
                    "model_id",
                    "variant_id",
                    "workload_namespace",
                    "execution_identity_sha256",
                    "plan_adapter",
                    "stages",
                },
                {
                    "model_id",
                    "variant_id",
                    "workload_namespace",
                    "execution_identity_sha256",
                    "plan_adapter",
                    "runtime_artifacts",
                    "stages",
                },
            ):
                raise ScientificExecutionMapError("scientific execution model fields differ")
            model_id = model["model_id"]
            if not isinstance(model_id, str):
                raise ScientificExecutionMapError("scientific execution model ID is invalid")
            variant_id = _bounded_string(model["variant_id"], "scientific execution variant ID", maximum=128)
            if re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", variant_id) is None:
                raise ScientificExecutionMapError("scientific execution variant ID is invalid")
            if model_id in variants:
                raise ScientificExecutionMapError("scientific execution model ID is duplicated")
            variants[model_id] = variant_id
            workload_namespaces[model_id] = _service_account(model["workload_namespace"])
            plan_adapter = _object(model["plan_adapter"], "scientific plan adapter")
            if set(plan_adapter) != {"module", "function"}:
                raise ScientificExecutionMapError("scientific plan adapter fields differ")
            module = _bounded_string(plan_adapter["module"], "scientific plan adapter module", maximum=253)
            function = _bounded_string(plan_adapter["function"], "scientific plan adapter function", maximum=64)
            if module != "fs2_serve.scientific_batch.adapters" or function != "compile_adapter_run":
                raise ScientificExecutionMapError("scientific plan adapter is outside the packaged adapter namespace")
            plan_adapters[model_id] = (module, function)
            profile = profiles.get(model_id)
            identity = _object(profile.value["execution_identity"], "profile execution identity")
            if model["execution_identity_sha256"] != identity["execution_identity_sha256"]:
                raise ScientificExecutionMapError("execution map identity differs from the qualified profile")
            raw_runtime_artifacts = model.get("runtime_artifacts", [])
            if not isinstance(raw_runtime_artifacts, list) or len(raw_runtime_artifacts) > 64:
                raise ScientificExecutionMapError("runtime artifact localizations are not bounded")
            for raw_artifact in raw_runtime_artifacts:
                artifact = _object(raw_artifact, "runtime artifact localization")
                if set(artifact) != {
                    "artifact_id",
                    "mount_path",
                    "content_digest",
                    "file_manifest",
                    "localization_receipt_digest",
                }:
                    raise ScientificExecutionMapError("runtime artifact localization fields differ")
                artifact_id = _bounded_string(artifact["artifact_id"], "runtime artifact ID", maximum=128)
                key = (model_id, artifact_id)
                if key in runtime_artifacts:
                    raise ScientificExecutionMapError("runtime artifact localization is duplicated")
                file_manifest = artifact["file_manifest"]
                if not isinstance(file_manifest, list) or not 1 <= len(file_manifest) <= 4096:
                    raise ScientificExecutionMapError("runtime artifact file manifest is not bounded")
                files: list[RuntimeArtifactFile] = []
                for raw_file in file_manifest:
                    file = _object(raw_file, "runtime artifact file")
                    if set(file) != {"path", "sha256", "size_bytes"}:
                        raise ScientificExecutionMapError("runtime artifact file fields differ")
                    digest = _bounded_string(file["sha256"], "runtime artifact file digest", maximum=71)
                    files.append(
                        RuntimeArtifactFile(
                            path=_bounded_string(file["path"], "runtime artifact file path", maximum=512),
                            digest=digest if digest.startswith("sha256:") else f"sha256:{digest}",
                            size_bytes=_positive_integer(
                                file["size_bytes"], "runtime artifact file size", maximum=128 * 1024**3
                            ),
                        )
                    )
                content_digest = _bounded_string(
                    artifact["content_digest"], "runtime artifact content digest", maximum=71
                )
                receipt_digest = _bounded_string(
                    artifact["localization_receipt_digest"], "runtime artifact localization receipt", maximum=71
                )
                runtime_artifacts[key] = RuntimeArtifactLocalization(
                    logical_artifact_id=artifact_id,
                    mount_path=_bounded_string(artifact["mount_path"], "runtime artifact mount path", maximum=512),
                    content_digest=(
                        content_digest if content_digest.startswith("sha256:") else f"sha256:{content_digest}"
                    ),
                    files=tuple(files),
                    localization_receipt_digest=(
                        receipt_digest if receipt_digest.startswith("sha256:") else f"sha256:{receipt_digest}"
                    ),
                )
            stages = model["stages"]
            if not isinstance(stages, list) or not stages:
                raise ScientificExecutionMapError("scientific execution stages are absent")
            for raw_stage in stages:
                stage = _object(raw_stage, "scientific execution stage")
                allowed = {
                    "stage_id",
                    "image",
                    "collector_id",
                    "validator_id",
                    "mounts",
                    "service_account_name",
                    "resources",
                    "active_deadline_seconds",
                    "termination_grace_seconds",
                    "environment",
                    "required_node_labels",
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
                if model_id == "bindcraft" and image_digest != BINDCRAFT_IMAGE_DIGEST:
                    raise ScientificExecutionMapError("BindCraft must use the reviewed artifact-free runtime image")
                collector_id = _bounded_string(stage["collector_id"], "scientific collector ID", maximum=128)
                if re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?", collector_id) is None:
                    raise ScientificExecutionMapError("scientific collector ID is invalid")
                validator_id = _bounded_string(stage["validator_id"], "scientific validator ID", maximum=128)
                if re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?", validator_id) is None:
                    raise ScientificExecutionMapError("scientific validator ID is invalid")
                raw_mounts = stage["mounts"]
                if not isinstance(raw_mounts, list) or not 1 <= len(raw_mounts) <= 32:
                    raise ScientificExecutionMapError("scientific mounts must be a bounded array")
                mounts: list[StageMount] = []
                mount_names: set[str] = set()
                mount_paths: set[str] = set()
                kinds: list[str] = []
                for raw_mount in raw_mounts:
                    mount = _object(raw_mount, "scientific execution mount")
                    if set(mount) != {
                        "name",
                        "kind",
                        "claim_name",
                        "host_path",
                        "mount_path",
                        "sub_path",
                        "read_only",
                    }:
                        raise ScientificExecutionMapError("scientific execution mount fields differ")
                    name = _bounded_string(mount["name"], "scientific mount name", maximum=63)
                    if re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", name) is None or name in mount_names:
                        raise ScientificExecutionMapError("scientific mount name is invalid or duplicated")
                    kind = mount["kind"]
                    if kind not in MOUNT_KINDS:
                        raise ScientificExecutionMapError("scientific mount kind is invalid")
                    mount_path = _bounded_string(mount["mount_path"], "scientific mount path", maximum=512)
                    parsed_path = PurePosixPath(mount_path)
                    if (
                        not parsed_path.is_absolute()
                        or parsed_path == PurePosixPath("/")
                        or parsed_path.as_posix() != mount_path
                    ):
                        raise ScientificExecutionMapError("scientific mount path must be normalized and absolute")
                    if mount_path in mount_paths:
                        raise ScientificExecutionMapError("scientific mount path is duplicated")
                    read_only = mount["read_only"]
                    if not isinstance(read_only, bool):
                        raise ScientificExecutionMapError("scientific mount read_only must be boolean")
                    claim_name = mount["claim_name"]
                    host_path = mount["host_path"]
                    sub_path = mount["sub_path"]
                    if kind == "artifact-workspace":
                        if claim_name is not None or host_path is not None or sub_path is not None:
                            raise ScientificExecutionMapError(
                                "run artifact workspace must use attempt-local emptyDir storage"
                            )
                        if read_only or mount_path != "/mnt/fs2-scientific":
                            raise ScientificExecutionMapError(
                                "run artifact workspace must be writable at /mnt/fs2-scientific"
                            )
                    else:
                        if (claim_name is None) == (host_path is None):
                            raise ScientificExecutionMapError(
                                "reference/private mounts require exactly one physical read-only source"
                            )
                        if claim_name is not None:
                            claim_name = _bounded_string(claim_name, "scientific mount PVC", maximum=253)
                            if re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?", claim_name) is None:
                                raise ScientificExecutionMapError("scientific mount PVC is invalid")
                        if host_path is not None:
                            host_path = _bounded_string(host_path, "scientific mount host path", maximum=512)
                            parsed_host = PurePosixPath(host_path)
                            if (
                                not parsed_host.is_absolute()
                                or parsed_host == PurePosixPath("/")
                                or parsed_host.as_posix() != host_path
                            ):
                                raise ScientificExecutionMapError("scientific mount host path is unsafe")
                            if host_path != REFERENCE_DATASETS_HOST_PATH:
                                raise ScientificExecutionMapError(
                                    "reference-data host path must use the published datasets root"
                                )
                            if (
                                sub_path is None
                                or re.fullmatch(
                                    r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?/[^/]+/sha256/[0-9a-f]{64}",
                                    str(sub_path),
                                )
                                is None
                            ):
                                raise ScientificExecutionMapError(
                                    "reference-data host path requires an exact content-addressed dataset subPath"
                                )
                        if not read_only:
                            raise ScientificExecutionMapError("reference/private mounts require a read-only source")
                        if sub_path is not None:
                            sub_path = _bounded_string(sub_path, "scientific mount subPath", maximum=512)
                            parsed_sub_path = PurePosixPath(sub_path)
                            if parsed_sub_path.is_absolute() or any(
                                part in {"", ".", ".."} for part in parsed_sub_path.parts
                            ):
                                raise ScientificExecutionMapError("scientific mount subPath is unsafe")
                    mount_names.add(name)
                    mount_paths.add(mount_path)
                    kinds.append(cast(str, kind))
                    mounts.append(
                        StageMount(
                            name=name,
                            kind=cast(str, kind),
                            claim_name=cast(str | None, claim_name),
                            host_path=cast(str | None, host_path),
                            mount_path=mount_path,
                            sub_path=cast(str | None, sub_path),
                            read_only=read_only,
                        )
                    )
                if kinds.count("artifact-workspace") != 1:
                    raise ScientificExecutionMapError("each stage requires exactly one contained artifact workspace")
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
                required_node_labels = _object(stage["required_node_labels"], "scientific required node labels")
                if len(required_node_labels) > 32 or not all(
                    isinstance(key, str)
                    and re.fullmatch(r"[a-z0-9.-]+/[A-Za-z0-9][A-Za-z0-9._-]*", key) is not None
                    and isinstance(value, str)
                    and 1 <= len(value) <= 63
                    for key, value in required_node_labels.items()
                ):
                    raise ScientificExecutionMapError("scientific required node labels are invalid")
                if any(mount.host_path is not None for mount in mounts) and not required_node_labels:
                    raise ScientificExecutionMapError("host-path runtime artifacts require explicit storage placement")
                if (
                    any(mount.host_path is not None for mount in mounts)
                    and required_node_labels.get(REFERENCE_DATA_STORAGE_LABEL) != "true"
                ):
                    raise ScientificExecutionMapError(
                        "reference-data host path requires the canonical storage placement label"
                    )
                service_account_name = _service_account(stage["service_account_name"])
                if model_id == "alphafold3":
                    if workload_namespaces[model_id] != ACADEMIC_NAMESPACE:
                        raise ScientificExecutionMapError("AlphaFold3 must execute in the academic asset namespace")
                    if service_account_name != ACADEMIC_SERVICE_ACCOUNT:
                        raise ScientificExecutionMapError("AlphaFold3 must use the academic runner service account")
                    if any(
                        mount.kind == "private"
                        and mount.mount_path == "/opt/fs2/academic/alphafold3"
                        and mount.claim_name != ACADEMIC_RUNTIME_PVC
                        for mount in mounts
                    ):
                        raise ScientificExecutionMapError("AlphaFold3 parameters must use the academic runtime PVC")
                if model_id == "bindcraft":
                    if workload_namespaces[model_id] != ACADEMIC_NAMESPACE:
                        raise ScientificExecutionMapError("BindCraft must execute in the academic asset namespace")
                    if service_account_name != ACADEMIC_SERVICE_ACCOUNT:
                        raise ScientificExecutionMapError("BindCraft must use the academic runner service account")
                    by_path = {mount.mount_path: mount for mount in mounts}
                    required_paths = {
                        "/models/alphafold2",
                        BINDCRAFT_PYROSETTA_PATH,
                        *BINDCRAFT_MPNN_PROJECTIONS,
                    }
                    if not required_paths.issubset(by_path):
                        raise ScientificExecutionMapError("BindCraft is missing an exact runtime artifact target")
                    if by_path[BINDCRAFT_PYROSETTA_PATH].claim_name != ACADEMIC_RUNTIME_PVC:
                        raise ScientificExecutionMapError("BindCraft PyRosetta must use the academic runtime PVC")
                executions[(model_id, stage_id)] = StageExecution(
                    image=image,
                    collector_id=collector_id,
                    validator_id=validator_id,
                    mounts=tuple(mounts),
                    service_account_name=service_account_name,
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
                    required_node_labels=cast(Mapping[str, str], required_node_labels),
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
        self.variants = MappingProxyType(variants)
        self.workload_namespaces = MappingProxyType(workload_namespaces)
        self.plan_adapters = MappingProxyType(plan_adapters)
        self.runtime_artifacts = MappingProxyType(runtime_artifacts)
        self.tools_image = tools_image
        self.internal_api_url = internal_api_url
        self.capability_authority = capability_authority

    def variant_id(self, model_id: str) -> str:
        """Return the operator-owned runtime binding for one public model ID."""

        try:
            return self.variants[model_id]
        except KeyError as error:
            raise ScientificExecutionMapError("scientific model has no exact runtime variant binding") from error

    def workload_namespace(self, model_id: str) -> str:
        """Return the execution namespace frozen by the operator-owned map."""

        try:
            return self.workload_namespaces[model_id]
        except KeyError as error:
            raise ScientificExecutionMapError("scientific model has no execution namespace binding") from error

    def collector_id(self, model_id: str, stage_id: str) -> str:
        try:
            return self.executions[(model_id, stage_id)].collector_id
        except KeyError as error:
            raise ScientificExecutionMapError("scientific stage has no canonical collector binding") from error

    def plan(
        self,
        profile: ScientificWorkloadProfile,
        request: Mapping[str, Any],
        *,
        operation_id: UUID | None = None,
        access_context: ArtifactAccessContext,
        input_artifacts: tuple[ScientificInputArtifact, ...],
    ) -> AdapterExecutionPlan:
        """Compile the one canonical adapter-to-controller execution plan."""

        try:
            module_name, function_name = self.plan_adapters[profile.model_id]
            factory = getattr(importlib.import_module(module_name), function_name)
            plan = factory(
                profile.model_id,
                profile.value,
                request,
                operation_id=str(operation_id or UUID(int=0)),
                variant_id=self.variant_id(profile.model_id),
                access_context=access_context,
                input_artifacts=input_artifacts,
            )
        except (AttributeError, ImportError, KeyError, TypeError, ValueError) as error:
            raise ScientificExecutionMapError(
                "scientific plan adapter is unavailable or rejected the request"
            ) from error
        if not isinstance(plan, AdapterExecutionPlan):
            raise ScientificExecutionMapError("scientific plan adapter returned another controller type")
        if plan.model_id != profile.model_id or plan.variant_id != self.variant_id(profile.model_id):
            raise ScientificExecutionMapError("scientific adapter changed the execution-map binding")
        if plan.source_revision != profile.model_revision:
            raise ScientificExecutionMapError("scientific adapter source differs from the public profile revision")
        profile_stage_ids = tuple(
            cast(str, cast(Mapping[str, Any], item)["id"])
            for item in cast(Mapping[str, Any], profile.value["workload"])["stages"]
        )
        if tuple(stage.stage_id for stage in plan.controller_plan.stages) != profile_stage_ids:
            raise ScientificExecutionMapError("scientific plan adapter changed the canonical profile stages")
        invocations: list[StageInvocation] = []
        for invocation in plan.invocations:
            execution = self.executions[(profile.model_id, invocation.stage_id)]
            if invocation.collector_id not in {
                "controller-unbound",
                execution.collector_id,
            } or invocation.validator_id not in {
                "controller-unbound",
                execution.validator_id,
            }:
                raise ScientificExecutionMapError("adapter collector or validator differs from the execution map")
            invocations.append(
                replace(
                    invocation,
                    collector_id=execution.collector_id,
                    validator_id=execution.validator_id,
                )
            )
        bound_plan = replace(plan, invocations=tuple(invocations))
        if profile.model_id == "bindcraft":
            for invocation in bound_plan.invocations:
                if invocation.argv[:2] != ("python", "/opt/fs2/runtime_entrypoint.py"):
                    raise ScientificExecutionMapError(
                        "BindCraft must explicitly invoke the reviewed runtime artifact gate"
                    )
                environment = {
                    **self.executions[(profile.model_id, invocation.stage_id)].environment,
                    **dict(invocation.environment),
                }
                if environment.get("FS2_NETWORK_MODE") != "offline":
                    raise ScientificExecutionMapError("BindCraft must remain fail-closed and offline")
                pythonpath = environment.get("PYTHONPATH")
                if pythonpath is not None and pythonpath.split(":", 1)[0] != BINDCRAFT_PYROSETTA_PATH:
                    raise ScientificExecutionMapError("BindCraft PYTHONPATH bypasses the reviewed PyRosetta tree")
        return bound_plan

    def verify_runtime_artifacts(
        self,
        profile: ScientificWorkloadProfile,
        execution_plan: AdapterExecutionPlan,
        access_context: ArtifactAccessContext,
    ) -> tuple[RuntimeArtifactLocalization, ...]:
        """Resolve exact attested files before the batch row can be admitted."""

        requirements = profile.value.get("artifact_requirements", [])
        if not isinstance(requirements, list):
            raise ScientificExecutionMapError("profile runtime artifact requirements are invalid")
        by_id = {
            item.get("artifact_id"): item
            for item in requirements
            if isinstance(item, Mapping) and isinstance(item.get("artifact_id"), str)
        }
        result: list[RuntimeArtifactLocalization] = []
        for artifact_id in execution_plan.required_model_artifacts:
            localization = self.runtime_artifacts.get((profile.model_id, artifact_id))
            requirement = by_id.get(artifact_id)
            if localization is None or not isinstance(requirement, Mapping):
                raise ScientificExecutionMapError(f"runtime artifact {artifact_id} has no verified localization")
            expected_digest = requirement.get("content_digest_sha256")
            raw_files = requirement.get("file_manifest")
            if not isinstance(expected_digest, str) or not isinstance(raw_files, list):
                raise ScientificExecutionMapError(f"runtime artifact {artifact_id} profile evidence is incomplete")
            expected_files = {
                (item.get("path"), item.get("sha256"), item.get("size_bytes"))
                for item in raw_files
                if isinstance(item, Mapping)
            }
            localized_files = {
                (item.path, item.digest.removeprefix("sha256:"), item.size_bytes) for item in localization.files
            }
            if (
                localization.content_digest.removeprefix("sha256:") != expected_digest
                or expected_files != localized_files
                or set(cast(list[str], requirement.get("required_files", [])))
                != {item.path for item in localization.files}
            ):
                raise ScientificExecutionMapError(f"runtime artifact {artifact_id} localization evidence differs")
            for invocation in execution_plan.invocations:
                if artifact_id not in invocation.runtime_artifacts:
                    continue
                mounts = self.executions[(profile.model_id, invocation.stage_id)].mounts
                bindings = tuple(item for item in invocation.runtime_mounts if item.artifact_id == artifact_id)
                if not bindings:
                    bindings = (RuntimeArtifactMount(artifact_id=artifact_id, mount_path=localization.mount_path),)
                expected_manifest = requirement.get("localization_manifest_sha256")
                for binding in bindings:
                    binding_path = PurePosixPath(binding.mount_path)
                    candidates = [
                        source
                        for source in mounts
                        if source.kind in {"reference", "private"}
                        and (
                            binding_path == PurePosixPath(source.mount_path)
                            or PurePosixPath(source.mount_path) in binding_path.parents
                        )
                    ]
                    if not candidates:
                        raise ScientificExecutionMapError(
                            f"runtime artifact {artifact_id} is not covered by the stage's read-only mounts"
                        )
                    projected = profile.model_id == "bindcraft" and artifact_id == BINDCRAFT_MPNN_ARTIFACT
                    if (
                        (not projected and binding.mount_path != localization.mount_path)
                        or binding.expected_content_sha256
                        not in {None, localization.content_digest.removeprefix("sha256:")}
                        or binding.expected_manifest_sha256 not in {None, expected_manifest}
                        or binding.readiness_receipt_sha256
                        not in {None, localization.localization_receipt_digest.removeprefix("sha256:")}
                        or binding.authorization_receipt_sha256
                        not in {
                            None,
                            None
                            if access_context.receipt_digest is None
                            else access_context.receipt_digest.removeprefix("sha256:"),
                        }
                    ):
                        raise ScientificExecutionMapError(
                            f"runtime artifact {artifact_id} invocation mount differs from verified localization"
                        )
            result.append(localization)
        if not set(execution_plan.required_model_artifacts).issubset(by_id):
            raise ScientificExecutionMapError("adapter requires an undeclared profile runtime artifact")
        if profile.model_id == "bindcraft":
            self._verify_bindcraft_runtime(execution_plan, tuple(result))
        return tuple(result)

    @staticmethod
    def _verify_bindcraft_runtime(
        execution_plan: AdapterExecutionPlan,
        localizations: tuple[RuntimeArtifactLocalization, ...],
    ) -> None:
        required = {
            BINDCRAFT_AF2_ARTIFACT,
            BINDCRAFT_MPNN_ARTIFACT,
            BINDCRAFT_PYROSETTA_ARTIFACT,
        }
        if set(execution_plan.required_model_artifacts) != required:
            raise ScientificExecutionMapError("BindCraft runtime artifact identities are incomplete")
        localized = {item.logical_artifact_id: item for item in localizations}
        if "manifest.json" not in {item.path for item in localized[BINDCRAFT_AF2_ARTIFACT].files}:
            raise ScientificExecutionMapError("BindCraft AlphaFold2 parameters require manifest.json")
        mpnn_files = {item.path for item in localized[BINDCRAFT_MPNN_ARTIFACT].files}
        if any(
            not any(path.startswith(f"{sub_path}/") for path in mpnn_files)
            for sub_path in BINDCRAFT_MPNN_PROJECTIONS.values()
        ):
            raise ScientificExecutionMapError("BindCraft ProteinMPNN artifact lacks a verified weight subtree")
        pyrosetta = localized[BINDCRAFT_PYROSETTA_ARTIFACT]
        if pyrosetta.content_digest.removeprefix(
            "sha256:"
        ) != BINDCRAFT_PYROSETTA_DIGEST or "pyrosetta/__init__.py" not in {item.path for item in pyrosetta.files}:
            raise ScientificExecutionMapError("BindCraft PyRosetta installed-tree identity differs")
        for invocation in execution_plan.invocations:
            if invocation.argv[:2] != ("python", "/opt/fs2/runtime_entrypoint.py"):
                raise ScientificExecutionMapError("BindCraft must explicitly invoke the reviewed runtime artifact gate")
            by_artifact: dict[str, list[RuntimeArtifactMount]] = defaultdict(list)
            for mount in invocation.runtime_mounts:
                by_artifact[mount.artifact_id].append(mount)
            af2 = by_artifact[BINDCRAFT_AF2_ARTIFACT]
            pyrosetta_mounts = by_artifact[BINDCRAFT_PYROSETTA_ARTIFACT]
            mpnn = by_artifact[BINDCRAFT_MPNN_ARTIFACT]
            if len(af2) != 1 or af2[0].mount_path != "/models/alphafold2":
                raise ScientificExecutionMapError("BindCraft AlphaFold2 parameters use the wrong target")
            if (
                len(pyrosetta_mounts) != 1
                or pyrosetta_mounts[0].mount_path != BINDCRAFT_PYROSETTA_PATH
                or ACADEMIC_RUNTIME_GID not in pyrosetta_mounts[0].supplemental_groups
            ):
                raise ScientificExecutionMapError("BindCraft PyRosetta installed tree uses the wrong target")
            projections = {mount.mount_path: mount.sub_path for mount in mpnn}
            if projections != BINDCRAFT_MPNN_PROJECTIONS:
                raise ScientificExecutionMapError("BindCraft ProteinMPNN projections are not exact")

    def bind_runtime_artifacts(
        self,
        profile: ScientificWorkloadProfile,
        execution_plan: AdapterExecutionPlan,
        access_context: ArtifactAccessContext,
        localizations: tuple[RuntimeArtifactLocalization, ...],
    ) -> AdapterExecutionPlan:
        """Inject controller-observed receipts into the immutable adapter plan."""

        verified = self.verify_runtime_artifacts(profile, execution_plan, access_context)
        if verified != localizations:
            raise ScientificExecutionMapError("runtime localization changed during durable admission")
        by_id = {item.logical_artifact_id: item for item in verified}
        invocations: list[StageInvocation] = []
        for invocation in execution_plan.invocations:
            bindings: dict[str, list[RuntimeArtifactMount]] = defaultdict(list)
            for item in invocation.runtime_mounts:
                bindings[item.artifact_id].append(item)
            mounted: list[RuntimeArtifactMount] = []
            for artifact_id in invocation.runtime_artifacts:
                localization = by_id[artifact_id]
                artifact_bindings = bindings.get(artifact_id) or [
                    RuntimeArtifactMount(artifact_id=artifact_id, mount_path=localization.mount_path)
                ]
                mounted.extend(
                    replace(
                        binding,
                        expected_content_sha256=localization.content_digest.removeprefix("sha256:"),
                        authorization_receipt_sha256=(
                            None
                            if access_context.receipt_digest is None
                            else access_context.receipt_digest.removeprefix("sha256:")
                        ),
                        readiness_receipt_sha256=localization.localization_receipt_digest.removeprefix("sha256:"),
                    )
                    for binding in artifact_bindings
                )
            invocations.append(replace(invocation, runtime_mounts=tuple(mounted)))
        bound = replace(execution_plan, invocations=tuple(invocations))
        try:
            bound.assert_controller_bound()
        except ValueError as error:
            raise ScientificExecutionMapError("adapter plan is not fully bound for admission") from error
        return bound

    def _pod(self, resource: WorkloadResource, execution: StageExecution) -> dict[str, Any]:
        invocation = resource.invocation
        if not isinstance(invocation, StageInvocation):
            raise ScientificExecutionMapError("scientific workload has no canonical stage invocation")
        if invocation.collector_id != execution.collector_id or invocation.validator_id != execution.validator_id:
            raise ScientificExecutionMapError("scientific workload collector or validator binding changed")
        localized = {item.logical_artifact_id: item for item in resource.runtime_artifacts}
        binding_ids = {item.artifact_id for item in invocation.runtime_mounts}
        if set(localized) != set(invocation.runtime_artifacts) or binding_ids != set(invocation.runtime_artifacts):
            raise ScientificExecutionMapError("runtime artifact localization is incomplete at workload apply")
        for binding in invocation.runtime_mounts:
            artifact = localized[binding.artifact_id]
            projected = resource.model_id == "bindcraft" and binding.artifact_id == BINDCRAFT_MPNN_ARTIFACT
            if (
                (not projected and artifact.mount_path != binding.mount_path)
                or artifact.content_digest.removeprefix("sha256:") != binding.expected_content_sha256
                or artifact.localization_receipt_digest.removeprefix("sha256:") != binding.readiness_receipt_sha256
                or binding.authorization_receipt_sha256
                != (
                    None
                    if resource.access_context.receipt_digest is None
                    else resource.access_context.receipt_digest.removeprefix("sha256:")
                )
            ):
                raise ScientificExecutionMapError(
                    f"runtime artifact {binding.artifact_id} lost its verified localization binding before apply"
                )
        gpu_count = resource.scheduling.accelerator_count
        limits = {
            "cpu": execution.cpu,
            "memory": execution.memory,
            "ephemeral-storage": execution.ephemeral_storage,
        }
        if gpu_count:
            accelerator_resource = resource.scheduling.accelerator_resource_name
            if accelerator_resource is None:
                raise ScientificExecutionMapError("GPU scheduling has no accelerator resource")
            limits[accelerator_resource] = str(gpu_count)
        runtime_marker = {
            "schema": RUNTIME_LOCALIZATION_SCHEMA,
            "operation_id": str(resource.operation_id),
            "attempt_id": str(resource.attempt_id),
            "tenant_id": resource.tenant_id,
            "model_id": resource.model_id,
            "variant_id": resource.variant_id,
            "stage_id": resource.stage_id,
            "artifacts": [
                {
                    "artifact_id": item.logical_artifact_id,
                    "mount_path": binding.mount_path,
                    "content_digest": item.content_digest,
                    "artifact_manifest_sha256": (binding.expected_manifest_sha256 or binding.expected_content_sha256),
                    "localization_receipt_digest": item.localization_receipt_digest,
                    "sub_path": binding.sub_path,
                    "readiness_receipt_sha256": binding.readiness_receipt_sha256,
                    "authorization_receipt_sha256": binding.authorization_receipt_sha256,
                }
                for item in resource.runtime_artifacts
                for binding in invocation.runtime_mounts
                if binding.artifact_id == item.logical_artifact_id
            ],
        }
        runtime_marker_json = json.dumps(runtime_marker, sort_keys=True, separators=(",", ":"))
        runtime_marker_path = f"{invocation.working_directory}/.fs2/runtime-localization.json"
        env = [
            {"name": key, "value": value}
            for key, value in sorted(
                {
                    **execution.environment,
                    **dict(invocation.environment),
                    "FS2_OPERATION_ID": str(resource.operation_id),
                    "FS2_BATCH_ID": str(resource.batch_id),
                    "FS2_WORKLOAD_ID": str(resource.workload_id),
                    "FS2_ATTEMPT_ID": str(resource.attempt_id),
                    "FS2_STAGE_ID": resource.stage_id,
                    "FS2_SHARD_ID": resource.shard_id or "gang",
                    "FS2_VARIANT_ID": resource.variant_id,
                    "FS2_INPUT_ARTIFACT_ID": str(resource.input_artifact_id),
                    "FS2_TENANT_ID": resource.tenant_id,
                    "FS2_ARTIFACT_ACCESS_PROFILE": resource.access_context.profile,
                    "FS2_ARTIFACT_ACCESS_RECEIPT_DIGEST": resource.access_context.receipt_digest or "",
                    "FS2_COLLECTOR_ID": invocation.collector_id,
                    "FS2_VALIDATOR_ID": invocation.validator_id,
                    "FS2_RUN_ROOT": "/mnt/fs2-scientific",
                    "FS2_LOGICAL_OUTPUT_ID": invocation.produces,
                    "FS2_RUNTIME_ARTIFACTS_JSON": runtime_marker_json,
                    "FS2_RUNTIME_LOCALIZATION_MARKER": runtime_marker_path,
                }.items()
            )
        ]
        workspace = next((mount for mount in execution.mounts if mount.kind == "artifact-workspace"), None)
        if workspace is None:
            raise ScientificExecutionMapError("scientific stage has no attempt-local artifact workspace")
        volume_mounts: list[dict[str, Any]] = [
            {"name": workspace.name, "mountPath": workspace.mount_path, "readOnly": False}
        ]
        volumes: list[dict[str, Any]] = [{"name": workspace.name, "emptyDir": {}}]
        volume_names = {workspace.name}
        used_sources: set[str] = set()
        for binding in invocation.runtime_mounts:
            artifact_path = PurePosixPath(binding.mount_path)
            candidates = [
                source
                for source in execution.mounts
                if source.kind in {"reference", "private"}
                and (
                    artifact_path == PurePosixPath(source.mount_path)
                    or PurePosixPath(source.mount_path) in artifact_path.parents
                )
            ]
            if not candidates:
                raise ScientificExecutionMapError(
                    f"runtime artifact {binding.artifact_id} has no physical read-only volume source"
                )
            source = max(candidates, key=lambda item: len(PurePosixPath(item.mount_path).parts))
            if (
                resource.model_id == "alphafold3"
                and binding.artifact_id == "alphafold3-parameters"
                and (source.kind != "private" or source.claim_name != ACADEMIC_RUNTIME_PVC)
            ):
                raise ScientificExecutionMapError("AlphaFold3 parameters lost their academic PVC binding")
            if (
                resource.model_id == "alphafold3"
                and binding.artifact_id == "alphafold3-public-databases-v3.0"
                and (source.kind != "reference" or source.host_path != REFERENCE_DATASETS_HOST_PATH)
            ):
                raise ScientificExecutionMapError("AlphaFold3 databases lost their content-addressed reference binding")
            if source.host_path is not None and REFERENCE_DATA_GID not in binding.supplemental_groups:
                raise ScientificExecutionMapError("reference-data runtime mount requires its published group")
            if source.claim_name == ACADEMIC_RUNTIME_PVC and ACADEMIC_RUNTIME_GID not in binding.supplemental_groups:
                raise ScientificExecutionMapError("academic runtime mount requires its published group")
            used_sources.add(source.name)
            volume_mount: dict[str, Any] = {
                "name": source.name,
                "mountPath": binding.mount_path,
                "readOnly": True,
            }
            sub_parts = tuple(
                part
                for value in (source.sub_path, binding.sub_path)
                if value is not None
                for part in PurePosixPath(value).parts
            )
            if sub_parts:
                volume_mount["subPath"] = PurePosixPath(*sub_parts).as_posix()
            volume_mounts.append(volume_mount)
            if source.name not in volume_names:
                physical_source: dict[str, Any]
                if source.claim_name is not None:
                    physical_source = {"persistentVolumeClaim": {"claimName": source.claim_name, "readOnly": True}}
                else:
                    assert source.host_path is not None
                    physical_source = {"hostPath": {"path": source.host_path, "type": "Directory"}}
                volumes.append({"name": source.name, **physical_source})
                volume_names.add(source.name)
        declared_sources = {mount.name for mount in execution.mounts if mount.kind in {"reference", "private"}}
        if declared_sources != used_sources:
            raise ScientificExecutionMapError("stage declares an unbound broad runtime artifact volume")
        if self.tools_image is None or self.internal_api_url is None or self.capability_authority is None:
            raise ScientificExecutionMapError("scientific artifact companion runtime is not configured")
        capability = self.capability_authority.issue(resource)
        workspace_mount = next(mount for mount in volume_mounts if mount["mountPath"] == "/mnt/fs2-scientific")
        companion_env = [
            {"name": "FS2_SCIENTIFIC_INTERNAL_API_URL", "value": self.internal_api_url},
            {"name": "FS2_SCIENTIFIC_WORKLOAD_CAPABILITY", "value": capability},
            {"name": "FS2_STAGE_INVOCATION_JSON", "value": _invocation_json(invocation)},
        ]
        companion_security = {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "readOnlyRootFilesystem": True,
        }
        init_containers = [
            {
                "name": "prepare-workspace",
                "image": self.tools_image,
                "imagePullPolicy": "IfNotPresent",
                "command": [
                    "fs2-serve",
                    "scientific-prepare-workspace",
                    "--workspace",
                    invocation.working_directory,
                ],
                "env": [{"name": "FS2_RUNTIME_ARTIFACTS_JSON", "value": runtime_marker_json}],
                "volumeMounts": [workspace_mount],
                "resources": {
                    "requests": {"cpu": "50m", "memory": "64Mi"},
                    "limits": {"cpu": "500m", "memory": "256Mi"},
                },
                "securityContext": companion_security,
            }
        ]
        for index, materialization in enumerate(resource.materializations):
            command = [
                "fs2-serve",
                "scientific-materialize",
                "--logical-artifact-id",
                materialization.logical_artifact_id,
                "--artifact-id",
                str(materialization.artifact_id),
                "--destination",
                materialization.destination,
                "--mode",
                materialization.mode.value,
                "--expected-digest",
                materialization.digest,
                "--expected-size-bytes",
                str(materialization.size_bytes),
                "--expected-media-type",
                materialization.media_type,
            ]
            if materialization.compression is not None:
                command.extend(("--compression", materialization.compression))
            if materialization.yaml_name is not None:
                command.extend(("--yaml-name", materialization.yaml_name))
            if materialization.reuse_prefix is not None:
                command.extend(("--reuse-prefix", materialization.reuse_prefix))
            init_containers.append(
                {
                    "name": f"materialize-{index}",
                    "image": self.tools_image,
                    "imagePullPolicy": "IfNotPresent",
                    "command": command,
                    "env": companion_env,
                    "volumeMounts": [workspace_mount],
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "256Mi"},
                        "limits": {"cpu": "1", "memory": "1Gi"},
                    },
                    "securityContext": companion_security,
                }
            )
        collector = {
            "name": "artifact-collector",
            "image": self.tools_image,
            "imagePullPolicy": "IfNotPresent",
            "command": [
                "fs2-serve",
                "scientific-collect",
                "--collector-id",
                invocation.collector_id,
                "--workspace",
                invocation.working_directory,
                "--logical-output-id",
                invocation.produces,
                "--validator-id",
                invocation.validator_id,
                "--max-artifacts",
                str(invocation.max_output_artifacts),
                "--max-output-bytes",
                str(invocation.max_output_bytes),
            ],
            "env": companion_env,
            "volumeMounts": [workspace_mount],
            "resources": {
                "requests": {"cpu": "100m", "memory": "256Mi"},
                "limits": {"cpu": "2", "memory": "2Gi"},
            },
            "securityContext": companion_security,
        }
        supplemental_groups = sorted(
            {group for mount in invocation.runtime_mounts for group in mount.supplemental_groups}
        )
        pod_security: dict[str, Any] = {
            "runAsNonRoot": True,
            "seccompProfile": {"type": "RuntimeDefault"},
        }
        if supplemental_groups:
            # The published immutable trees are pre-owned. Supplemental groups
            # grant read access without fsGroup, so kubelet has no recursive
            # permission rewrite to perform for every scientific attempt.
            pod_security["supplementalGroups"] = supplemental_groups
        affinity: dict[str, Any] | None = None
        if gpu_count:
            if not resource.scheduling.resolved_pool_preference:
                raise ScientificExecutionMapError("GPU scheduling has no compatible pool constraint")
            affinity = {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {
                                        "key": "accelerator.fs2.nebius/pool-id",
                                        "operator": "In",
                                        "values": list(resource.scheduling.resolved_pool_preference),
                                    }
                                ]
                            }
                        ]
                    }
                }
            }
        pod_spec: dict[str, Any] = {
            "serviceAccountName": execution.service_account_name,
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "restartPolicy": "Never",
            "terminationGracePeriodSeconds": execution.termination_grace_seconds,
            "securityContext": pod_security,
            "initContainers": init_containers,
            "containers": [
                {
                    "name": "scientific-stage",
                    "image": execution.image,
                    "imagePullPolicy": "IfNotPresent",
                    "command": list(invocation.argv),
                    "workingDir": invocation.working_directory,
                    "env": env,
                    "volumeMounts": volume_mounts,
                    "resources": {"requests": copy.deepcopy(limits), "limits": limits},
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                    },
                },
                collector,
            ],
            "volumes": volumes,
        }
        if execution.required_node_labels:
            pod_spec["nodeSelector"] = dict(execution.required_node_labels)
        if affinity is not None:
            pod_spec["affinity"] = affinity
        return {
            "metadata": {},
            "spec": pod_spec,
        }

    def render(self, resource: WorkloadResource) -> Mapping[str, Any]:
        expected_namespace = self.workload_namespace(resource.model_id)
        if resource.namespace != expected_namespace or resource.route_namespace != expected_namespace:
            raise ScientificExecutionMapError(
                "scientific workload namespace differs from the immutable execution-map route"
            )
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
