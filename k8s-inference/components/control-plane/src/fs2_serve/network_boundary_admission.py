"""Fail-closed admission boundary for model-network privileged objects.

The generic workload-profile CEL policies validate object shape.  This service
supplies the two checks CEL cannot perform safely: dereferencing a controller
owner to its live UID/profile and binding a privileged mutation to the random
holder of the retained transition Lease.  It runs under a read-only service
account that is distinct from model, scientific, acquisition, Helm and
Terraform writers.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

PROFILE_LABEL = "fs2-serve.nebius.ai/network-profile"
CLASS_LABEL = "fs2-serve.nebius.ai/network-workload-class"
TRANSITION_WRITER_ANNOTATION = "fs2-serve.nebius.ai/network-transition-writer"
TRANSITION_HOLDER_ANNOTATION = "fs2-serve.nebius.ai/network-transition-holder"
TRANSITION_LEASE = "fs2-model-network-transition"
BOUNDARY_MARKER = "fs2-runtime-network-policy-boundary-v2"
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
class NetworkBoundaryConfig:
    model_namespace: str
    system_namespace: str
    acquisition_writer: str
    direct_job_writer: str
    jobset_writer: str
    authorizer_writer: str
    transition_writer: str
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


class NetworkBoundaryAdmission:
    def __init__(self, *, config: NetworkBoundaryConfig, reader: KubernetesBoundaryReader) -> None:
        self.config = config
        self.reader = reader

    async def _authorize_child(
        self,
        *,
        kind: str,
        namespace: str,
        operation: str,
        username: str,
        value: Mapping[str, Any],
        old_value: Mapping[str, Any],
    ) -> None:
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
            if operation == "CREATE" and username != expected_writer:
                raise NetworkBoundaryError("a direct Job requires its exact platform writer")
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
        if operation == "CREATE" and username not in parent.writers:
            raise NetworkBoundaryError("the child request did not come from its exact Kubernetes controller")
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

    def _is_protected(self, resource: Mapping[str, Any], namespace: str, name: str) -> bool:
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
        if group == "admissionregistration.k8s.io" and plural in {
            "validatingadmissionpolicies",
            "validatingadmissionpolicybindings",
        }:
            return name.startswith("fs2-model-network-")
        return False

    def _is_transition_lease(
        self,
        resource: Mapping[str, Any],
        namespace: str,
        name: str,
    ) -> bool:
        return (
            resource.get("group") == "coordination.k8s.io"
            and resource.get("resource") == "leases"
            and namespace == self.config.system_namespace
            and name == TRANSITION_LEASE
        )

    def _authorize_lease_mutation(
        self,
        *,
        username: str,
        operation: str,
        value: Mapping[str, Any],
        old_value: Mapping[str, Any],
    ) -> None:
        if username != self.config.transition_writer:
            raise NetworkBoundaryError("only the dedicated transition principal may mutate the Lease")
        if operation == "DELETE":
            raise NetworkBoundaryError("the retained transition Lease cannot be deleted")
        new_metadata = _mapping(value.get("metadata"), "transition Lease.metadata")
        new_annotations = _mapping(new_metadata.get("annotations"), "transition Lease.annotations")
        new_spec = _mapping(value.get("spec"), "transition Lease.spec")
        new_holder = new_spec.get("holderIdentity", "")
        if not isinstance(new_holder, str):
            raise NetworkBoundaryError("transition Lease holder is malformed")
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
            raise NetworkBoundaryError("an active transition holder cannot be replaced")
        if new_holder and (
            HOLDER_PATTERN.fullmatch(new_holder) is None
            or new_annotations.get(TRANSITION_HOLDER_ANNOTATION) != new_holder
        ):
            raise NetworkBoundaryError("transition Lease update lacks its random holder token")
        if not old_holder and not new_holder:
            raise NetworkBoundaryError("an idle transition Lease update is not authorized")

    async def _authorize_transition(
        self,
        *,
        username: str,
        operation: str,
        resource: Mapping[str, Any],
        namespace: str,
        value: Mapping[str, Any],
        old_value: Mapping[str, Any],
    ) -> None:
        metadata = _mapping((old_value if operation == "DELETE" else value).get("metadata"), "target.metadata")
        name = metadata.get("name")
        if not isinstance(name, str):
            raise NetworkBoundaryError("protected object has no name")
        if self._is_transition_lease(resource, namespace, name):
            self._authorize_lease_mutation(
                username=username,
                operation=operation,
                value=value,
                old_value=old_value,
            )
            return
        if not self._is_protected(resource, namespace, name):
            return
        marker = await self.reader.get_optional(
            f"api/v1/namespaces/{quote(self.config.model_namespace, safe='')}/configmaps/{BOUNDARY_MARKER}"
        )
        lease = await self.reader.get(
            "apis/coordination.k8s.io/v1/namespaces/"
            f"{quote(self.config.system_namespace, safe='')}/leases/{TRANSITION_LEASE}"
        )
        lease_metadata = _mapping(lease.get("metadata"), "transition Lease.metadata")
        lease_annotations = _mapping(lease_metadata.get("annotations"), "transition Lease.annotations")
        lease_spec = _mapping(lease.get("spec"), "transition Lease.spec")
        holder = lease_spec.get("holderIdentity")
        writer = lease_annotations.get(TRANSITION_WRITER_ANNOTATION)
        target_annotations = _mapping(metadata.get("annotations"), "protected object annotations")
        if not isinstance(holder, str) or not holder:
            raise NetworkBoundaryError("the protected mutation has no active transition holder")
        if HOLDER_PATTERN.fullmatch(holder) is None:
            raise NetworkBoundaryError("the protected mutation has a malformed transition holder")
        if writer != self.config.transition_writer:
            raise NetworkBoundaryError("the transition Lease has the wrong authenticated writer")
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
                username=username,
                value=value,
                old_value=old_value,
            )
        await self._authorize_transition(
            username=username,
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


def create_network_boundary_app(admission: NetworkBoundaryAdmission) -> FastAPI:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.get("/livez")
    async def livez() -> dict[str, str]:
        return {"status": "ok"}

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
    "create_network_boundary_app",
]
