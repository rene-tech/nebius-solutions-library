#!/usr/bin/env python3
"""Canonical executable policy for the externally owned DaemonSet fence.

The external owner installs these exact source bytes as the admission
evaluator.  The SAI-08 verifier hashes this file and requires the installed
enforcer artifact to match, so a signed description cannot stand in for the
code that evaluates CREATE, UPDATE, DELETE, and controller-created Pods.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


POLICY_SPEC = {
    "schema": "fs2-serve.nebius.ai/daemonset-snapshot-fence-policy/v4",
    "evaluator": "EXACT_SOURCE_BYTES_AND_CANONICAL_JSON_SHA256",
    "action": "Deny",
    "scope": "Cluster",
    "namespace_exclusions": [],
    "object_selector": {},
    "failure_policy": "Fail",
    "match_policy": "Equivalent",
    "daemonset_operations": ["CREATE", "UPDATE", "DELETE"],
    "pod_operations": ["CREATE"],
    "resources": ["apps/v1/daemonsets", "v1/pods"],
    "ledger_mode": "SIGNED_APPEND_ONLY_HASH_CHAIN_WITH_ATOMIC_TRANSITIONS",
    "migration_mode": "ADOPT_EXISTING_UID_AND_SPEC_WITHOUT_OBJECT_MUTATION",
    "validations": [
        "authenticated-maintainer-identity",
        "authenticated-daemonset-controller-identity",
        "canonical-daemonset-spec-equality",
        "canonical-pod-spec-equality",
        "content-bound-transition-token",
        "controller-injection-normalization",
        "delete-denied-retained-uid",
        "exact-daemonset-uid",
        "in-place-successor-snapshot-ledger-membership",
        "no-ephemeral-containers",
        "no-node-name",
        "old-snapshot-ledger-membership",
        "pod-owner-reference",
        "retained-adoption-without-update",
        "runtime-state-signature-and-head",
        "single-active-transition",
    ],
}


def agent_contract(agents: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return the exact full-spec contract consumed by the evaluator."""
    result: dict[str, dict[str, Any]] = {}
    for key, agent in sorted(agents.items()):
        namespace, name = key.split("/", 1)
        spec = agent["daemonset_spec"]
        result[key] = {
            "namespace": namespace,
            "name": name,
            "uid": agent["uid"],
            "daemonset_spec": spec,
            "daemonset_spec_sha256": digest(spec),
            "pod_template": spec["template"],
            "pod_template_sha256": digest(spec["template"]),
            "maintenance_identity": agent["maintenance_identity"],
            "snapshot_generation": agent["snapshot_generation"],
            "snapshot_sha256": agent["snapshot_sha256"],
        }
    return result


def _identity_matches(request: dict[str, Any], expected: dict[str, Any]) -> bool:
    return (
        request.get("username") == expected.get("username")
        and request.get("uid") == expected.get("uid")
        and sorted(request.get("groups", [])) == sorted(expected.get("groups", []))
    )


def _transition_token(metadata: dict[str, Any]) -> str:
    annotations = metadata.get("annotations") or {}
    value = annotations.get("security.fs2.nebius.ai/daemonset-transition-sha256", "")
    return value if isinstance(value, str) and len(value) == 64 else ""


def _blanket_tolerating(spec: object) -> bool:
    if not isinstance(spec, dict):
        return False
    tolerations = spec.get("tolerations")
    return isinstance(tolerations, list) and any(
        isinstance(item, dict)
        and item.get("key") in {None, ""}
        and item.get("operator") == "Exists"
        and item.get("effect") in {None, "", "NoSchedule"}
        for item in tolerations
    )


_CONTROLLER_TOLERATIONS = {
    canonical(value)
    for value in (
        {"key": "node.kubernetes.io/not-ready", "operator": "Exists", "effect": "NoExecute"},
        {"key": "node.kubernetes.io/unreachable", "operator": "Exists", "effect": "NoExecute"},
        {"key": "node.kubernetes.io/disk-pressure", "operator": "Exists", "effect": "NoSchedule"},
        {"key": "node.kubernetes.io/memory-pressure", "operator": "Exists", "effect": "NoSchedule"},
        {"key": "node.kubernetes.io/pid-pressure", "operator": "Exists", "effect": "NoSchedule"},
        {"key": "node.kubernetes.io/unschedulable", "operator": "Exists", "effect": "NoSchedule"},
        {"key": "node.kubernetes.io/network-unavailable", "operator": "Exists", "effect": "NoSchedule"},
    )
}


def _without_controller_tolerations(
    actual: object, expected: object
) -> list[dict[str, Any]] | None:
    if not isinstance(actual, list) or not isinstance(expected, list):
        return None
    remaining = [canonical(item) for item in actual if isinstance(item, dict)]
    if len(remaining) != len(actual):
        return None
    for item in expected:
        encoded = canonical(item)
        if encoded not in remaining:
            return None
        remaining.remove(encoded)
    if len(remaining) != len(set(remaining)) or any(
        item not in _CONTROLLER_TOLERATIONS for item in remaining
    ):
        return None
    return [json.loads(item) for item in remaining]


def _strip_controller_node_affinity(
    actual: object, expected: object
) -> tuple[dict[str, Any], str] | None:
    """Remove only the DaemonSet controller's exact metadata.name injection.

    Kubernetes ANDs a singleton ``metadata.name In`` matchField into every
    required node-selector term.  All remaining affinity bytes must equal the
    signed template, preserving term OR and requirement AND semantics.
    """

    if not isinstance(actual, dict) or not isinstance(expected, dict):
        return None
    observed = json.loads(canonical(actual))
    wanted = json.loads(canonical(expected))
    observed_node = observed.get("nodeAffinity")
    wanted_node = wanted.get("nodeAffinity")
    if not isinstance(observed_node, dict):
        return None
    required = observed_node.get("requiredDuringSchedulingIgnoredDuringExecution")
    if not isinstance(required, dict) or not isinstance(required.get("nodeSelectorTerms"), list):
        return None
    wanted_required = (
        wanted_node.get("requiredDuringSchedulingIgnoredDuringExecution")
        if isinstance(wanted_node, dict)
        else None
    )
    wanted_terms = (
        wanted_required.get("nodeSelectorTerms")
        if isinstance(wanted_required, dict)
        else [{}]
    )
    if not isinstance(wanted_terms, list) or len(required["nodeSelectorTerms"]) != len(wanted_terms):
        return None
    selected_node = ""
    stripped_terms: list[dict[str, Any]] = []
    for observed_term, wanted_term in zip(required["nodeSelectorTerms"], wanted_terms, strict=True):
        if not isinstance(observed_term, dict) or not isinstance(wanted_term, dict):
            return None
        fields = observed_term.get("matchFields")
        if not isinstance(fields, list):
            return None
        node_fields = [
            field
            for field in fields
            if isinstance(field, dict)
            and field.get("key") == "metadata.name"
            and field.get("operator") == "In"
            and isinstance(field.get("values"), list)
            and len(field["values"]) == 1
            and isinstance(field["values"][0], str)
            and field["values"][0]
        ]
        if len(node_fields) != 1:
            return None
        node_name = node_fields[0]["values"][0]
        if selected_node and selected_node != node_name:
            return None
        selected_node = node_name
        stripped = json.loads(canonical(observed_term))
        stripped_fields = [field for field in fields if field is not node_fields[0]]
        if stripped_fields:
            stripped["matchFields"] = stripped_fields
        else:
            stripped.pop("matchFields", None)
        stripped_terms.append(stripped)
    required["nodeSelectorTerms"] = stripped_terms
    if wanted_node is None:
        if observed_node != {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [{}]
            }
        }:
            return None
        observed.pop("nodeAffinity", None)
    if observed != wanted:
        return None
    return observed, selected_node


def _pod_spec_matches(actual: object, expected: object) -> bool:
    if not isinstance(actual, dict) or not isinstance(expected, dict):
        return False
    observed = json.loads(canonical(actual))
    wanted = json.loads(canonical(expected))
    if observed.get("nodeName") not in {None, ""}:
        return False
    ephemeral = observed.get("ephemeralContainers")
    if ephemeral is not None and ephemeral != []:
        return False
    observed.pop("ephemeralContainers", None)
    wanted.pop("ephemeralContainers", None)
    observed_tolerations = observed.pop("tolerations", [])
    wanted_tolerations = wanted.pop("tolerations", [])
    if _without_controller_tolerations(observed_tolerations, wanted_tolerations) is None:
        return False
    observed_affinity = observed.pop("affinity", {})
    wanted_affinity = wanted.pop("affinity", {})
    if _strip_controller_node_affinity(observed_affinity, wanted_affinity) is None:
        return False
    wanted.pop("nodeName", None)
    observed.pop("nodeName", None)
    return observed == wanted


def _pod_matches(
    request: dict[str, Any], object_: dict[str, Any], agent: dict[str, Any]
) -> bool:
    controller = agent.get("daemonset_controller_identity")
    if not isinstance(controller, dict) or not _identity_matches(request, controller):
        return False
    metadata = object_.get("metadata") or {}
    owners = metadata.get("ownerReferences")
    if owners != [
        {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "name": agent["name"],
            "uid": agent["uid"],
            "controller": True,
            "blockOwnerDeletion": True,
        }
    ]:
        return False
    expected_metadata = agent["pod_template"].get("metadata") or {}
    expected_labels = expected_metadata.get("labels") or {}
    actual_labels = metadata.get("labels") or {}
    dynamic = {"controller-revision-hash", "pod-template-generation"}
    if not all(actual_labels.get(key) == value for key, value in expected_labels.items()):
        return False
    if any(
        key not in expected_labels
        and (key not in dynamic or not isinstance(value, str) or not value)
        for key, value in actual_labels.items()
    ):
        return False
    return _pod_spec_matches(
        object_.get("spec") or {}, agent["pod_template"].get("spec") or {}
    )


def _transition_for(
    transitions: dict[str, dict[str, Any]],
    *,
    token: str,
    namespace: str,
    name: str,
    predecessor_uid: str = "",
) -> dict[str, Any] | None:
    if token:
        candidate = transitions.get(token)
        if isinstance(candidate, dict):
            return candidate
        return None
    candidates = [
        transition
        for transition in transitions.values()
        if transition.get("namespace") == namespace
        and transition.get("predecessor_name") == name
        and transition.get("predecessor_uid") == predecessor_uid
        and transition.get("phase") == "SUCCESSOR_READY"
    ]
    return candidates[0] if len(candidates) == 1 else None


def allows(
    request: dict[str, Any],
    *,
    verified_state: dict[str, Any],
) -> bool:
    """Evaluate one request against already signature-verified runtime state."""
    if verified_state.get("verified") is not True:
        return False
    active_agents = verified_state.get("active_agents")
    transitions = verified_state.get("transitions")
    controller_identity = verified_state.get("daemonset_controller_identity")
    if (
        not isinstance(active_agents, dict)
        or not isinstance(transitions, dict)
        or not isinstance(controller_identity, dict)
    ):
        return False
    active_agents = {
        key: {**agent, "daemonset_controller_identity": controller_identity}
        for key, agent in active_agents.items()
    }
    operation = request.get("operation")
    resource = request.get("resource")
    namespace = request.get("namespace", "")
    object_ = request.get("object") or {}
    old_object = request.get("old_object") or {}
    metadata = object_.get("metadata") or {}
    old_metadata = old_object.get("metadata") or {}
    name = metadata.get("name") or old_metadata.get("name") or request.get("name")
    key = f"{namespace}/{name}"
    active = active_agents.get(key)

    if resource == "pods" and operation == "CREATE":
        if not _blanket_tolerating(object_.get("spec")):
            return True
        pod_agents = list(active_agents.values())
        for transition in transitions.values():
            if transition.get("phase") in {"PREPARED", "ADMITTED", "SUCCESSOR_READY"}:
                pod_agents.append(transition["successor_agent"])
        return any(_pod_matches(request, object_, agent) for agent in pod_agents)
    if resource != "daemonsets" or operation not in {"CREATE", "UPDATE", "DELETE"}:
        return False
    current_template = (object_.get("spec") or {}).get("template") or {}
    previous_template = (old_object.get("spec") or {}).get("template") or {}
    if not _blanket_tolerating(current_template.get("spec")) and not _blanket_tolerating(
        previous_template.get("spec")
    ):
        return True

    transition = _transition_for(
        transitions,
        token=_transition_token(metadata or old_metadata),
        namespace=namespace,
        name=str(name),
        predecessor_uid=str(old_metadata.get("uid", "")),
    )
    if operation == "CREATE":
        # Existing blanket agents are adopted and updated in place. A create
        # would obtain an unknowable UID and strand every retained ordinary
        # Deny generation on a stale owner reference.
        return False
    if active is None or not _identity_matches(
        request, active.get("maintenance_identity") or {}
    ):
        return False
    if operation == "UPDATE":
        if old_metadata.get("uid") != active.get("uid"):
            return False
        if old_object.get("spec") != active.get("daemonset_spec"):
            return False
        if transition is None:
            return (
                metadata.get("uid") == active.get("uid")
                and object_.get("spec") == active.get("daemonset_spec")
            )
        return bool(
            transition.get("phase") in {"PREPARED", "ADMITTED"}
            and transition.get("predecessor_uid") == active.get("uid")
            and transition.get("successor_name") == name
            and object_.get("spec")
            == transition.get("successor_agent", {}).get("daemonset_spec")
            and _identity_matches(request, transition.get("maintenance_identity") or {})
        )
    # DELETE remains in the webhook rule as a fail-closed operation. The
    # adopted UID is preserved by the no-delete migration protocol.
    return False
