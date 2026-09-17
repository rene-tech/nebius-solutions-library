#!/usr/bin/env python3
"""Executable admission policy for additive storage-reconciler cutovers."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


POLICY_SPEC = {
    "schema": "fs2-serve.nebius.ai/storage-reconciler-cutover-policy/v1",
    "resources": ["apps/v1/deployments", "apps/v1/replicasets", "v1/pods"],
    "operations": ["CREATE", "UPDATE", "DELETE"],
    "failure_policy": "Fail",
    "match_policy": "Equivalent",
    "deployment_mode": "PASSIVE_CREATE_SIGNED_SCALE_TRANSITION",
    "retirement_mode": "QUIESCE_TO_ZERO_RETAIN_OBJECT",
    "rollback_mode": "HIGHER_EPOCH_AFTER_EXACT_QUIESCENCE_AND_COMPATIBILITY",
}


def _identity(request: dict[str, Any], identity: dict[str, Any]) -> bool:
    return (
        request.get("username") == identity.get("username")
        and request.get("uid") == identity.get("uid")
        and sorted(request.get("groups") or []) == sorted(identity.get("groups") or [])
    )


def _transition_id(object_: dict[str, Any]) -> str:
    annotations = (object_.get("metadata") or {}).get("annotations") or {}
    value = annotations.get("fs2.nebius.ai/storage-cutover-transition-sha256", "")
    return value if isinstance(value, str) and len(value) == 64 else ""


def _annotations_without_cutover(object_: dict[str, Any]) -> dict[str, Any]:
    annotations = json.loads(canonical((object_.get("metadata") or {}).get("annotations") or {}))
    annotations.pop("fs2.nebius.ai/storage-cutover-transition-sha256", None)
    return annotations


def _deployment_transition(
    request: dict[str, Any], state: dict[str, Any], transition: dict[str, Any]
) -> bool:
    current = request.get("object") or {}
    previous = request.get("old_object") or {}
    operation = request.get("operation")
    generation = ((current.get("metadata") or {}).get("labels") or {}).get(
        "fs2.nebius.ai/storage-rollout-generation"
    ) or ((previous.get("metadata") or {}).get("labels") or {}).get(
        "fs2.nebius.ai/storage-rollout-generation"
    )
    contract = state["deployments"].get(generation)
    if contract is None:
        return False
    if operation == "CREATE":
        # Passive creation is already constrained to the exact replicas=0
        # chart by the ordinary workload VAP. The external fence adopts its
        # server-assigned UID afterward and gates every executable UPDATE.
        return False
    if operation == "DELETE":
        return False
    if operation != "UPDATE" or not _identity(request, state["security_owner_identity"]):
        return False
    if (
        (current.get("metadata") or {}).get("uid") != contract["uid"]
        or (previous.get("metadata") or {}).get("uid") != contract["uid"]
        or _transition_id(current) != transition["transition_id"]
    ):
        return False
    old_spec = json.loads(canonical(previous.get("spec") or {}))
    new_spec = json.loads(canonical(current.get("spec") or {}))
    old_spec["replicas"] = 0
    new_spec["replicas"] = 0
    previous_metadata = previous.get("metadata") or {}
    current_metadata = current.get("metadata") or {}
    if (
        old_spec != new_spec
        or old_spec != contract["passive_spec"]
        or previous_metadata.get("name") != current_metadata.get("name")
        or previous_metadata.get("namespace") != current_metadata.get("namespace")
        or previous_metadata.get("uid") != current_metadata.get("uid")
        or previous_metadata.get("labels") != current_metadata.get("labels")
        or _annotations_without_cutover(previous)
        != _annotations_without_cutover(current)
    ):
        return False
    previous_replicas = (previous.get("spec") or {}).get("replicas")
    current_replicas = (current.get("spec") or {}).get("replicas")
    phase = transition["phase"]
    if phase == "QUIESCE_PREDECESSOR":
        return bool(
            generation == transition["predecessor_generation"]
            and previous_replicas == 1
            and current_replicas == 0
        )
    if phase == "ACTIVATE_SUCCESSOR":
        return bool(
            generation == transition["successor_generation"]
            and previous_replicas == 0
            and current_replicas == 1
            and transition.get("predecessor_quiescence") is not None
            and digest(transition["predecessor_quiescence"])
            == transition.get("predecessor_quiescence_sha256")
        )
    if phase == "ROLLBACK_QUIESCE":
        return bool(
            generation == transition["successor_generation"]
            and previous_replicas == 1
            and current_replicas == 0
            and transition.get("rollback") is not None
        )
    if phase == "ROLLBACK_ACTIVATE":
        rollback = transition.get("rollback")
        return bool(
            generation == transition["predecessor_generation"]
            and previous_replicas == 0
            and current_replicas == 1
            and isinstance(rollback, dict)
            and rollback.get("successor_quiesced") is True
            and isinstance(rollback.get("successor_quiescence"), dict)
            and digest(rollback["successor_quiescence"])
            == rollback.get("successor_quiescence_sha256")
            and all(
                isinstance(rollback.get(field), str) and len(rollback[field]) == 64
                for field in (
                    "zero_inflight_actions_receipt_sha256",
                    "schema_compatibility_receipt_sha256",
                    "provider_continuity_receipt_sha256",
                )
            )
        )
    return False


def _replicaset(request: dict[str, Any], state: dict[str, Any]) -> bool:
    if not _identity(request, state["deployment_controller_identity"]):
        return False
    object_ = request.get("object") or {}
    metadata = object_.get("metadata") or {}
    labels = metadata.get("labels") or {}
    generation = labels.get("fs2.nebius.ai/storage-rollout-generation")
    contract = state["deployments"].get(generation)
    if contract is None:
        return False
    owners = metadata.get("ownerReferences")
    spec = object_.get("spec") or {}
    template = spec.get("template") or {}
    template_labels = (template.get("metadata") or {}).get("labels") or {}
    selector_labels = (spec.get("selector") or {}).get("matchLabels") or {}
    pod_hash = labels.get("pod-template-hash")
    expected_template = json.loads(canonical(contract["pod_template"]))
    expected_template.setdefault("metadata", {}).setdefault("labels", {})[
        "pod-template-hash"
    ] = pod_hash
    return bool(
        request.get("operation") in {"CREATE", "UPDATE"}
        and isinstance(pod_hash, str)
        and pod_hash
        and owners
        == [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "name": contract["name"],
                "uid": contract["uid"],
                "controller": True,
                "blockOwnerDeletion": True,
            }
        ]
        and metadata.get("name", "").startswith(f"{contract['name']}-")
        and template == expected_template
        and selector_labels == template_labels
        and spec.get("replicas") in {0, 1}
    )


def _pod(request: dict[str, Any], state: dict[str, Any]) -> bool:
    if not _identity(request, state["replicaset_controller_identity"]):
        return False
    object_ = request.get("object") or {}
    metadata = object_.get("metadata") or {}
    labels = metadata.get("labels") or {}
    generation = labels.get("fs2.nebius.ai/storage-rollout-generation")
    if generation != state.get("active_generation"):
        return False
    contract = state["deployments"].get(generation)
    owners = metadata.get("ownerReferences")
    if contract is None or not isinstance(owners, list) or len(owners) != 1:
        return False
    owner = owners[0]
    pod_hash = labels.get("pod-template-hash")
    expected = json.loads(canonical(contract["pod_template"]))
    expected.setdefault("metadata", {}).setdefault("labels", {})["pod-template-hash"] = pod_hash
    return bool(
        request.get("operation") == "CREATE"
        and isinstance(pod_hash, str)
        and pod_hash
        and owner.get("apiVersion") == "apps/v1"
        and owner.get("kind") == "ReplicaSet"
        and owner.get("name", "").startswith(f"{contract['name']}-")
        and isinstance(owner.get("uid"), str)
        and owner["uid"]
        and owner.get("controller") is True
        and owner.get("blockOwnerDeletion") is True
        and metadata.get("labels") == expected["metadata"]["labels"]
        and object_.get("spec") == expected["spec"]
    )


def allows(request: dict[str, Any], state: dict[str, Any]) -> bool:
    if state.get("verified") is not True:
        return False
    resource = request.get("resource")
    if resource == "deployments":
        transition = state.get("transition")
        return isinstance(transition, dict) and _deployment_transition(
            request, state, transition
        )
    if resource == "replicasets":
        return _replicaset(request, state)
    if resource == "pods":
        return _pod(request, state)
    return False
