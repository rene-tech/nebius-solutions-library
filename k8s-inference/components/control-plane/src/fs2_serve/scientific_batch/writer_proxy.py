"""Least-privilege Kubernetes writer for internal scientific Jobs and JobSets.

The public control-plane runtime authenticates with an audience-scoped projected
token. This process validates that token with TokenReview, accepts only the
finite internal scientific network profile, and performs the mutation with its
own dedicated ServiceAccount. Read/observe traffic never traverses this proxy.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote

import httpx
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .models import COLLECTOR_CONTAINER_NAME, STAGE_CONTAINER_NAME

PROFILE_LABEL = "fs2-serve.nebius.ai/network-profile"
CLASS_LABEL = "fs2-serve.nebius.ai/network-workload-class"
PART_OF_LABEL = "app.kubernetes.io/part-of"
MODEL_LABEL = "fs2.nebius.ai/model-id"
VARIANT_LABEL = "fs2.nebius.ai/variant-id"
STAGE_LABEL = "fs2.nebius.ai/stage-id"
MANIFEST_ANNOTATION = "fs2.nebius.ai/scientific-manifest-sha256"
FENCE_ANNOTATION = "fs2.nebius.ai/scientific-controller-fence"
OPERATION_LABEL = "fs2.nebius.ai/operation-id"
WORKLOAD_LABEL = "fs2.nebius.ai/workload-id"
ATTEMPT_LABEL = "fs2.nebius.ai/attempt-id"
INTERNAL_PROFILE = "job-internal-v1"
INTERNAL_CLASS = "internal-job"
UUID_LABEL_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_PATH = re.compile(
    r"^/apis/(?P<group>batch|jobset\.x-k8s\.io)/(?P<version>v1|v1alpha2)/"
    r"namespaces/(?P<namespace>[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?)/"
    r"(?P<resource>jobs|jobsets)(?:/(?P<name>[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?))?$"
)

# This independent allowlist is deliberately narrower than "any command in a
# digest-pinned image".  Each key is also required to exist in the immutable
# execution map before it can be selected.  The tuple is the fixed portion of
# the controller-owned exec-form command after the trusted workspace runner;
# request-derived arguments may follow, but cannot replace the reviewed
# entrypoint or subcommand.  A new model/stage therefore fails closed until its
# writer command contract receives source review.
_WRAPPED_STAGE_COMMAND_PREFIXES: dict[tuple[str, str], tuple[str, ...]] = {
    ("boltzgen", "configure"): ("boltzgen", "configure"),
    ("boltzgen", "design"): ("boltzgen", "execute"),
    ("boltzgen", "inverse-folding"): ("boltzgen", "execute"),
    ("boltzgen", "folding"): ("boltzgen", "execute"),
    ("boltzgen", "design-folding"): ("boltzgen", "execute"),
    ("boltzgen", "affinity"): ("boltzgen", "execute"),
    ("boltzgen", "analysis"): ("boltzgen", "execute"),
    ("boltzgen", "filtering"): ("boltzgen", "execute"),
    ("proteina-complexa", "generate"): ("complexa", "generate"),
    ("proteina-complexa", "filter"): ("complexa", "filter"),
    ("proteina-complexa", "evaluate"): ("complexa", "evaluate"),
    ("proteina-complexa", "analyze"): ("complexa", "analyze"),
    ("bindcraft", "design"): (
        "python",
        "/opt/fs2/runtime_entrypoint.py",
        "/opt/fs2/bin/bindcraft-batch",
        "run-trajectory",
    ),
    ("bindcraft", "aggregate"): (
        "python",
        "/opt/fs2/runtime_entrypoint.py",
        "/opt/fs2/bin/bindcraft-batch",
        "aggregate",
    ),
    ("mosaic", "design"): ("/opt/fs2/bin/mosaic-batch", "run-shard"),
    ("mosaic", "aggregate"): ("/opt/fs2/bin/mosaic-batch", "aggregate"),
    ("rfdiffusion", "inference"): (
        "python",
        "/opt/fs2/runtime_entrypoint.py",
        "run",
    ),
    ("rfdiffusion", "collect"): ("python", "--version"),
}
_DIRECT_STAGE_COMMAND_PREFIXES: dict[tuple[str, str], tuple[str, ...]] = {
    ("esmfold2", "prepare-input"): (
        "/usr/local/bin/fs2-run-esmfold2",
        "prepare-input",
    ),
    ("esmfold2", "fold"): ("/usr/local/bin/fs2-run-esmfold2", "fold"),
    ("esmfold2-fast", "prepare-input"): (
        "/usr/local/bin/fs2-run-esmfold2",
        "prepare-input",
    ),
    ("esmfold2-fast", "fold"): (
        "/usr/local/bin/fs2-run-esmfold2",
        "fold",
    ),
    ("openfold3-openbind", "data-pipeline"): (
        "/usr/local/bin/fs2-run-openfold3",
        "prepare",
    ),
    ("openfold3-openbind", "inference"): (
        "/usr/local/bin/fs2-run-openfold3",
        "predict",
    ),
    ("protenix-v2", "prepare-data"): (
        "/usr/local/bin/fs2-run-protenix",
        "prep",
    ),
    ("protenix-v2", "sample-structure"): (
        "/usr/local/bin/fs2-run-protenix",
        "pred",
    ),
    ("alphafold3", "data-pipeline"): (
        "/alphafold3_venv/bin/python3",
        "/opt/fs2/af3_runtime.py",
        "data",
    ),
    ("alphafold3", "inference"): (
        "/alphafold3_venv/bin/python3",
        "/opt/fs2/af3_runtime.py",
        "inference",
    ),
}


class ScientificWriterError(RuntimeError):
    """A mutation cannot be proven to be a bounded scientific write."""


class Mutation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: str = Field(pattern=r"^(POST|DELETE)$")
    path: str = Field(min_length=1, max_length=1024)
    body: dict[str, Any]
    ownership: dict[str, str] | None = None


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ScientificWriterError(f"{label} is missing or malformed")
    return value


def _profile(metadata: Mapping[str, Any], label: str) -> None:
    labels = _mapping(metadata.get("labels"), f"{label}.labels")
    if (
        labels.get(PART_OF_LABEL) != "fs2-serve"
        or labels.get(CLASS_LABEL) != INTERNAL_CLASS
        or labels.get(PROFILE_LABEL) != INTERNAL_PROFILE
    ):
        raise ScientificWriterError(f"{label} does not select the internal scientific profile")


class ScientificExecutionPolicy:
    """Immutable execution-map projection enforced by the isolated writer."""

    def __init__(self, value: Mapping[str, Any], *, tools_image: str) -> None:
        if value.get("schema") != "fs2-serve.nebius.ai/scientific-execution-map/v3":
            raise ScientificWriterError("scientific writer execution-map schema is not exact")
        if re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", tools_image) is None:
            raise ScientificWriterError("scientific writer tools image is not digest pinned")
        models = value.get("models")
        if not isinstance(models, list):
            raise ScientificWriterError("scientific writer execution map has no model list")
        stages: dict[tuple[str, str, str], Mapping[str, Any]] = {}
        namespaces: dict[str, str] = {}
        for model in models:
            if not isinstance(model, Mapping):
                raise ScientificWriterError("scientific writer execution map contains a non-object")
            model_id = model.get("model_id")
            variant_id = model.get("variant_id")
            namespace = model.get("workload_namespace")
            raw_stages = model.get("stages")
            if not all(isinstance(item, str) and item for item in (model_id, variant_id, namespace)):
                raise ScientificWriterError("scientific writer model identity is incomplete")
            if not isinstance(raw_stages, list) or not raw_stages:
                raise ScientificWriterError("scientific writer model has no stages")
            namespaces[model_id] = namespace
            for stage in raw_stages:
                if not isinstance(stage, Mapping):
                    raise ScientificWriterError("scientific writer stage is malformed")
                stage_id = stage.get("stage_id")
                image = stage.get("image")
                account = stage.get("service_account_name")
                if (
                    not isinstance(stage_id, str)
                    or not stage_id
                    or not isinstance(image, str)
                    or re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", image) is None
                    or not isinstance(account, str)
                    or not account
                ):
                    raise ScientificWriterError("scientific writer stage authority is incomplete")
                key = (model_id, variant_id, stage_id)
                if key in stages:
                    raise ScientificWriterError("scientific writer execution map has duplicate stages")
                stages[key] = stage
        writer_stage_keys = set(_WRAPPED_STAGE_COMMAND_PREFIXES) | set(
            _DIRECT_STAGE_COMMAND_PREFIXES
        )
        mapped_stage_keys = {(key[0], key[2]) for key in stages}
        if not mapped_stage_keys.issubset(writer_stage_keys):
            raise ScientificWriterError(
                "scientific execution map contains a stage absent from the independent writer command inventory"
            )
        self.stages = stages
        self.namespaces = namespaces
        self.tools_image = tools_image

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        expected_sha256: str,
        tools_image: str,
    ) -> ScientificExecutionPolicy:
        try:
            raw = path.read_bytes()
            value = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise ScientificWriterError("scientific writer execution map is unavailable") from exc
        if (
            re.fullmatch(r"[a-f0-9]{64}", expected_sha256) is None
            or hashlib.sha256(raw).hexdigest() != expected_sha256
            or not isinstance(value, Mapping)
        ):
            raise ScientificWriterError("scientific writer execution-map digest differs from release")
        return cls(value, tools_image=tools_image)

    @staticmethod
    def _container_security(container: Mapping[str, Any], *, tools: bool, uid: int, gid: int) -> None:
        security = _mapping(container.get("securityContext"), "container.securityContext")
        allowed_fields = {
            "allowPrivilegeEscalation",
            "capabilities",
            "privileged",
            "readOnlyRootFilesystem",
            "runAsGroup",
            "runAsUser",
        }
        if (
            set(security) - allowed_fields
            or security.get("allowPrivilegeEscalation") is not False
            or security.get("privileged", False) is not False
            or security.get("runAsUser") != uid
            or security.get("runAsGroup") != gid
            or _mapping(security.get("capabilities"), "container capabilities").get("drop")
            != ["ALL"]
            or (tools and security.get("readOnlyRootFilesystem") is not True)
            or container.get("volumeDevices") not in (None, [])
        ):
            raise ScientificWriterError("scientific container security differs from execution policy")

    @staticmethod
    def _literal_environment(container: Mapping[str, Any], *, label: str) -> Mapping[str, str]:
        raw = container.get("env", [])
        if not isinstance(raw, list) or len(raw) > 128:
            raise ScientificWriterError(f"{label} environment is malformed")
        result: dict[str, str] = {}
        for item in raw:
            entry = _mapping(item, f"{label} environment entry")
            name = entry.get("name")
            value = entry.get("value")
            if (
                set(entry) != {"name", "value"}
                or not isinstance(name, str)
                or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,252}", name) is None
                or not isinstance(value, str)
                or len(value) > 131_072
                or name in result
                or any(marker in name.upper() for marker in ("PASSWORD", "PRIVATE_KEY", "ACCESS_KEY"))
            ):
                raise ScientificWriterError(f"{label} environment is not literal and bounded")
            result[name] = value
        return result

    @staticmethod
    def _command(container: Mapping[str, Any], *, label: str) -> tuple[str, ...]:
        raw = container.get("command")
        if (
            not isinstance(raw, list)
            or not 1 <= len(raw) <= 256
            or not all(
                isinstance(item, str)
                and 0 < len(item) <= 131_072
                and "\x00" not in item
                for item in raw
            )
            or container.get("args") not in (None, [])
        ):
            raise ScientificWriterError(f"{label} command is not bounded exec form")
        return tuple(raw)

    @staticmethod
    def _resources(container: Mapping[str, Any], *, label: str) -> Mapping[str, Mapping[str, Any]]:
        resources = _mapping(container.get("resources"), f"{label}.resources")
        requests = _mapping(resources.get("requests"), f"{label}.resources.requests")
        limits = _mapping(resources.get("limits"), f"{label}.resources.limits")
        if set(resources) != {"requests", "limits"}:
            raise ScientificWriterError(f"{label} resource envelope is malformed")
        return {"requests": requests, "limits": limits}

    @staticmethod
    def _mounts(container: Mapping[str, Any], *, label: str) -> tuple[Mapping[str, Any], ...]:
        raw = container.get("volumeMounts", [])
        if not isinstance(raw, list) or not all(isinstance(item, Mapping) for item in raw):
            raise ScientificWriterError(f"{label} volume mounts are malformed")
        names: set[str] = set()
        result: list[Mapping[str, Any]] = []
        for item in raw:
            name = item.get("name")
            mount_path = item.get("mountPath")
            sub_path = item.get("subPath")
            if (
                not isinstance(name, str)
                or name in names
                or not isinstance(mount_path, str)
                or not PurePosixPath(mount_path).is_absolute()
                or "mountPropagation" in item
                or "subPathExpr" in item
                or (sub_path is not None and (
                    not isinstance(sub_path, str)
                    or PurePosixPath(sub_path).is_absolute()
                    or any(part in {"", ".", ".."} for part in PurePosixPath(sub_path).parts)
                ))
            ):
                raise ScientificWriterError(f"{label} volume mount is outside the execution policy")
            names.add(name)
            result.append(item)
        return tuple(result)

    @staticmethod
    def _exact_companion_resources(container: Mapping[str, Any], *, label: str) -> None:
        expected = {
            "prepare-workspace": (
                {"cpu": "50m", "memory": "64Mi"},
                {"cpu": "500m", "memory": "256Mi"},
            ),
            "verify-runtime-artifacts": (
                {"cpu": "100m", "memory": "128Mi"},
                {"cpu": "1", "memory": "512Mi"},
            ),
            "artifact-collector": (
                {"cpu": "100m", "memory": "256Mi"},
                {"cpu": "2", "memory": "2Gi"},
            ),
        }
        name = container.get("name")
        if isinstance(name, str) and (name.startswith("materialize-") or name == "materialize-inputs"):
            values = ({"cpu": "100m", "memory": "256Mi"}, {"cpu": "1", "memory": "1Gi"})
        else:
            values = expected.get(str(name))
        resources = ScientificExecutionPolicy._resources(container, label=label)
        if values is None or resources["requests"] != values[0] or resources["limits"] != values[1]:
            raise ScientificWriterError(f"{label} resource envelope differs from the renderer")

    @staticmethod
    def _command_prefix(
        *, model_id: str, stage_id: str, working_directory: str
    ) -> tuple[str, ...]:
        key = (model_id, stage_id)
        wrapped = _WRAPPED_STAGE_COMMAND_PREFIXES.get(key)
        direct = _DIRECT_STAGE_COMMAND_PREFIXES.get(key)
        if (wrapped is None) == (direct is None):
            raise ScientificWriterError("scientific stage has no unique writer command policy")
        if wrapped is not None:
            return (
                "python",
                f"{working_directory}/.fs2/stage-runner.py",
                "--",
                *wrapped,
            )
        assert direct is not None
        return direct

    def validate_pod(self, pod: Mapping[str, Any], *, namespace: str, label: str) -> None:
        metadata = _mapping(pod.get("metadata"), f"{label}.metadata")
        labels = _mapping(metadata.get("labels"), f"{label}.metadata.labels")
        key = (labels.get(MODEL_LABEL), labels.get(VARIANT_LABEL), labels.get(STAGE_LABEL))
        stage = self.stages.get(key)  # type: ignore[arg-type]
        if stage is None or self.namespaces.get(str(key[0])) != namespace:
            raise ScientificWriterError("scientific Pod has no exact execution-map stage authority")
        model_id, _variant_id, stage_id = key
        if not isinstance(model_id, str) or not isinstance(stage_id, str):
            raise ScientificWriterError("scientific Pod execution identity is malformed")
        spec = _mapping(pod.get("spec"), f"{label}.spec")
        uid = stage.get("workspace_uid")
        gid = stage.get("workspace_gid")
        if not isinstance(uid, int) or not isinstance(gid, int):
            raise ScientificWriterError("scientific stage has no exact runtime UID/GID")
        pod_security = _mapping(spec.get("securityContext"), f"{label}.securityContext")
        supplemental_groups = pod_security.get("supplementalGroups", [])
        if (
            spec.get("serviceAccountName") != stage.get("service_account_name")
            or spec.get("automountServiceAccountToken") is not False
            or spec.get("enableServiceLinks") is not False
            or spec.get("restartPolicy") != "Never"
            or spec.get("terminationGracePeriodSeconds") != stage.get("termination_grace_seconds")
            or any(spec.get(field, False) is not False for field in ("hostNetwork", "hostPID", "hostIPC"))
            or spec.get("hostUsers", True) is not True
            or any(field in spec for field in ("serviceAccount", "imagePullSecrets", "runtimeClassName"))
            or set(pod_security) - {"runAsNonRoot", "seccompProfile", "supplementalGroups"}
            or pod_security.get("runAsNonRoot") is not True
            or _mapping(pod_security.get("seccompProfile"), "Pod seccompProfile").get("type")
            != "RuntimeDefault"
            or not isinstance(supplemental_groups, list)
            or any(
                not isinstance(group, int)
                or isinstance(group, bool)
                or not 1 <= group <= 65535
                for group in supplemental_groups
            )
            or len(supplemental_groups) != len(set(supplemental_groups))
        ):
            raise ScientificWriterError("scientific Pod identity or host/security boundary is not exact")
        containers = spec.get("containers")
        init_containers = spec.get("initContainers", [])
        if (
            not isinstance(containers, list)
            or len(containers) != 2
            or not isinstance(init_containers, list)
            or not all(isinstance(item, Mapping) for item in [*containers, *init_containers])
        ):
            raise ScientificWriterError("scientific Pod container set is malformed")
        by_name = {item.get("name"): item for item in containers if isinstance(item, Mapping)}
        stage_container = by_name.get(STAGE_CONTAINER_NAME)
        collector = by_name.get(COLLECTOR_CONTAINER_NAME)
        if not isinstance(stage_container, Mapping) or not isinstance(collector, Mapping):
            raise ScientificWriterError("scientific Pod lacks exact stage and collector containers")
        if stage_container.get("image") != stage.get("image") or collector.get("image") != self.tools_image:
            raise ScientificWriterError("scientific Pod image differs from the immutable execution map")
        self._container_security(stage_container, tools=False, uid=uid, gid=gid)
        self._container_security(collector, tools=True, uid=uid, gid=gid)
        if (
            stage_container.get("imagePullPolicy") != "IfNotPresent"
            or collector.get("imagePullPolicy") != "IfNotPresent"
        ):
            raise ScientificWriterError("scientific image pull policy differs from the renderer")
        working_directory = stage_container.get("workingDir")
        if (
            not isinstance(working_directory, str)
            or not working_directory.startswith("/mnt/fs2-scientific/work/")
            or PurePosixPath(working_directory).as_posix() != working_directory
            or ".." in PurePosixPath(working_directory).parts
        ):
            raise ScientificWriterError("scientific stage working directory is not contained")
        command = self._command(stage_container, label="scientific stage")
        command_prefix = self._command_prefix(
            model_id=model_id,
            stage_id=stage_id,
            working_directory=working_directory,
        )
        if command[: len(command_prefix)] != command_prefix:
            raise ScientificWriterError("scientific stage command differs from the reviewed adapter entrypoint")
        stage_environment = self._literal_environment(stage_container, label="scientific stage")
        declared_environment = stage.get("environment")
        if not isinstance(declared_environment, Mapping) or any(
            stage_environment.get(name) != value for name, value in declared_environment.items()
        ):
            raise ScientificWriterError("scientific stage lost an execution-map environment binding")
        collector_command = self._command(collector, label="scientific collector")
        if collector_command[:2] != ("fs2-serve", "scientific-collect"):
            raise ScientificWriterError("scientific collector command differs from the renderer")
        self._literal_environment(collector, label="scientific collector")
        self._exact_companion_resources(collector, label="scientific collector")
        init_names: set[str] = set()
        for container in init_containers:
            name = container.get("name")
            if (
                not isinstance(name, str)
                or name in init_names
                or container.get("image") != self.tools_image
                or container.get("imagePullPolicy") != "IfNotPresent"
            ):
                raise ScientificWriterError("scientific init image differs from the immutable tools image")
            init_names.add(name)
            self._container_security(container, tools=True, uid=uid, gid=gid)
            init_command = self._command(container, label=f"scientific init {name}")
            allowed_prefix = (
                ("fs2-serve", "scientific-prepare-workspace")
                if name == "prepare-workspace"
                else ("fs2-serve", "scientific-verify-runtime-artifacts")
                if name == "verify-runtime-artifacts"
                else ("fs2-serve", "scientific-materialize-many")
                if name == "materialize-inputs"
                else ("fs2-serve", "scientific-materialize")
                if re.fullmatch(r"materialize-[0-9]+", name) is not None
                else ()
            )
            if not allowed_prefix or init_command[: len(allowed_prefix)] != allowed_prefix:
                raise ScientificWriterError("scientific init command differs from the renderer")
            self._literal_environment(container, label=f"scientific init {name}")
            self._exact_companion_resources(container, label=f"scientific init {name}")
        if "prepare-workspace" not in init_names:
            raise ScientificWriterError("scientific Pod lacks its workspace initializer")

        raw_mounts = stage.get("mounts")
        if not isinstance(raw_mounts, list) or not raw_mounts or not all(
            isinstance(item, Mapping) for item in raw_mounts
        ):
            raise ScientificWriterError("scientific stage mount policy is malformed")
        stage_mounts = {str(item.get("name")): item for item in raw_mounts}
        if len(stage_mounts) != len(raw_mounts) or "" in stage_mounts:
            raise ScientificWriterError("scientific stage mount names are not unique")
        volumes = spec.get("volumes", [])
        if not isinstance(volumes, list) or not all(isinstance(item, Mapping) for item in volumes):
            raise ScientificWriterError("scientific Pod volumes are malformed")
        volume_names: set[str] = set()
        for volume in volumes:
            name = volume.get("name")
            source = stage_mounts.get(str(name))
            if not isinstance(name, str) or name in volume_names or source is None:
                raise ScientificWriterError("scientific volume is absent from the execution map")
            volume_names.add(name)
            kind = source.get("kind")
            if kind == "artifact-workspace":
                expected_volume: Mapping[str, Any] = {"name": name, "emptyDir": {}}
            elif source.get("claim_name") is not None:
                expected_volume = {
                    "name": name,
                    "persistentVolumeClaim": {
                        "claimName": source.get("claim_name"),
                        "readOnly": source.get("read_only"),
                    },
                }
            elif source.get("host_path") is not None:
                expected_volume = {
                    "name": name,
                    "hostPath": {"path": source.get("host_path"), "type": "Directory"},
                }
            else:
                raise ScientificWriterError("scientific volume source is not finite")
            if volume != expected_volume:
                raise ScientificWriterError("scientific volume differs from the execution map")
        required_volume_names = {
            name
            for name, source in stage_mounts.items()
            if source.get("kind") in {"artifact-workspace", "runtime-cache"}
        }
        if not required_volume_names.issubset(volume_names):
            raise ScientificWriterError("scientific Pod lost a required execution-map volume")
        for container_label, container in (
            ("scientific stage", stage_container),
            ("scientific collector", collector),
            *((f"scientific init {item.get('name')}", item) for item in init_containers),
        ):
            for mount in self._mounts(container, label=container_label):
                source = stage_mounts.get(str(mount.get("name")))
                if source is None or mount.get("name") not in volume_names:
                    raise ScientificWriterError(f"{container_label} mounts an unapproved volume")
                source_path = source.get("mount_path")
                source_sub_path = source.get("sub_path")
                mounted_path = mount.get("mountPath")
                if (
                    not isinstance(source_path, str)
                    or not isinstance(mounted_path, str)
                    or not (
                        mounted_path == source_path
                        or PurePosixPath(source_path) in PurePosixPath(mounted_path).parents
                    )
                    or bool(mount.get("readOnly", False)) != bool(source.get("read_only", False))
                    or (
                        isinstance(source_sub_path, str)
                        and mount.get("subPath") != source_sub_path
                        and not str(mount.get("subPath", "")).startswith(source_sub_path + "/")
                    )
                ):
                    raise ScientificWriterError(f"{container_label} mount differs from the execution map")

        resources = _mapping(stage.get("resources"), "scientific stage resources")
        expected_requests = dict(_mapping(resources.get("requests"), "stage requests"))
        expected_limits = dict(_mapping(resources.get("limits"), "stage limits"))
        expected_requests["ephemeral-storage"] = expected_requests.pop("ephemeral_storage")
        expected_limits["ephemeral-storage"] = expected_limits.pop("ephemeral_storage")
        actual_resources = self._resources(stage_container, label="scientific stage")
        extra_request_keys = set(actual_resources["requests"]) - set(expected_requests)
        extra_limit_keys = set(actual_resources["limits"]) - set(expected_limits)
        if (
            any(actual_resources["requests"].get(name) != value for name, value in expected_requests.items())
            or any(actual_resources["limits"].get(name) != value for name, value in expected_limits.items())
            or extra_request_keys != extra_limit_keys
            or len(extra_request_keys) > 1
            or any(
                "/" not in name
                or actual_resources["requests"].get(name) != actual_resources["limits"].get(name)
                or re.fullmatch(r"[1-9][0-9]*", str(actual_resources["requests"].get(name))) is None
                for name in extra_request_keys
            )
        ):
            raise ScientificWriterError("scientific stage resources differ from the execution map")
        node_selector = spec.get("nodeSelector", {})
        if not isinstance(node_selector, Mapping):
            raise ScientificWriterError("scientific Pod node selector is malformed")
        required_labels = stage.get("required_node_labels", {})
        if not isinstance(required_labels, Mapping) or any(
            node_selector.get(key_name) != expected for key_name, expected in required_labels.items()
        ):
            raise ScientificWriterError("scientific Pod lost an execution-map node constraint")


def _manifest_digest(value: Mapping[str, Any]) -> str:
    body = copy.deepcopy(dict(value))
    metadata = _mapping(body.get("metadata"), "workload.metadata")
    annotations = _mapping(metadata.get("annotations"), "workload.metadata.annotations")
    annotations.pop(MANIFEST_ANNOTATION, None)
    annotations.pop(FENCE_ANNOTATION, None)
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def validate_scientific_manifest(
    value: Mapping[str, Any],
    *,
    resource: str,
    execution_policy: ScientificExecutionPolicy | None = None,
    verify_submission_digest: bool = True,
) -> None:
    expected = ("batch/v1", "Job") if resource == "jobs" else ("jobset.x-k8s.io/v1alpha2", "JobSet")
    if (value.get("apiVersion"), value.get("kind")) != expected:
        raise ScientificWriterError("scientific mutation has the wrong API kind")
    _profile(_mapping(value.get("metadata"), "workload.metadata"), "workload.metadata")
    spec = _mapping(value.get("spec"), "workload.spec")
    metadata = _mapping(value.get("metadata"), "workload.metadata")
    namespace = metadata.get("namespace")
    if execution_policy is not None:
        labels = _mapping(metadata.get("labels"), "workload.metadata.labels")
        if (
            labels.get("fs2-serve.nebius.ai/job-kind") not in {"batch", "evaluation"}
            or any(
                not isinstance(labels.get(label_name), str)
                or UUID_LABEL_PATTERN.fullmatch(labels[label_name]) is None
                for label_name in (OPERATION_LABEL, WORKLOAD_LABEL, ATTEMPT_LABEL)
            )
        ):
            raise ScientificWriterError(
                "scientific workload ownership labels are absent or malformed"
            )
        annotations = _mapping(
            metadata.get("annotations"), "workload.metadata.annotations"
        )
        manifest_digest = annotations.get(MANIFEST_ANNOTATION)
        fence = annotations.get(FENCE_ANNOTATION)
        if (
            not isinstance(manifest_digest, str)
            or re.fullmatch(r"[a-f0-9]{64}", manifest_digest) is None
            or not isinstance(fence, str)
            or re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?:[1-9][0-9]*", fence)
            is None
        ):
            raise ScientificWriterError(
                "scientific workload has no exact manifest digest/controller fence"
            )
        if verify_submission_digest and manifest_digest != _manifest_digest(value):
            raise ScientificWriterError("scientific manifest digest is absent or inconsistent")
    if not isinstance(namespace, str) or not namespace:
        raise ScientificWriterError("scientific manifest namespace is absent")
    if resource == "jobs":
        if verify_submission_digest and (
            set(spec) != {"activeDeadlineSeconds", "backoffLimit", "suspend", "template"}
            or not isinstance(spec.get("activeDeadlineSeconds"), int)
            or isinstance(spec.get("activeDeadlineSeconds"), bool)
            or not 1 <= spec["activeDeadlineSeconds"] <= 7 * 24 * 3600
            or spec.get("backoffLimit") != 0
            or spec.get("suspend") is not True
        ):
            raise ScientificWriterError("scientific Job controller envelope differs from the renderer")
        template = _mapping(spec.get("template"), "Job.spec.template")
        _profile(_mapping(template.get("metadata"), "Job Pod template metadata"), "Job Pod template")
        if execution_policy is not None:
            execution_policy.validate_pod(template, namespace=namespace, label="Job Pod template")
        return
    replicated = spec.get("replicatedJobs")
    if not isinstance(replicated, list) or not replicated:
        raise ScientificWriterError("JobSet has no replicated Jobs")
    if verify_submission_digest and (
        set(spec) != {"failurePolicy", "replicatedJobs", "suspend"}
        or spec.get("failurePolicy") != {"maxRestarts": 0}
        or spec.get("suspend") is not True
        or len(replicated) != 1
    ):
        raise ScientificWriterError("scientific JobSet controller envelope differs from the renderer")
    for index, raw in enumerate(replicated):
        job = _mapping(raw, f"JobSet replicatedJobs[{index}]")
        if verify_submission_digest and (
            set(job) != {"name", "replicas", "template"}
            or job.get("name") != "gang"
            or not isinstance(job.get("replicas"), int)
            or isinstance(job.get("replicas"), bool)
            or not 1 <= job["replicas"] <= 256
        ):
            raise ScientificWriterError("scientific JobSet member differs from the renderer")
        template = _mapping(job.get("template"), f"JobSet replicatedJobs[{index}].template")
        _profile(
            _mapping(template.get("metadata"), f"JobSet replicatedJobs[{index}] metadata"),
            f"JobSet replicatedJobs[{index}]",
        )
        job_spec = _mapping(template.get("spec"), "Job template spec")
        if verify_submission_digest and (
            set(job_spec) != {"activeDeadlineSeconds", "backoffLimit", "template"}
            or job_spec.get("backoffLimit") != 0
            or not isinstance(job_spec.get("activeDeadlineSeconds"), int)
            or isinstance(job_spec.get("activeDeadlineSeconds"), bool)
            or not 1 <= job_spec["activeDeadlineSeconds"] <= 7 * 24 * 3600
        ):
            raise ScientificWriterError("scientific JobSet Job envelope differs from the renderer")
        pod = _mapping(job_spec.get("template"), "Job Pod template")
        _profile(_mapping(pod.get("metadata"), "JobSet Pod template metadata"), "JobSet Pod template")
        if execution_policy is not None:
            execution_policy.validate_pod(
                pod,
                namespace=namespace,
                label=f"JobSet replicatedJobs[{index}] Pod template",
            )


class ScientificWriter:
    def __init__(
        self,
        *,
        api_url: str,
        token_file: Path,
        ca_file: Path,
        caller_username: str,
        caller_audience: str,
        allowed_namespaces: frozenset[str],
        execution_policy: ScientificExecutionPolicy | None = None,
        timeout_seconds: float = 5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.token_file = token_file
        self.caller_username = caller_username
        self.caller_audience = caller_audience
        self.allowed_namespaces = allowed_namespaces
        self.execution_policy = execution_policy
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url=api_url.rstrip("/"),
            verify=str(ca_file),
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=False,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def _api_headers(self) -> dict[str, str]:
        token = self.token_file.read_text(encoding="utf-8").strip()
        if len(token) < 16:
            raise ScientificWriterError("scientific writer Kubernetes token is unavailable")
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    async def authorize(self, authorization: str | None) -> None:
        if authorization is None or not authorization.startswith("Bearer "):
            raise ScientificWriterError("scientific writer caller token is absent")
        caller_token = authorization.removeprefix("Bearer ").strip()
        if len(caller_token) < 16:
            raise ScientificWriterError("scientific writer caller token is malformed")
        response = await self.client.post(
            "/apis/authentication.k8s.io/v1/tokenreviews",
            headers=self._api_headers(),
            json={
                "apiVersion": "authentication.k8s.io/v1",
                "kind": "TokenReview",
                "spec": {"token": caller_token, "audiences": [self.caller_audience]},
            },
        )
        if response.status_code != 201:
            raise ScientificWriterError("scientific writer TokenReview failed closed")
        try:
            token_review = response.json()
        except ValueError as exc:
            raise ScientificWriterError("scientific writer TokenReview returned malformed JSON") from exc
        status = _mapping(_mapping(token_review, "TokenReview").get("status"), "TokenReview.status")
        user = _mapping(status.get("user"), "TokenReview.status.user")
        groups = user.get("groups", [])
        extra = user.get("extra", {})
        allowed_extra_keys = {
            "authentication.kubernetes.io/credential-id",
            "authentication.kubernetes.io/node-name",
            "authentication.kubernetes.io/node-uid",
            "authentication.kubernetes.io/pod-name",
            "authentication.kubernetes.io/pod-uid",
        }
        expected_namespace = self.caller_username.split(":", 3)[2]
        if (
            status.get("authenticated") is not True
            or status.get("audiences") != [self.caller_audience]
            or user.get("username") != self.caller_username
            or not isinstance(groups, list)
            or frozenset(groups)
            != frozenset(
                {
                    "system:authenticated",
                    "system:serviceaccounts",
                    f"system:serviceaccounts:{expected_namespace}",
                }
            )
            or not isinstance(extra, Mapping)
            or not set(extra).issubset(allowed_extra_keys)
        ):
            raise ScientificWriterError("scientific writer caller identity is not exact")

    async def mutate(self, mutation: Mutation) -> httpx.Response:
        decoded = unquote(mutation.path)
        match = _PATH.fullmatch(decoded)
        if match is None or decoded != mutation.path:
            raise ScientificWriterError("scientific writer path is not canonical")
        values = match.groupdict()
        namespace = values["namespace"]
        resource = values["resource"]
        if namespace not in self.allowed_namespaces:
            raise ScientificWriterError("scientific writer namespace is not authorized")
        if resource == "jobs" and (values["group"], values["version"]) != ("batch", "v1"):
            raise ScientificWriterError("scientific Job API is not exact")
        if resource == "jobsets" and (values["group"], values["version"]) != (
            "jobset.x-k8s.io",
            "v1alpha2",
        ):
            raise ScientificWriterError("scientific JobSet API is not exact")
        if mutation.method == "POST":
            if values["name"] is not None:
                raise ScientificWriterError("scientific create must target a collection")
            if mutation.ownership is not None:
                raise ScientificWriterError("scientific create must not assert delete ownership")
            if self.execution_policy is None:
                raise ScientificWriterError("scientific writer has no immutable execution policy")
            validate_scientific_manifest(
                mutation.body,
                resource=resource,
                execution_policy=self.execution_policy,
            )
            metadata = _mapping(mutation.body.get("metadata"), "workload.metadata")
            if metadata.get("namespace") != namespace:
                raise ScientificWriterError("scientific manifest namespace differs from its path")
        else:
            if values["name"] is None:
                raise ScientificWriterError("scientific delete must target one exact object")
            expected_ownership = mutation.ownership
            if (
                not isinstance(expected_ownership, Mapping)
                or set(expected_ownership)
                != {
                    OPERATION_LABEL,
                    WORKLOAD_LABEL,
                    ATTEMPT_LABEL,
                    FENCE_ANNOTATION,
                    MANIFEST_ANNOTATION,
                }
                or any(
                    UUID_LABEL_PATTERN.fullmatch(
                        expected_ownership[label_name]
                    )
                    is None
                    for label_name in (
                        OPERATION_LABEL,
                        WORKLOAD_LABEL,
                        ATTEMPT_LABEL,
                    )
                )
                or re.fullmatch(
                    r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?:[1-9][0-9]*",
                    expected_ownership[FENCE_ANNOTATION],
                )
                is None
                or re.fullmatch(
                    r"[a-f0-9]{64}", expected_ownership[MANIFEST_ANNOTATION]
                )
                is None
            ):
                raise ScientificWriterError(
                    "scientific delete lacks exact controller ownership"
                )
            preconditions = _mapping(mutation.body.get("preconditions"), "DeleteOptions.preconditions")
            if (
                set(mutation.body)
                != {"apiVersion", "kind", "propagationPolicy", "preconditions"}
                or set(preconditions) != {"uid", "resourceVersion"}
                or mutation.body.get("apiVersion") != "v1"
                or mutation.body.get("kind") != "DeleteOptions"
                or mutation.body.get("propagationPolicy") != "Foreground"
                or not isinstance(preconditions.get("uid"), str)
                or not isinstance(preconditions.get("resourceVersion"), str)
            ):
                raise ScientificWriterError("scientific delete lacks exact UID/resourceVersion fencing")
            response = await self.client.get(mutation.path, headers=self._api_headers())
            if response.status_code != 200:
                raise ScientificWriterError("scientific delete target is unavailable")
            try:
                live = response.json()
            except ValueError as exc:
                raise ScientificWriterError("scientific delete target is malformed") from exc
            if not isinstance(live, Mapping) or self.execution_policy is None:
                raise ScientificWriterError("scientific delete target has no immutable execution policy")
            validate_scientific_manifest(
                live,
                resource=resource,
                execution_policy=self.execution_policy,
                # The API server defaults and annotates stored Jobs/JobSets.
                # Recomputing the submit-time digest over that live object is
                # neither stable nor useful. The writer instead revalidates the
                # complete immutable execution policy and exact ownership/fence,
                # then binds DELETE to the live UID and resourceVersion below.
                verify_submission_digest=False,
            )
            live_metadata = _mapping(live.get("metadata"), "scientific delete target metadata")
            live_labels = _mapping(
                live_metadata.get("labels"), "scientific delete target labels"
            )
            live_annotations = _mapping(
                live_metadata.get("annotations"),
                "scientific delete target annotations",
            )
            if (
                live_metadata.get("name") != values["name"]
                or live_metadata.get("namespace") != namespace
                or live_metadata.get("uid") != preconditions.get("uid")
                or live_metadata.get("resourceVersion") != preconditions.get("resourceVersion")
                or any(
                    live_labels.get(label_name)
                    != expected_ownership[label_name]
                    for label_name in (
                        OPERATION_LABEL,
                        WORKLOAD_LABEL,
                        ATTEMPT_LABEL,
                    )
                )
                or live_annotations.get(FENCE_ANNOTATION)
                != expected_ownership[FENCE_ANNOTATION]
                or live_annotations.get(MANIFEST_ANNOTATION)
                != expected_ownership[MANIFEST_ANNOTATION]
            ):
                raise ScientificWriterError(
                    "scientific delete target differs from its exact UID/resourceVersion/ownership fence"
                )
        return await self.client.request(
            mutation.method,
            mutation.path,
            headers=self._api_headers(),
            json=mutation.body,
        )

    async def ready(self) -> None:
        response = await self.client.get("/version", headers=self._api_headers())
        if response.status_code != 200:
            raise ScientificWriterError("scientific writer cannot reach Kubernetes")


def create_scientific_writer_app(writer: ScientificWriter) -> FastAPI:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.get("/livez")
    async def livez() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        try:
            await writer.ready()
        except (OSError, httpx.HTTPError, ScientificWriterError) as exc:
            return JSONResponse({"status": "not-ready", "detail": str(exc)}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.post("/v1/mutate")
    async def mutate(
        mutation: Mutation,
        authorization: str | None = Header(default=None),
    ) -> Response:
        try:
            await writer.authorize(authorization)
            response = await writer.mutate(mutation)
        except (OSError, httpx.HTTPError, ScientificWriterError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        return Response(
            content=response.content,
            status_code=response.status_code,
            media_type=response.headers.get("content-type", "application/json"),
        )

    return app


__all__ = [
    "Mutation",
    "ScientificExecutionPolicy",
    "ScientificWriter",
    "ScientificWriterError",
    "create_scientific_writer_app",
    "validate_scientific_manifest",
]
