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
    ("authorization.k8s.io", "selfsubjectaccessreviews", ("create",)),
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


def verify_subject_inventory(
    service_accounts: list[dict[str, Any]],
    system_subjects: list[dict[str, Any]],
    effective_authority: list[dict[str, Any]],
) -> None:
    """Require each signed non-human subject to equal its live effective rules."""

    declarations: list[tuple[dict[str, Any], str, str, str, list[str]]] = []
    for subject in service_accounts:
        declarations.append(
            (subject, "ServiceAccount", subject["namespace"], subject["name"], subject["groups"])
        )
    for subject in system_subjects:
        groups = subject["groups"] if subject["kind"] == "User" else [subject["name"]]
        declarations.append((subject, subject["kind"], "", subject["name"], groups))
    for declaration, kind, namespace, name, groups in declarations:
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
