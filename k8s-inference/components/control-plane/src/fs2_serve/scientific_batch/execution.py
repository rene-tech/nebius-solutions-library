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
    RuntimeArtifactAggregateTree,
    RuntimeArtifactFile,
    RuntimeArtifactLocalization,
    RuntimeArtifactMount,
    RuntimeArtifactNodeAccessibility,
    ScientificInputArtifact,
    StageInvocation,
    WorkloadKind,
    WorkloadResource,
)
from .profile_catalog import ScientificProfileCatalog, ScientificWorkloadProfile

EXECUTION_SCHEMA = "fs2-serve.nebius.ai/scientific-execution-map/v3"
MOUNT_KINDS = {"artifact-workspace", "reference", "private", "operator-host-path", "cache"}
DEFAULT_CONTROLLER_SERVICE_ACCOUNT_NAMESPACE = "fs2-system"
DEFAULT_CONTROLLER_SERVICE_ACCOUNT_NAME = "fs2-serve-control-plane-runtime"
AF3_REFERENCE_HOST_PATH = "/mnt/fs2-reference-data/data"
AF3_REFERENCE_ARTIFACT = "alphafold3-public-databases-v3.0"
AF3_REFERENCE_REVISION = "v3.0-paper-snapshot-2022-09-28"
AF3_REFERENCE_MARKER = ".fs2-manifest-sha256"
AF3_REFERENCE_DATASET_PREFIX = f"datasets/{AF3_REFERENCE_ARTIFACT}/{AF3_REFERENCE_REVISION}/sha256"
# Both AlphaFold 3 stages run in the one namespace that holds the licensed
# claim and the durable controller state. Only the queue and the pool differ:
# preprocessing is CPU work on the academic CPU LocalQueue, inference is GPU
# work on the academic accelerator LocalQueue.
AF3_EXECUTION_NAMESPACE = "fs2-academic-poc"
AF3_GPU_LOCAL_QUEUE = "academic-scientific"
AF3_CPU_LOCAL_QUEUE = "academic-scientific-cpu"
AF3_GPU_CLUSTER_QUEUE = "inference-accelerators"
AF3_CPU_CLUSTER_QUEUE = "reference-data-cpu"
AF3_SERVICE_ACCOUNT = "fs2-academic-runner"
AF3_PARAMETER_SUPPLEMENTAL_GROUP = 65532
AF3_REFERENCE_SUPPLEMENTAL_GROUP = 1000
_AF3_ACCELERATOR_SELECTOR = {"accelerator.fs2.nebius/class": "nvidia-h100-sxm5-80gb"}
_AF3_REFERENCE_ACCESS_SELECTOR = {"storage.fs2.nebius/reference-data": "true"}
# The reference-data CPU pool of the accepted reference-data placement contract.
# Deliberately carries no accelerator label: a stage that requests a GPU while
# doing database search would hold an idle H100 for the length of an MSA.
_AF3_REFERENCE_SELECTOR = {
    "capacity.fs2.nebius/pool": "reference-data",
    "capacity.fs2.nebius/type": "regular",
    **_AF3_REFERENCE_ACCESS_SELECTOR,
    "workload.fs2.nebius/reference-data": "true",
}
_DEDICATED_INFERENCE_TOLERATION = ("dedicated", "Equal", "fs2-inference", "NoSchedule")
_REFERENCE_DATA_TOLERATION = ("workload.fs2.nebius/reference-data", "Equal", "true", "NoSchedule")
_AF3_STAGE_NODE_SELECTORS = {
    ("alphafold3", "data-pipeline"): _AF3_REFERENCE_SELECTOR,
    ("alphafold3", "inference"): _AF3_ACCELERATOR_SELECTOR,
}
_AF3_STAGE_TOLERATIONS = {
    ("alphafold3", "data-pipeline"): (_REFERENCE_DATA_TOLERATION,),
    ("alphafold3", "inference"): (_DEDICATED_INFERENCE_TOLERATION,),
}
_AF3_STAGE_QUEUES = {
    ("alphafold3", "data-pipeline"): (AF3_CPU_LOCAL_QUEUE, AF3_CPU_CLUSTER_QUEUE),
    ("alphafold3", "inference"): (AF3_GPU_LOCAL_QUEUE, AF3_GPU_CLUSTER_QUEUE),
}
_RUNTIME_ARTIFACT_SUPPLEMENTAL_GROUPS = {
    ("alphafold3", "data-pipeline", AF3_REFERENCE_ARTIFACT): (AF3_REFERENCE_SUPPLEMENTAL_GROUP,),
    ("alphafold3", "inference", "alphafold3-parameters"): (AF3_PARAMETER_SUPPLEMENTAL_GROUP,),
}
# The whole reference plane, read-only, with no subPath. The receipt, the
# dataset tree, its readiness marker and the manifest describing it are
# siblings under this root, so mounting only the dataset would hide three of
# the four documents a preprocessing run has to read.
AF3_REFERENCE_MOUNT_PATH = "/reference-data"
_OPERATOR_HOST_PATH_ALLOWLIST = {
    (
        "alphafold3",
        "data-pipeline",
        AF3_REFERENCE_ARTIFACT,
    ): (AF3_REFERENCE_HOST_PATH, AF3_REFERENCE_MOUNT_PATH),
}
_OPERATOR_HOST_PATH_REQUIRED_NODE_SELECTORS = {
    (
        "alphafold3",
        "data-pipeline",
        AF3_REFERENCE_ARTIFACT,
    ): _AF3_REFERENCE_ACCESS_SELECTOR,
}
_WRITABLE_CACHE_CONTRACTS = {
    ("alphafold3", "inference"): (
        "/cache/alphafold3",
        {
            "FS2_AF3_CACHE_ROOT": "/cache/alphafold3",
            "FS2_AF3_JAX_CACHE_DIR": "/cache/alphafold3/jax",
            "FS2_AF3_TRITON_CACHE_DIR": "/cache/alphafold3/triton",
            "FS2_AF3_XDG_CACHE_DIR": "/cache/alphafold3/xdg",
        },
    ),
    ("openfold3", "inference"): (
        "/cache/openfold3",
        {
            "TRITON_CACHE_DIR": "/cache/openfold3/triton",
            "TORCH_EXTENSIONS_DIR": "/cache/openfold3/torch-extensions",
            "XDG_CACHE_HOME": "/cache/openfold3/xdg",
        },
    ),
    ("protenix-v2", "sample-structure"): (
        "/cache/protenix",
        {
            "TRITON_CACHE_DIR": "/cache/protenix/triton",
            "CUEQ_TRITON_CACHE_DIR": "/cache/protenix/cueq-triton",
            "TORCH_EXTENSIONS_DIR": "/cache/protenix/torch-extensions",
            "XDG_CACHE_HOME": "/cache/protenix/xdg",
        },
    ),
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


def _kubernetes_name(value: object, label: str) -> str:
    result = _bounded_string(value, label, maximum=63)
    if re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", result) is None:
        raise ScientificExecutionMapError(f"{label} is invalid")
    return result


@dataclass(frozen=True, slots=True)
class StageMount:
    name: str
    kind: str
    artifact_id: str | None
    claim_name: str | None
    claim_namespace: str | None
    host_path: str | None
    operator_owned: bool
    mount_path: str
    sub_path: str | None
    read_only: bool
    supplemental_groups: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class StageExecution:
    namespace: str
    local_queue_name: str
    cluster_queue_name: str | None
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
    node_selector: Mapping[str, str]
    tolerations: tuple[tuple[str, str, str, str], ...]


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
            "namespace": invocation.namespace,
            "local_queue_name": invocation.local_queue_name,
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
        controller_service_account_namespace: str = DEFAULT_CONTROLLER_SERVICE_ACCOUNT_NAMESPACE,
        controller_service_account_name: str = DEFAULT_CONTROLLER_SERVICE_ACCOUNT_NAME,
    ) -> None:
        try:
            raw = path.read_bytes()
            if len(raw) > 4 * 1024 * 1024:
                raise ScientificExecutionMapError("scientific execution map exceeds the bound")
            value = json.loads(raw)
        except (OSError, RecursionError, ValueError) as error:
            raise ScientificExecutionMapError("scientific execution map is unavailable or invalid") from error
        root = _object(value, "scientific execution map")
        if set(root) != {"schema", "controller_service_account", "models"} or root["schema"] != EXECUTION_SCHEMA:
            raise ScientificExecutionMapError("scientific execution map schema is unsupported")
        configured_controller = (
            _kubernetes_name(
                controller_service_account_namespace,
                "scientific controller ServiceAccount namespace",
            ),
            _service_account(controller_service_account_name),
        )
        raw_controller = _object(
            root["controller_service_account"],
            "scientific controller ServiceAccount",
        )
        if set(raw_controller) != {"namespace", "name"}:
            raise ScientificExecutionMapError("scientific controller ServiceAccount fields differ")
        mapped_controller = (
            _kubernetes_name(
                raw_controller["namespace"],
                "scientific execution-map controller namespace",
            ),
            _service_account(raw_controller["name"]),
        )
        if mapped_controller != configured_controller:
            raise ScientificExecutionMapError(
                "scientific execution-map controller ServiceAccount differs from deployment configuration"
            )
        models = root["models"]
        if not isinstance(models, list) or len(models) > 256:
            raise ScientificExecutionMapError("scientific execution models are not bounded")
        executions: dict[tuple[str, str], StageExecution] = {}
        runtime_artifacts: dict[tuple[str, str], RuntimeArtifactLocalization] = {}
        variants: dict[str, str] = {}
        plan_adapters: dict[str, tuple[str, str]] = {}
        for raw_model in models:
            model = _object(raw_model, "scientific execution model")
            if set(model) not in (
                {"model_id", "variant_id", "execution_identity_sha256", "plan_adapter", "stages"},
                {
                    "model_id",
                    "variant_id",
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
                file_fields = {
                    "artifact_id",
                    "mount_path",
                    "content_digest",
                    "file_manifest",
                    "localization_receipt_digest",
                }
                aggregate_fields = {
                    "artifact_id",
                    "mount_path",
                    "content_digest",
                    "aggregate_tree",
                    "localization_receipt_digest",
                }
                if frozenset(artifact) not in {frozenset(file_fields), frozenset(aggregate_fields)}:
                    raise ScientificExecutionMapError("runtime artifact localization fields differ")
                artifact_id = _bounded_string(artifact["artifact_id"], "runtime artifact ID", maximum=128)
                key = (model_id, artifact_id)
                if key in runtime_artifacts:
                    raise ScientificExecutionMapError("runtime artifact localization is duplicated")
                content_digest = _bounded_string(
                    artifact["content_digest"], "runtime artifact content digest", maximum=71
                )
                normalized_content_digest = (
                    content_digest if content_digest.startswith("sha256:") else f"sha256:{content_digest}"
                )
                receipt_digest = _bounded_string(
                    artifact["localization_receipt_digest"], "runtime artifact localization receipt", maximum=71
                )
                files: list[RuntimeArtifactFile] = []
                aggregate_tree = None
                if set(artifact) == file_fields:
                    file_manifest = artifact["file_manifest"]
                    if not isinstance(file_manifest, list) or not 1 <= len(file_manifest) <= 4096:
                        raise ScientificExecutionMapError("runtime artifact file manifest is not bounded")
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
                else:
                    raw_tree = _object(artifact["aggregate_tree"], "runtime artifact aggregate tree")
                    if set(raw_tree) != {
                        "manifest_digest",
                        "dataset_relative_path",
                        "dataset_uri",
                        "file_count",
                        "node_accessibility",
                    }:
                        raise ScientificExecutionMapError("runtime artifact aggregate-tree fields differ")
                    raw_accessibility = _object(raw_tree["node_accessibility"], "runtime artifact node accessibility")
                    if set(raw_accessibility) != {
                        "evidence_receipt_digest",
                        "required_node_labels",
                        "node_names",
                    }:
                        raise ScientificExecutionMapError("runtime artifact node-accessibility fields differ")
                    raw_node_names = raw_accessibility["node_names"]
                    if not isinstance(raw_node_names, list) or raw_node_names:
                        raise ScientificExecutionMapError(
                            "AF3 runtime artifact placement must use the trusted selector, not node IDs"
                        )
                    raw_accessibility_labels = _object(
                        raw_accessibility["required_node_labels"],
                        "runtime artifact node-accessibility selector",
                    )
                    if raw_accessibility_labels != _AF3_REFERENCE_ACCESS_SELECTOR:
                        raise ScientificExecutionMapError(
                            "runtime artifact node-accessibility selector differs from the trusted reference mount"
                        )
                    manifest_digest = _bounded_string(
                        raw_tree["manifest_digest"], "runtime artifact aggregate-tree manifest", maximum=71
                    )
                    node_receipt = _bounded_string(
                        raw_accessibility["evidence_receipt_digest"],
                        "runtime artifact node-accessibility receipt",
                        maximum=71,
                    )
                    aggregate_tree = RuntimeArtifactAggregateTree(
                        manifest_digest=(
                            manifest_digest if manifest_digest.startswith("sha256:") else f"sha256:{manifest_digest}"
                        ),
                        dataset_relative_path=_bounded_string(
                            raw_tree["dataset_relative_path"],
                            "runtime artifact aggregate-tree dataset path",
                            maximum=1024,
                        ),
                        dataset_uri=_bounded_string(
                            raw_tree["dataset_uri"], "runtime artifact aggregate-tree dataset URI", maximum=1200
                        ),
                        file_count=_positive_integer(
                            raw_tree["file_count"],
                            "runtime artifact aggregate-tree file count",
                            maximum=100_000_000,
                        ),
                        node_accessibility=RuntimeArtifactNodeAccessibility(
                            evidence_receipt_digest=(
                                node_receipt if node_receipt.startswith("sha256:") else f"sha256:{node_receipt}"
                            ),
                            required_node_labels=tuple(
                                sorted(cast(Mapping[str, str], raw_accessibility_labels).items())
                            ),
                            node_names=tuple(
                                _bounded_string(item, "runtime artifact accessible node", maximum=253)
                                for item in raw_node_names
                            ),
                        ),
                    )
                    expected_relative_path = (
                        f"{AF3_REFERENCE_DATASET_PREFIX}/{normalized_content_digest.removeprefix('sha256:')}"
                    )
                    if (
                        key != ("alphafold3", AF3_REFERENCE_ARTIFACT)
                        or aggregate_tree.dataset_relative_path != expected_relative_path
                        or PurePosixPath(aggregate_tree.dataset_relative_path).name
                        != normalized_content_digest.removeprefix("sha256:")
                        or _bounded_string(artifact["mount_path"], "runtime artifact mount path", maximum=512)
                        != AF3_REFERENCE_MOUNT_PATH
                    ):
                        raise ScientificExecutionMapError(
                            "runtime aggregate tree is outside the exact AF3 dataset layout"
                        )
                runtime_artifacts[key] = RuntimeArtifactLocalization(
                    logical_artifact_id=artifact_id,
                    mount_path=_bounded_string(artifact["mount_path"], "runtime artifact mount path", maximum=512),
                    content_digest=normalized_content_digest,
                    files=tuple(files),
                    localization_receipt_digest=(
                        receipt_digest if receipt_digest.startswith("sha256:") else f"sha256:{receipt_digest}"
                    ),
                    aggregate_tree=aggregate_tree,
                )
            stages = model["stages"]
            if not isinstance(stages, list) or not stages:
                raise ScientificExecutionMapError("scientific execution stages are absent")
            for raw_stage in stages:
                stage = _object(raw_stage, "scientific execution stage")
                allowed = {
                    "stage_id",
                    "execution_namespace",
                    "local_queue_name",
                    "cluster_queue_name",
                    "image",
                    "collector_id",
                    "validator_id",
                    "mounts",
                    "service_account_name",
                    "resources",
                    "active_deadline_seconds",
                    "termination_grace_seconds",
                    "environment",
                    "node_selector",
                    "tolerations",
                }
                if set(stage) - allowed or not (
                    allowed - {"cluster_queue_name", "node_selector", "tolerations"}
                ).issubset(stage):
                    raise ScientificExecutionMapError("scientific execution stage fields differ")
                stage_id = stage["stage_id"]
                if not isinstance(stage_id, str) or (model_id, stage_id) in executions:
                    raise ScientificExecutionMapError("scientific execution stage identity is invalid")
                execution_namespace = _kubernetes_name(stage["execution_namespace"], "scientific execution namespace")
                local_queue_name = _kubernetes_name(stage["local_queue_name"], "scientific execution LocalQueue")
                cluster_queue_name = (
                    None
                    if stage.get("cluster_queue_name") is None
                    else _kubernetes_name(stage["cluster_queue_name"], "scientific execution ClusterQueue")
                )
                image = stage["image"]
                image_digest = identity["runtime_image_digest"]
                if not isinstance(image, str) or len(image) > 1024 or not image.endswith(f"@{image_digest}"):
                    raise ScientificExecutionMapError("execution image is not the profile's immutable digest")
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
                    mount_fields = {
                        "name",
                        "kind",
                        "artifact_id",
                        "claim_name",
                        "claim_namespace",
                        "host_path",
                        "operator_owned",
                        "mount_path",
                        "sub_path",
                        "read_only",
                        "supplemental_groups",
                    }
                    if set(mount) - mount_fields or not (mount_fields - {"supplemental_groups"}).issubset(mount):
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
                    operator_owned = mount["operator_owned"]
                    if not isinstance(read_only, bool) or not isinstance(operator_owned, bool):
                        raise ScientificExecutionMapError("scientific mount ownership/read-only flags must be boolean")
                    raw_supplemental_groups = mount.get("supplemental_groups", [])
                    if (
                        not isinstance(raw_supplemental_groups, list)
                        or len(raw_supplemental_groups) > 32
                        or any(
                            not isinstance(group, int) or isinstance(group, bool) or group < 1 or group > 2**31 - 1
                            for group in raw_supplemental_groups
                        )
                        or len(raw_supplemental_groups) != len(set(raw_supplemental_groups))
                    ):
                        raise ScientificExecutionMapError("scientific mount supplemental groups are invalid")
                    supplemental_groups = tuple(sorted(cast(list[int], raw_supplemental_groups)))
                    artifact_id = mount["artifact_id"]
                    claim_name = mount["claim_name"]
                    claim_namespace = mount["claim_namespace"]
                    host_path = mount["host_path"]
                    sub_path = mount["sub_path"]
                    if artifact_id is not None:
                        artifact_id = _bounded_string(artifact_id, "scientific mount artifact ID", maximum=128)
                        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", artifact_id) is None:
                            raise ScientificExecutionMapError("scientific mount artifact ID is invalid")
                    if sub_path is not None:
                        sub_path = _bounded_string(sub_path, "scientific mount subPath", maximum=512)
                        parsed_sub_path = PurePosixPath(sub_path)
                        if parsed_sub_path.is_absolute() or any(
                            part in {"", ".", ".."} for part in parsed_sub_path.parts
                        ):
                            raise ScientificExecutionMapError("scientific mount subPath is unsafe")
                    if kind == "artifact-workspace":
                        if (
                            any(
                                value is not None
                                for value in (artifact_id, claim_name, claim_namespace, host_path, sub_path)
                            )
                            or operator_owned
                        ):
                            raise ScientificExecutionMapError(
                                "run artifact workspace must use attempt-local emptyDir storage"
                            )
                        if read_only or mount_path != "/mnt/fs2-scientific":
                            raise ScientificExecutionMapError(
                                "run artifact workspace must be writable at /mnt/fs2-scientific"
                            )
                    elif kind in {"reference", "private", "cache"}:
                        if artifact_id is None and kind != "cache":
                            raise ScientificExecutionMapError(
                                "reference/private PVC mounts require one logical artifact ID"
                            )
                        if artifact_id is not None and kind == "cache":
                            raise ScientificExecutionMapError("cache PVCs cannot impersonate runtime artifacts")
                        claim_name = _bounded_string(claim_name, "scientific mount PVC", maximum=253)
                        claim_namespace = _kubernetes_name(claim_namespace, "scientific mount PVC namespace")
                        if (
                            re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?", claim_name) is None
                            or claim_namespace != execution_namespace
                            or host_path is not None
                            or not operator_owned
                            or (kind != "cache" and not read_only)
                            or (kind == "cache" and read_only)
                        ):
                            raise ScientificExecutionMapError(
                                "PVC mounts must be operator-owned and remain in the execution namespace"
                            )
                        cache_contract = _WRITABLE_CACHE_CONTRACTS.get((model_id, stage_id))
                        if kind == "cache" and (
                            sub_path is not None
                            or cache_contract is None
                            or cache_contract[0] != mount_path
                        ):
                            raise ScientificExecutionMapError(
                                "writable cache mount is outside the model/stage allowlist"
                            )
                    else:
                        # The reference plane is mounted whole and read-only. A
                        # subPath would expose only the dataset tree and hide
                        # the terminal receipt and the sibling manifest the run
                        # verifies it against, so the dataset is pinned by the
                        # aggregate tree below rather than by the mount.
                        if (
                            artifact_id is None
                            or claim_name is not None
                            or claim_namespace is not None
                            or sub_path is not None
                            or not operator_owned
                            or not read_only
                        ):
                            raise ScientificExecutionMapError(
                                "operator hostPath mounts expose one read-only plane root with no subPath"
                            )
                        host_path = _bounded_string(host_path, "scientific operator hostPath", maximum=512)
                        parsed_host_path = PurePosixPath(host_path)
                        aggregate_localization = runtime_artifacts.get((model_id, artifact_id))
                        if (
                            not parsed_host_path.is_absolute()
                            or parsed_host_path == PurePosixPath("/")
                            or parsed_host_path.as_posix() != host_path
                            or _OPERATOR_HOST_PATH_ALLOWLIST.get((model_id, stage_id, artifact_id))
                            != (host_path, mount_path)
                            or aggregate_localization is None
                            or aggregate_localization.aggregate_tree is None
                            or aggregate_localization.mount_path != mount_path
                        ):
                            raise ScientificExecutionMapError(
                                "operator hostPath is outside the exact model/stage/artifact tree allowlist"
                            )
                    storage_identity = (model_id, stage_id, artifact_id) if isinstance(artifact_id, str) else None
                    expected_supplemental_groups = (
                        _RUNTIME_ARTIFACT_SUPPLEMENTAL_GROUPS.get(storage_identity, ())
                        if storage_identity is not None
                        else ()
                    )
                    if supplemental_groups != expected_supplemental_groups:
                        raise ScientificExecutionMapError(
                            "scientific mount supplemental groups differ from the trusted storage identity"
                        )
                    mount_names.add(name)
                    mount_paths.add(mount_path)
                    kinds.append(cast(str, kind))
                    mounts.append(
                        StageMount(
                            name=name,
                            kind=cast(str, kind),
                            artifact_id=cast(str | None, artifact_id),
                            claim_name=cast(str | None, claim_name),
                            claim_namespace=cast(str | None, claim_namespace),
                            host_path=cast(str | None, host_path),
                            operator_owned=operator_owned,
                            mount_path=mount_path,
                            sub_path=cast(str | None, sub_path),
                            read_only=read_only,
                            supplemental_groups=supplemental_groups,
                        )
                    )
                if kinds.count("artifact-workspace") != 1:
                    raise ScientificExecutionMapError("each stage requires exactly one contained artifact workspace")
                cache_mounts = [mount for mount in mounts if mount.kind == "cache"]
                if len(cache_mounts) > 1:
                    raise ScientificExecutionMapError("a scientific stage may bind at most one writable cache")
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
                cache_contract = _WRITABLE_CACHE_CONTRACTS.get((model_id, stage_id))
                if cache_contract is not None and (
                    len(cache_mounts) != 1
                    or any(environment.get(key) != value for key, value in cache_contract[1].items())
                ):
                    raise ScientificExecutionMapError(
                        f"{model_id}/{stage_id} execution requires the exact deployment-owned warm-cache contract"
                    )
                raw_node_selector = _object(stage.get("node_selector", {}), "scientific node selector")
                if len(raw_node_selector) > 32 or any(
                    not isinstance(key, str)
                    or re.fullmatch(r"[A-Za-z0-9](?:[-A-Za-z0-9_.]*/)?[A-Za-z0-9](?:[-A-Za-z0-9_.]*[A-Za-z0-9])?", key)
                    is None
                    or not isinstance(value, str)
                    or not value
                    or len(value) > 63
                    for key, value in raw_node_selector.items()
                ):
                    raise ScientificExecutionMapError("scientific node selector is invalid")
                node_selector = dict(cast(Mapping[str, str], raw_node_selector))
                raw_tolerations = stage.get("tolerations", [])
                if not isinstance(raw_tolerations, list) or len(raw_tolerations) > 16:
                    raise ScientificExecutionMapError("scientific tolerations are invalid")
                tolerations: list[tuple[str, str, str, str]] = []
                for raw_toleration in raw_tolerations:
                    toleration = _object(raw_toleration, "scientific toleration")
                    if set(toleration) != {"key", "operator", "value", "effect"}:
                        raise ScientificExecutionMapError("scientific toleration fields differ")
                    values = tuple(
                        _bounded_string(toleration[field], f"scientific toleration {field}", maximum=253)
                        for field in ("key", "operator", "value", "effect")
                    )
                    if values[1] != "Equal" or values[3] not in {"NoSchedule", "NoExecute"}:
                        raise ScientificExecutionMapError("scientific toleration is invalid")
                    tolerations.append(cast(tuple[str, str, str, str], values))
                stage_key = (model_id, stage_id)
                if stage_key in _AF3_STAGE_NODE_SELECTORS and (
                    node_selector != _AF3_STAGE_NODE_SELECTORS[stage_key]
                    or tuple(tolerations) != _AF3_STAGE_TOLERATIONS[stage_key]
                    or execution_namespace != AF3_EXECUTION_NAMESPACE
                    or (local_queue_name, cluster_queue_name) != _AF3_STAGE_QUEUES[stage_key]
                    or stage["service_account_name"] != AF3_SERVICE_ACCOUNT
                ):
                    raise ScientificExecutionMapError(
                        "AlphaFold 3 execution target differs from the trusted academic placement"
                    )
                executions[(model_id, stage_id)] = StageExecution(
                    namespace=execution_namespace,
                    local_queue_name=local_queue_name,
                    cluster_queue_name=cluster_queue_name,
                    image=image,
                    collector_id=collector_id,
                    validator_id=validator_id,
                    mounts=tuple(mounts),
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
                    node_selector=MappingProxyType(node_selector),
                    tolerations=tuple(tolerations),
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
        self.plan_adapters = MappingProxyType(plan_adapters)
        self.runtime_artifacts = MappingProxyType(runtime_artifacts)
        self.tools_image = tools_image
        self.internal_api_url = internal_api_url
        self.capability_authority = capability_authority
        self.controller_service_account = mapped_controller

    def variant_id(self, model_id: str) -> str:
        """Return the operator-owned runtime binding for one public model ID."""

        try:
            return self.variants[model_id]
        except KeyError as error:
            raise ScientificExecutionMapError("scientific model has no exact runtime variant binding") from error

    def collector_id(self, model_id: str, stage_id: str) -> str:
        try:
            return self.executions[(model_id, stage_id)].collector_id
        except KeyError as error:
            raise ScientificExecutionMapError("scientific stage has no canonical collector binding") from error

    def scheduling_targets(self) -> Mapping[tuple[str, str], tuple[str, str, str | None]]:
        """Project deployment-owned stage routing for immutable scheduling-contract validation."""

        return MappingProxyType(
            {
                identity: (
                    execution.namespace,
                    execution.local_queue_name,
                    execution.cluster_queue_name,
                )
                for identity, execution in self.executions.items()
            }
        )

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
        bound_invocations: list[StageInvocation] = []
        for invocation in plan.invocations:
            execution = self.executions[(profile.model_id, invocation.stage_id)]
            if invocation.collector_id != execution.collector_id or invocation.validator_id != execution.validator_id:
                raise ScientificExecutionMapError("adapter collector or validator differs from the execution map")
            if invocation.namespace not in {None, execution.namespace} or invocation.local_queue_name not in {
                None,
                execution.local_queue_name,
            }:
                raise ScientificExecutionMapError("adapter cannot override the deployment-owned execution target")
            trusted_runtime_mounts: list[RuntimeArtifactMount] = []
            for binding in invocation.runtime_mounts:
                storage_identity = (profile.model_id, invocation.stage_id, binding.artifact_id)
                expected_groups = _RUNTIME_ARTIFACT_SUPPLEMENTAL_GROUPS.get(storage_identity)
                if expected_groups is None:
                    trusted_runtime_mounts.append(binding)
                    continue
                if binding.supplemental_groups:
                    raise ScientificExecutionMapError("adapter cannot declare deployment-owned runtime storage groups")
                artifact_path = PurePosixPath(binding.mount_path)
                physical_sources = [
                    source
                    for source in execution.mounts
                    if source.artifact_id == binding.artifact_id
                    and source.kind in {"reference", "private", "operator-host-path"}
                    and (
                        artifact_path == PurePosixPath(source.mount_path)
                        or PurePosixPath(source.mount_path) in artifact_path.parents
                    )
                ]
                if len(physical_sources) != 1 or physical_sources[0].supplemental_groups != expected_groups:
                    raise ScientificExecutionMapError(
                        "runtime artifact has no exact deployment-owned storage group source"
                    )
                trusted_runtime_mounts.append(replace(binding, supplemental_groups=expected_groups))
            bound_invocations.append(
                replace(
                    invocation,
                    namespace=execution.namespace,
                    local_queue_name=execution.local_queue_name,
                    runtime_mounts=tuple(trusted_runtime_mounts),
                )
            )
        return replace(plan, invocations=tuple(bound_invocations))

    def verify_runtime_artifacts(
        self,
        profile: ScientificWorkloadProfile,
        execution_plan: AdapterExecutionPlan,
        access_context: ArtifactAccessContext,
    ) -> tuple[RuntimeArtifactLocalization, ...]:
        """Resolve exact attested files before the batch row can be admitted."""

        # Academic/non-commercial authorization is deployment readiness
        # metadata. It is deliberately not a tenant-controlled request receipt.
        del access_context

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
            raw_aggregate = requirement.get("aggregate_tree")
            if not isinstance(expected_digest, str):
                raise ScientificExecutionMapError(f"runtime artifact {artifact_id} profile evidence is incomplete")
            evidence_differs = localization.content_digest.removeprefix("sha256:") != expected_digest
            if localization.aggregate_tree is None:
                if not isinstance(raw_files, list) or raw_aggregate is not None:
                    raise ScientificExecutionMapError(
                        f"runtime artifact {artifact_id} profile file evidence is incomplete"
                    )
                expected_files = {
                    (item.get("path"), item.get("sha256"), item.get("size_bytes"))
                    for item in raw_files
                    if isinstance(item, Mapping)
                }
                localized_files = {
                    (item.path, item.digest.removeprefix("sha256:"), item.size_bytes) for item in localization.files
                }
                evidence_differs = (
                    evidence_differs
                    or expected_files != localized_files
                    or set(cast(list[str], requirement.get("required_files", [])))
                    != {item.path for item in localization.files}
                )
            else:
                aggregate = localization.aggregate_tree
                if not isinstance(raw_aggregate, Mapping) or raw_files is not None:
                    raise ScientificExecutionMapError(
                        f"runtime artifact {artifact_id} profile aggregate-tree evidence is incomplete"
                    )
                expected_manifest = requirement.get("localization_manifest_sha256")
                evidence_differs = evidence_differs or (
                    expected_manifest != aggregate.manifest_digest.removeprefix("sha256:")
                    or raw_aggregate.get("kind") != "aggregate-tree"
                    or raw_aggregate.get("dataset_relative_path") != aggregate.dataset_relative_path
                    or raw_aggregate.get("dataset_uri") != aggregate.dataset_uri
                    or raw_aggregate.get("file_count") != aggregate.file_count
                    or requirement.get("required_files") != [AF3_REFERENCE_MARKER]
                )
            if evidence_differs:
                raise ScientificExecutionMapError(f"runtime artifact {artifact_id} localization evidence differs")
            for invocation in execution_plan.invocations:
                if artifact_id not in invocation.runtime_artifacts:
                    continue
                binding = next(item for item in invocation.runtime_mounts if item.artifact_id == artifact_id)
                if (
                    binding.mount_path != localization.mount_path
                    or binding.expected_content_sha256 != localization.content_digest.removeprefix("sha256:")
                    or (
                        localization.aggregate_tree is not None
                        and binding.expected_manifest_sha256
                        != localization.aggregate_tree.manifest_digest.removeprefix("sha256:")
                    )
                    or (
                        binding.readiness_receipt_sha256 is not None
                        and binding.readiness_receipt_sha256
                        != localization.localization_receipt_digest.removeprefix("sha256:")
                    )
                    or binding.authorization_receipt_sha256 is not None
                ):
                    raise ScientificExecutionMapError(
                        f"runtime artifact {artifact_id} invocation mount differs from verified localization"
                    )
                mounts = self.executions[(profile.model_id, invocation.stage_id)].mounts
                artifact_path = PurePosixPath(localization.mount_path)
                candidates = [
                    source
                    for source in mounts
                    if source.kind in {"reference", "private", "operator-host-path"}
                    and source.artifact_id == artifact_id
                    and (
                        artifact_path == PurePosixPath(source.mount_path)
                        or PurePosixPath(source.mount_path) in artifact_path.parents
                    )
                ]
                if not candidates:
                    raise ScientificExecutionMapError(
                        f"runtime artifact {artifact_id} is not covered by the stage's read-only mounts"
                    )
                expected_groups = tuple(
                    sorted({group for source in candidates for group in source.supplemental_groups})
                )
                if (
                    profile.model_id,
                    invocation.stage_id,
                    artifact_id,
                ) in _RUNTIME_ARTIFACT_SUPPLEMENTAL_GROUPS and binding.supplemental_groups != expected_groups:
                    raise ScientificExecutionMapError(
                        f"runtime artifact {artifact_id} lost its trusted storage group binding"
                    )
                # An aggregate tree is exposed as one read-only plane root with
                # no subPath. The dataset inside it is pinned by the promoted
                # content digest checked when the map was loaded, and by the
                # runtime's own check that the mounted directory name equals
                # that digest, so the mount itself must stay unqualified.
                if localization.aggregate_tree is not None and any(
                    source.kind != "operator-host-path"
                    or source.sub_path is not None
                    or source.mount_path != localization.mount_path
                    for source in candidates
                ):
                    raise ScientificExecutionMapError(
                        f"runtime artifact {artifact_id} aggregate tree lost its pinned physical source"
                    )
            result.append(localization)
        if not set(execution_plan.required_model_artifacts).issubset(by_id):
            raise ScientificExecutionMapError("adapter requires an undeclared profile runtime artifact")
        return tuple(result)

    def _pod(self, resource: WorkloadResource, execution: StageExecution) -> dict[str, Any]:
        invocation = resource.invocation
        if not isinstance(invocation, StageInvocation):
            raise ScientificExecutionMapError("scientific workload has no canonical stage invocation")
        if invocation.collector_id != execution.collector_id or invocation.validator_id != execution.validator_id:
            raise ScientificExecutionMapError("scientific workload collector or validator binding changed")
        if invocation.namespace != execution.namespace or invocation.local_queue_name != execution.local_queue_name:
            raise ScientificExecutionMapError("scientific invocation lost its trusted execution target")
        localized = {item.logical_artifact_id: item for item in resource.runtime_artifacts}
        bindings = {item.artifact_id: item for item in invocation.runtime_mounts}
        if set(localized) != set(invocation.runtime_artifacts) or set(bindings) != set(invocation.runtime_artifacts):
            raise ScientificExecutionMapError("runtime artifact localization is incomplete at workload apply")
        for artifact_id, binding in bindings.items():
            artifact = localized[artifact_id]
            if (
                artifact.mount_path != binding.mount_path
                or artifact.content_digest.removeprefix("sha256:") != binding.expected_content_sha256
                or (
                    artifact.aggregate_tree is not None
                    and artifact.aggregate_tree.manifest_digest.removeprefix("sha256:")
                    != binding.expected_manifest_sha256
                )
                or (
                    binding.readiness_receipt_sha256 is not None
                    and artifact.localization_receipt_digest.removeprefix("sha256:") != binding.readiness_receipt_sha256
                )
                or binding.authorization_receipt_sha256 is not None
            ):
                raise ScientificExecutionMapError(
                    f"runtime artifact {artifact_id} lost its verified localization binding before apply"
                )
        gpu_count = resource.scheduling.accelerator_count
        limits = {
            "cpu": execution.cpu,
            "memory": execution.memory,
            "ephemeral-storage": execution.ephemeral_storage,
        }
        if gpu_count:
            limits[resource.scheduling.accelerator_resource_name] = str(gpu_count)
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
                    "mount_path": item.mount_path,
                    "content_digest": item.content_digest,
                    "localization_receipt_digest": item.localization_receipt_digest,
                    "sub_path": binding.sub_path,
                    "expected_manifest_sha256": binding.expected_manifest_sha256,
                    "readiness_receipt_sha256": item.localization_receipt_digest.removeprefix("sha256:"),
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
                    **dict(invocation.environment),
                    **execution.environment,
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
                if source.kind in {"reference", "private", "operator-host-path"}
                and source.artifact_id == binding.artifact_id
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
                resource.model_id,
                resource.stage_id,
                binding.artifact_id,
            ) in _RUNTIME_ARTIFACT_SUPPLEMENTAL_GROUPS and binding.supplemental_groups != source.supplemental_groups:
                raise ScientificExecutionMapError(
                    f"runtime artifact {binding.artifact_id} storage groups changed before apply"
                )
            used_sources.add(source.name)
            volume_mount: dict[str, Any] = {
                "name": source.name,
                "mountPath": binding.mount_path,
                "readOnly": True,
            }
            binding_sub_path = (
                None if source.sub_path is not None and source.sub_path == binding.sub_path else binding.sub_path
            )
            sub_parts = tuple(
                part
                for value in (source.sub_path, binding_sub_path)
                if value is not None
                for part in PurePosixPath(value).parts
            )
            if sub_parts:
                volume_mount["subPath"] = PurePosixPath(*sub_parts).as_posix()
            volume_mounts.append(volume_mount)
            if source.name not in volume_names:
                if source.kind == "operator-host-path":
                    assert source.host_path is not None
                    volumes.append(
                        {
                            "name": source.name,
                            "hostPath": {"path": source.host_path, "type": "Directory"},
                        }
                    )
                else:
                    assert source.claim_name is not None
                    volumes.append(
                        {
                            "name": source.name,
                            "persistentVolumeClaim": {
                                "claimName": source.claim_name,
                                "readOnly": True,
                            },
                        }
                    )
                volume_names.add(source.name)
        for source in execution.mounts:
            if source.kind != "cache":
                continue
            assert source.claim_name is not None
            used_sources.add(source.name)
            volume_mounts.append({"name": source.name, "mountPath": source.mount_path, "readOnly": False})
            volumes.append(
                {
                    "name": source.name,
                    "persistentVolumeClaim": {
                        "claimName": source.claim_name,
                        "readOnly": False,
                    },
                }
            )
        declared_sources = {
            mount.name
            for mount in execution.mounts
            if mount.kind in {"reference", "private", "operator-host-path", "cache"}
        }
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
            pod_security.update(
                {
                    "supplementalGroups": supplemental_groups,
                    # Do not merge image-declared groups into access to private
                    # or operator-owned scientific storage.
                    "supplementalGroupsPolicy": "Strict",
                }
            )
        for source in execution.mounts:
            if source.kind != "operator-host-path":
                continue
            if source.artifact_id is None:
                raise ScientificExecutionMapError("operator hostPath lost its trusted artifact identity before apply")
            required_selector = _OPERATOR_HOST_PATH_REQUIRED_NODE_SELECTORS.get(
                (resource.model_id, resource.stage_id, source.artifact_id)
            )
            if required_selector is None or any(
                execution.node_selector.get(key) != value for key, value in required_selector.items()
            ):
                raise ScientificExecutionMapError("operator hostPath lost its trusted stage node selector before apply")
        aggregate_node_sets = [
            set(item.aggregate_tree.node_accessibility.node_names)
            for item in resource.runtime_artifacts
            if item.aggregate_tree is not None and item.aggregate_tree.node_accessibility.node_names
        ]
        eligible_nodes = set.intersection(*aggregate_node_sets) if aggregate_node_sets else set()
        if aggregate_node_sets and not eligible_nodes:
            raise ScientificExecutionMapError("aggregate runtime artifact trees have no common accessible node")
        pod_node_selector = dict(execution.node_selector)
        for item in resource.runtime_artifacts:
            if item.aggregate_tree is None:
                continue
            for key, value in item.aggregate_tree.node_accessibility.required_node_labels:
                if key in pod_node_selector and pod_node_selector[key] != value:
                    raise ScientificExecutionMapError(
                        "aggregate runtime artifact placement conflicts with the trusted stage selector"
                    )
                pod_node_selector[key] = value
        node_affinity = (
            {}
            if not eligible_nodes
            else {
                "affinity": {
                    "nodeAffinity": {
                        "requiredDuringSchedulingIgnoredDuringExecution": {
                            "nodeSelectorTerms": [
                                {
                                    "matchFields": [
                                        {
                                            "key": "metadata.name",
                                            "operator": "In",
                                            "values": sorted(eligible_nodes),
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                }
            }
        )
        return {
            "metadata": {},
            "spec": {
                **node_affinity,
                **({"nodeSelector": pod_node_selector} if pod_node_selector else {}),
                **(
                    {
                        "tolerations": [
                            {"key": key, "operator": operator, "value": value, "effect": effect}
                            for key, operator, value, effect in execution.tolerations
                        ]
                    }
                    if execution.tolerations
                    else {}
                ),
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
            },
        }

    def render(self, resource: WorkloadResource) -> Mapping[str, Any]:
        execution = self.executions.get((resource.model_id, resource.stage_id))
        if execution is None:
            raise ScientificExecutionMapError("scientific stage has no qualified execution mapping")
        if resource.namespace != execution.namespace:
            raise ScientificExecutionMapError("scientific workload namespace differs from the trusted execution target")
        if resource.scheduling.resolved_local_queue != execution.local_queue_name:
            raise ScientificExecutionMapError(
                "scientific workload LocalQueue differs from the trusted execution target"
            )
        if (
            execution.cluster_queue_name is not None
            and resource.scheduling.resolved_cluster_queue != execution.cluster_queue_name
        ):
            raise ScientificExecutionMapError(
                "scientific workload ClusterQueue differs from the trusted execution target"
            )
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
