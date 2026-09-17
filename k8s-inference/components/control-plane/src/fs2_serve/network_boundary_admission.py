"""Fail-closed admission boundary for model-network privileged objects.

The generic workload-profile CEL policies validate object shape.  This service
supplies the two checks CEL cannot perform safely: dereferencing a controller
owner to its live UID/profile and binding a privileged mutation to the random
holder of the retained transition Lease.  It runs under a read-only service
account that is distinct from model, scientific, acquisition, Helm and
Terraform writers.
"""

from __future__ import annotations

import hashlib
import json
import re
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

PROFILE_LABEL = "fs2-serve.nebius.ai/network-profile"
REFERENCE_DATA_HOST_PATH = "/mnt/fs2-reference-data/data"
CLASS_LABEL = "fs2-serve.nebius.ai/network-workload-class"
TRANSITION_WRITER_ANNOTATION = "fs2-serve.nebius.ai/network-transition-writer"
TRANSITION_HOLDER_ANNOTATION = "fs2-serve.nebius.ai/network-transition-holder"
TRANSITION_LEASE = "fs2-model-network-transition"
MAINTENANCE_LEASE = "fs2-model-network-maintenance"
BOUNDARY_MARKER = "fs2-runtime-network-policy-boundary-v2"
BOUNDARY_OBJECT_LABEL = "fs2-serve.nebius.ai/network-boundary-object"
BOUNDARY_AUTHORITY_LABEL = "fs2-serve.nebius.ai/network-boundary-authority"
HOLDER_PATTERN = re.compile(r"^[a-z][a-z0-9]{5,11}:[1-9][0-9]*:[a-f0-9]{32}$")


class NetworkBoundaryError(RuntimeError):
    """An admission request cannot be proven safe."""


@dataclass(frozen=True)
class ParentKind:
    api_version: str
    kind: str
    resource_path: str
    writers: frozenset[str]


@dataclass(frozen=True)
class ReleaseInventoryEntry:
    """One externally signed Helm mutation, with no label-derived authority."""

    group: str
    resource: str
    namespace: str
    name: str
    operations: frozenset[str]
    object_sha256: str

    @classmethod
    def from_value(cls, value: Mapping[str, Any]) -> ReleaseInventoryEntry:
        operations = value.get("operations")
        fields = {key: value.get(key) for key in ("group", "resource", "namespace", "name")}
        digest = value.get("objectSha256")
        if (
            not all(isinstance(item, str) for item in fields.values())
            or not fields["resource"]
            or not fields["name"]
            or not isinstance(operations, (list, tuple))
            or not operations
            or not all(item in {"CREATE", "UPDATE", "DELETE"} for item in operations)
            or len(set(operations)) != len(operations)
            or not isinstance(digest, str)
            or re.fullmatch(r"[a-f0-9]{64}", digest) is None
        ):
            raise NetworkBoundaryError("the signed Helm release inventory is malformed")
        return cls(
            group=fields["group"],
            resource=fields["resource"],
            namespace=fields["namespace"],
            name=fields["name"],
            operations=frozenset(operations),
            object_sha256=digest,
        )


@dataclass(frozen=True)
class NetworkBoundaryConfig:
    model_namespace: str
    system_namespace: str
    authority_namespace: str
    acquisition_writer: str
    direct_job_writer: str
    jobset_writer: str
    model_controller_writer: str
    authorizer_writer: str
    transition_writer: str
    maintenance_writer: str
    certificate_writer: str
    authorizer_groups: frozenset[str]
    transition_groups: frozenset[str]
    maintenance_groups: frozenset[str]
    certificate_groups: frozenset[str]
    custody_epoch: str
    custody_active_from: datetime
    custody_active_until: datetime
    release_inventory: tuple[ReleaseInventoryEntry, ...] = ()
    controller_manager_writer: str = "system:kube-controller-manager"

    def parent_kinds(self) -> Mapping[tuple[str, str], ParentKind]:
        manager = self.controller_manager_writer
        service_account = "system:serviceaccount:kube-system:"
        return {
            ("ReplicaSet", "Deployment"): ParentKind(
                "apps/v1",
                "Deployment",
                "apis/apps/v1/namespaces/{namespace}/deployments/{name}",
                frozenset({manager, f"{service_account}deployment-controller"}),
            ),
            ("Job", "JobSet"): ParentKind(
                "jobset.x-k8s.io/v1alpha2",
                "JobSet",
                "apis/jobset.x-k8s.io/v1alpha2/namespaces/{namespace}/jobsets/{name}",
                frozenset({self.jobset_writer}),
            ),
            ("Job", "CronJob"): ParentKind(
                "batch/v1",
                "CronJob",
                "apis/batch/v1/namespaces/{namespace}/cronjobs/{name}",
                frozenset({manager, f"{service_account}cronjob-controller"}),
            ),
            ("Pod", "ReplicaSet"): ParentKind(
                "apps/v1",
                "ReplicaSet",
                "apis/apps/v1/namespaces/{namespace}/replicasets/{name}",
                frozenset({manager, f"{service_account}replicaset-controller"}),
            ),
            ("Pod", "StatefulSet"): ParentKind(
                "apps/v1",
                "StatefulSet",
                "apis/apps/v1/namespaces/{namespace}/statefulsets/{name}",
                frozenset({manager, f"{service_account}statefulset-controller"}),
            ),
            ("Pod", "DaemonSet"): ParentKind(
                "apps/v1",
                "DaemonSet",
                "apis/apps/v1/namespaces/{namespace}/daemonsets/{name}",
                frozenset({manager, f"{service_account}daemon-set-controller"}),
            ),
            ("Pod", "ReplicationController"): ParentKind(
                "v1",
                "ReplicationController",
                "api/v1/namespaces/{namespace}/replicationcontrollers/{name}",
                frozenset({manager, f"{service_account}replication-controller"}),
            ),
            ("Pod", "Job"): ParentKind(
                "batch/v1",
                "Job",
                "apis/batch/v1/namespaces/{namespace}/jobs/{name}",
                frozenset({manager, f"{service_account}job-controller"}),
            ),
        }


class KubernetesBoundaryReader:
    """Read-only in-cluster Kubernetes adapter used by admission."""

    def __init__(
        self,
        *,
        base_url: str,
        token_file: Path,
        ca_file: Path,
        timeout_seconds: float = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.token_file = token_file
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            verify=str(ca_file),
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=False,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def _headers(self) -> dict[str, str]:
        token = self.token_file.read_text(encoding="utf-8").strip()
        if len(token) < 16:
            raise NetworkBoundaryError("projected Kubernetes token is unavailable")
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    async def get(self, path: str) -> dict[str, Any]:
        try:
            response = await self.client.get("/" + path.lstrip("/"), headers=self._headers())
        except (OSError, httpx.HTTPError) as exc:
            raise NetworkBoundaryError("Kubernetes parent lookup failed") from exc
        if response.status_code == 404:
            raise NetworkBoundaryError("the referenced live parent does not exist")
        if response.status_code >= 400:
            raise NetworkBoundaryError(f"Kubernetes parent lookup returned HTTP {response.status_code}")
        value = response.json()
        if not isinstance(value, dict):
            raise NetworkBoundaryError("Kubernetes parent lookup returned a non-object")
        return value

    async def get_optional(self, path: str) -> dict[str, Any] | None:
        try:
            response = await self.client.get("/" + path.lstrip("/"), headers=self._headers())
        except (OSError, httpx.HTTPError) as exc:
            raise NetworkBoundaryError("Kubernetes boundary lookup failed") from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise NetworkBoundaryError(f"Kubernetes boundary lookup returned HTTP {response.status_code}")
        value = response.json()
        if not isinstance(value, dict):
            raise NetworkBoundaryError("Kubernetes boundary lookup returned a non-object")
        return value


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise NetworkBoundaryError(f"{label} is missing or malformed")
    return value


def _labels(value: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    return _mapping(_mapping(value.get("metadata"), f"{label}.metadata").get("labels"), f"{label}.labels")


def _profile(value: Mapping[str, Any], label: str) -> tuple[str, str]:
    labels = _labels(value, label)
    workload_class = labels.get(CLASS_LABEL)
    profile = labels.get(PROFILE_LABEL)
    if not isinstance(workload_class, str) or not workload_class:
        raise NetworkBoundaryError(f"{label} has no network workload class")
    if not isinstance(profile, str) or not profile:
        raise NetworkBoundaryError(f"{label} has no network profile")
    return workload_class, profile


def _controller_owner(value: Mapping[str, Any]) -> Mapping[str, Any] | None:
    metadata = _mapping(value.get("metadata"), "object.metadata")
    raw = metadata.get("ownerReferences", [])
    if not isinstance(raw, list):
        raise NetworkBoundaryError("object ownerReferences is malformed")
    owners = [owner for owner in raw if isinstance(owner, Mapping) and owner.get("controller") is True]
    if not owners:
        return None
    if len(owners) != 1:
        raise NetworkBoundaryError("object must have exactly one controller owner")
    return owners[0]


def _parent_child_profile(parent: Mapping[str, Any], parent_kind: str, child: Mapping[str, Any]) -> tuple[str, str]:
    spec = _mapping(parent.get("spec"), "parent.spec")
    if parent_kind == "CronJob":
        job_template = _mapping(spec.get("jobTemplate"), "CronJob.spec.jobTemplate")
        parent_profile = _profile(job_template, "CronJob Job template")
        pod_template = _mapping(
            _mapping(job_template.get("spec"), "CronJob Job spec").get("template"),
            "CronJob Pod template",
        )
        if _profile(pod_template, "CronJob Pod template") != parent_profile:
            raise NetworkBoundaryError("CronJob Job and Pod templates disagree on network profile")
        return parent_profile
    if parent_kind == "JobSet":
        child_labels = _labels(child, "Job")
        replicated_name = child_labels.get("jobset.sigs.k8s.io/replicatedjob-name")
        if not isinstance(replicated_name, str) or not replicated_name:
            raise NetworkBoundaryError("JobSet child has no replicated-job identity")
        replicated = spec.get("replicatedJobs")
        if not isinstance(replicated, list):
            raise NetworkBoundaryError("JobSet replicatedJobs is malformed")
        matches = [item for item in replicated if isinstance(item, Mapping) and item.get("name") == replicated_name]
        if len(matches) != 1:
            raise NetworkBoundaryError("JobSet child does not identify one live replicated job")
        job_template = _mapping(matches[0].get("template"), "JobSet Job template")
        parent_profile = _profile(job_template, "JobSet Job template")
        pod_template = _mapping(
            _mapping(job_template.get("spec"), "JobSet Job spec").get("template"),
            "JobSet Pod template",
        )
        if _profile(pod_template, "JobSet Pod template") != parent_profile:
            raise NetworkBoundaryError("JobSet Job and Pod templates disagree on network profile")
        return parent_profile
    template = _mapping(spec.get("template"), f"{parent_kind}.spec.template")
    return _profile(template, f"{parent_kind} Pod template")


def _pod_specs(value: Mapping[str, Any], kind: str) -> tuple[Mapping[str, Any], ...]:
    """Return every PodSpec embedded in a profiled object, without guessing."""

    spec = _mapping(value.get("spec"), f"{kind}.spec")
    if kind == "Pod":
        return (spec,)
    if kind in {
        "Deployment",
        "StatefulSet",
        "DaemonSet",
        "ReplicaSet",
        "ReplicationController",
        "Job",
    }:
        template = _mapping(spec.get("template"), f"{kind}.spec.template")
        return (_mapping(template.get("spec"), f"{kind}.spec.template.spec"),)
    if kind == "CronJob":
        job_template = _mapping(spec.get("jobTemplate"), "CronJob.spec.jobTemplate")
        job_spec = _mapping(job_template.get("spec"), "CronJob.spec.jobTemplate.spec")
        template = _mapping(job_spec.get("template"), "CronJob Job template")
        return (_mapping(template.get("spec"), "CronJob Pod template spec"),)
    if kind == "JobSet":
        replicated = spec.get("replicatedJobs")
        if not isinstance(replicated, list) or not replicated:
            raise NetworkBoundaryError("JobSet has no replicated Job PodSpecs")
        result: list[Mapping[str, Any]] = []
        for index, raw_job in enumerate(replicated):
            job = _mapping(raw_job, f"JobSet.spec.replicatedJobs[{index}]")
            template = _mapping(job.get("template"), f"JobSet Job template {index}")
            job_spec = _mapping(template.get("spec"), f"JobSet Job spec {index}")
            pod_template = _mapping(job_spec.get("template"), f"JobSet Pod template {index}")
            result.append(_mapping(pod_template.get("spec"), f"JobSet PodSpec {index}"))
        return tuple(result)
    raise NetworkBoundaryError("the profiled object kind has no reviewed PodSpec projection")


def _validate_pod_security(
    spec: Mapping[str, Any], label: str, *, allow_ipc_lock: bool
) -> None:
    """Enforce the restricted host/capability boundary on every selected PodSpec."""

    if (
        any(
            spec.get(field, False) is not False
            for field in ("hostNetwork", "hostPID", "hostIPC")
        )
        or spec.get("automountServiceAccountToken") is not False
        or spec.get("shareProcessNamespace", False) is not False
    ):
        raise NetworkBoundaryError(
            f"{label} can reach host namespaces or a service-account token"
        )
    volumes = spec.get("volumes", [])
    if not isinstance(volumes, list) or not all(
        isinstance(volume, Mapping) for volume in volumes
    ):
        raise NetworkBoundaryError(f"{label} has a malformed volume")
    if any(
        "secret" in volume
        or "projected" in volume
        or any(
            isinstance(source, Mapping) and "serviceAccountToken" in source
            for source in _mapping(
                volume.get("projected", {}), f"{label} projected volume"
            ).get("sources", [])
            if isinstance(source, Mapping)
        )
        for volume in volumes
    ):
        raise NetworkBoundaryError(
            f"{label} can mount a Secret or projected service-account token"
        )
    pod_security = spec.get("securityContext", {})
    if not isinstance(pod_security, Mapping):
        raise NetworkBoundaryError(f"{label} Pod securityContext is malformed")
    pod_run_as_non_root = pod_security.get("runAsNonRoot") is True
    pod_seccomp = pod_security.get("seccompProfile", {})
    pod_runtime_default = (
        isinstance(pod_seccomp, Mapping)
        and pod_seccomp.get("type") == "RuntimeDefault"
    )
    raw_containers = [
        spec.get("containers"),
        spec.get("initContainers", []),
        spec.get("ephemeralContainers", []),
    ]
    if not isinstance(raw_containers[0], list) or not raw_containers[0]:
        raise NetworkBoundaryError(f"{label} has no bounded container set")
    containers: list[Mapping[str, Any]] = []
    for collection in raw_containers:
        if not isinstance(collection, list):
            raise NetworkBoundaryError(f"{label} container collection is malformed")
        for container in collection:
            if not isinstance(container, Mapping):
                raise NetworkBoundaryError(f"{label} contains a malformed container")
            containers.append(container)
            security = container.get("securityContext")
            if not isinstance(security, Mapping):
                raise NetworkBoundaryError(f"{label} container securityContext is absent")
            capabilities = security.get("capabilities")
            container_seccomp = security.get("seccompProfile", {})
            runtime_default = pod_runtime_default or (
                isinstance(container_seccomp, Mapping)
                and container_seccomp.get("type") == "RuntimeDefault"
            )
            ports = container.get("ports", [])
            if (
                security.get("privileged", False) is not False
                or security.get("allowPrivilegeEscalation") is not False
                or not (pod_run_as_non_root or security.get("runAsNonRoot") is True)
                or not isinstance(capabilities, Mapping)
                or set(capabilities) - {"add", "drop"}
                or capabilities.get("drop") != ["ALL"]
                or capabilities.get("add", [])
                not in ((None, [], ["IPC_LOCK"]) if allow_ipc_lock else (None, []))
                or security.get("procMount", "Default") != "Default"
                or not runtime_default
                or not isinstance(ports, list)
                or container.get("volumeDevices") not in (None, [])
                or any(
                    not isinstance(port, Mapping)
                    or port.get("hostPort", 0) not in (None, 0)
                    for port in ports
                )
            ):
                raise NetworkBoundaryError(
                    f"{label} container escapes the non-root/no-capabilities security envelope"
                )
    for volume in volumes:
        if "hostPath" not in volume:
            continue
        host_path = volume.get("hostPath")
        name = volume.get("name")
        matching_mounts = [
            mount
            for container in containers
            for mount in container.get("volumeMounts", [])
            if isinstance(mount, Mapping) and mount.get("name") == name
        ]
        if (
            set(volume) != {"name", "hostPath"}
            or not isinstance(name, str)
            or host_path
            != {"path": REFERENCE_DATA_HOST_PATH, "type": "Directory"}
            or not matching_mounts
            or any(
                mount.get("readOnly") is not True
                or "mountPropagation" in mount
                or "subPathExpr" in mount
                for mount in matching_mounts
            )
        ):
            raise NetworkBoundaryError(
                f"{label} has a hostPath outside the exact read-only reference plane"
            )


class NetworkBoundaryAdmission:
    def __init__(
        self,
        *,
        config: NetworkBoundaryConfig,
        reader: KubernetesBoundaryReader,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.reader = reader
        self.clock = clock or (lambda: datetime.now(UTC))
        keys = [
            (entry.group, entry.resource, entry.namespace, entry.name, operation)
            for entry in config.release_inventory
            for operation in entry.operations
        ]
        if len(keys) != len(set(keys)):
            raise NetworkBoundaryError("the signed Helm release inventory has duplicate authorities")
        if (
            re.fullmatch(r"[a-f0-9]{64}", config.custody_epoch) is None
            or config.custody_active_from.tzinfo is None
            or config.custody_active_until.tzinfo is None
            or config.custody_active_from >= config.custody_active_until
            or config.custody_active_until - config.custody_active_from
            > timedelta(hours=2)
        ):
            raise NetworkBoundaryError("the admission custody epoch is malformed")

    def _require_active_custody(self) -> datetime:
        now = self.clock().astimezone(UTC)
        if not (
            self.config.custody_active_from.astimezone(UTC)
            <= now
            < self.config.custody_active_until.astimezone(UTC)
        ):
            raise NetworkBoundaryError("the signed admission custody epoch is not active")
        return now

    @staticmethod
    def _service_account_groups(username: str) -> frozenset[str] | None:
        prefix = "system:serviceaccount:"
        if not username.startswith(prefix):
            return None
        parts = username.removeprefix(prefix).split(":", 1)
        if len(parts) != 2 or not all(parts):
            raise NetworkBoundaryError("service-account username is malformed")
        return frozenset(
            {
                "system:authenticated",
                "system:serviceaccounts",
                f"system:serviceaccounts:{parts[0]}",
            }
        )

    def _expected_groups(self, username: str) -> frozenset[str]:
        configured = {
            self.config.authorizer_writer: self.config.authorizer_groups,
            self.config.transition_writer: self.config.transition_groups,
            self.config.maintenance_writer: self.config.maintenance_groups,
            self.config.certificate_writer: self.config.certificate_groups,
        }.get(username)
        if configured is not None:
            return configured
        service_account = self._service_account_groups(username)
        if service_account is not None:
            return service_account
        if username == self.config.controller_manager_writer:
            return frozenset({"system:authenticated"})
        raise NetworkBoundaryError("the authenticated writer has no exact group contract")

    def _authorize_identity(self, user_info: Mapping[str, Any], expected_username: str) -> None:
        if expected_username in {
            self.config.authorizer_writer,
            self.config.transition_writer,
            self.config.maintenance_writer,
        }:
            self._require_active_custody()
        username = user_info.get("username")
        groups = user_info.get("groups", [])
        extra = user_info.get("extra", {})
        if username != expected_username or not isinstance(groups, list) or not all(
            isinstance(group, str) for group in groups
        ):
            raise NetworkBoundaryError("the authenticated writer identity is not exact")
        if frozenset(groups) != self._expected_groups(expected_username):
            raise NetworkBoundaryError("the authenticated writer group set is not exact")
        if not isinstance(extra, Mapping):
            raise NetworkBoundaryError("the authenticated writer extras are malformed")
        allowed_service_account_extras = {
            "authentication.kubernetes.io/credential-id",
            "authentication.kubernetes.io/node-name",
            "authentication.kubernetes.io/node-uid",
            "authentication.kubernetes.io/pod-name",
            "authentication.kubernetes.io/pod-uid",
        }
        allowed = (
            allowed_service_account_extras
            if expected_username.startswith("system:serviceaccount:")
            else set()
        )
        if not set(extra).issubset(allowed):
            raise NetworkBoundaryError("the authenticated writer has unapproved identity extras")

    def _lease_expires_at(self, value: Mapping[str, Any], label: str) -> datetime:
        spec = _mapping(value.get("spec"), f"{label}.spec")
        duration = spec.get("leaseDurationSeconds")
        renew_time = spec.get("renewTime")
        if (
            not isinstance(duration, int)
            or isinstance(duration, bool)
            or duration < 1
            or duration > 7200
            or not isinstance(renew_time, str)
        ):
            raise NetworkBoundaryError(f"{label} timing is malformed")
        try:
            renewed = datetime.fromisoformat(renew_time.replace("Z", "+00:00"))
        except ValueError as exc:
            raise NetworkBoundaryError(f"{label} renewTime is malformed") from exc
        if renewed.tzinfo is None:
            raise NetworkBoundaryError(f"{label} renewTime has no timezone")
        renewed = renewed.astimezone(UTC)
        if renewed - self.clock().astimezone(UTC) > timedelta(seconds=30):
            raise NetworkBoundaryError(f"{label} renewTime is ahead of server time")
        return renewed + timedelta(seconds=duration)

    async def _active_holder(self, *, lease_name: str, writer: str) -> str:
        lease = await self.reader.get(
            "apis/coordination.k8s.io/v1/namespaces/"
            f"{quote(self.config.system_namespace, safe='')}/leases/{lease_name}"
        )
        metadata = _mapping(lease.get("metadata"), "transition Lease.metadata")
        annotations = _mapping(metadata.get("annotations"), "transition Lease.annotations")
        spec = _mapping(lease.get("spec"), "transition Lease.spec")
        holder = spec.get("holderIdentity")
        if not isinstance(holder, str) or HOLDER_PATTERN.fullmatch(holder) is None:
            raise NetworkBoundaryError("the request has no valid transition holder")
        if (
            annotations.get(TRANSITION_WRITER_ANNOTATION) != writer
            or annotations.get(TRANSITION_HOLDER_ANNOTATION) != holder
        ):
            raise NetworkBoundaryError("the transition Lease identity is not exact")
        if self._lease_expires_at(lease, "transition Lease") <= self.clock():
            raise NetworkBoundaryError("the request has an expired transition holder")
        return holder

    async def _active_transition_holder(self) -> str:
        return await self._active_holder(
            lease_name=TRANSITION_LEASE,
            writer=self.config.transition_writer,
        )

    async def _active_maintenance_holder(self) -> str:
        return await self._active_holder(
            lease_name=MAINTENANCE_LEASE,
            writer=self.config.maintenance_writer,
        )

    async def _authorize_child(
        self,
        *,
        kind: str,
        namespace: str,
        operation: str,
        user_info: Mapping[str, Any],
        value: Mapping[str, Any],
        old_value: Mapping[str, Any],
    ) -> None:
        username = user_info.get("username")
        if not isinstance(username, str):
            raise NetworkBoundaryError("the authenticated child writer is malformed")
        owner = _controller_owner(value)
        if owner is None:
            if kind != "Job":
                raise NetworkBoundaryError("a Pod or ReplicaSet requires one live controller owner")
            workload_class, _profile_name = _profile(value, "Job")
            expected_writer = (
                self.config.acquisition_writer
                if workload_class == "public-acquisition"
                else self.config.direct_job_writer
            )
            if username != expected_writer:
                raise NetworkBoundaryError("a direct Job requires its exact platform writer")
            self._authorize_identity(user_info, expected_writer)
            if operation == "UPDATE":
                if _controller_owner(old_value) is not None:
                    raise NetworkBoundaryError("a direct Job cannot drop its controller owner")
                if _profile(old_value, "old Job") != (workload_class, _profile_name):
                    raise NetworkBoundaryError("a direct Job cannot change its network profile")
            return
        owner_kind = owner.get("kind")
        parent = self.config.parent_kinds().get((kind, owner_kind))
        if parent is None:
            raise NetworkBoundaryError("the controller owner kind is not admitted for this child")
        if username not in parent.writers:
            raise NetworkBoundaryError("the child request did not come from its exact Kubernetes controller")
        self._authorize_identity(user_info, username)
        if owner.get("apiVersion") != parent.api_version:
            raise NetworkBoundaryError("the child owner apiVersion is not exact")
        owner_name = owner.get("name")
        owner_uid = owner.get("uid")
        if not isinstance(owner_name, str) or not owner_name or not isinstance(owner_uid, str) or not owner_uid:
            raise NetworkBoundaryError("the child owner name or UID is missing")
        if operation == "UPDATE":
            old_owner = _controller_owner(old_value)
            if old_owner is None or any(
                old_owner.get(field) != owner.get(field)
                for field in ("apiVersion", "kind", "name", "uid", "controller")
            ):
                raise NetworkBoundaryError("a child update cannot change its controller owner")
            if _profile(old_value, f"old {kind}") != _profile(value, kind):
                raise NetworkBoundaryError("a child update cannot change its network profile")
        live = await self.reader.get(
            parent.resource_path.format(
                namespace=quote(namespace, safe=""),
                name=quote(owner_name, safe=""),
            )
        )
        metadata = _mapping(live.get("metadata"), "live parent.metadata")
        if live.get("apiVersion") != parent.api_version or live.get("kind") != parent.kind:
            raise NetworkBoundaryError("the live parent GVK differs from the child owner")
        if metadata.get("name") != owner_name or metadata.get("uid") != owner_uid:
            raise NetworkBoundaryError("the live parent name or UID differs from the child owner")
        if metadata.get("deletionTimestamp") is not None:
            raise NetworkBoundaryError("a deleting parent cannot authorize a new network child")
        child_profile = _profile(value, kind)
        if _profile(live, f"live {parent.kind}") != child_profile:
            raise NetworkBoundaryError("the child network profile differs from its live parent")
        if _parent_child_profile(live, parent.kind, value) != child_profile:
            raise NetworkBoundaryError("the child network profile differs from its live parent template")

    def _is_protected(
        self,
        resource: Mapping[str, Any],
        namespace: str,
        name: str,
        value: Mapping[str, Any],
        old_value: Mapping[str, Any],
    ) -> bool:
        group = resource.get("group", "")
        plural = resource.get("resource")
        if group == "networking.k8s.io" and plural == "networkpolicies" and namespace == self.config.model_namespace:
            return True
        if (
            group == ""
            and plural == "configmaps"
            and namespace == self.config.model_namespace
            and name == BOUNDARY_MARKER
        ):
            return True
        protected_namespaces = {
            self.config.model_namespace,
            self.config.system_namespace,
            self.config.authority_namespace,
        }
        if namespace in protected_namespaces or not namespace:
            for candidate in (value, old_value):
                metadata = candidate.get("metadata")
                if not isinstance(metadata, Mapping):
                    continue
                labels = metadata.get("labels")
                if isinstance(labels, Mapping) and labels.get(BOUNDARY_AUTHORITY_LABEL) == "true":
                    return True
        return False

    def _lease_identity(
        self,
        resource: Mapping[str, Any],
        namespace: str,
        name: str,
    ) -> tuple[str, str] | None:
        if not (
            resource.get("group") == "coordination.k8s.io"
            and resource.get("resource") == "leases"
            and namespace == self.config.system_namespace
        ):
            return None
        if name == TRANSITION_LEASE:
            return TRANSITION_LEASE, self.config.transition_writer
        if name == MAINTENANCE_LEASE:
            return MAINTENANCE_LEASE, self.config.maintenance_writer
        return None

    @staticmethod
    def _release_semantic_sha256(value: Mapping[str, Any]) -> str:
        document = json.loads(json.dumps(value))
        document.pop("status", None)
        metadata = document.get("metadata")
        if isinstance(metadata, dict):
            for field in (
                "creationTimestamp",
                "deletionGracePeriodSeconds",
                "deletionTimestamp",
                "generation",
                "managedFields",
                "resourceVersion",
                "selfLink",
                "uid",
            ):
                metadata.pop(field, None)
        return hashlib.sha256(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _claims_control_plane_release(
        self,
        resource: Mapping[str, Any],
        namespace: str,
        value: Mapping[str, Any],
        old_value: Mapping[str, Any],
    ) -> bool:
        if namespace != self.config.system_namespace:
            return False
        for candidate in (value, old_value):
            metadata = candidate.get("metadata")
            if not isinstance(metadata, Mapping):
                continue
            labels = metadata.get("labels")
            if (
                isinstance(labels, Mapping)
                and (
                    (
                        labels.get("owner") == "helm"
                        and labels.get("name") == "fs2-serve-control-plane"
                    )
                    or labels.get("app.kubernetes.io/instance")
                    == "fs2-serve-control-plane"
                )
            ):
                return True
        return False

    def _is_control_plane_release_object(
        self,
        resource: Mapping[str, Any],
        namespace: str,
        name: str,
        operation: str,
        value: Mapping[str, Any],
        old_value: Mapping[str, Any],
    ) -> bool:
        candidate = old_value if operation == "DELETE" else value
        digest = self._release_semantic_sha256(candidate)
        return any(
            entry.group == resource.get("group", "")
            and entry.resource == resource.get("resource")
            and entry.namespace == namespace
            and entry.name == name
            and operation in entry.operations
            and entry.object_sha256 == digest
            for entry in self.config.release_inventory
        )

    def _matches_release_identity(
        self,
        resource: Mapping[str, Any],
        namespace: str,
        name: str,
        operation: str,
    ) -> bool:
        return any(
            entry.group == resource.get("group", "")
            and entry.resource == resource.get("resource")
            and entry.namespace == namespace
            and entry.name == name
            and operation in entry.operations
            for entry in self.config.release_inventory
        )

    async def _authorize_lease_mutation(
        self,
        *,
        user_info: Mapping[str, Any],
        lease_name: str,
        expected_writer: str,
        operation: str,
        value: Mapping[str, Any],
        old_value: Mapping[str, Any],
    ) -> None:
        self._authorize_identity(user_info, expected_writer)
        now = self._require_active_custody()
        username = user_info.get("username")
        if operation == "DELETE":
            raise NetworkBoundaryError("the retained network-boundary Lease cannot be deleted")
        new_metadata = _mapping(value.get("metadata"), "transition Lease.metadata")
        new_annotations = _mapping(new_metadata.get("annotations"), "transition Lease.annotations")
        new_spec = _mapping(value.get("spec"), "transition Lease.spec")
        new_holder = new_spec.get("holderIdentity", "")
        duration = new_spec.get("leaseDurationSeconds")
        renew_time = new_spec.get("renewTime")
        try:
            renewed = datetime.fromisoformat(str(renew_time).replace("Z", "+00:00"))
        except ValueError as exc:
            raise NetworkBoundaryError("transition Lease renewTime is malformed") from exc
        if (
            not isinstance(duration, int)
            or isinstance(duration, bool)
            or not 1 <= duration <= 7200
            or renewed.tzinfo is None
            or abs((renewed.astimezone(UTC) - now).total_seconds()) > 30
            or renewed.astimezone(UTC) + timedelta(seconds=duration)
            > self.config.custody_active_until.astimezone(UTC)
        ):
            raise NetworkBoundaryError(
                "transition Lease timing is outside server time or signed custody"
            )
        if not isinstance(new_holder, str):
            raise NetworkBoundaryError(f"{lease_name} holder is malformed")
        if new_annotations.get(TRANSITION_WRITER_ANNOTATION) != username:
            raise NetworkBoundaryError("transition Lease writer annotation is not exact")
        if operation == "CREATE":
            if (
                HOLDER_PATTERN.fullmatch(new_holder) is None
                or new_annotations.get(TRANSITION_HOLDER_ANNOTATION) != new_holder
            ):
                raise NetworkBoundaryError("a new transition Lease requires its random holder token")
            return
        old_metadata = _mapping(old_value.get("metadata"), "old transition Lease.metadata")
        old_annotations = _mapping(old_metadata.get("annotations"), "old transition Lease.annotations")
        old_spec = _mapping(old_value.get("spec"), "old transition Lease.spec")
        old_holder = old_spec.get("holderIdentity", "")
        if not isinstance(old_holder, str):
            raise NetworkBoundaryError("old transition Lease holder is malformed")
        if old_annotations.get(TRANSITION_WRITER_ANNOTATION) != username:
            raise NetworkBoundaryError("old transition Lease belongs to another writer")
        if old_holder and old_annotations.get(TRANSITION_HOLDER_ANNOTATION) != old_holder:
            raise NetworkBoundaryError("old transition Lease is not bound to its holder token")
        if old_holder and new_holder not in {"", old_holder}:
            if self._lease_expires_at(old_value, "old transition Lease") > self.clock():
                raise NetworkBoundaryError("an active transition holder cannot be replaced")
        if new_holder and (
            HOLDER_PATTERN.fullmatch(new_holder) is None
            or new_annotations.get(TRANSITION_HOLDER_ANNOTATION) != new_holder
        ):
            raise NetworkBoundaryError("transition Lease update lacks its random holder token")
        if not old_holder and not new_holder:
            raise NetworkBoundaryError("an idle transition Lease update is not authorized")
        if new_holder and new_holder != old_holder:
            other_name = MAINTENANCE_LEASE if lease_name == TRANSITION_LEASE else TRANSITION_LEASE
            other = await self.reader.get_optional(
                "apis/coordination.k8s.io/v1/namespaces/"
                f"{quote(self.config.system_namespace, safe='')}/leases/{other_name}"
            )
            if other is not None:
                other_spec = _mapping(other.get("spec"), "other boundary Lease.spec")
                other_holder = other_spec.get("holderIdentity", "")
                if (
                    isinstance(other_holder, str)
                    and other_holder
                    and self._lease_expires_at(other, "other boundary Lease") > self.clock()
                ):
                    raise NetworkBoundaryError(
                        "transition and maintenance Leases cannot be active concurrently"
                    )

    async def _authorize_transition(
        self,
        *,
        user_info: Mapping[str, Any],
        operation: str,
        resource: Mapping[str, Any],
        namespace: str,
        value: Mapping[str, Any],
        old_value: Mapping[str, Any],
    ) -> None:
        username = user_info.get("username")
        if not isinstance(username, str):
            raise NetworkBoundaryError("the authenticated transition writer is malformed")
        metadata = _mapping((old_value if operation == "DELETE" else value).get("metadata"), "target.metadata")
        name = metadata.get("name")
        if not isinstance(name, str):
            raise NetworkBoundaryError("protected object has no name")
        lease_identity = self._lease_identity(resource, namespace, name)
        if lease_identity is not None:
            await self._authorize_lease_mutation(
                user_info=user_info,
                lease_name=lease_identity[0],
                expected_writer=lease_identity[1],
                operation=operation,
                value=value,
                old_value=old_value,
            )
            return
        claims_release = self._claims_control_plane_release(resource, namespace, value, old_value)
        release_identity = self._matches_release_identity(
            resource, namespace, name, operation
        )
        control_plane_release = self._is_control_plane_release_object(
            resource, namespace, name, operation, value, old_value
        )
        protected = self._is_protected(resource, namespace, name, value, old_value)
        release_writers = {
            self.config.authorizer_writer,
            self.config.maintenance_writer,
            self.config.transition_writer,
        }
        if (claims_release or release_identity or username in release_writers) and not (
            control_plane_release or protected
        ):
            if username in release_writers:
                self._authorize_identity(user_info, username)
            raise NetworkBoundaryError(
                "the mutation is absent from the externally signed finite Helm release inventory"
            )
        if not control_plane_release and not protected:
            return
        marker = await self.reader.get_optional(
            f"api/v1/namespaces/{quote(self.config.model_namespace, safe='')}/configmaps/{BOUNDARY_MARKER}"
        )
        if control_plane_release:
            default_deny = await self.reader.get_optional(
                "apis/networking.k8s.io/v1/namespaces/"
                f"{quote(self.config.model_namespace, safe='')}/networkpolicies/default-deny"
            )
            if username == self.config.authorizer_writer:
                self._authorize_identity(user_info, self.config.authorizer_writer)
                await self._active_transition_holder()
                if marker is None and default_deny is None:
                    return
                raise NetworkBoundaryError(
                    "release bootstrap requires the transition fence with marker and default-deny absent"
                )
            if username == self.config.maintenance_writer:
                self._authorize_identity(user_info, self.config.maintenance_writer)
                await self._active_maintenance_holder()
                if marker is not None and default_deny is not None:
                    return
                raise NetworkBoundaryError(
                    "release maintenance requires the armed marker and live default-deny"
                )
            if username == self.config.transition_writer:
                self._authorize_identity(user_info, self.config.transition_writer)
                await self._active_transition_holder()
                if marker is not None and default_deny is None:
                    return
                raise NetworkBoundaryError(
                    "rollback release mutation requires the armed marker and absent default-deny"
                )
            raise NetworkBoundaryError(
                "control-plane release mutation requires an active maintenance or rollback identity"
            )
        holder = await self._active_transition_holder()
        if username in {self.config.authorizer_writer, self.config.transition_writer}:
            self._authorize_identity(user_info, username)
        if operation == "DELETE":
            if username == self.config.transition_writer:
                return
        target_annotations = _mapping(metadata.get("annotations"), "protected object annotations")
        allowed_writers = (
            {self.config.authorizer_writer, self.config.transition_writer}
            if marker is None
            else {self.config.transition_writer}
        )
        if username not in allowed_writers:
            raise NetworkBoundaryError("the protected mutation has the wrong authenticated writer")
        if operation != "DELETE" and target_annotations.get(TRANSITION_HOLDER_ANNOTATION) != holder:
            raise NetworkBoundaryError("the protected mutation is not bound to the random transition holder")

    async def review(self, document: Mapping[str, Any]) -> dict[str, Any]:
        request = _mapping(document.get("request"), "AdmissionReview.request")
        uid = request.get("uid")
        if not isinstance(uid, str) or not uid:
            raise NetworkBoundaryError("AdmissionReview request UID is missing")
        operation = request.get("operation")
        if operation not in {"CREATE", "UPDATE", "DELETE"}:
            raise NetworkBoundaryError("AdmissionReview operation is unsupported")
        resource = _mapping(request.get("resource"), "AdmissionReview resource")
        kind_value = _mapping(request.get("kind"), "AdmissionReview kind").get("kind")
        namespace = request.get("namespace", "")
        user_info = _mapping(request.get("userInfo"), "AdmissionReview userInfo")
        username = user_info.get("username")
        value = _mapping(request.get("object") or {}, "AdmissionReview object")
        old_value = _mapping(request.get("oldObject") or {}, "AdmissionReview oldObject")
        if not isinstance(kind_value, str) or not isinstance(namespace, str) or not isinstance(username, str):
            raise NetworkBoundaryError("AdmissionReview identity is malformed")
        profiled_kinds = {
            "CronJob",
            "DaemonSet",
            "Deployment",
            "Job",
            "JobSet",
            "Pod",
            "ReplicaSet",
            "ReplicationController",
            "StatefulSet",
        }
        if (
            operation in {"CREATE", "UPDATE"}
            and namespace == self.config.model_namespace
            and kind_value in profiled_kinds
        ):
            _workload_class, profile_name = _profile(value, kind_value)
            for index, pod_spec in enumerate(_pod_specs(value, kind_value)):
                _validate_pod_security(
                    pod_spec,
                    f"{kind_value} PodSpec[{index}]",
                    allow_ipc_lock=profile_name.startswith("mx-"),
                )
        if (
            operation in {"CREATE", "UPDATE"}
            and namespace == self.config.model_namespace
            and kind_value
            in {
                "Job",
                "Pod",
                "ReplicaSet",
            }
        ):
            await self._authorize_child(
                kind=kind_value,
                namespace=namespace,
                operation=operation,
                user_info=user_info,
                value=value,
                old_value=old_value,
            )
        if operation in {"CREATE", "UPDATE"} and namespace == self.config.model_namespace:
            if kind_value in {"Deployment", "StatefulSet", "DaemonSet"}:
                if username == self.config.model_controller_writer:
                    self._authorize_identity(user_info, self.config.model_controller_writer)
                elif username != self.config.transition_writer:
                    raise NetworkBoundaryError(
                        "a profiled runtime parent requires the exact model-controller writer"
                    )
            elif kind_value == "JobSet":
                if username == self.config.direct_job_writer:
                    self._authorize_identity(user_info, self.config.direct_job_writer)
                elif username != self.config.transition_writer:
                    raise NetworkBoundaryError(
                        "a scientific JobSet requires the exact scientific writer"
                    )
            elif kind_value in {"CronJob", "ReplicationController"} and username != self.config.transition_writer:
                raise NetworkBoundaryError(
                    "a profiled compatibility parent requires the exact transition writer"
                )
        if (
            operation in {"CREATE", "UPDATE"}
            and namespace == self.config.model_namespace
            and username == self.config.transition_writer
            and kind_value
            in {
                "CronJob",
                "DaemonSet",
                "Deployment",
                "Job",
                "JobSet",
                "Pod",
                "ReplicaSet",
                "ReplicationController",
                "StatefulSet",
            }
        ):
            self._authorize_identity(user_info, self.config.transition_writer)
            await self._active_transition_holder()
        await self._authorize_transition(
            user_info=user_info,
            operation=operation,
            resource=resource,
            namespace=namespace,
            value=value,
            old_value=old_value,
        )
        return {
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {"uid": uid, "allowed": True},
        }

    async def ready(self) -> None:
        """Prove the projected reader can reach both retained authority Leases."""

        for name in (TRANSITION_LEASE, MAINTENANCE_LEASE):
            await self.reader.get(
                "apis/coordination.k8s.io/v1/namespaces/"
                f"{quote(self.config.system_namespace, safe='')}/leases/{name}"
            )


def create_network_boundary_app(
    admission: NetworkBoundaryAdmission,
    *,
    readiness_files: tuple[Path, ...] = (),
    reload_tls_files: tuple[Path, Path] | None = None,
) -> FastAPI:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    loaded_certificate_sha256 = (
        hashlib.sha256(reload_tls_files[0].read_bytes()).hexdigest()
        if reload_tls_files is not None
        else ""
    )

    def reload_tls() -> tuple[str, str | None]:
        """Hot-load projected TLS files into Uvicorn's live SSLContext.

        A projection race leaves the last valid certificate serving. Readiness
        reports the loaded digest so the external receipt gate can refuse a
        transition until every ready endpoint has converged, without killing
        both replicas together.
        """

        nonlocal loaded_certificate_sha256
        if reload_tls_files is None:
            return loaded_certificate_sha256, None
        certificate, key = reload_tls_files
        desired = hashlib.sha256(certificate.read_bytes()).hexdigest()
        if desired == loaded_certificate_sha256:
            return desired, None
        context = getattr(app.state, "tls_context", None)
        if not isinstance(context, ssl.SSLContext):
            return loaded_certificate_sha256, "live TLS context is unavailable"
        try:
            context.load_cert_chain(str(certificate), str(key))
        except (OSError, ssl.SSLError) as exc:
            return loaded_certificate_sha256, f"projected TLS reload is pending: {exc}"
        loaded_certificate_sha256 = desired
        return desired, None

    @app.get("/livez")
    async def livez() -> JSONResponse:
        # Certificate rotation never drives liveness. Killing both replicas on
        # the same projected Secret update would defeat the admission boundary.
        return JSONResponse({"status": "ok"})

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        try:
            for path in readiness_files:
                if not path.is_file() or path.stat().st_size < 1:
                    raise NetworkBoundaryError("a projected certificate or token is unavailable")
            await admission.ready()
            digest, warning = reload_tls()
        except (NetworkBoundaryError, OSError, httpx.HTTPError) as exc:
            return JSONResponse({"status": "not-ready", "detail": str(exc)}, status_code=503)
        return JSONResponse(
            {
                "status": "ok" if warning is None else "serving-last-valid-certificate",
                "serving_certificate_sha256": digest,
                **({} if warning is None else {"detail": warning}),
            }
        )

    @app.post("/validate")
    async def validate(request: Request) -> JSONResponse:
        body = await request.json()
        uid = ""
        if isinstance(body, Mapping) and isinstance(body.get("request"), Mapping):
            raw_uid = body["request"].get("uid")
            uid = raw_uid if isinstance(raw_uid, str) else ""
        try:
            response = await admission.review(_mapping(body, "AdmissionReview"))
        except (NetworkBoundaryError, ValueError, httpx.HTTPError) as exc:
            response = {
                "apiVersion": "admission.k8s.io/v1",
                "kind": "AdmissionReview",
                "response": {
                    "uid": uid,
                    "allowed": False,
                    "status": {"code": 403, "message": str(exc)},
                },
            }
        return JSONResponse(response)

    return app


__all__ = [
    "KubernetesBoundaryReader",
    "NetworkBoundaryAdmission",
    "NetworkBoundaryConfig",
    "NetworkBoundaryError",
    "ReleaseInventoryEntry",
    "create_network_boundary_app",
]
