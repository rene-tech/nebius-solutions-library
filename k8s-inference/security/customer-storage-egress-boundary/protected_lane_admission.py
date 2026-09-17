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

LANE_ROLES = {"gpu-allocation-observer", "otel-node"}
NODE_AGENT_ROLES = {"filesystem-csi", "prometheus-node-exporter", "retained-otel-node"}
ROLES = LANE_ROLES | NODE_AGENT_ROLES
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
        "daemonset_controller_username",
        "scheduler_username",
        "observers",
        "observer_inventory_sha256",
    }
    if set(value) != expected:
        raise ValueError("protected-lane contract fields differ")
    if value.get("schema") != "fs2-serve.nebius.ai/protected-lane-admission/v2":
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
    if value.get("daemonset_controller_username") != "system:controller:daemon-set-controller":
        raise ValueError("DaemonSet controller identity differs")
    if value.get("scheduler_username") != "system:kube-scheduler":
        raise ValueError("scheduler identity differs")

    observers = value.get("observers")
    if not isinstance(observers, dict) or set(observers) != ROLES:
        raise ValueError("exact lane-observer and retained node-agent inventory is required")
    seen: set[tuple[str, str] | str] = set()
    for role, observer in observers.items():
        if not isinstance(observer, dict) or set(observer) != {
            "namespace",
            "name",
            "uid",
            "owner_username",
            "daemonset_spec",
            "daemonset_spec_sha256",
        }:
            raise ValueError(f"{role} observer fields differ")
        namespace = _string(observer.get("namespace"), f"{role} namespace")
        name = _string(observer.get("name"), f"{role} name")
        uid = _string(observer.get("uid"), f"{role} UID")
        owner = _string(observer.get("owner_username"), f"{role} owner")
        spec = observer.get("daemonset_spec")
        if role in LANE_ROLES and namespace != "kube-system":
            raise ValueError(f"{role} additive compatibility observer must be in kube-system")
        if role in LANE_ROLES and name != f"fs2-{role}-{lane_id[-12:]}":
            raise ValueError(f"{role} observer name is not lane-bound")
        if not UID_RE.fullmatch(uid):
            raise ValueError(f"{role} UID is invalid")
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
        if role in LANE_ROLES:
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
        if owner.startswith("system:"):
            raise ValueError(f"{role} owner must be a separately inventoried release identity")

    if digest(observers) != value.get("observer_inventory_sha256"):
        raise ValueError("observer inventory digest differs")
    return value


def _q(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _target_cel(contract: dict[str, Any], path: str) -> str:
    key = _q(contract["taint_key"])
    value = _q(contract["taint_value"])
    effect = _q(contract["taint_effect"])
    node_names = json.dumps(contract["protected_node_names"], separators=(",", ":"))
    lane_affinity = " ".join(
        [
            f"(has({path}.affinity) && has({path}.affinity.nodeAffinity) &&",
            f"has({path}.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution) &&",
            f"has({path}.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms) &&",
            f"{path}.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms.exists(term,",
            "has(term.matchExpressions) && term.matchExpressions.exists(requirement,",
            f"requirement.key == {key} && (",
            f"(requirement.operator == 'In' && has(requirement.values) && {value} in requirement.values) ||",
            "requirement.operator == 'Exists' ||",
            f"(requirement.operator == 'NotIn' && (!has(requirement.values) || !({value} in requirement.values)))))))",
        ]
    )
    direct_affinity = " ".join(
        [
            f"(has({path}.affinity) && has({path}.affinity.nodeAffinity) &&",
            f"has({path}.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution) &&",
            f"has({path}.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms) &&",
            f"{path}.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms.exists(term,",
            "has(term.matchFields) && term.matchFields.exists(requirement,",
            "requirement.key == 'metadata.name' && requirement.operator == 'In' &&",
            f"has(requirement.values) && requirement.values.exists(nodeName, nodeName in {node_names}))))",
        ]
    )
    keyed_toleration = " ".join(
        [
            f"(has({path}.tolerations) && {path}.tolerations.exists(toleration,",
            f"has(toleration.key) && toleration.key == {key} &&",
            f"(!has(toleration.effect) || toleration.effect == '' || toleration.effect == {effect}) && (",
            "(has(toleration.operator) && toleration.operator == 'Exists') ||",
            "((!has(toleration.operator) || toleration.operator == '' || toleration.operator == 'Equal') &&",
            f"has(toleration.value) && toleration.value == {value}))))",
        ]
    )
    blanket_toleration = " ".join(
        [
            f"(has({path}.tolerations) && {path}.tolerations.exists(toleration,",
            "(!has(toleration.key) || toleration.key == '') &&",
            "has(toleration.operator) && toleration.operator == 'Exists' &&",
            f"(!has(toleration.effect) || toleration.effect == '' || toleration.effect == {effect})))",
        ]
    )
    lane_selector = " ".join(
        [
            f"(has({path}.nodeSelector) && {key} in {path}.nodeSelector &&",
            f"{path}.nodeSelector[{key}] == {value})",
        ]
    )
    return " ".join(
        [
            "(",
            keyed_toleration,
            "||",
            f"(has({path}.nodeName) && {path}.nodeName in {node_names}) ||",
            f"((({lane_selector}) || ({lane_affinity}) || ({direct_affinity})) &&",
            f"({blanket_toleration})))",
        ]
    )


def _observer_daemonset_cel(contract: dict[str, Any]) -> str:
    choices: list[str] = []
    for observer in contract["observers"].values():
        choices.append(
            " ".join(
                [
                    f"(request.namespace == {_q(observer['namespace'])} &&",
                    f"request.name == {_q(observer['name'])} &&",
                    f"request.userInfo.username == {_q(observer['owner_username'])} &&",
                    "request.operation == 'UPDATE' &&",
                    f"object.metadata.uid == {_q(observer['uid'])} && oldObject.metadata.uid == {_q(observer['uid'])} &&",
                    f"object.spec == {json.dumps(observer['daemonset_spec'], sort_keys=True, separators=(',', ':'))} &&",
                    f"oldObject.spec == {json.dumps(observer['daemonset_spec'], sort_keys=True, separators=(',', ':'))})",
                ]
            )
        )
    return "(" + " || ".join(choices) + ")"


def _observer_pod_cel(contract: dict[str, Any]) -> str:
    choices: list[str] = []
    controller = _q(contract["daemonset_controller_username"])
    for observer in contract["observers"].values():
        labels = observer["daemonset_spec"]["template"]["metadata"]["labels"]
        pod_spec = observer["daemonset_spec"]["template"]["spec"]
        labels_json = json.dumps(labels, sort_keys=True, separators=(",", ":"))
        required_spec_fields = (
            "tolerations",
            "containers",
        )
        exact_spec = " && ".join(
            f"object.spec.{field} == {json.dumps(pod_spec[field], sort_keys=True, separators=(',', ':'))}"
            for field in required_spec_fields
        )
        optional_spec_fields = (
            "serviceAccountName",
            "automountServiceAccountToken",
            "nodeSelector",
            "initContainers",
            "volumes",
            "imagePullSecrets",
            "securityContext",
            "runtimeClassName",
            "priorityClassName",
            "dnsPolicy",
            "dnsConfig",
        )
        exact_optional = " && ".join(
            (
                f"object.spec.{field} == {json.dumps(pod_spec[field], sort_keys=True, separators=(',', ':'))}"
                if field in pod_spec
                else f"!has(object.spec.{field})"
            )
            for field in optional_spec_fields
        )
        choices.append(
            " ".join(
                [
                    f"(request.namespace == {_q(observer['namespace'])} && request.userInfo.username == {controller} &&",
                    "request.operation == 'CREATE' && size(object.metadata.ownerReferences) == 1 &&",
                    "object.metadata.ownerReferences[0].apiVersion == 'apps/v1' && object.metadata.ownerReferences[0].kind == 'DaemonSet' &&",
                    f"object.metadata.ownerReferences[0].name == {_q(observer['name'])} && object.metadata.ownerReferences[0].uid == {_q(observer['uid'])} &&",
                    "object.metadata.ownerReferences[0].controller == true && object.metadata.ownerReferences[0].blockOwnerDeletion == true &&",
                    f"{labels_json}.all(key, value, key in object.metadata.labels && object.metadata.labels[key] == value) &&",
                    f"object.metadata.labels.all(key, value, (key in {labels_json} && {labels_json}[key] == value) ||",
                    "(key in ['controller-revision-hash','pod-template-generation'] && value != '')) &&",
                    f"{exact_spec} && {exact_optional} &&",
                    "(!has(object.spec.nodeName) || object.spec.nodeName == '') &&",
                    "(!has(object.spec.hostNetwork) || object.spec.hostNetwork == false) &&",
                    "(!has(object.spec.hostPID) || object.spec.hostPID == false) &&",
                    "(!has(object.spec.hostIPC) || object.spec.hostIPC == false) &&",
                    "(!has(object.spec.shareProcessNamespace) || object.spec.shareProcessNamespace == false) &&",
                    "(!has(object.spec.ephemeralContainers) || size(object.spec.ephemeralContainers) == 0))",
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


def targets_lane(spec: object, contract: object) -> bool:
    value = validate_contract(contract)
    if not isinstance(spec, dict):
        return False
    if spec.get("nodeName") in value["protected_node_names"]:
        return True
    selector = spec.get("nodeSelector")
    lane_constraint = bool(
        isinstance(selector, dict)
        and selector.get(value["selector_key"]) == value["selector_value"]
    )
    required = (
        (spec.get("affinity") or {})
        .get("nodeAffinity", {})
        .get("requiredDuringSchedulingIgnoredDuringExecution", {})
    )
    terms = required.get("nodeSelectorTerms", []) if isinstance(required, dict) else []
    direct_affinity = False
    for term in terms if isinstance(terms, list) else []:
        if not isinstance(term, dict):
            continue
        for requirement in term.get("matchExpressions", []):
            if not isinstance(requirement, dict) or requirement.get("key") != value["selector_key"]:
                continue
            operator = requirement.get("operator")
            values = requirement.get("values", [])
            if (
                operator == "Exists"
                or (operator == "In" and value["selector_value"] in values)
                or (operator == "NotIn" and value["selector_value"] not in values)
            ):
                lane_constraint = True
        direct_affinity = direct_affinity or any(
            isinstance(requirement, dict)
            and requirement.get("key") == "metadata.name"
            and requirement.get("operator") == "In"
            and isinstance(requirement.get("values"), list)
            and any(
                node_name in value["protected_node_names"]
                for node_name in requirement["values"]
            )
            for requirement in term.get("matchFields", [])
        )
    tolerations = spec.get("tolerations")
    if not isinstance(tolerations, list):
        return False
    blanket = False
    for toleration in tolerations:
        if not isinstance(toleration, dict):
            continue
        if (
            toleration.get("key") == value["taint_key"]
            and toleration.get("effect") in {None, "", value["taint_effect"]}
            and (
                toleration.get("operator") == "Exists"
                or (
                    toleration.get("operator") in {None, "", "Equal"}
                    and toleration.get("value") == value["taint_value"]
                )
            )
        ):
            return True
        if (
            toleration.get("key") in {None, ""}
            and toleration.get("operator") == "Exists"
            and toleration.get("effect") in {None, "", value["taint_effect"]}
        ):
            blanket = True
    return blanket and (lane_constraint or direct_affinity)


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
    obj = request.get("object") or {}
    metadata = obj.get("metadata") or {}
    for observer in contract["observers"].values():
        if namespace == observer["namespace"] and metadata.get("name") == observer["name"]:
            return observer
    return None


def successor_allows(request: dict[str, Any], contract: object) -> bool:
    value = validate_contract(contract)
    if not request_targets_lane(request, value):
        return True
    if request.get("operation") == "DELETE":
        return False
    if request.get("storage_contract") is True:
        return True
    if request.get("resource") == "pods/binding":
        return request.get("username") == value["scheduler_username"]
    observer = _observer_for_request(request, value)
    obj = request.get("object") or {}
    metadata = obj.get("metadata") or {}
    if request.get("resource") == "daemonsets" and observer is not None:
        old = request.get("old_object") or {}
        return (
            request.get("operation") == "UPDATE"
            and request.get("username") == observer["owner_username"]
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
                and request.get("username") == value["daemonset_controller_username"]
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
                required_spec_fields = {
                    "tolerations",
                    "containers",
                }
                optional_spec_fields = {
                    "serviceAccountName",
                    "automountServiceAccountToken",
                    "nodeSelector",
                    "initContainers",
                    "volumes",
                    "imagePullSecrets",
                    "securityContext",
                    "runtimeClassName",
                    "priorityClassName",
                    "dnsPolicy",
                    "dnsConfig",
                }
                spec_matches = all(
                    actual_spec.get(field) == expected_spec.get(field)
                    for field in required_spec_fields
                ) and all(
                    actual_spec.get(field) == expected_spec.get(field)
                    for field in optional_spec_fields
                )
                no_pivots = (
                    actual_spec.get("nodeName") in {None, ""}
                    and actual_spec.get("hostNetwork") in {None, False}
                    and actual_spec.get("hostPID") in {None, False}
                    and actual_spec.get("hostIPC") in {None, False}
                    and actual_spec.get("shareProcessNamespace") in {None, False}
                    and actual_spec.get("ephemeralContainers") in (None, [], ())
                )
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
        and request.get("username") == value["daemonset_controller_username"]
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
