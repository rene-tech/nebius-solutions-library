#!/usr/bin/env python3
"""Create non-secret receipts for the phased fs2-models NetworkPolicy rollout.

The tool never changes Kubernetes. It reads a Terraform transition contract and
the live API, then emits a content-addressed receipt only when every Pod-producing
workload and every extant Pod has a recognized finite profile, all controller
rollouts are converged, the admission fence is live, and the running
model-controller identity matches the release. Rollback receipts prove
default-deny is absent while the finite profiles remain.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

PROFILE_LABEL = "fs2-serve.nebius.ai/network-profile"
REFERENCE_DATA_HOST_PATH = "/mnt/fs2-reference-data/data"
WORKLOAD_CLASS_LABEL = "fs2-serve.nebius.ai/network-workload-class"
COMPONENT_LABEL = "app.kubernetes.io/component"
PART_OF_LABEL = "app.kubernetes.io/part-of"
NAMESPACE = "fs2-models"
SYSTEM_NAMESPACE = "fs2-system"
INVENTORY_SCHEMA = "fs2-serve.nebius.ai/model-runtime-network-inventory/v7"
DENY_ABSENT_SCHEMA = "fs2-serve.nebius.ai/model-runtime-network-deny-absent/v2"
HOLDER_PATTERN = re.compile(r"^[a-z][a-z0-9]{5,11}:[1-9][0-9]*:[a-f0-9]{32}$")
WORKLOAD_RESOURCES = {
    "deployments": ("apps/v1", "Deployment", "deployments.apps"),
    "statefulsets": ("apps/v1", "StatefulSet", "statefulsets.apps"),
    "daemonsets": ("apps/v1", "DaemonSet", "daemonsets.apps"),
    "replicasets": ("apps/v1", "ReplicaSet", "replicasets.apps"),
    "replicationcontrollers": (
        "v1",
        "ReplicationController",
        "replicationcontrollers",
    ),
    "jobs": ("batch/v1", "Job", "jobs.batch"),
    "cronjobs": ("batch/v1", "CronJob", "cronjobs.batch"),
    "jobsets": ("jobset.x-k8s.io/v1alpha2", "JobSet", "jobsets.jobset.x-k8s.io"),
}


class ReceiptError(ValueError):
    """The live inventory cannot authorize the requested transition."""


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReceiptError(f"{field} must be a JSON object")
    return value


def _strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ReceiptError(f"{field} must be a non-empty-string JSON array")
    if value != sorted(set(value)):
        raise ReceiptError(f"{field} must be sorted and duplicate-free")
    return value


def _sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _finite_release_match_conditions(
    entries: list[dict[str, Any]],
) -> list[dict[str, str]]:
    if not entries or not all(isinstance(entry, dict) for entry in entries):
        raise ReceiptError("the finite signed release inventory is malformed")
    identities = sorted(
        {
            "|".join(
                (
                    str(entry.get("group", "")),
                    str(entry.get("resource", "")),
                    str(entry.get("namespace", "")),
                    str(entry.get("name", "")),
                    str(operation),
                )
            )
            for entry in entries
            for operation in entry.get("operations", [])
        }
    )
    return [
        {
            "name": "finite-signed-release-inventory",
            "expression": (
                json.dumps(identities, separators=(",", ":"))
                + ".exists(identity, identity == request.resource.group + '|' + "
                "request.resource.resource + '|' + request.namespace + '|' + "
                "request.name + '|' + request.operation)"
            ),
        }
    ]


def _load(path: Path) -> dict[str, Any]:
    try:
        return _object(json.loads(path.read_text(encoding="utf-8")), str(path))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"cannot read JSON from {path}: {exc}") from exc


def _load_env(name: str) -> dict[str, Any]:
    value = os.environ.get(name)
    if value is None:
        raise ReceiptError(f"environment variable {name} is missing")
    try:
        return _object(json.loads(value), name)
    except json.JSONDecodeError as exc:
        raise ReceiptError(f"environment variable {name} is not valid JSON") from exc


def _captured_at(value: str | None) -> str:
    if value is None:
        return (
            datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReceiptError("--captured-at must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ReceiptError("--captured-at must include a timezone")
    return value


def _kubectl_json(
    *,
    kubectl: str,
    kubeconfig: Path,
    context: str,
    resource: str,
    namespace: str | None = None,
    selector: str | None = None,
    optional_api: bool = False,
) -> dict[str, Any]:
    command = [
        kubectl,
        "--kubeconfig",
        str(kubeconfig),
        "--context",
        context,
        "get",
        resource,
    ]
    if namespace is not None:
        command.extend(["--namespace", namespace])
    if selector is not None:
        command.extend(["--selector", selector])
    command.extend(["--output", "json"])
    result = subprocess.run(  # noqa: S603 - operator-selected kubectl with fixed arguments
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "kubectl returned no diagnostic"
        absent = (
            "the server doesn't have a resource type" in detail.lower()
            or "could not find the requested resource" in detail.lower()
        )
        if optional_api and absent:
            return {"api_available": False, "items": []}
        raise ReceiptError(f"kubectl inventory failed for {resource}: {detail}")
    try:
        return {
            "api_available": True,
            **_object(json.loads(result.stdout), f"kubectl get {resource}"),
        }
    except json.JSONDecodeError as exc:
        raise ReceiptError(f"kubectl get {resource} returned invalid JSON") from exc


def _contract(contract: dict[str, Any], *, phases: set[str]) -> dict[str, Any]:
    if contract.get("phase") not in phases:
        raise ReceiptError(f"transition contract phase must be one of {sorted(phases)}")
    if contract.get("namespace") != NAMESPACE:
        raise ReceiptError(f"transition contract namespace must be {NAMESPACE}")
    profiles = _strings(contract.get("profiles"), "contract.profiles")
    serving_profiles = _strings(
        contract.get("serving_profiles"), "contract.serving_profiles"
    )
    if not set(serving_profiles).issubset(profiles):
        raise ReceiptError("transition serving profiles are outside its finite catalog")
    policy_names = _strings(
        contract.get("allow_policy_names"), "contract.allow_policy_names"
    )
    expected_profile_digest = hashlib.sha256(
        json.dumps(profiles, separators=(",", ":")).encode()
    ).hexdigest()
    if contract.get("profiles_sha256") != expected_profile_digest:
        raise ReceiptError("transition contract profile digest is inconsistent")
    if policy_names != [f"fs2-runtime-profile-{profile}" for profile in profiles]:
        raise ReceiptError("transition contract policy names do not match its profiles")
    admission_policies = _strings(
        contract.get("admission_policy_names"), "contract.admission_policy_names"
    )
    admission_bindings = _strings(
        contract.get("admission_binding_names"), "contract.admission_binding_names"
    )
    policy_hashes = _object(
        contract.get("admission_policy_spec_sha256"),
        "contract.admission_policy_spec_sha256",
    )
    binding_hashes = _object(
        contract.get("admission_binding_spec_sha256"),
        "contract.admission_binding_spec_sha256",
    )
    if (
        sorted(policy_hashes) != admission_policies
        or sorted(binding_hashes) != admission_bindings
    ):
        raise ReceiptError("transition admission spec hashes do not cover exact names")
    if not all(
        isinstance(value, str) and len(value) == 64
        for value in [*policy_hashes.values(), *binding_hashes.values()]
    ):
        raise ReceiptError("transition admission spec hashes are invalid")
    cluster_id = contract.get("cluster_id")
    if not isinstance(cluster_id, str) or not cluster_id:
        raise ReceiptError("transition contract cluster_id is missing")
    controller_name = contract.get("controller_deployment_name")
    if not isinstance(controller_name, str) or not controller_name:
        raise ReceiptError("transition contract controller Deployment name is missing")
    if contract.get("transition_lock_name") != "fs2-model-network-transition":
        raise ReceiptError("transition contract Lease name is invalid")
    if contract.get("transition_lock_namespace") != SYSTEM_NAMESPACE:
        raise ReceiptError("transition contract Lease namespace is invalid")
    if contract.get("boundary_webhook_name") != "fs2-model-network-boundary":
        raise ReceiptError("transition contract boundary webhook name is invalid")
    transition_writer = contract.get("transition_writer_username")
    if transition_writer != "fs2-model-network-transition":
        raise ReceiptError("transition contract authenticated writer is not exact")
    authority = _object(
        contract.get("boundary_authority"), "contract.boundary_authority"
    )
    if (
        authority.get("schema")
        != "fs2-serve.nebius.ai/model-network-boundary-authority/v4"
        or authority.get("phase") != "armed"
        or authority.get("cluster_id") != cluster_id
        or authority.get("authority_namespace") != "fs2-network-security"
    ):
        raise ReceiptError(
            "transition contract has no exact external boundary authority"
        )
    authority_payload = {
        key: value for key, value in authority.items() if key != "payload_sha256"
    }
    if authority.get("payload_sha256") != _sha256(authority_payload):
        raise ReceiptError("external boundary authority payload digest is inconsistent")
    provider_root = contract.get("provider_trust_root_sha256")
    signature_verifier = contract.get("signature_verifier_sha256")
    external_custody = _object(
        authority.get("external_custody"), "contract.boundary_authority.external_custody"
    )
    if (
        not isinstance(provider_root, str)
        or re.fullmatch(r"[a-f0-9]{64}", provider_root) is None
        or external_custody.get("schema")
        != "fs2-serve.nebius.ai/model-network-boundary-provider-custody/v8"
        or external_custody.get("provider_trust_root_sha256") != provider_root
        or not isinstance(signature_verifier, str)
        or re.fullmatch(r"[a-f0-9]{64}", signature_verifier) is None
        or external_custody.get("signature_verifier_sha256")
        != signature_verifier
        or re.fullmatch(
            r"[a-f0-9]{64}",
            str(external_custody.get("client_tools_sha256", "")),
        )
        is None
        or re.fullmatch(
            r"[a-f0-9]{64}",
            str(external_custody.get("stable_policy_sha256", "")),
        )
        is None
        or re.fullmatch(
            r"[a-f0-9]{64}",
            str(external_custody.get("provider_inventory_sha256", "")),
        )
        is None
        or re.fullmatch(
            r"[a-f0-9]{64}",
            str(external_custody.get("kubernetes_authorization_sha256", "")),
        )
        is None
        or re.fullmatch(
            r"[a-f0-9]{64}",
            str(external_custody.get("provider_authority_census_sha256", "")),
        )
        is None
        or re.fullmatch(
            r"[a-f0-9]{64}",
            str(external_custody.get("credential_epochs_sha256", "")),
        )
        is None
        or re.fullmatch(
            r"[a-f0-9]{64}",
            str(external_custody.get("gateway_runtime_measurements_sha256", "")),
        )
        is None
        or not isinstance(external_custody.get("gateway_member_ids"), list)
        or not 2 <= len(external_custody["gateway_member_ids"]) <= 8
        or external_custody["gateway_member_ids"]
        != sorted(set(external_custody["gateway_member_ids"]))
        or re.fullmatch(
            r"[a-z][a-z0-9]{5,31}:[1-9][0-9]*:[a-f0-9]{32}",
            str(external_custody.get("freeze_transaction_id", "")),
        )
        is None
        or not isinstance(external_custody.get("freeze_expires_at"), str)
        or re.fullmatch(
            r"[a-f0-9]{64}",
            str(external_custody.get("frozen_resources_sha256", "")),
        )
        is None
    ):
        raise ReceiptError("external provider custody is not root-contract pinned")
    jobset_writer = _object(
        authority.get("jobset_writer"), "contract.boundary_authority.jobset_writer"
    )
    if jobset_writer.get("username") != contract.get("jobset_writer_username"):
        raise ReceiptError("live boundary JobSet writer differs from the workload contract")
    service = _object(
        authority.get("service"), "contract.boundary_authority.service"
    )
    ready_endpoints = service.get("ready_endpoints")
    if (
        not isinstance(ready_endpoints, list)
        or len(ready_endpoints) < 2
        or len(
            {
                endpoint.get("node_name")
                for endpoint in ready_endpoints
                if isinstance(endpoint, dict)
            }
        )
        != len(ready_endpoints)
        or any(
            re.fullmatch(r"[a-f0-9]{64}", str(service.get(field, ""))) is None
            for field in (
                "object_sha256",
                "endpoints_object_sha256",
                "serving_certificate_sha256",
            )
        )
        or not isinstance(service.get("endpoints_uid"), str)
        or not service["endpoints_uid"]
        or not isinstance(service.get("endpoints_resource_version"), str)
        or not service["endpoints_resource_version"]
    ):
        raise ReceiptError(
            "external boundary Service does not prove stable distinct-node TLS-ready endpoints"
        )
    image = _object(contract.get("control_plane_image"), "contract.control_plane_image")
    if not isinstance(image.get("repository"), str) or not image["repository"]:
        raise ReceiptError("control-plane image repository is missing")
    digest = image.get("digest")
    if (
        not isinstance(digest, str)
        or len(digest) != 71
        or not digest.startswith("sha256:")
    ):
        raise ReceiptError("control-plane image digest is not immutable")
    return contract


def _items(value: dict[str, Any], field: str) -> list[dict[str, Any]]:
    items = value.get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ReceiptError(f"{field}.items must be a JSON object array")
    return items


def _labels(metadata: dict[str, Any], field: str) -> dict[str, Any]:
    return _object(metadata.get("labels"), f"{field}.labels")


def _profile(labels: dict[str, Any], recognized: set[str], field: str) -> str:
    if labels.get(PART_OF_LABEL) != "fs2-serve":
        raise ReceiptError(f"{field} is not owned by fs2-serve")
    profile = labels.get(PROFILE_LABEL)
    if not isinstance(profile, str) or profile not in recognized:
        raise ReceiptError(f"{field} has no recognized finite network profile")
    return profile


def _authorized_profile(
    contract: dict[str, Any],
    kind: str,
    metadata: dict[str, Any],
    field: str,
) -> tuple[str, str]:
    labels = _labels(metadata, field)
    profile = _profile(labels, set(contract["profiles"]), field)
    workload_class = labels.get(WORKLOAD_CLASS_LABEL)
    component = labels.get(COMPONENT_LABEL)
    job_kind = labels.get("fs2-serve.nebius.ai/job-kind")
    authorized = False
    if workload_class == "runtime":
        authorized = (
            kind
            in {
                "Deployment",
                "StatefulSet",
                "ReplicaSet",
                "ReplicationController",
                "Pod",
            }
            and component == "model-runtime"
            and profile in set(contract["serving_profiles"])
        )
    elif workload_class == "cache-keeper":
        authorized = (
            kind in {"DaemonSet", "Pod"}
            and component == "model-cache-keeper"
            and profile == "cache-resident-zero-egress-v1"
        )
    elif workload_class == "acceptance":
        authorized = (
            kind in {"Job", "Pod"}
            and component == "acceptance"
            and profile == "acceptance-zero-egress-v1"
        )
    elif workload_class == "internal-job":
        authorized = (
            kind in {"Job", "JobSet", "CronJob", "Pod"}
            and profile == "job-internal-v1"
            and (kind == "Pod" or job_kind in {"batch", "evaluation"})
        )
    elif workload_class == "cache-resident":
        authorized = (
            kind in {"Job", "Pod"}
            and profile == "cache-resident-zero-egress-v1"
            and (kind == "Pod" or job_kind == "cache")
        )
    elif workload_class == "public-acquisition":
        annotations = _object(metadata.get("annotations", {}), f"{field}.annotations")
        acquisition_plan = annotations.get(
            "fs2-serve.nebius.ai/acquisition-plan-sha256"
        )
        authorized = (
            kind in {"Job", "Pod"}
            and profile == "job-public-acquisition-v1"
            and labels.get("app.kubernetes.io/managed-by") == "fs2-serve-models"
            and isinstance(labels.get("fs2-serve.nebius.ai/model-id"), str)
            and bool(labels.get("fs2-serve.nebius.ai/model-id"))
            and isinstance(labels.get("fs2-serve.nebius.ai/operation-id"), str)
            and bool(labels.get("fs2-serve.nebius.ai/operation-id"))
            and labels.get("fs2-serve.nebius.ai/acquisition-authority")
            == "catalog-qualified-v1"
            and (kind == "Pod" or job_kind == "cache")
            and isinstance(acquisition_plan, str)
            and len(acquisition_plan) == 64
            and all(character in "0123456789abcdef" for character in acquisition_plan)
        )
    if not authorized:
        raise ReceiptError(
            f"{field} profile {profile!r} is not authorized for {kind} "
            f"workload class {workload_class!r}"
        )
    return profile, str(workload_class)


def _identity(metadata: dict[str, Any], field: str) -> tuple[str, str, int]:
    name = metadata.get("name")
    uid = metadata.get("uid")
    generation = metadata.get("generation", 0)
    if not isinstance(name, str) or not name:
        raise ReceiptError(f"{field} has no name")
    if not isinstance(uid, str) or not uid:
        raise ReceiptError(f"{field} {name} has no live UID")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 0
    ):
        raise ReceiptError(f"{field} {name} has an invalid generation")
    return name, uid, generation


def _replicas(status: dict[str, Any], field: str) -> int:
    value = status.get(field, 0)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ReceiptError(f"workload status {field} is invalid")
    return value


def _controller_converged(kind: str, item: dict[str, Any]) -> dict[str, int]:
    metadata = _object(item.get("metadata"), f"{kind}.metadata")
    spec = _object(item.get("spec"), f"{kind}.spec")
    status = _object(item.get("status", {}), f"{kind}.status")
    generation = metadata.get("generation", 0)
    if (
        kind
        in {
            "Deployment",
            "StatefulSet",
            "DaemonSet",
            "ReplicaSet",
            "ReplicationController",
        }
        and status.get("observedGeneration", 0) != generation
    ):
        raise ReceiptError(
            f"{kind} {metadata.get('name')} has not observed generation {generation}"
        )
    if kind == "Deployment":
        desired = spec.get("replicas", 1)
        values = {
            "desired": desired,
            "updated": _replicas(status, "updatedReplicas"),
            "ready": _replicas(status, "readyReplicas"),
            "available": _replicas(status, "availableReplicas"),
            "unavailable": _replicas(status, "unavailableReplicas"),
        }
        expected = {
            "desired": desired,
            "updated": desired,
            "ready": desired,
            "available": desired,
            "unavailable": 0,
        }
        if values != expected:
            raise ReceiptError(
                f"Deployment {metadata.get('name')} rollout is not converged"
            )
        return values
    if kind == "StatefulSet":
        desired = spec.get("replicas", 1)
        values = {
            "desired": desired,
            "updated": _replicas(status, "updatedReplicas"),
            "ready": _replicas(status, "readyReplicas"),
            "current": _replicas(status, "currentReplicas"),
        }
        if any(value != desired for value in values.values()) or status.get(
            "currentRevision"
        ) != status.get("updateRevision"):
            raise ReceiptError(
                f"StatefulSet {metadata.get('name')} rollout is not converged"
            )
        return values
    if kind == "DaemonSet":
        desired = _replicas(status, "desiredNumberScheduled")
        values = {
            "desired": desired,
            "updated": _replicas(status, "updatedNumberScheduled"),
            "ready": _replicas(status, "numberReady"),
            "available": _replicas(status, "numberAvailable"),
            "unavailable": _replicas(status, "numberUnavailable"),
        }
        expected = {
            "desired": desired,
            "updated": desired,
            "ready": desired,
            "available": desired,
            "unavailable": 0,
        }
        if values != expected:
            raise ReceiptError(
                f"DaemonSet {metadata.get('name')} rollout is not converged"
            )
        return values
    if kind in {"ReplicaSet", "ReplicationController"}:
        desired = spec.get("replicas", 1)
        values = {
            "desired": desired,
            "ready": _replicas(status, "readyReplicas"),
            "available": _replicas(status, "availableReplicas"),
        }
        if values != {"desired": desired, "ready": desired, "available": desired}:
            raise ReceiptError(
                f"{kind} {metadata.get('name')} rollout is not converged"
            )
        return values
    return {}


def _pod_template(item: dict[str, Any], kind: str) -> dict[str, Any]:
    spec = _object(item.get("spec"), f"{kind}.spec")
    if kind == "CronJob":
        job_template = _object(spec.get("jobTemplate"), "CronJob.spec.jobTemplate")
        job_spec = _object(job_template.get("spec"), "CronJob.spec.jobTemplate.spec")
        return _object(job_spec.get("template"), "CronJob PodTemplate")
    if kind != "JobSet":
        return _object(spec.get("template"), f"{kind}.spec.template")
    replicated_jobs = spec.get("replicatedJobs")
    if not isinstance(replicated_jobs, list) or not replicated_jobs:
        raise ReceiptError("JobSet has no replicatedJobs")
    templates = []
    for index, raw in enumerate(replicated_jobs):
        replicated = _object(raw, f"JobSet.replicatedJobs[{index}]")
        job_template = _object(
            replicated.get("template"),
            f"JobSet.replicatedJobs[{index}].template",
        )
        job_spec = _object(
            job_template.get("spec"),
            f"JobSet.replicatedJobs[{index}].template.spec",
        )
        templates.append(
            _object(
                job_spec.get("template"),
                f"JobSet.replicatedJobs[{index}].PodTemplate",
            )
        )
    first = templates[0]
    first_labels = _labels(
        _object(first.get("metadata"), "JobSet PodTemplate.metadata"),
        "JobSet PodTemplate",
    )
    if any(
        _labels(
            _object(template.get("metadata"), "JobSet PodTemplate.metadata"),
            "JobSet PodTemplate",
        )
        != first_labels
        for template in templates[1:]
    ):
        raise ReceiptError("JobSet replicated jobs do not share one network profile")
    return first


def _validate_pod_security(
    spec: dict[str, Any], label: str, *, allow_ipc_lock: bool
) -> None:
    if (
        any(
            spec.get(field, False) is not False
            for field in ("hostNetwork", "hostPID", "hostIPC")
        )
        or spec.get("shareProcessNamespace", False) is not False
        or spec.get("automountServiceAccountToken") is not False
    ):
        raise ReceiptError(f"{label} can reach host namespaces or a service-account token")
    volumes = spec.get("volumes", [])
    if not isinstance(volumes, list) or not all(
        isinstance(volume, dict) for volume in volumes
    ):
        raise ReceiptError(f"{label} has a malformed volume")
    if any(
        "secret" in volume
        or "projected" in volume
        or any(
            isinstance(source, dict) and "serviceAccountToken" in source
            for source in _object(
                volume.get("projected", {}), f"{label} projected volume"
            ).get("sources", [])
            if isinstance(source, dict)
        )
        for volume in volumes
    ):
        raise ReceiptError(
            f"{label} can mount a Secret or projected service-account token"
        )
    pod_security = spec.get("securityContext", {})
    if not isinstance(pod_security, dict):
        raise ReceiptError(f"{label} Pod securityContext is malformed")
    pod_non_root = pod_security.get("runAsNonRoot") is True
    pod_seccomp = pod_security.get("seccompProfile", {})
    pod_runtime_default = (
        isinstance(pod_seccomp, dict) and pod_seccomp.get("type") == "RuntimeDefault"
    )
    collections = (
        spec.get("containers"),
        spec.get("initContainers", []),
        spec.get("ephemeralContainers", []),
    )
    if not isinstance(collections[0], list) or not collections[0]:
        raise ReceiptError(f"{label} has no containers")
    containers: list[dict[str, Any]] = []
    for collection in collections:
        if not isinstance(collection, list):
            raise ReceiptError(f"{label} container collection is malformed")
        for container in collection:
            if not isinstance(container, dict):
                raise ReceiptError(f"{label} contains a malformed container")
            containers.append(container)
            security = container.get("securityContext")
            if not isinstance(security, dict):
                raise ReceiptError(f"{label} container securityContext is absent")
            capabilities = security.get("capabilities")
            container_seccomp = security.get("seccompProfile", {})
            ports = container.get("ports", [])
            if (
                security.get("privileged", False) is not False
                or security.get("allowPrivilegeEscalation") is not False
                or not (pod_non_root or security.get("runAsNonRoot") is True)
                or not isinstance(capabilities, dict)
                or set(capabilities) - {"add", "drop"}
                or capabilities.get("drop") != ["ALL"]
                or capabilities.get("add", [])
                not in ((None, [], ["IPC_LOCK"]) if allow_ipc_lock else (None, []))
                or security.get("procMount", "Default") != "Default"
                or not (
                    pod_runtime_default
                    or (
                        isinstance(container_seccomp, dict)
                        and container_seccomp.get("type") == "RuntimeDefault"
                    )
                )
                or not isinstance(ports, list)
                or container.get("volumeDevices") not in (None, [])
                or any(
                    not isinstance(port, dict)
                    or port.get("hostPort", 0) not in (None, 0)
                    for port in ports
                )
            ):
                raise ReceiptError(
                    f"{label} escapes the non-root/no-capabilities security envelope"
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
            if isinstance(mount, dict) and mount.get("name") == name
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
            raise ReceiptError(
                f"{label} has a hostPath outside the exact read-only reference plane"
            )


def _workload_inventory(
    contract: dict[str, Any],
    resources: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, bool]]:
    workloads: dict[str, Any] = {}
    availability: dict[str, bool] = {}
    for key, (api_version, kind, _resource) in WORKLOAD_RESOURCES.items():
        document = _object(resources.get(key), key)
        available = document.get("api_available", True)
        if not isinstance(available, bool):
            raise ReceiptError(f"{key}.api_available must be boolean")
        availability[f"{api_version}/{kind}"] = available
        if not available:
            if kind != "JobSet":
                raise ReceiptError(
                    f"required Kubernetes API {api_version}/{kind} is unavailable"
                )
            continue
        for item in _items(document, key):
            metadata = _object(item.get("metadata"), f"{kind}.metadata")
            name, uid, generation = _identity(metadata, kind)
            profile, workload_class = _authorized_profile(
                contract, kind, metadata, f"{kind} {name}"
            )
            template = _pod_template(item, kind)
            _validate_pod_security(
                _object(template.get("spec"), f"{kind} {name} PodTemplate.spec"),
                f"{kind} {name} PodTemplate",
                allow_ipc_lock=profile.startswith("mx-"),
            )
            template_metadata = _object(
                template.get("metadata"),
                f"{kind} {name} PodTemplate.metadata",
            )
            template_profile, template_class = _authorized_profile(
                contract,
                "Pod",
                template_metadata,
                f"{kind} {name} PodTemplate",
            )
            if profile != template_profile or workload_class != template_class:
                raise ReceiptError(
                    f"{kind} {name} workload and Pod template network identities differ"
                )
            if workload_class == "public-acquisition":
                template_spec = _object(
                    template.get("spec"),
                    f"{kind} {name} PodTemplate.spec",
                )
                if template_spec.get("serviceAccountName") != "cache-service-account":
                    raise ReceiptError(
                        f"{kind} {name} public acquisition has the wrong "
                        "service account"
                    )
                workload_annotations = _object(
                    metadata.get("annotations", {}),
                    f"{kind} {name}.metadata.annotations",
                )
                template_annotations = _object(
                    template_metadata.get("annotations", {}),
                    f"{kind} {name} PodTemplate.metadata.annotations",
                )
                if template_annotations.get(
                    "fs2-serve.nebius.ai/acquisition-plan-sha256"
                ) != workload_annotations.get(
                    "fs2-serve.nebius.ai/acquisition-plan-sha256"
                ):
                    raise ReceiptError(
                        f"{kind} {name} acquisition plan differs from its Pod template"
                    )
            if kind == "JobSet":
                replicated_jobs = _object(item.get("spec"), "JobSet.spec").get(
                    "replicatedJobs"
                )
                if not isinstance(replicated_jobs, list):
                    raise ReceiptError(f"JobSet {name} has no replicatedJobs")
                for index, raw in enumerate(replicated_jobs):
                    job_template = _object(
                        _object(raw, f"JobSet.replicatedJobs[{index}]").get("template"),
                        f"JobSet.replicatedJobs[{index}].template",
                    )
                    job_profile, job_class = _authorized_profile(
                        contract,
                        "Job",
                        _object(
                            job_template.get("metadata"),
                            f"JobSet.replicatedJobs[{index}].template.metadata",
                        ),
                        f"JobSet {name} replicated Job template {index}",
                    )
                    if job_profile != profile or job_class != workload_class:
                        raise ReceiptError(
                            f"JobSet {name} replicated Job and workload network "
                            "identities differ"
                        )
                    job_spec = _object(
                        job_template.get("spec"),
                        f"JobSet.replicatedJobs[{index}].template.spec",
                    )
                    pod_template = _object(
                        job_spec.get("template"),
                        f"JobSet.replicatedJobs[{index}].PodTemplate",
                    )
                    _validate_pod_security(
                        _object(
                            pod_template.get("spec"),
                            f"JobSet.replicatedJobs[{index}].PodTemplate.spec",
                        ),
                        f"JobSet {name} replicated PodTemplate {index}",
                        allow_ipc_lock=profile.startswith("mx-"),
                    )
            if kind == "CronJob":
                job_template = _object(
                    _object(item.get("spec"), "CronJob.spec").get("jobTemplate"),
                    "CronJob.spec.jobTemplate",
                )
                job_profile, job_class = _authorized_profile(
                    contract,
                    "Job",
                    _object(
                        job_template.get("metadata"),
                        "CronJob JobTemplate.metadata",
                    ),
                    f"CronJob {name} JobTemplate",
                )
                if job_profile != profile or job_class != workload_class:
                    raise ReceiptError(
                        f"CronJob {name} Job and Pod template network identities differ"
                    )
            identity = f"{api_version}/{kind}/{name}"
            if identity in workloads:
                raise ReceiptError(f"duplicate workload identity {identity}")
            workloads[identity] = {
                "uid": uid,
                "generation": generation,
                "profile": profile,
                "workload_class": workload_class,
                "rollout": _controller_converged(kind, item),
            }
    if not workloads:
        raise ReceiptError("live fs2-models workload inventory is empty")
    return dict(sorted(workloads.items())), dict(sorted(availability.items()))


def _pod_inventory(
    contract: dict[str, Any], resources: dict[str, Any]
) -> dict[str, Any]:
    pods: dict[str, Any] = {}
    for item in _items(resources, "pods"):
        metadata = _object(item.get("metadata"), "Pod.metadata")
        name, uid, _generation = _identity(metadata, "Pod")
        profile, workload_class = _authorized_profile(
            contract, "Pod", metadata, f"Pod {name}"
        )
        owners = metadata.get("ownerReferences")
        if not isinstance(owners, list):
            raise ReceiptError(
                f"Pod {name} is naked; every fs2-models Pod needs a controller owner"
            )
        controlling = [
            owner
            for owner in owners
            if isinstance(owner, dict)
            and owner.get("controller") is True
            and isinstance(owner.get("uid"), str)
            and owner["uid"]
        ]
        if len(controlling) != 1:
            raise ReceiptError(f"Pod {name} must have exactly one controller owner")
        status = _object(item.get("status", {}), f"Pod {name}.status")
        phase = status.get("phase")
        if phase not in {"Pending", "Running", "Succeeded", "Failed"}:
            raise ReceiptError(f"Pod {name} has an invalid phase")
        ready = any(
            isinstance(condition, dict)
            and condition.get("type") == "Ready"
            and condition.get("status") == "True"
            for condition in status.get("conditions", [])
        )
        owner_kind = controlling[0].get("kind")
        if phase == "Running" and not ready:
            raise ReceiptError(f"Pod {name} is Running but not Ready")
        if phase == "Pending" and owner_kind != "Job":
            raise ReceiptError(f"non-Job Pod {name} is still Pending")
        if workload_class == "public-acquisition":
            spec = _object(item.get("spec"), f"Pod {name}.spec")
            if spec.get("serviceAccountName") != "cache-service-account":
                raise ReceiptError(
                    f"Pod {name} public acquisition has the wrong service account"
                )
        _validate_pod_security(
            _object(item.get("spec"), f"Pod {name}.spec"),
            f"Pod {name}",
            allow_ipc_lock=profile.startswith("mx-"),
        )
        pods[name] = {
            "uid": uid,
            "profile": profile,
            "workload_class": workload_class,
            "owner_kind": owner_kind,
            "owner_uid": controlling[0]["uid"],
            "phase": phase,
            "ready": ready,
        }
    return dict(sorted(pods.items()))


def _verify_pod_owners(workloads: dict[str, Any], pods: dict[str, Any]) -> None:
    workload_uids = {
        entry["uid"]: identity.rsplit("/", 2)[1]
        for identity, entry in workloads.items()
    }
    unbound = sorted(
        name for name, pod in pods.items() if pod["owner_uid"] not in workload_uids
    )
    if unbound:
        raise ReceiptError(
            "Pods have owners outside the complete workload census: "
            + ", ".join(unbound)
        )
    mismatched = sorted(
        name
        for name, pod in pods.items()
        if workload_uids[pod["owner_uid"]] != pod["owner_kind"]
    )
    if mismatched:
        raise ReceiptError(
            "Pods disagree with their inventoried controller kind: "
            + ", ".join(mismatched)
        )


def _live_controller(
    contract: dict[str, Any],
    deployments: dict[str, Any],
    pods: dict[str, Any],
) -> dict[str, Any]:
    expected_name = contract["controller_deployment_name"]
    matches = [
        item
        for item in _items(deployments, "model-controller deployments")
        if _object(
            item.get("metadata"),
            "model-controller Deployment.metadata",
        ).get("name")
        == expected_name
    ]
    if len(matches) != 1:
        raise ReceiptError(
            "live model-controller Deployment identity is missing or ambiguous"
        )
    deployment = matches[0]
    metadata = _object(
        deployment.get("metadata"),
        "model-controller Deployment.metadata",
    )
    name, uid, generation = _identity(metadata, "model-controller Deployment")
    rollout = _controller_converged("Deployment", deployment)
    template_spec = _object(
        _pod_template(deployment, "Deployment").get("spec"),
        "model-controller PodTemplate.spec",
    )
    containers = template_spec.get("containers")
    if not isinstance(containers, list):
        raise ReceiptError("model-controller Deployment has no containers")
    selected = [
        container
        for container in containers
        if isinstance(container, dict) and container.get("name") == "model-controller"
    ]
    if len(selected) != 1:
        raise ReceiptError(
            "model-controller container identity is missing or ambiguous"
        )
    image = selected[0].get("image")
    if not isinstance(image, str) or "@sha256:" not in image:
        raise ReceiptError("live model-controller image is not digest pinned")
    pod_inventory: dict[str, Any] = {}
    for pod in _items(pods, "model-controller pods"):
        pod_metadata = _object(pod.get("metadata"), "model-controller Pod.metadata")
        pod_name, pod_uid, _generation = _identity(
            pod_metadata,
            "model-controller Pod",
        )
        status = _object(
            pod.get("status", {}),
            f"model-controller Pod {pod_name}.status",
        )
        ready = any(
            isinstance(condition, dict)
            and condition.get("type") == "Ready"
            and condition.get("status") == "True"
            for condition in status.get("conditions", [])
        )
        if status.get("phase") != "Running" or not ready:
            raise ReceiptError(
                f"model-controller Pod {pod_name} is not Running and Ready"
            )
        statuses = status.get("containerStatuses")
        if not isinstance(statuses, list):
            raise ReceiptError(
                f"model-controller Pod {pod_name} has no container status"
            )
        container_status = [
            value
            for value in statuses
            if isinstance(value, dict) and value.get("name") == "model-controller"
        ]
        if len(container_status) != 1:
            raise ReceiptError(
                f"model-controller Pod {pod_name} container status is ambiguous"
            )
        image_id = container_status[0].get("imageID")
        if not isinstance(image_id, str) or image.rsplit("@", 1)[1] not in image_id:
            raise ReceiptError(
                f"model-controller Pod {pod_name} imageID differs from its Deployment"
            )
        pod_inventory[pod_name] = {
            "uid": pod_uid,
            "image_id": image_id,
            "ready": True,
        }
    if not pod_inventory:
        raise ReceiptError("live model-controller has no Ready Pods")
    return {
        "deployment_name": name,
        "deployment_uid": uid,
        "generation": generation,
        "observed_generation": generation,
        "image": image,
        "rollout": rollout,
        "pods": dict(sorted(pod_inventory.items())),
    }


def _admission_state(
    contract: dict[str, Any],
    policy_resources: dict[str, Any],
    binding_resources: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    expected_policies = set(contract["admission_policy_names"])
    policies: dict[str, Any] = {}
    for item in _items(policy_resources, "admission policies"):
        metadata = _object(item.get("metadata"), "admission policy.metadata")
        name = metadata.get("name")
        if not isinstance(name, str) or name not in expected_policies:
            continue
        uid = metadata.get("uid")
        if not isinstance(uid, str) or not uid:
            raise ReceiptError(f"admission policy {name} has no UID")
        resource_version = metadata.get("resourceVersion")
        if not isinstance(resource_version, str) or not resource_version:
            raise ReceiptError(f"admission policy {name} has no resourceVersion")
        spec = _object(item.get("spec"), f"admission policy {name}.spec")
        spec_sha256 = _sha256(spec)
        if spec_sha256 != contract["admission_policy_spec_sha256"].get(name):
            raise ReceiptError(f"admission policy {name} spec differs from Terraform")
        policies[name] = {
            "uid": uid,
            "resource_version": resource_version,
            "spec_sha256": spec_sha256,
        }
    missing_policies = sorted(expected_policies - set(policies))
    if missing_policies:
        raise ReceiptError(
            "network-profile admission policies are missing: "
            + ", ".join(missing_policies)
        )

    expected_bindings = set(contract["admission_binding_names"])
    bindings: dict[str, Any] = {}
    for item in _items(binding_resources, "admission bindings"):
        metadata = _object(item.get("metadata"), "admission binding.metadata")
        name = metadata.get("name")
        uid = metadata.get("uid")
        if isinstance(name, str) and name in expected_bindings:
            if not isinstance(uid, str) or not uid:
                raise ReceiptError(f"admission binding {name} has no UID")
            resource_version = metadata.get("resourceVersion")
            if not isinstance(resource_version, str) or not resource_version:
                raise ReceiptError(f"admission binding {name} has no resourceVersion")
            spec = _object(item.get("spec"), f"admission binding {name}.spec")
            spec_sha256 = _sha256(spec)
            if spec_sha256 != contract["admission_binding_spec_sha256"].get(name):
                raise ReceiptError(
                    f"admission binding {name} spec differs from Terraform"
                )
            bindings[name] = {
                "uid": uid,
                "resource_version": resource_version,
                "spec_sha256": spec_sha256,
            }
    missing_bindings = sorted(expected_bindings - set(bindings))
    if missing_bindings:
        raise ReceiptError(
            "network-profile admission bindings are missing: "
            + ", ".join(missing_bindings)
        )
    return dict(sorted(policies.items())), dict(sorted(bindings.items()))


def _transition_lock(
    contract: dict[str, Any],
    resources: dict[str, Any],
    *,
    expected_holder: str | None,
) -> str:
    matches = []
    for item in _items(resources, "transition Leases"):
        metadata = _object(item.get("metadata"), "transition Lease.metadata")
        if (
            metadata.get("name") == contract["transition_lock_name"]
            and metadata.get("namespace") == contract["transition_lock_namespace"]
        ):
            matches.append(item)
    if len(matches) != 1:
        raise ReceiptError("model-network transition Lease is missing or ambiguous")
    metadata = _object(matches[0].get("metadata"), "transition Lease.metadata")
    uid = metadata.get("uid")
    if not isinstance(uid, str) or not uid:
        raise ReceiptError("model-network transition Lease has no UID")
    annotations = _object(
        metadata.get("annotations", {}), "transition Lease.metadata.annotations"
    )
    if (
        annotations.get("fs2-serve.nebius.ai/network-transition-writer")
        != contract["transition_writer_username"]
    ):
        raise ReceiptError(
            "model-network transition Lease authenticated writer differs from Terraform"
        )
    spec = _object(matches[0].get("spec", {}), "transition Lease.spec")
    holder = spec.get("holderIdentity", "")
    if not isinstance(holder, str):
        raise ReceiptError("model-network transition Lease holder is malformed")
    if expected_holder is None:
        if holder:
            raise ReceiptError(
                "model-network transition Lease is active; capture after it is released"
            )
        return uid
    if (
        not expected_holder
        or HOLDER_PATTERN.fullmatch(expected_holder) is None
        or holder != expected_holder
    ):
        raise ReceiptError(
            "model-network transition Lease holder differs from this apply"
        )
    if (
        annotations.get("fs2-serve.nebius.ai/network-transition-holder")
        != expected_holder
    ):
        raise ReceiptError(
            "model-network transition Lease is not bound to this apply's random holder"
        )
    duration = spec.get("leaseDurationSeconds")
    renew_time = spec.get("renewTime")
    if (
        not isinstance(duration, int)
        or isinstance(duration, bool)
        or duration <= 0
        or not isinstance(renew_time, str)
    ):
        raise ReceiptError("model-network transition Lease timing is malformed")
    try:
        renewed = datetime.fromisoformat(renew_time.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReceiptError(
            "model-network transition Lease renewTime is invalid"
        ) from exc
    if renewed.tzinfo is None or renewed + timedelta(seconds=duration) <= datetime.now(
        UTC
    ):
        raise ReceiptError("model-network transition Lease expired during verification")
    return uid


def _boundary_webhook_state(
    contract: dict[str, Any], resources: dict[str, Any]
) -> dict[str, str]:
    matches = []
    for item in _items(resources, "boundary admission webhooks"):
        metadata = _object(item.get("metadata"), "boundary webhook.metadata")
        if metadata.get("name") == contract["boundary_webhook_name"]:
            matches.append(item)
    if len(matches) != 1:
        raise ReceiptError(
            "independent model-network boundary webhook is missing or ambiguous"
        )
    webhook = matches[0]
    metadata = _object(webhook.get("metadata"), "boundary webhook.metadata")
    uid = metadata.get("uid")
    resource_version = metadata.get("resourceVersion")
    if not isinstance(uid, str) or not uid:
        raise ReceiptError("boundary webhook has no UID")
    if not isinstance(resource_version, str) or not resource_version:
        raise ReceiptError("boundary webhook has no resourceVersion")
    raw = webhook.get("webhooks")
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise ReceiptError("boundary webhook list is malformed")
    by_name = {item.get("name"): item for item in raw}
    expected_names = {
        "children.network.fs2.nebius.ai",
        "control-plane.network.fs2.nebius.ai",
        "helm.network.fs2.nebius.ai",
        "lease.network.fs2.nebius.ai",
        "marker.network.fs2.nebius.ai",
        "models.network.fs2.nebius.ai",
        "release-writers.network.fs2.nebius.ai",
        "release-identities.network.fs2.nebius.ai",
    }
    if set(by_name) != expected_names or len(raw) != len(expected_names):
        raise ReceiptError("boundary webhook does not contain the exact bounded hooks")
    common = {
        "admissionReviewVersions": ["v1"],
        "sideEffects": "None",
        "failurePolicy": "Fail",
        "matchPolicy": "Equivalent",
        "timeoutSeconds": 3,
    }
    expected_namespace_selectors = {
        "children.network.fs2.nebius.ai": {
            "matchLabels": {"kubernetes.io/metadata.name": NAMESPACE}
        },
        "models.network.fs2.nebius.ai": {
            "matchLabels": {"kubernetes.io/metadata.name": NAMESPACE}
        },
        "marker.network.fs2.nebius.ai": {
            "matchLabels": {"kubernetes.io/metadata.name": NAMESPACE}
        },
        "helm.network.fs2.nebius.ai": {
            "matchLabels": {"kubernetes.io/metadata.name": SYSTEM_NAMESPACE}
        },
        "control-plane.network.fs2.nebius.ai": {
            "matchLabels": {"kubernetes.io/metadata.name": SYSTEM_NAMESPACE}
        },
        "release-writers.network.fs2.nebius.ai": {
            "matchLabels": {"kubernetes.io/metadata.name": SYSTEM_NAMESPACE}
        },
        "release-identities.network.fs2.nebius.ai": {
            "matchLabels": {"kubernetes.io/metadata.name": SYSTEM_NAMESPACE}
        },
        "lease.network.fs2.nebius.ai": {
            "matchLabels": {"kubernetes.io/metadata.name": SYSTEM_NAMESPACE}
        },
    }
    expected_object_selectors = {
        "children.network.fs2.nebius.ai": {
            "matchExpressions": [
                {
                    "key": PROFILE_LABEL,
                    "operator": "Exists",
                }
            ]
        },
        "models.network.fs2.nebius.ai": {},
        "marker.network.fs2.nebius.ai": {},
        "helm.network.fs2.nebius.ai": {
            "matchLabels": {
                "name": "fs2-serve-control-plane",
                "owner": "helm",
            }
        },
        "control-plane.network.fs2.nebius.ai": {
            "matchLabels": {"app.kubernetes.io/instance": "fs2-serve-control-plane"}
        },
        "release-writers.network.fs2.nebius.ai": {},
        "release-identities.network.fs2.nebius.ai": {},
        "lease.network.fs2.nebius.ai": {
            "matchLabels": {"fs2-serve.nebius.ai/network-boundary-object": "true"}
        },
    }
    expected_conditions = {
        "children.network.fs2.nebius.ai": [],
        "models.network.fs2.nebius.ai": [],
        "marker.network.fs2.nebius.ai": [
            {
                "name": "exact-boundary-marker",
                "expression": "request.name == 'fs2-runtime-network-policy-boundary-v2'",
            }
        ],
        "helm.network.fs2.nebius.ai": [],
        "control-plane.network.fs2.nebius.ai": [],
        "release-writers.network.fs2.nebius.ai": _finite_release_match_conditions(
            contract["boundary_authority"]["release_inventory"]["entries"]
        ),
        "release-identities.network.fs2.nebius.ai": [
            {
                "name": "exact-release-identity",
                "expression": (
                    'request.userInfo.username in ["fs2-model-network-authorizer", '
                    '"fs2-model-network-maintenance", "fs2-model-network-transition"]'
                ),
            }
        ],
        "lease.network.fs2.nebius.ai": [
            {
                "name": "exact-boundary-lease",
                "expression": "request.name in ['fs2-model-network-transition', 'fs2-model-network-maintenance']",
            }
        ],
    }
    allowed_keys = {
        "name",
        "admissionReviewVersions",
        "sideEffects",
        "failurePolicy",
        "matchPolicy",
        "timeoutSeconds",
        "clientConfig",
        "namespaceSelector",
        "objectSelector",
        "matchConditions",
        "rules",
    }
    expected_rules = {
        "children.network.fs2.nebius.ai": [
            {
                "apiGroups": [""],
                "apiVersions": ["v1"],
                "operations": ["CREATE", "UPDATE"],
                "resources": ["pods", "replicationcontrollers"],
                "scope": "Namespaced",
            },
            {
                "apiGroups": ["apps"],
                "apiVersions": ["v1"],
                "operations": ["CREATE", "UPDATE"],
                "resources": [
                    "deployments",
                    "statefulsets",
                    "daemonsets",
                    "replicasets",
                ],
                "scope": "Namespaced",
            },
            {
                "apiGroups": ["batch"],
                "apiVersions": ["v1"],
                "operations": ["CREATE", "UPDATE"],
                "resources": ["jobs", "cronjobs"],
                "scope": "Namespaced",
            },
            {
                "apiGroups": ["jobset.x-k8s.io"],
                "apiVersions": ["v1alpha2"],
                "operations": ["CREATE", "UPDATE"],
                "resources": ["jobsets"],
                "scope": "Namespaced",
            },
        ],
        "models.network.fs2.nebius.ai": [
            {
                "apiGroups": ["networking.k8s.io"],
                "apiVersions": ["v1"],
                "operations": ["CREATE", "UPDATE", "DELETE"],
                "resources": ["networkpolicies"],
                "scope": "Namespaced",
            }
        ],
        "marker.network.fs2.nebius.ai": [
            {
                "apiGroups": [""],
                "apiVersions": ["v1"],
                "operations": ["CREATE", "UPDATE", "DELETE"],
                "resources": ["configmaps"],
                "scope": "Namespaced",
            },
        ],
        "helm.network.fs2.nebius.ai": [
            {
                "apiGroups": [""],
                "apiVersions": ["v1"],
                "operations": ["CREATE", "UPDATE", "DELETE"],
                "resources": ["configmaps", "secrets"],
                "scope": "Namespaced",
            }
        ],
        "control-plane.network.fs2.nebius.ai": [
            {
                "apiGroups": [""],
                "apiVersions": ["v1"],
                "operations": ["CREATE", "UPDATE", "DELETE"],
                "resources": [
                    "configmaps",
                    "secrets",
                    "serviceaccounts",
                    "services",
                    "persistentvolumeclaims",
                ],
                "scope": "Namespaced",
            },
            {
                "apiGroups": ["apps"],
                "apiVersions": ["v1"],
                "operations": ["CREATE", "UPDATE", "DELETE"],
                "resources": ["deployments", "statefulsets", "daemonsets"],
                "scope": "Namespaced",
            },
            {
                "apiGroups": ["batch"],
                "apiVersions": ["v1"],
                "operations": ["CREATE", "UPDATE", "DELETE"],
                "resources": ["jobs", "cronjobs"],
                "scope": "Namespaced",
            },
            {
                "apiGroups": ["autoscaling"],
                "apiVersions": ["v2"],
                "operations": ["CREATE", "UPDATE", "DELETE"],
                "resources": ["horizontalpodautoscalers"],
                "scope": "Namespaced",
            },
            {
                "apiGroups": ["networking.k8s.io"],
                "apiVersions": ["v1"],
                "operations": ["CREATE", "UPDATE", "DELETE"],
                "resources": ["ingresses", "networkpolicies"],
                "scope": "Namespaced",
            },
            {
                "apiGroups": ["policy"],
                "apiVersions": ["v1"],
                "operations": ["CREATE", "UPDATE", "DELETE"],
                "resources": ["poddisruptionbudgets"],
                "scope": "Namespaced",
            },
            {
                "apiGroups": ["rbac.authorization.k8s.io"],
                "apiVersions": ["v1"],
                "operations": ["CREATE", "UPDATE", "DELETE"],
                "resources": ["roles", "rolebindings"],
                "scope": "Namespaced",
            },
        ],
        "release-writers.network.fs2.nebius.ai": [
            {
                "apiGroups": ["*"],
                "apiVersions": ["*"],
                "operations": ["CREATE", "UPDATE", "DELETE"],
                "resources": ["*"],
                "scope": "Namespaced",
            }
        ],
        "release-identities.network.fs2.nebius.ai": [
            {
                "apiGroups": ["*"],
                "apiVersions": ["*"],
                "operations": ["CREATE", "UPDATE", "DELETE"],
                "resources": ["*"],
                "scope": "Namespaced",
            }
        ],
        "lease.network.fs2.nebius.ai": [
            {
                "apiGroups": ["coordination.k8s.io"],
                "apiVersions": ["v1"],
                "operations": ["CREATE", "UPDATE", "DELETE"],
                "resources": ["leases"],
                "scope": "Namespaced",
            }
        ],
    }
    ca_bundle_hashes: set[str] = set()
    for name in sorted(expected_names):
        item = by_name[name]
        if set(item) - allowed_keys:
            raise ReceiptError(
                f"boundary webhook {name} has unreviewed semantic fields"
            )
        if any(item.get(key) != value for key, value in common.items()):
            raise ReceiptError(f"boundary webhook {name} is not fail closed")
        if item.get("objectSelector", {}) != expected_object_selectors[name]:
            raise ReceiptError(f"boundary webhook {name} objectSelector is not exact")
        if item.get("namespaceSelector", {}) != expected_namespace_selectors[name]:
            raise ReceiptError(
                f"boundary webhook {name} namespaceSelector is not exact"
            )
        if item.get("matchConditions", []) != expected_conditions[name]:
            raise ReceiptError(f"boundary webhook {name} matchConditions are not exact")
        client = _object(
            item.get("clientConfig"), f"boundary webhook {name}.clientConfig"
        )
        if set(client) != {"caBundle", "service"}:
            raise ReceiptError(
                f"boundary webhook {name} clientConfig is not service-only"
            )
        ca_bundle = client.get("caBundle")
        if not isinstance(ca_bundle, str) or not ca_bundle:
            raise ReceiptError(f"boundary webhook {name} has no injected CA bundle")
        ca_bundle_hashes.add(_sha256({"caBundle": ca_bundle}))
        if client.get("service") != {
            "name": "fs2-model-network-boundary",
            "namespace": "fs2-network-security",
            "path": "/validate",
            "port": 443,
        }:
            raise ReceiptError(f"boundary webhook {name} service target is not exact")
        if item.get("rules") != expected_rules[name]:
            raise ReceiptError(f"boundary webhook {name} resource rules are not exact")
    expected_ca_hash = _object(
        contract["boundary_authority"].get("tls"),
        "contract.boundary_authority.tls",
    ).get("webhook_ca_bundle_sha256")
    if ca_bundle_hashes != {expected_ca_hash}:
        raise ReceiptError("live webhook CA bundles differ from signed live TLS custody")
    state = {
        "uid": uid,
        "resource_version": resource_version,
        "spec_sha256": _sha256({"webhooks": raw}),
    }
    expected_hash = contract["boundary_authority"]["webhook"]["spec_sha256"]
    if state["spec_sha256"] != expected_hash:
        raise ReceiptError("live boundary webhook differs from signed external custody")
    return state


def inventory_receipt(
    contract: dict[str, Any],
    resources: dict[str, dict[str, Any]],
    pods: dict[str, Any],
    controller_deployments: dict[str, Any],
    controller_pods: dict[str, Any],
    admission_policies: dict[str, Any],
    admission_bindings: dict[str, Any],
    boundary_webhooks: dict[str, Any],
    transition_leases: dict[str, Any],
    *,
    captured_at: str,
    expected_lock_holder: str | None = None,
) -> dict[str, Any]:
    contract = _contract(contract, phases={"inventory", "enforce", "maintenance"})
    workloads, resource_apis = _workload_inventory(contract, resources)
    pod_inventory = _pod_inventory(contract, pods)
    _verify_pod_owners(workloads, pod_inventory)
    policy_inventory, binding_inventory = _admission_state(
        contract,
        admission_policies,
        admission_bindings,
    )
    payload = {
        "schema": INVENTORY_SCHEMA,
        "cluster_id": contract["cluster_id"],
        "namespace": NAMESPACE,
        "captured_at": captured_at,
        "profiles_sha256": contract["profiles_sha256"],
        "resource_apis": resource_apis,
        "workloads": workloads,
        "pods": pod_inventory,
        "live_controller": _live_controller(
            contract,
            controller_deployments,
            controller_pods,
        ),
        "transition_lock_uid": _transition_lock(
            contract,
            transition_leases,
            expected_holder=expected_lock_holder,
        ),
        "admission_policies": policy_inventory,
        "admission_bindings": binding_inventory,
        "admission_webhook": _boundary_webhook_state(contract, boundary_webhooks),
        "boundary_authority_sha256": contract["boundary_authority"]["payload_sha256"],
        "boundary_service_sha256": contract["boundary_authority"]["service"]["object_sha256"],
        "boundary_endpoints_sha256": contract["boundary_authority"]["service"][
            "endpoints_object_sha256"
        ],
        "boundary_ready_endpoints_sha256": _sha256(
            {
                "ready_endpoints": contract["boundary_authority"]["service"][
                    "ready_endpoints"
                ]
            }
        ),
        "boundary_serving_certificate_sha256": contract["boundary_authority"]["service"][
            "serving_certificate_sha256"
        ],
        "provider_custody_attestation_sha256": contract["boundary_authority"][
            "external_custody"
        ]["attestation_sha256"],
        "jobset_writer_username": contract["jobset_writer_username"],
    }
    return {**payload, "payload_sha256": _sha256(payload)}


def verify_enforce(
    contract: dict[str, Any],
    receipt: dict[str, Any],
    resources: dict[str, dict[str, Any]],
    pods: dict[str, Any],
    controller_deployments: dict[str, Any],
    controller_pods: dict[str, Any],
    admission_policies: dict[str, Any],
    admission_bindings: dict[str, Any],
    boundary_webhooks: dict[str, Any],
    transition_leases: dict[str, Any],
) -> dict[str, Any]:
    _contract(contract, phases={"enforce"})
    captured_at = receipt.get("captured_at")
    if not isinstance(captured_at, str):
        raise ReceiptError("inventory receipt captured_at is missing")
    lock_identity = os.environ.get("FS2_NETWORK_TRANSITION_LOCK_IDENTITY")
    if not lock_identity:
        raise ReceiptError("apply-time model-network transition lock is missing")
    observed = inventory_receipt(
        contract,
        resources,
        pods,
        controller_deployments,
        controller_pods,
        admission_policies,
        admission_bindings,
        boundary_webhooks,
        transition_leases,
        captured_at=captured_at,
        expected_lock_holder=lock_identity,
    )
    if observed != receipt:
        raise ReceiptError(
            "apply-time workload, Pod, controller, or admission inventory differs "
            "from the supplied receipt; refresh the receipt and retry"
        )
    desired_image = (
        f"{contract['control_plane_image']['repository']}@"
        f"{contract['control_plane_image']['digest']}"
    )
    if receipt["live_controller"]["image"] != desired_image:
        raise ReceiptError(
            "live model-controller image differs from the exact Terraform release image"
        )
    return {
        "status": "verified",
        "receipt_sha256": receipt["payload_sha256"],
        "live_controller": receipt["live_controller"],
    }


def deny_absent_receipt(
    contract: dict[str, Any], resources: dict[str, Any], *, captured_at: str
) -> dict[str, Any]:
    contract = _contract(contract, phases={"rollback-remove-deny"})
    enforcement_digest = contract.get("inventory_receipt_sha256")
    if not isinstance(enforcement_digest, str) or len(enforcement_digest) != 64:
        raise ReceiptError("rollback contract has no enforcement receipt digest")
    items = resources.get("items")
    if not isinstance(items, list):
        raise ReceiptError("live NetworkPolicy inventory has no items array")
    names: list[str] = []
    for raw in items:
        metadata = _object(_object(raw, "network policy").get("metadata"), "metadata")
        name = metadata.get("name")
        if not isinstance(name, str) or not name:
            raise ReceiptError("live NetworkPolicy has no name")
        names.append(name)
    if "default-deny" in names:
        raise ReceiptError("default-deny is still present; apply deny removal first")
    missing = sorted(set(contract["allow_policy_names"]) - set(names))
    if missing:
        raise ReceiptError(f"finite allow policies are missing: {', '.join(missing)}")
    payload = {
        "schema": DENY_ABSENT_SCHEMA,
        "cluster_id": contract.get("cluster_id"),
        "namespace": NAMESPACE,
        "captured_at": captured_at,
        "enforcement_payload_sha256": enforcement_digest,
        "profiles_sha256": contract["profiles_sha256"],
        "allow_policy_names": contract["allow_policy_names"],
        "default_deny_absent": True,
    }
    if not isinstance(payload["cluster_id"], str) or not payload["cluster_id"]:
        raise ReceiptError("transition contract cluster_id is missing")
    return {**payload, "payload_sha256": _sha256(payload)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("inventory", "verify-enforce", "deny-absent"):
        child = subparsers.add_parser(command)
        contract = child.add_mutually_exclusive_group(required=True)
        contract.add_argument("--contract", type=Path)
        contract.add_argument("--contract-json-env")
        if command == "verify-enforce":
            receipt = child.add_mutually_exclusive_group(required=True)
            receipt.add_argument("--receipt", type=Path)
            receipt.add_argument("--receipt-json-env")
        child.add_argument("--kubeconfig", required=True, type=Path)
        child.add_argument("--context", required=True)
        child.add_argument("--kubectl", default="kubectl")
        child.add_argument("--captured-at")
    return parser


def _document(path: Path | None, environment: str | None) -> dict[str, Any]:
    if path is not None:
        return _load(path)
    if environment is None:
        raise ReceiptError("a JSON file or environment variable is required")
    return _load_env(environment)


def _collect_inventory(
    args: argparse.Namespace,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    resources = {
        key: _kubectl_json(
            kubectl=args.kubectl,
            kubeconfig=args.kubeconfig,
            context=args.context,
            namespace=NAMESPACE,
            resource=resource,
            optional_api=key == "jobsets",
        )
        for key, (_api, _kind, resource) in WORKLOAD_RESOURCES.items()
    }
    pods = _kubectl_json(
        kubectl=args.kubectl,
        kubeconfig=args.kubeconfig,
        context=args.context,
        namespace=NAMESPACE,
        resource="pods",
    )
    controller_deployments = _kubectl_json(
        kubectl=args.kubectl,
        kubeconfig=args.kubeconfig,
        context=args.context,
        namespace=SYSTEM_NAMESPACE,
        resource="deployments.apps",
        selector=f"{COMPONENT_LABEL}=model-controller",
    )
    controller_pods = _kubectl_json(
        kubectl=args.kubectl,
        kubeconfig=args.kubeconfig,
        context=args.context,
        namespace=SYSTEM_NAMESPACE,
        resource="pods",
        selector=f"{COMPONENT_LABEL}=model-controller",
    )
    admission_policies = _kubectl_json(
        kubectl=args.kubectl,
        kubeconfig=args.kubeconfig,
        context=args.context,
        resource="validatingadmissionpolicies.admissionregistration.k8s.io",
    )
    admission_bindings = _kubectl_json(
        kubectl=args.kubectl,
        kubeconfig=args.kubeconfig,
        context=args.context,
        resource="validatingadmissionpolicybindings.admissionregistration.k8s.io",
    )
    boundary_webhooks = _kubectl_json(
        kubectl=args.kubectl,
        kubeconfig=args.kubeconfig,
        context=args.context,
        resource="validatingwebhookconfigurations.admissionregistration.k8s.io",
    )
    transition_leases = _kubectl_json(
        kubectl=args.kubectl,
        kubeconfig=args.kubeconfig,
        context=args.context,
        namespace=SYSTEM_NAMESPACE,
        resource="leases.coordination.k8s.io",
    )
    return (
        resources,
        pods,
        controller_deployments,
        controller_pods,
        admission_policies,
        admission_bindings,
        boundary_webhooks,
        transition_leases,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        contract = _document(args.contract, args.contract_json_env)
        captured_at = _captured_at(args.captured_at)
        if args.command == "inventory":
            inventory = _collect_inventory(args)
            receipt = inventory_receipt(
                contract,
                *inventory,
                captured_at=captured_at,
            )
        elif args.command == "verify-enforce":
            supplied = _document(args.receipt, args.receipt_json_env)
            inventory = _collect_inventory(args)
            receipt = verify_enforce(contract, supplied, *inventory)
        else:
            resources = _kubectl_json(
                kubectl=args.kubectl,
                kubeconfig=args.kubeconfig,
                context=args.context,
                resource="networkpolicies.networking.k8s.io",
                namespace=NAMESPACE,
            )
            receipt = deny_absent_receipt(contract, resources, captured_at=captured_at)
    except ReceiptError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
