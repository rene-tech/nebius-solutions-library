"""Canonical per-subject Kubernetes RBAC effective-authority checks."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


# Every capability here can cross the customer-storage credential, workload,
# node, admission, or identity boundary. Read-only ordinary API discovery is
# intentionally absent; wildcard rules still expand against every target.
DANGEROUS_TARGETS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("", "secrets", ("get", "list", "watch", "create", "update", "patch", "delete", "deletecollection")),
    ("", "configmaps", ("create", "update", "patch", "delete", "deletecollection")),
    ("", "pods", ("create", "update", "patch", "delete", "deletecollection")),
    ("", "pods/exec", ("get", "create")),
    ("", "pods/attach", ("get", "create")),
    ("", "pods/portforward", ("get", "create")),
    ("", "pods/ephemeralcontainers", ("update", "patch")),
    ("", "pods/binding", ("create",)),
    ("policy", "pods/eviction", ("create",)),
    ("", "serviceaccounts", ("create", "update", "patch", "delete", "deletecollection")),
    ("", "serviceaccounts/token", ("create",)),
    ("", "nodes", ("get", "list", "watch", "create", "update", "patch", "delete", "deletecollection")),
    ("", "nodes/proxy", ("get", "create", "update", "patch", "delete")),
    ("", "nodes/status", ("get", "update", "patch")),
    ("", "namespaces", ("create", "update", "patch", "delete", "deletecollection")),
    ("", "users", ("impersonate",)),
    ("", "groups", ("impersonate",)),
    ("", "serviceaccounts", ("impersonate",)),
    ("authentication.k8s.io", "uids", ("impersonate",)),
    ("authentication.k8s.io", "userextras", ("impersonate",)),
    ("authentication.k8s.io", "tokenreviews", ("create",)),
    ("authorization.k8s.io", "subjectaccessreviews", ("create",)),
    ("apps", "deployments", ("create", "update", "patch", "delete", "deletecollection")),
    ("apps", "replicasets", ("create", "update", "patch", "delete", "deletecollection")),
    ("apps", "daemonsets", ("create", "update", "patch", "delete", "deletecollection")),
    ("apps", "statefulsets", ("create", "update", "patch", "delete", "deletecollection")),
    ("batch", "jobs", ("create", "update", "patch", "delete", "deletecollection")),
    ("batch", "cronjobs", ("create", "update", "patch", "delete", "deletecollection")),
    ("networking.k8s.io", "networkpolicies", ("create", "update", "patch", "delete", "deletecollection")),
    ("rbac.authorization.k8s.io", "roles", ("create", "update", "patch", "delete", "deletecollection", "bind", "escalate")),
    ("rbac.authorization.k8s.io", "rolebindings", ("create", "update", "patch", "delete", "deletecollection")),
    ("rbac.authorization.k8s.io", "clusterroles", ("create", "update", "patch", "delete", "deletecollection", "bind", "escalate")),
    ("rbac.authorization.k8s.io", "clusterrolebindings", ("create", "update", "patch", "delete", "deletecollection")),
    ("admissionregistration.k8s.io", "validatingadmissionpolicies", ("create", "update", "patch", "delete", "deletecollection")),
    ("admissionregistration.k8s.io", "validatingadmissionpolicybindings", ("create", "update", "patch", "delete", "deletecollection")),
    ("admissionregistration.k8s.io", "validatingwebhookconfigurations", ("create", "update", "patch", "delete", "deletecollection")),
    ("admissionregistration.k8s.io", "mutatingwebhookconfigurations", ("create", "update", "patch", "delete", "deletecollection")),
    ("certificates.k8s.io", "certificatesigningrequests", ("create", "update", "patch", "delete", "deletecollection", "approve")),
    ("certificates.k8s.io", "signers", ("approve", "sign")),
)


def _matches_resource(pattern: str, resource: str) -> bool:
    return (
        pattern == "*"
        or pattern == resource
        or (pattern.endswith("/*") and resource.startswith(f"{pattern[:-2]}/"))
    )


def _rule_capabilities(rule: dict[str, Any], scope: str) -> list[str]:
    api_groups = rule.get("apiGroups", [""])
    resources = rule.get("resources", [])
    verbs = rule.get("verbs", [])
    resource_names = rule.get("resourceNames", [])
    if (
        not isinstance(api_groups, list)
        or not isinstance(resources, list)
        or not isinstance(verbs, list)
        or not isinstance(resource_names, list)
        or any(not isinstance(value, str) for value in (*api_groups, *resources, *verbs, *resource_names))
    ):
        raise ValueError("Kubernetes RBAC rule fields are malformed")
    names = ",".join(sorted(set(resource_names))) if resource_names else "*"
    capabilities: list[str] = []
    for api_group, resource, target_verbs in DANGEROUS_TARGETS:
        if not ("*" in api_groups or api_group in api_groups):
            continue
        if not any(_matches_resource(pattern, resource) for pattern in resources):
            continue
        for verb in target_verbs:
            if "*" in verbs or verb in verbs:
                capabilities.append(
                    f"{scope}|{api_group or 'core'}|{resource}|{verb}|names={names}"
                )
    non_resource_urls = rule.get("nonResourceURLs", [])
    if not isinstance(non_resource_urls, list) or any(
        not isinstance(value, str) for value in non_resource_urls
    ):
        raise ValueError("Kubernetes non-resource RBAC rule is malformed")
    for url in sorted(set(non_resource_urls)):
        for verb in sorted(set(verbs)):
            if verb in {"*", "post", "put", "patch", "delete"}:
                capabilities.append(f"{scope}|nonresource|{url}|{verb}|names=*")
    return capabilities


def subject_authority(
    effective_authority: list[dict[str, Any]],
    *,
    kind: str,
    namespace: str,
    name: str,
    groups: list[str],
) -> tuple[str, list[str]]:
    """Return exact authority digest and expanded dangerous capability set."""

    selected: list[dict[str, Any]] = []
    for entry in effective_authority:
        subject = entry.get("subject")
        if not isinstance(subject, dict):
            raise ValueError("Kubernetes effective-authority subject is malformed")
        direct = subject == {"kind": kind, "namespace": namespace, "name": name}
        inherited = (
            subject.get("kind") == "Group"
            and subject.get("namespace") == ""
            and subject.get("name") in groups
        )
        if direct or inherited:
            selected.append(entry)
    selected.sort(
        key=lambda item: (
            item["subject"]["kind"],
            item["subject"]["namespace"],
            item["subject"]["name"],
            item["binding"]["kind"],
            item["binding"]["namespace"],
            item["binding"]["name"],
        )
    )
    capabilities: list[str] = []
    for entry in selected:
        rules = entry.get("rules")
        scope = entry.get("scope")
        if not isinstance(rules, list) or not isinstance(scope, str) or not scope:
            raise ValueError("Kubernetes effective-authority rule closure is malformed")
        for rule in rules:
            if not isinstance(rule, dict):
                raise ValueError("Kubernetes effective-authority rule is malformed")
            capabilities.extend(_rule_capabilities(rule, scope))
    return hashlib.sha256(_canonical(selected)).hexdigest(), sorted(set(capabilities))


def deterministic_groups(*, kind: str, namespace: str, name: str) -> list[str]:
    """Return Kubernetes-defined groups without accepting caller membership claims."""

    if kind == "ServiceAccount":
        if not namespace or not name:
            raise ValueError("ServiceAccount identity is incomplete")
        return sorted(
            {
                "system:authenticated",
                "system:serviceaccounts",
                f"system:serviceaccounts:{namespace}",
            }
        )
    if kind == "User":
        if not name.startswith("system:"):
            raise ValueError("only Kubernetes-native system Users are deterministic")
        if name == "system:anonymous":
            return ["system:unauthenticated"]
        return ["system:authenticated"]
    if kind == "Group":
        if not name.startswith("system:"):
            raise ValueError("only Kubernetes-native system Groups are deterministic")
        return []
    raise ValueError("Kubernetes subject kind is unsupported")


CONTROLLER_ALLOWANCES: dict[str, frozenset[tuple[str, str, str]]] = {
    "deployment": frozenset(
        ("apps", "replicasets", verb)
        for verb in ("create", "update", "patch", "delete", "deletecollection")
    ),
    "replicaset": frozenset(
        ("core", "pods", verb)
        for verb in ("create", "update", "patch", "delete", "deletecollection")
    ),
    "daemonset": frozenset(
        ("core", "pods", verb)
        for verb in ("create", "update", "patch", "delete", "deletecollection")
    ),
    "scheduler": frozenset(
        {
            ("core", "pods/binding", "create"),
            ("core", "nodes", "get"),
            ("core", "nodes", "list"),
            ("core", "nodes", "watch"),
        }
    ),
}

CONTROLLER_ROLES = frozenset(CONTROLLER_ALLOWANCES)


def _controller_capability_allowed(capability: str, role: str) -> bool:
    fields = capability.split("|")
    if len(fields) != 5 or not fields[4].startswith("names="):
        raise ValueError("expanded Kubernetes capability is malformed")
    scope, api_group, resource, verb, names = fields
    return (
        scope == "*"
        and names == "names=*"
        and (api_group, resource, verb) in CONTROLLER_ALLOWANCES[role]
    )


def reject_unapproved_dangerous(
    dangerous: list[str],
    *,
    controller_role: str | None = None,
    explicitly_allowed: set[str] | None = None,
) -> None:
    """Reject semantic authority unless source policy independently mediates it."""

    if controller_role is not None and controller_role not in CONTROLLER_ALLOWANCES:
        raise ValueError("Kubernetes controller role is unsupported")
    allowed = explicitly_allowed or set()
    unexpected = [
        capability
        for capability in dangerous
        if capability not in allowed
        and not (
            controller_role is not None
            and _controller_capability_allowed(capability, controller_role)
        )
    ]
    if unexpected:
        raise ValueError(
            f"independently derived dangerous Kubernetes authority is not mediated: {unexpected[0]}"
        )


def verify_subject_inventory(
    service_accounts: list[dict[str, Any]],
    system_subjects: list[dict[str, Any]],
    effective_authority: list[dict[str, Any]],
    *,
    controller_identities: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Derive groups/rules independently and reject unmediated authority."""

    controllers = controller_identities or {}
    if controller_identities is not None and set(controllers) != CONTROLLER_ROLES:
        raise ValueError("all observed Kubernetes controller identities are required")
    controller_role_by_subject: dict[tuple[str, str, str], str] = {}
    for role, identity in controllers.items():
        if not isinstance(identity, dict) or set(identity) != {
            "kind",
            "namespace",
            "name",
            "username",
            "uid",
            "groups",
            "audit_evidence_sha256",
        }:
            raise ValueError(f"{role} controller identity fields differ")
        kind = identity.get("kind")
        namespace = identity.get("namespace")
        name = identity.get("name")
        username = identity.get("username")
        uid = identity.get("uid")
        groups = identity.get("groups")
        if kind == "ServiceAccount" and namespace == "kube-system":
            expected_username = f"system:serviceaccount:{namespace}:{name}"
            declared = next(
                (
                    subject
                    for subject in service_accounts
                    if subject.get("namespace") == namespace
                    and subject.get("name") == name
                ),
                None,
            )
        elif kind == "User" and namespace == "" and str(name).startswith("system:"):
            expected_username = name
            declared = next(
                (
                    subject
                    for subject in system_subjects
                    if subject.get("kind") == "User" and subject.get("name") == name
                ),
                None,
            )
        else:
            raise ValueError(
                f"{role} controller must be an audited kube-system ServiceAccount or native system User"
            )
        if (
            declared is None
            or username != expected_username
            or not isinstance(uid, str)
            or not uid
            or declared.get("uid") != uid
            or groups
            != deterministic_groups(kind=kind, namespace=namespace, name=name)
            or declared.get("groups") != groups
            or not isinstance(identity.get("audit_evidence_sha256"), str)
            or len(identity["audit_evidence_sha256"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in identity["audit_evidence_sha256"]
            )
        ):
            raise ValueError(
                f"{role} controller identity is not bound to its audited live subject"
            )
        subject_key = (kind, namespace, name)
        if subject_key in controller_role_by_subject:
            raise ValueError("controller roles cannot alias one authenticated subject")
        controller_role_by_subject[subject_key] = role

    declarations: list[tuple[dict[str, Any], str, str, str]] = []
    for subject in service_accounts:
        declarations.append(
            (subject, "ServiceAccount", subject["namespace"], subject["name"])
        )
    for subject in system_subjects:
        declarations.append((subject, subject["kind"], "", subject["name"]))
    for declaration, kind, namespace, name in declarations:
        groups = deterministic_groups(
            kind=kind,
            namespace=namespace,
            name=name,
        )
        if declaration.get("groups") != groups:
            raise ValueError(
                f"signed {kind} {namespace}/{name} groups are not deterministic"
            )
        authority_sha256, dangerous = subject_authority(
            effective_authority,
            kind=kind,
            namespace=namespace,
            name=name,
            groups=groups,
        )
        if declaration.get("effective_authority_sha256") != authority_sha256:
            raise ValueError(
                f"signed {kind} {namespace}/{name} effective authority differs"
            )
        if declaration.get("dangerous_permissions") != dangerous:
            raise ValueError(
                f"signed {kind} {namespace}/{name} dangerous authority differs"
            )
        reject_unapproved_dangerous(
            dangerous,
            controller_role=controller_role_by_subject.get((kind, namespace, name)),
        )
