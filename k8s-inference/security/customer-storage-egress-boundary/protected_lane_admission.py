#!/usr/bin/env python3
"""Render and model the additive protected-node admission contract.

The Terraform boundary consumes the CEL fragments emitted by this module.  The
pure-Python decision functions intentionally model the same predicates so the
retained-policy conjunction can be exercised without a Kubernetes API server.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from typing import Any

CONTROLLER_ROLES = {
    "deployment",
    "replicaset",
    "daemonset",
    "scheduler",
    "node_health",
}
OBSERVER_CLASSES = {"lane", "critical-blanket-agent"}
UID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def validate_contract(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("protected-lane contract must be an object")
    expected = {
        "schema",
        "generation",
        "lane_id",
        "selector_key",
        "selector_value",
        "taint_key",
        "taint_value",
        "taint_effect",
        "protected_node_names",
        "protected_node_inventory_sha256",
        "protected_node_scheduling_labels",
        "protected_node_scheduling_labels_sha256",
        "protected_node_attestations",
        "protected_node_attestation_sha256",
        "controller_identities",
        "controller_audit_receipt_sha256",
        "node_health_mutation",
        "daemonset_inventory_sha256",
        "daemonset_list_resource_version",
        "daemonset_admission_fence_receipt_sha256",
        "daemonset_snapshot_ledger_head_sha256",
        "node_lifecycle_mode",
        "observers",
        "observer_inventory_sha256",
    }
    if set(value) != expected:
        raise ValueError("protected-lane contract fields differ")
    if value.get("schema") != "fs2-serve.nebius.ai/protected-lane-admission/v6":
        raise ValueError("protected-lane contract schema differs")
    generation = _string(value.get("generation"), "generation")
    if not re.fullmatch(r"g[0-9]{14}-[a-f0-9]{12}", generation):
        raise ValueError("generation is invalid")
    lane_id = _string(value.get("lane_id"), "lane_id")
    if not re.fullmatch(r"l[0-9]{14}-[a-f0-9]{12}", lane_id):
        raise ValueError("protected-lane ID is invalid")
    expected_key = f"workload.fs2.nebius/customer-storage-egress-{lane_id[-12:]}"
    if value.get("selector_key") != expected_key or value.get("taint_key") != expected_key:
        raise ValueError("protected-lane scheduling key is not lane-unique")
    if value.get("selector_value") != lane_id or value.get("taint_value") != lane_id:
        raise ValueError("protected-lane scheduling value differs from lane ID")
    if value.get("taint_effect") != "NoSchedule":
        raise ValueError("protected-lane taint effect differs")
    protected_node_names = value.get("protected_node_names")
    if (
        not isinstance(protected_node_names, list)
        or len(protected_node_names) != 1
        or any(not isinstance(node_name, str) for node_name in protected_node_names)
        or protected_node_names != sorted(set(protected_node_names))
        or not re.fullmatch(
            r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?", protected_node_names[0]
        )
        or digest(protected_node_names) != value.get("protected_node_inventory_sha256")
    ):
        raise ValueError("exact activated protected-node inventory differs")
    scheduling_labels = value.get("protected_node_scheduling_labels")
    if (
        not isinstance(scheduling_labels, dict)
        or set(scheduling_labels) != set(protected_node_names)
        or any(
            not isinstance(labels, dict)
            or not labels
            or any(
                not isinstance(key, str)
                or not key
                or not isinstance(label_value, str)
                for key, label_value in labels.items()
            )
            for labels in scheduling_labels.values()
        )
        or scheduling_labels[protected_node_names[0]].get(expected_key) != lane_id
        or digest(scheduling_labels)
        != value.get("protected_node_scheduling_labels_sha256")
    ):
        raise ValueError("complete protected-node scheduling-label projection differs")
    attestations = value.get("protected_node_attestations")
    if (
        not isinstance(attestations, dict)
        or set(attestations) != set(protected_node_names)
        or digest(attestations) != value.get("protected_node_attestation_sha256")
    ):
        raise ValueError("protected-node attestation inventory differs")
    for node_name, attestation in attestations.items():
        if (
            not isinstance(attestation, dict)
            or set(attestation)
            != {
                "name",
                "uid",
                "resource_version",
                "provider_id",
                "node_group_id",
                "provisioning_receipt_sha256",
                "observed_at",
                "labels",
                "taints",
            }
            or attestation.get("name") != node_name
            or not UID_RE.fullmatch(str(attestation.get("uid", "")))
            or not isinstance(attestation.get("resource_version"), str)
            or not attestation["resource_version"]
            or not isinstance(attestation.get("provider_id"), str)
            or not attestation["provider_id"]
            or not isinstance(attestation.get("node_group_id"), str)
            or not attestation["node_group_id"]
            or not re.fullmatch(
                r"[a-f0-9]{64}",
                str(attestation.get("provisioning_receipt_sha256", "")),
            )
            or not isinstance(attestation.get("observed_at"), str)
            or not attestation["observed_at"]
            or attestation.get("labels") != scheduling_labels[node_name]
            or not isinstance(attestation.get("taints"), list)
            or {
                "key": expected_key,
                "value": lane_id,
                "effect": "NoSchedule",
            }
            not in attestation["taints"]
        ):
            raise ValueError("protected-node provider membership, identity, labels, or taints differ")

    if not re.fullmatch(
        r"[a-f0-9]{64}", str(value.get("controller_audit_receipt_sha256", ""))
    ) or not re.fullmatch(
        r"[a-f0-9]{64}", str(value.get("daemonset_inventory_sha256", ""))
    ) or not re.fullmatch(
        r"[a-f0-9]{64}",
        str(value.get("daemonset_admission_fence_receipt_sha256", "")),
    ) or not re.fullmatch(
        r"[a-f0-9]{64}",
        str(value.get("daemonset_snapshot_ledger_head_sha256", "")),
    ):
        raise ValueError("controller, DaemonSet inventory, or continuous fence digest is absent")
    if not isinstance(value.get("daemonset_list_resource_version"), str) or not value[
        "daemonset_list_resource_version"
    ]:
        raise ValueError("DaemonSet list resourceVersion is absent")
    if (
        value.get("node_lifecycle_mode")
        != "PARALLEL_GENERATIONAL_SINGLETON_CUTOVER_RETAIN_PREDECESSOR"
    ):
        raise ValueError("protected lane is not a retained generational singleton")
    node_health = value.get("node_health_mutation")
    if (
        not isinstance(node_health, dict)
        or set(node_health)
        != {
            "identity_role",
            "mutable_label_keys",
            "mutable_taint_keys",
            "allow_unschedulable",
        }
        or node_health.get("identity_role") != "node_health"
        or not isinstance(node_health.get("mutable_label_keys"), list)
        or node_health["mutable_label_keys"]
        != sorted(set(node_health["mutable_label_keys"]))
        or not isinstance(node_health.get("mutable_taint_keys"), list)
        or not node_health["mutable_taint_keys"]
        or node_health["mutable_taint_keys"]
        != sorted(set(node_health["mutable_taint_keys"]))
        or node_health.get("allow_unschedulable") is not True
    ):
        raise ValueError("narrow node-health mutation contract differs")

    identities = value.get("controller_identities")
    if not isinstance(identities, dict) or set(identities) != CONTROLLER_ROLES:
        raise ValueError("audited controller identity inventory differs")
    for role, identity in identities.items():
        service_account = (
            isinstance(identity, dict)
            and identity.get("kind") == "ServiceAccount"
            and identity.get("namespace") == "kube-system"
            and identity.get("username")
            == f"system:serviceaccount:kube-system:{identity.get('name', '')}"
            and UID_RE.fullmatch(str(identity.get("uid", ""))) is not None
            and identity.get("groups")
            == [
                "system:authenticated",
                "system:serviceaccounts",
                "system:serviceaccounts:kube-system",
            ]
        )
        native_user = (
            isinstance(identity, dict)
            and identity.get("kind") == "User"
            and identity.get("namespace") == ""
            and isinstance(identity.get("name"), str)
            and identity["name"].startswith("system:")
            and identity.get("username") == identity["name"]
            and isinstance(identity.get("uid"), str)
            and bool(identity["uid"])
            and identity.get("groups") == ["system:authenticated"]
        )
        if (
            not isinstance(identity, dict)
            or set(identity)
            != {
                "kind",
                "namespace",
                "name",
                "username",
                "uid",
                "groups",
                "audit_evidence_sha256",
            }
            or not (service_account or native_user)
            or not re.fullmatch(
                r"[a-f0-9]{64}", str(identity.get("audit_evidence_sha256", ""))
            )
        ):
            raise ValueError(f"{role} controller is not an audited live identity")

    observers = value.get("observers")
    if not isinstance(observers, dict) or not observers:
        raise ValueError("complete signed observer inventory is required")
    seen: set[tuple[str, str] | str] = set()
    for role, observer in observers.items():
        observer_fields = {
            "class",
            "namespace",
            "name",
            "uid",
            "owner_identity",
            "daemonset_spec",
            "daemonset_spec_sha256",
        }
        critical_observer_fields = observer_fields | {
            "maintenance_audit_sha256",
            "snapshot_generation",
            "snapshot_sha256",
        }
        if not isinstance(observer, dict) or set(observer) not in (
            observer_fields,
            critical_observer_fields,
        ):
            raise ValueError(f"{role} observer fields differ")
        observer_class = observer.get("class")
        if observer_class not in OBSERVER_CLASSES:
            raise ValueError(f"{role} observer class differs")
        namespace = _string(observer.get("namespace"), f"{role} namespace")
        name = _string(observer.get("name"), f"{role} name")
        uid = _string(observer.get("uid"), f"{role} UID")
        owner = observer.get("owner_identity")
        spec = observer.get("daemonset_spec")
        if not isinstance(owner, dict) or set(owner) != {"username", "uid", "groups"}:
            raise ValueError(f"{role} owner identity differs")
        _string(owner.get("username"), f"{role} owner username")
        owner_uid = _string(owner.get("uid"), f"{role} owner UID")
        owner_match = re.fullmatch(
            r"system:serviceaccount:([^:]+):([^:]+)", owner["username"]
        )
        if (
            owner_match is None
            or not UID_RE.fullmatch(owner_uid)
            or not isinstance(owner.get("groups"), list)
            or owner["groups"]
            != [
                "system:authenticated",
                "system:serviceaccounts",
                f"system:serviceaccounts:{owner_match.group(1)}",
            ]
        ):
            raise ValueError(f"{role} owner groups differ")
        if observer_class == "lane" and namespace != "kube-system":
            raise ValueError(f"{role} additive compatibility observer must be in kube-system")
        if not UID_RE.fullmatch(uid):
            raise ValueError(f"{role} UID is invalid")
        if observer_class == "critical-blanket-agent" and not re.fullmatch(
            r"[a-f0-9]{64}", str(observer.get("maintenance_audit_sha256", ""))
        ):
            raise ValueError(f"{role} maintainer lacks authenticated audit evidence")
        if observer_class == "critical-blanket-agent" and (
            not re.fullmatch(
                r"s[0-9]{14}-[a-f0-9]{12}",
                str(observer.get("snapshot_generation", "")),
            )
            or not re.fullmatch(
                r"[a-f0-9]{64}", str(observer.get("snapshot_sha256", ""))
            )
        ):
            raise ValueError(f"{role} is absent from the external snapshot ledger")
        if (
            observer_class != "critical-blanket-agent"
            and set(observer) != observer_fields
        ):
            raise ValueError(f"{role} has an inapplicable maintenance audit binding")
        if (namespace, name) in seen or uid in seen:
            raise ValueError("observer names and UIDs must be unique")
        seen.update({(namespace, name), uid})
        if not isinstance(spec, dict) or digest(spec) != observer.get("daemonset_spec_sha256"):
            raise ValueError(f"{role} DaemonSet spec digest differs")
        template = spec.get("template")
        if not isinstance(template, dict):
            raise ValueError(f"{role} DaemonSet template is absent")
        metadata = template.get("metadata")
        pod_spec = template.get("spec")
        if not isinstance(metadata, dict) or not isinstance(metadata.get("labels"), dict):
            raise ValueError(f"{role} template labels are absent")
        labels = metadata["labels"]
        if (
            spec.get("selector") != {"matchLabels": labels}
            or not isinstance(labels.get("app.kubernetes.io/component"), str)
            or not labels["app.kubernetes.io/component"]
        ):
            raise ValueError(f"{role} selector or component labels differ")
        if not isinstance(pod_spec, dict):
            raise ValueError(f"{role} Pod spec is absent")
        if (
            not isinstance(pod_spec.get("containers"), list)
            or not pod_spec["containers"]
            or any(not isinstance(container, dict) for container in pod_spec["containers"])
        ):
            raise ValueError(f"{role} observer identity or image custody differs")
        if observer_class == "lane":
            if (
                not isinstance(pod_spec.get("serviceAccountName"), str)
                or not pod_spec["serviceAccountName"]
                or pod_spec.get("automountServiceAccountToken") is not False
                or any(
                    not re.fullmatch(
                        r"[^@]+@sha256:[a-f0-9]{64}", str(container.get("image", ""))
                    )
                    for container in pod_spec["containers"]
                )
            ):
                raise ValueError(f"{role} additive observer image is not digest-pinned")
            if labels.get("fs2.nebius.ai/protected-lane-id") != lane_id:
                raise ValueError(f"{role} protected-lane label differs")
            if pod_spec.get("nodeSelector") != {expected_key: lane_id}:
                raise ValueError(f"{role} observer does not select only this lane")
            if pod_spec.get("tolerations") != [
                {
                    "key": expected_key,
                    "operator": "Equal",
                    "value": lane_id,
                    "effect": "NoSchedule",
                }
            ]:
                raise ValueError(f"{role} observer must use the exact lane toleration")
        else:
            if labels.get("fs2.nebius.ai/protected-lane-id") is not None:
                raise ValueError(f"{role} retained node agent cannot claim a lane identity")
            selector = pod_spec.get("nodeSelector", {})
            if not isinstance(selector, dict) or expected_key in selector:
                raise ValueError(f"{role} retained node-agent selector is not independent")
            if not any(
                isinstance(toleration, dict)
                and toleration.get("key") in {None, ""}
                and toleration.get("operator") == "Exists"
                and toleration.get("effect") in {None, "", "NoSchedule"}
                for toleration in pod_spec.get("tolerations", [])
            ):
                raise ValueError(f"{role} retained node agent lacks its recorded blanket toleration")
        if pod_spec.get("nodeName") not in {None, ""}:
            raise ValueError(f"{role} observer cannot bind nodeName directly")

    if digest(observers) != value.get("observer_inventory_sha256"):
        raise ValueError("observer inventory digest differs")
    return value


def _q(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _requirement_cel(requirement: str, attributes: str) -> str:
    key = f"{requirement}.key"
    operator = f"{requirement}.operator"
    values = f"{requirement}.values"
    present = f"({key} in {attributes})"
    numeric = "^-?[0-9]+$"
    return " ".join(
        [
            "(",
            f"({operator} == 'In' && has({values}) && {present} && {attributes}[{key}] in {values}) ||",
            f"({operator} == 'NotIn' && has({values}) && (!{present} || !({attributes}[{key}] in {values}))) ||",
            f"({operator} == 'Exists' && {present}) ||",
            f"({operator} == 'DoesNotExist' && !{present}) ||",
            f"({operator} == 'Gt' && has({values}) && size({values}) == 1 && {present} &&",
            f"{attributes}[{key}].matches({_q(numeric)}) && {values}[0].matches({_q(numeric)}) &&",
            f"int({attributes}[{key}]) > int({values}[0])) ||",
            f"({operator} == 'Lt' && has({values}) && size({values}) == 1 && {present} &&",
            f"{attributes}[{key}].matches({_q(numeric)}) && {values}[0].matches({_q(numeric)}) &&",
            f"int({attributes}[{key}]) < int({values}[0]))",
            ")",
        ]
    )


def _target_cel(contract: dict[str, Any], path: str) -> str:
    key = _q(contract["taint_key"])
    value = _q(contract["taint_value"])
    effect = _q(contract["taint_effect"])
    node_name = _q(contract["protected_node_names"][0])
    toleration_matches = " ".join(
        [
            f"(has({path}.tolerations) && {path}.tolerations.exists(toleration,",
            f"(!has(toleration.effect) || toleration.effect == '' || toleration.effect == {effect}) && (",
            "(has(toleration.operator) && toleration.operator == 'Exists' &&",
            f"(!has(toleration.key) || toleration.key == '' || toleration.key == {key})) ||",
            "((!has(toleration.operator) || toleration.operator == '' || toleration.operator == 'Equal') &&",
            f"has(toleration.key) && toleration.key == {key} &&",
            f"has(toleration.value) && toleration.value == {value}))))",
        ]
    )
    return " ".join(
        [
            "(",
            f"(has({path}.nodeName) && {path}.nodeName == {node_name}) ||",
            f"({toleration_matches})",
            ")",
        ]
    )


def _identity_cel(identity: dict[str, Any]) -> str:
    groups = json.dumps(identity["groups"], separators=(",", ":"))
    return " ".join(
        [
            f"request.userInfo.username == {_q(identity['username'])} &&",
            f"has(request.userInfo.uid) && request.userInfo.uid == {_q(identity['uid'])} &&",
            f"size(request.userInfo.groups) == size({groups}) &&",
            f"request.userInfo.groups.all(group, group in {groups}) &&",
            f"{groups}.all(group, group in request.userInfo.groups)",
        ]
    )


def _snapshot_metadata_cel(path: str) -> str:
    generation = "security.fs2.nebius.ai/daemonset-snapshot-generation"
    snapshot = "security.fs2.nebius.ai/daemonset-snapshot-sha256"
    return " ".join(
        [
            f"has({path}.annotations) &&",
            f"{_q(generation)} in {path}.annotations &&",
            f"{path}.annotations[{_q(generation)}].matches('^s[0-9]{{14}}-[a-f0-9]{{12}}$') &&",
            f"{_q(snapshot)} in {path}.annotations &&",
            f"{path}.annotations[{_q(snapshot)}].matches('^[a-f0-9]{{64}}$')",
        ]
    )


def _observer_daemonset_cel(contract: dict[str, Any]) -> str:
    choices: list[str] = []
    for observer in contract["observers"].values():
        if observer["class"] == "critical-blanket-agent":
            transition = "security.fs2.nebius.ai/daemonset-transition-sha256"
            choices.append(
                " ".join(
                    [
                        f"(request.namespace == {_q(observer['namespace'])} &&",
                        f"request.name == {_q(observer['name'])} &&",
                        f"({_identity_cel(observer['owner_identity'])}) &&",
                        "(request.operation == 'UPDATE' &&",
                        f"object.metadata.uid == {_q(observer['uid'])} && oldObject.metadata.uid == {_q(observer['uid'])} &&",
                        "has(object.metadata.annotations) &&",
                        f"{_q(transition)} in object.metadata.annotations && object.metadata.annotations[{_q(transition)}].matches('^[a-f0-9]{{64}}$')))",
                    ]
                )
            )
            continue
        spec = json.dumps(
            observer["daemonset_spec"], sort_keys=True, separators=(",", ":")
        )
        transition = "security.fs2.nebius.ai/daemonset-transition-sha256"
        choices.append(
            " ".join(
                [
                    f"(request.namespace == {_q(observer['namespace'])} &&",
                    f"request.name == {_q(observer['name'])} &&",
                    f"({_identity_cel(observer['owner_identity'])}) &&",
                    "((request.operation == 'UPDATE' &&",
                    f"object.metadata.uid == {_q(observer['uid'])} && oldObject.metadata.uid == {_q(observer['uid'])} &&",
                    f"object.spec == {spec} && oldObject.spec == {spec}) ||",
                    "(request.operation == 'CREATE' &&",
                    f"object.spec == {spec} && has(object.metadata.annotations) &&",
                    f"{_q(transition)} in object.metadata.annotations && object.metadata.annotations[{_q(transition)}].matches('^[a-f0-9]{{64}}$')) ||",
                    "(request.operation == 'DELETE' &&",
                    f"oldObject.metadata.uid == {_q(observer['uid'])} && oldObject.spec == {spec} &&",
                    f"has(oldObject.metadata.annotations) && {_q(transition)} in oldObject.metadata.annotations &&",
                    f"oldObject.metadata.annotations[{_q(transition)}].matches('^[a-f0-9]{{64}}$'))))",
                ]
            )
        )
    return "(" + " || ".join(choices) + ")"


def _observer_pod_cel(contract: dict[str, Any]) -> str:
    choices: list[str] = []
    controller = _identity_cel(contract["controller_identities"]["daemonset"])
    for observer in contract["observers"].values():
        labels = observer["daemonset_spec"]["template"]["metadata"]["labels"]
        pod_spec = observer["daemonset_spec"]["template"]["spec"]
        labels_json = json.dumps(labels, sort_keys=True, separators=(",", ":"))
        pod_spec_json = json.dumps(pod_spec, sort_keys=True, separators=(",", ":"))
        exact_workload = (
            "true"
            if observer["class"] == "critical-blanket-agent"
            else " ".join(
                [
                    f"{labels_json}.all(key, value, key in object.metadata.labels && object.metadata.labels[key] == value) &&",
                    f"object.metadata.labels.all(key, value, (key in {labels_json} && {labels_json}[key] == value) ||",
                    "(key in ['controller-revision-hash','pod-template-generation'] && value != '')) &&",
                    f"object.spec == {pod_spec_json} &&",
                    "(!has(object.spec.nodeName) || object.spec.nodeName == '') &&",
                    "(!has(object.spec.ephemeralContainers) || size(object.spec.ephemeralContainers) == 0)",
                ]
            )
        )
        choices.append(
            " ".join(
                [
                    f"(request.namespace == {_q(observer['namespace'])} && ({controller}) &&",
                    "request.operation == 'CREATE' && size(object.metadata.ownerReferences) == 1 &&",
                    "object.metadata.ownerReferences[0].apiVersion == 'apps/v1' && object.metadata.ownerReferences[0].kind == 'DaemonSet' &&",
                    f"object.metadata.ownerReferences[0].name == {_q(observer['name'])} && object.metadata.ownerReferences[0].uid == {_q(observer['uid'])} &&",
                    "object.metadata.ownerReferences[0].controller == true && object.metadata.ownerReferences[0].blockOwnerDeletion == true &&",
                    f"({exact_workload}))",
                ]
            )
        )
    return "(" + " || ".join(choices) + ")"


def render(contract: object) -> dict[str, str]:
    value = validate_contract(contract)
    result = {
        "pod_target_cel": _target_cel(value, "object.spec"),
        "old_pod_target_cel": _target_cel(value, "oldObject.spec"),
        "template_target_cel": _target_cel(value, "object.spec.template.spec"),
        "old_template_target_cel": _target_cel(value, "oldObject.spec.template.spec"),
        "cronjob_target_cel": _target_cel(value, "object.spec.jobTemplate.spec.template.spec"),
        "old_cronjob_target_cel": _target_cel(
            value, "oldObject.spec.jobTemplate.spec.template.spec"
        ),
        "observer_daemonset_allow_cel": _observer_daemonset_cel(value),
        "observer_pod_allow_cel": _observer_pod_cel(value),
        "contract_sha256": digest(value),
    }
    return result


def _spec(request: dict[str, Any], *, old: bool = False) -> dict[str, Any]:
    source = request.get("old_object" if old else "object") or {}
    resource = request.get("resource")
    if resource == "pods":
        return source.get("spec", {})
    if resource == "cronjobs":
        return source.get("spec", {}).get("jobTemplate", {}).get("spec", {}).get(
            "template", {}
        ).get("spec", {})
    return source.get("spec", {}).get("template", {}).get("spec", {})


def _requirement_matches(requirement: object, attributes: dict[str, str]) -> bool:
    if not isinstance(requirement, dict):
        return False
    key = requirement.get("key")
    operator = requirement.get("operator")
    values = requirement.get("values")
    if not isinstance(key, str) or not isinstance(operator, str):
        return False
    present = key in attributes
    if operator == "In":
        return isinstance(values, list) and present and attributes[key] in values
    if operator == "NotIn":
        return isinstance(values, list) and (
            not present or attributes[key] not in values
        )
    if operator == "Exists":
        return present
    if operator == "DoesNotExist":
        return not present
    if operator in {"Gt", "Lt"}:
        if (
            not present
            or not isinstance(values, list)
            or len(values) != 1
            or not isinstance(values[0], str)
            or not re.fullmatch(r"-?[0-9]+", attributes[key])
            or not re.fullmatch(r"-?[0-9]+", values[0])
        ):
            return False
        observed = int(attributes[key])
        threshold = int(values[0])
        return observed > threshold if operator == "Gt" else observed < threshold
    return False


def _term_matches(term: object, *, labels: dict[str, str], node_name: str) -> bool:
    if not isinstance(term, dict):
        return False
    expressions = term.get("matchExpressions", [])
    fields = term.get("matchFields", [])
    if not isinstance(expressions, list) or not isinstance(fields, list):
        return False
    if not expressions and not fields:
        return False
    return all(_requirement_matches(item, labels) for item in expressions) and all(
        _requirement_matches(item, {"metadata.name": node_name}) for item in fields
    )


def _constraints_match(
    spec: dict[str, Any], *, labels: dict[str, str], node_name: str
) -> bool:
    selector = spec.get("nodeSelector", {})
    if not isinstance(selector, dict) or any(
        not isinstance(key, str)
        or not isinstance(expected, str)
        or labels.get(key) != expected
        for key, expected in selector.items()
    ):
        return False
    affinity = spec.get("affinity")
    if affinity is None:
        return True
    if not isinstance(affinity, dict):
        return False
    node_affinity = affinity.get("nodeAffinity")
    if node_affinity is None:
        return True
    if not isinstance(node_affinity, dict):
        return False
    required = node_affinity.get("requiredDuringSchedulingIgnoredDuringExecution")
    if required is None:
        return True
    if not isinstance(required, dict):
        return False
    terms = required.get("nodeSelectorTerms")
    return isinstance(terms, list) and any(
        _term_matches(term, labels=labels, node_name=node_name) for term in terms
    )


def _tolerates_protected_taint(spec: dict[str, Any], contract: dict[str, Any]) -> bool:
    tolerations = spec.get("tolerations")
    if not isinstance(tolerations, list):
        return False
    for toleration in tolerations:
        if not isinstance(toleration, dict) or toleration.get("effect") not in {
            None,
            "",
            contract["taint_effect"],
        }:
            continue
        operator = toleration.get("operator")
        key = toleration.get("key")
        if operator == "Exists" and key in {None, "", contract["taint_key"]}:
            return True
        if (
            operator in {None, "", "Equal"}
            and key == contract["taint_key"]
            and toleration.get("value") == contract["taint_value"]
        ):
            return True
    return False


def targets_lane(spec: object, contract: object) -> bool:
    value = validate_contract(contract)
    if not isinstance(spec, dict):
        return False
    node_name = value["protected_node_names"][0]
    direct_name = spec.get("nodeName")
    if direct_name not in {None, ""}:
        return direct_name == node_name
    # The taint key is a stable provider-provisioning identity.  Any exact-key
    # or blanket toleration can schedule onto the lane regardless of selectors
    # that may later change, so admission conservatively guards it.  Exact
    # critical DaemonSet identities are the only availability exceptions.
    return _tolerates_protected_taint(spec, value)


def _request_identity_matches(
    request: dict[str, Any], identity: dict[str, Any]
) -> bool:
    return (
        request.get("username") == identity["username"]
        and request.get("uid") == identity["uid"]
        and sorted(request.get("groups", [])) == identity["groups"]
    )


def request_targets_lane(request: dict[str, Any], contract: object) -> bool:
    operation = request.get("operation")
    current = operation != "DELETE" and targets_lane(_spec(request), contract)
    previous = operation in {"UPDATE", "DELETE"} and targets_lane(
        _spec(request, old=True), contract
    )
    return current or previous


def _observer_for_request(
    request: dict[str, Any], contract: dict[str, Any]
) -> dict[str, Any] | None:
    namespace = request.get("namespace")
    obj = (
        request.get("old_object")
        if request.get("operation") == "DELETE"
        else request.get("object")
    ) or {}
    metadata = obj.get("metadata") or {}
    for observer in contract["observers"].values():
        if namespace == observer["namespace"] and metadata.get("name") == observer["name"]:
            return observer
    return None


def _snapshot_metadata_valid(metadata: object) -> bool:
    if not isinstance(metadata, dict):
        return False
    annotations = metadata.get("annotations")
    if not isinstance(annotations, dict):
        return False
    return bool(
        re.fullmatch(
            r"s[0-9]{14}-[a-f0-9]{12}",
            str(
                annotations.get(
                    "security.fs2.nebius.ai/daemonset-snapshot-generation", ""
                )
            ),
        )
        and re.fullmatch(
            r"[a-f0-9]{64}",
            str(
                annotations.get(
                    "security.fs2.nebius.ai/daemonset-snapshot-sha256", ""
                )
            ),
        )
    )


def _transition_metadata_valid(metadata: object) -> bool:
    if not isinstance(metadata, dict):
        return False
    annotations = metadata.get("annotations")
    return bool(
        isinstance(annotations, dict)
        and re.fullmatch(
            r"[a-f0-9]{64}",
            str(
                annotations.get(
                    "security.fs2.nebius.ai/daemonset-transition-sha256", ""
                )
            ),
        )
    )


def successor_allows(request: dict[str, Any], contract: object) -> bool:
    value = validate_contract(contract)
    if not request_targets_lane(request, value):
        return True
    if request.get("operation") == "DELETE" and request.get("resource") != "daemonsets":
        return False
    if request.get("storage_contract") is True:
        return True
    if request.get("resource") == "pods/binding":
        return _request_identity_matches(
            request, value["controller_identities"]["scheduler"]
        )
    observer = _observer_for_request(request, value)
    obj = request.get("object") or {}
    metadata = obj.get("metadata") or {}
    if request.get("resource") == "daemonsets" and observer is not None:
        old = request.get("old_object") or {}
        operation = request.get("operation")
        if not _request_identity_matches(request, observer["owner_identity"]):
            return False
        if observer["class"] == "critical-blanket-agent":
            if operation != "UPDATE" or not _transition_metadata_valid(metadata):
                return False
            old_metadata = old.get("metadata") or {}
            return bool(
                metadata.get("uid") == observer["uid"]
                and old_metadata.get("uid") == observer["uid"]
            )
        if operation == "CREATE":
            return bool(
                obj.get("spec") == observer["daemonset_spec"]
                and _transition_metadata_valid(metadata)
            )
        if operation == "DELETE":
            old_metadata = old.get("metadata") or {}
            return bool(
                old_metadata.get("uid") == observer["uid"]
                and old.get("spec") == observer["daemonset_spec"]
                and _transition_metadata_valid(old_metadata)
            )
        return bool(
            operation == "UPDATE"
            and metadata.get("uid") == observer["uid"]
            and (old.get("metadata") or {}).get("uid") == observer["uid"]
            and obj.get("spec") == observer["daemonset_spec"]
            and old.get("spec") == observer["daemonset_spec"]
        )
    if request.get("resource") == "pods":
        owners = metadata.get("ownerReferences")
        if not isinstance(owners, list) or len(owners) != 1:
            return False
        owner = owners[0]
        for candidate in value["observers"].values():
            if (
                request.get("namespace") == candidate["namespace"]
                and _request_identity_matches(
                    request, value["controller_identities"]["daemonset"]
                )
                and request.get("operation") == "CREATE"
                and owner
                == {
                    "apiVersion": "apps/v1",
                    "kind": "DaemonSet",
                    "name": candidate["name"],
                    "uid": candidate["uid"],
                    "controller": True,
                    "blockOwnerDeletion": True,
                }
            ):
                if candidate["class"] == "critical-blanket-agent":
                    # Every append-only ordinary policy delegates the mutable
                    # spec to the separately owned signed transition fence.
                    # Pinning a generation-local spec here would make retained
                    # Deny bindings conjunctively deadlock the next upgrade.
                    return True
                required = candidate["daemonset_spec"]["template"]["metadata"]["labels"]
                observed = metadata.get("labels")
                if not isinstance(observed, dict):
                    return False
                dynamic = {"controller-revision-hash", "pod-template-generation"}
                labels_match = all(
                    observed.get(key) == val for key, val in required.items()
                ) and all(
                    key in required or (key in dynamic and isinstance(val, str) and val)
                    for key, val in observed.items()
                )
                expected_spec = candidate["daemonset_spec"]["template"]["spec"]
                actual_spec = obj.get("spec") or {}
                spec_matches = actual_spec == expected_spec
                no_pivots = actual_spec.get("nodeName") in {None, ""} and actual_spec.get(
                    "ephemeralContainers"
                ) in (None, [], ())
                return labels_match and spec_matches and no_pivots
    return False


def predecessor_allows(request: dict[str, Any], predecessor: object) -> bool:
    """Model the retained predecessor's blanket kube-system Pod exception."""
    value = validate_contract(predecessor)
    if not request_targets_lane(request, value):
        return True
    if request.get("storage_contract") is True:
        return True
    obj = request.get("object") or {}
    metadata = obj.get("metadata") or {}
    owners = metadata.get("ownerReferences")
    spec = obj.get("spec") or {}
    blanket = any(
        isinstance(item, dict)
        and item.get("key") in {None, ""}
        and item.get("operator") == "Exists"
        for item in spec.get("tolerations", [])
    )
    return bool(
        request.get("resource") == "pods"
        and request.get("namespace") == "kube-system"
        and request.get("operation") == "CREATE"
        and _request_identity_matches(
            request, value["controller_identities"]["daemonset"]
        )
        and isinstance(owners, list)
        and len(owners) == 1
        and owners[0].get("kind") == "DaemonSet"
        and owners[0].get("controller") is True
        and owners[0].get("blockOwnerDeletion") is True
        and spec.get("nodeName") in {None, ""}
        and blanket
    )


def conjunction_allows(
    request: dict[str, Any], *, retained: list[dict[str, Any]], successor: dict[str, Any]
) -> bool:
    return all(predecessor_allows(request, policy) for policy in retained) and successor_allows(
        request, successor
    )


def main() -> None:
    query = json.load(sys.stdin)
    if not isinstance(query, dict) or set(query) != {"contract_json"}:
        raise ValueError("external query fields differ")
    contract = json.loads(_string(query.get("contract_json"), "contract_json"))
    json.dump(render(contract), sys.stdout, sort_keys=True, separators=(",", ":"))


if __name__ == "__main__":
    main()
