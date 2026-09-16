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
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROFILE_LABEL = "fs2-serve.nebius.ai/network-profile"
COMPONENT_LABEL = "app.kubernetes.io/component"
PART_OF_LABEL = "app.kubernetes.io/part-of"
NAMESPACE = "fs2-models"
SYSTEM_NAMESPACE = "fs2-system"
INVENTORY_SCHEMA = "fs2-serve.nebius.ai/model-runtime-network-inventory/v2"
DENY_ABSENT_SCHEMA = "fs2-serve.nebius.ai/model-runtime-network-deny-absent/v2"
WORKLOAD_RESOURCES = {
    "deployments": ("apps/v1", "Deployment", "deployments.apps"),
    "statefulsets": ("apps/v1", "StatefulSet", "statefulsets.apps"),
    "daemonsets": ("apps/v1", "DaemonSet", "daemonsets.apps"),
    "replicasets": ("apps/v1", "ReplicaSet", "replicasets.apps"),
    "jobs": ("batch/v1", "Job", "jobs.batch"),
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
    if admission_bindings != [f"{name}-fs2-models" for name in admission_policies]:
        raise ReceiptError(
            "transition contract admission bindings do not match its policies"
        )
    cluster_id = contract.get("cluster_id")
    if not isinstance(cluster_id, str) or not cluster_id:
        raise ReceiptError("transition contract cluster_id is missing")
    controller_name = contract.get("controller_deployment_name")
    if not isinstance(controller_name, str) or not controller_name:
        raise ReceiptError("transition contract controller Deployment name is missing")
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
        kind in {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"}
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
    if kind == "ReplicaSet":
        desired = spec.get("replicas", 1)
        values = {
            "desired": desired,
            "ready": _replicas(status, "readyReplicas"),
            "available": _replicas(status, "availableReplicas"),
        }
        if values != {"desired": desired, "ready": desired, "available": desired}:
            raise ReceiptError(
                f"ReplicaSet {metadata.get('name')} rollout is not converged"
            )
        return values
    return {}


def _pod_template(item: dict[str, Any], kind: str) -> dict[str, Any]:
    spec = _object(item.get("spec"), f"{kind}.spec")
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


def _workload_inventory(
    contract: dict[str, Any],
    resources: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, bool]]:
    recognized = set(contract["profiles"])
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
            profile = _profile(_labels(metadata, kind), recognized, f"{kind} {name}")
            template = _pod_template(item, kind)
            template_metadata = _object(
                template.get("metadata"),
                f"{kind} {name} PodTemplate.metadata",
            )
            template_profile = _profile(
                _labels(template_metadata, f"{kind} {name} PodTemplate"),
                recognized,
                f"{kind} {name} PodTemplate",
            )
            if profile != template_profile:
                raise ReceiptError(
                    f"{kind} {name} workload and Pod template profiles differ"
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
                    job_profile = _profile(
                        _labels(
                            _object(
                                job_template.get("metadata"),
                                f"JobSet.replicatedJobs[{index}].template.metadata",
                            ),
                            f"JobSet.replicatedJobs[{index}].template",
                        ),
                        recognized,
                        f"JobSet {name} replicated Job template {index}",
                    )
                    if job_profile != profile:
                        raise ReceiptError(
                            f"JobSet {name} replicated Job and workload profiles differ"
                        )
            identity = f"{api_version}/{kind}/{name}"
            if identity in workloads:
                raise ReceiptError(f"duplicate workload identity {identity}")
            workloads[identity] = {
                "uid": uid,
                "generation": generation,
                "profile": profile,
                "rollout": _controller_converged(kind, item),
            }
    if not workloads:
        raise ReceiptError("live fs2-models workload inventory is empty")
    return dict(sorted(workloads.items())), dict(sorted(availability.items()))


def _pod_inventory(
    contract: dict[str, Any], resources: dict[str, Any]
) -> dict[str, Any]:
    recognized = set(contract["profiles"])
    pods: dict[str, Any] = {}
    for item in _items(resources, "pods"):
        metadata = _object(item.get("metadata"), "Pod.metadata")
        name, uid, _generation = _identity(metadata, "Pod")
        profile = _profile(_labels(metadata, "Pod"), recognized, f"Pod {name}")
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
        pods[name] = {
            "uid": uid,
            "profile": profile,
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


def _admission_bindings(
    contract: dict[str, Any],
    resources: dict[str, Any],
) -> dict[str, str]:
    expected = set(contract["admission_binding_names"])
    found: dict[str, str] = {}
    for item in _items(resources, "admission bindings"):
        metadata = _object(item.get("metadata"), "admission binding.metadata")
        name = metadata.get("name")
        uid = metadata.get("uid")
        if isinstance(name, str) and name in expected:
            if not isinstance(uid, str) or not uid:
                raise ReceiptError(f"admission binding {name} has no UID")
            found[name] = uid
    missing = sorted(expected - set(found))
    if missing:
        raise ReceiptError(
            "network-profile admission bindings are missing: " + ", ".join(missing)
        )
    return dict(sorted(found.items()))


def inventory_receipt(
    contract: dict[str, Any],
    resources: dict[str, dict[str, Any]],
    pods: dict[str, Any],
    controller_deployments: dict[str, Any],
    controller_pods: dict[str, Any],
    admission_bindings: dict[str, Any],
    *,
    captured_at: str,
) -> dict[str, Any]:
    contract = _contract(contract, phases={"inventory", "enforce"})
    workloads, resource_apis = _workload_inventory(contract, resources)
    pod_inventory = _pod_inventory(contract, pods)
    _verify_pod_owners(workloads, pod_inventory)
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
        "admission_bindings": _admission_bindings(
            contract,
            admission_bindings,
        ),
    }
    return {**payload, "payload_sha256": _sha256(payload)}


def verify_enforce(
    contract: dict[str, Any],
    receipt: dict[str, Any],
    resources: dict[str, dict[str, Any]],
    pods: dict[str, Any],
    controller_deployments: dict[str, Any],
    controller_pods: dict[str, Any],
    admission_bindings: dict[str, Any],
) -> dict[str, Any]:
    _contract(contract, phases={"enforce"})
    captured_at = receipt.get("captured_at")
    if not isinstance(captured_at, str):
        raise ReceiptError("inventory receipt captured_at is missing")
    observed = inventory_receipt(
        contract,
        resources,
        pods,
        controller_deployments,
        controller_pods,
        admission_bindings,
        captured_at=captured_at,
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
    admission_bindings = _kubectl_json(
        kubectl=args.kubectl,
        kubeconfig=args.kubeconfig,
        context=args.context,
        resource="validatingadmissionpolicybindings.admissionregistration.k8s.io",
    )
    return (
        resources,
        pods,
        controller_deployments,
        controller_pods,
        admission_bindings,
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
