#!/usr/bin/env python3
"""Emit a complete, identity-bound SAI-07 effective-authority audit.

Unlike the retained v1 audit, this artifact contains every SSRR rule and every
SSAR decision.  Each returned rule is normalized to atomic authority and must
equal the complete source-owned profile contract. Unknown API groups, CRDs,
subresources, resource names, verbs, wildcard grants and non-resource URLs are
rejected even when they remain stable across two observations. This command is
read-only apart from Kubernetes review APIs, which do not mutate resources.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from audit_sai07_effective_authority import Client, AuditError, canonical

SCHEMA = "fs2-serve.nebius.ai/sai07-effective-authority-audit/v2"
MATRIX_VERSION = "sai07-authority-matrix-2026-09-17-v4"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
PROFILES = {
    "external-executor",
    "inactive-owner",
    "metadata-reader",
    "platform",
    "receipt-service-account",
    "token-issuer",
}
FIXED_PERSISTENT_VOLUMES = {
    "fs2-sai07-ref-bioir-boltz2",
    "fs2-sai07-ref-bioir-coverage",
    "fs2-sai07-ref-bioir-openfold",
    "fs2-sai07-ref-bioir-protenix",
    "fs2-sai07-ref-bioir-snapshot",
    "fs2-sai07-ref-snapshot-operations",
}
MUTATING = ("create", "update", "patch", "delete", "deletecollection")
READING = ("get", "list", "watch")

# (api group, resource, subresource).  Empty names deliberately test unbounded
# authority. Exact allowed edges are added separately and no broad grant may
# satisfy them.
CLUSTER_RESOURCES = (
    ("", "namespaces", ""),
    ("", "nodes", ""),
    ("", "persistentvolumes", ""),
    ("rbac.authorization.k8s.io", "clusterroles", ""),
    ("rbac.authorization.k8s.io", "clusterrolebindings", ""),
    ("admissionregistration.k8s.io", "validatingadmissionpolicies", ""),
    ("admissionregistration.k8s.io", "validatingadmissionpolicybindings", ""),
    ("admissionregistration.k8s.io", "validatingwebhookconfigurations", ""),
    ("admissionregistration.k8s.io", "mutatingwebhookconfigurations", ""),
    ("apiextensions.k8s.io", "customresourcedefinitions", ""),
    ("certificates.k8s.io", "certificatesigningrequests", ""),
    ("certificates.k8s.io", "certificatesigningrequests", "approval"),
    ("certificates.k8s.io", "signers", ""),
    ("authentication.k8s.io", "tokenreviews", ""),
    ("authentication.k8s.io", "selfsubjectreviews", ""),
    ("authorization.k8s.io", "subjectaccessreviews", ""),
    ("authorization.k8s.io", "selfsubjectaccessreviews", ""),
    ("authorization.k8s.io", "selfsubjectrulesreviews", ""),
)
NAMESPACED_RESOURCES = (
    ("", "secrets", ""),
    ("", "configmaps", ""),
    ("", "serviceaccounts", ""),
    ("", "serviceaccounts", "token"),
    ("", "pods", ""),
    ("", "pods", "exec"),
    ("", "pods", "attach"),
    ("", "pods", "portforward"),
    ("", "pods", "proxy"),
    ("", "pods", "log"),
    ("", "pods", "eviction"),
    ("", "podtemplates", ""),
    ("", "replicationcontrollers", ""),
    ("", "replicationcontrollers", "scale"),
    ("", "services", ""),
    ("", "services", "proxy"),
    ("", "persistentvolumeclaims", ""),
    ("apps", "daemonsets", ""),
    ("apps", "deployments", ""),
    ("apps", "deployments", "scale"),
    ("apps", "replicasets", ""),
    ("apps", "statefulsets", ""),
    ("apps", "statefulsets", "scale"),
    ("batch", "jobs", ""),
    ("batch", "cronjobs", ""),
    ("networking.k8s.io", "networkpolicies", ""),
    ("rbac.authorization.k8s.io", "roles", ""),
    ("rbac.authorization.k8s.io", "rolebindings", ""),
    ("jobset.x-k8s.io", "jobsets", ""),
    ("inference.fs2.nebius.ai", "modeldeployments", ""),
    ("keda.sh", "scaledobjects", ""),
)
# Kubernetes authorizes impersonation against two API groups.  User, group and
# ServiceAccount identities are core resources; only UID and user-extra keys
# are authentication.k8s.io resources.  Keep every edge explicit so a false
# negative cannot be hidden behind an invalid SSAR resource tuple.
IMPERSONATION = (
    ("", "users"),
    ("", "groups"),
    ("", "serviceaccounts"),
    ("authentication.k8s.io", "uids"),
    ("authentication.k8s.io", "userextras"),
)

# These are the exact read-only discovery URLs granted by the Kubernetes
# bootstrap system:discovery/system:public-info-viewer roles to authenticated
# identities. A prefix match would also admit arbitrary future/debug/metrics
# endpoints, so the SSRR closure compares these literal rule strings only.
DISCOVERY_NON_RESOURCE_RULES = frozenset(
    {
        ("get", "/api"),
        ("get", "/api/*"),
        ("get", "/apis"),
        ("get", "/apis/*"),
        ("get", "/healthz"),
        ("get", "/livez"),
        ("get", "/openapi"),
        ("get", "/openapi/*"),
        ("get", "/readyz"),
        ("get", "/version"),
        ("get", "/version/"),
    }
)


def check_id(verb: str, group: str, resource: str, subresource: str, namespace: str, name: str = "") -> str:
    api = group or "v1"
    target = resource + (f"/{subresource}" if subresource else "")
    scope = namespace or "_cluster"
    return f"{verb}:{api}:{target}:{scope}:{name or '*'}"


def expected_allowed(profile: str, namespaces: list[str], persistent_volume_names: list[str]) -> set[str]:
    result = {
        check_id("create", "authentication.k8s.io", "selfsubjectreviews", "", ""),
        check_id("create", "authorization.k8s.io", "selfsubjectaccessreviews", "", ""),
        check_id("create", "authorization.k8s.io", "selfsubjectrulesreviews", "", ""),
    }
    if profile == "external-executor":
        # The execution identity can read the exact signed object inventory,
        # create only the empty anchor Secret, and create/SSA one immutable
        # generation-addressed acknowledgement ConfigMap. Admission, whose
        # exact live object is part of the signed inventory, constrains those
        # otherwise name-unbounded CREATE/PATCH edges. It has no update,
        # delete, RBAC, admission, workload, proxy, token, or impersonation
        # authority.
        for verb in ("get", "list"):
            result.add(check_id(verb, "", "namespaces", "", ""))
        for group, resource in (
            ("rbac.authorization.k8s.io", "clusterroles"),
            ("rbac.authorization.k8s.io", "clusterrolebindings"),
            ("admissionregistration.k8s.io", "validatingadmissionpolicies"),
            ("admissionregistration.k8s.io", "validatingadmissionpolicybindings"),
        ):
            result.add(check_id("get", group, resource, "", ""))
        for name in persistent_volume_names:
            result.add(check_id("get", "", "persistentvolumes", "", "", name))
        readable = {
            ("", "configmaps"),
            ("", "serviceaccounts"),
            ("apps", "daemonsets"),
            ("networking.k8s.io", "networkpolicies"),
            ("rbac.authorization.k8s.io", "roles"),
            ("rbac.authorization.k8s.io", "rolebindings"),
        }
        for namespace in namespaces:
            for group, resource in readable:
                result.add(check_id("get", group, resource, "", namespace))
        result.update(
            {
                check_id("create", "", "configmaps", "", "fs2-system"),
                check_id("patch", "", "configmaps", "", "fs2-system"),
                check_id("create", "", "secrets", "", "fs2-system"),
                check_id("list", "", "secrets", "", "fs2-system"),
            }
        )
        return result
    if profile == "token-issuer":
        result.update(
            {
            check_id("create", "", "serviceaccounts", "token", "fs2-system", "fs2-pod-security-rollout-custodian"),
            check_id("create", "", "serviceaccounts", "token", "fs2-system", "fs2-pod-security-metadata-reader"),
            check_id("get", "", "namespaces", "", ""),
            check_id("list", "", "namespaces", "", ""),
            }
        )
        return result
    if profile == "receipt-service-account":
        result.update(
            {
            check_id("get", "", "configmaps", "", "fs2-system", "fs2-pod-security-rollout-ledger"),
            check_id("update", "", "configmaps", "", "fs2-system", "fs2-pod-security-rollout-ledger"),
            }
        )
        for namespace in namespaces:
            for group, resource in (
                ("", "configmaps"),
                ("", "serviceaccounts"),
                ("", "pods"),
                ("", "podtemplates"),
                ("", "replicationcontrollers"),
                ("", "persistentvolumeclaims"),
                ("apps", "daemonsets"),
                ("apps", "deployments"),
                ("apps", "replicasets"),
                ("apps", "statefulsets"),
                ("batch", "jobs"),
                ("batch", "cronjobs"),
                ("networking.k8s.io", "networkpolicies"),
                ("rbac.authorization.k8s.io", "roles"),
                ("rbac.authorization.k8s.io", "rolebindings"),
                ("jobset.x-k8s.io", "jobsets"),
                ("inference.fs2.nebius.ai", "modeldeployments"),
                ("keda.sh", "scaledobjects"),
            ):
                for verb in ("get", "list"):
                    result.add(check_id(verb, group, resource, "", namespace))
        for verb in ("get", "list"):
            result.add(check_id(verb, "", "namespaces", "", ""))
            result.add(check_id(verb, "admissionregistration.k8s.io", "validatingadmissionpolicies", "", ""))
            result.add(check_id(verb, "admissionregistration.k8s.io", "validatingadmissionpolicybindings", "", ""))
        for name in persistent_volume_names:
            result.add(check_id("get", "", "persistentvolumes", "", "", name))
        for name in (
            "fs2-pod-security-external-custody-audit",
            "fs2-pod-security-rollout-reader",
        ):
            result.add(check_id("get", "rbac.authorization.k8s.io", "clusterroles", "", "", name))
        for name in (
            "fs2-pod-security-external-custody-audit",
            "fs2-pod-security-rollout-custodian-reader",
            "fs2-pod-security-rollout-reader",
        ):
            result.add(check_id("get", "rbac.authorization.k8s.io", "clusterrolebindings", "", "", name))
        return result
    if profile == "metadata-reader":
        result.update(
            {
            check_id("list", "", "secrets", "", "fs2-models"),
            check_id("list", "", "secrets", "", "fs2-system"),
            }
        )
    return result


def expected_reviews(profile: str, namespaces: list[str], persistent_volume_names: list[str]) -> dict[str, bool]:
    allowed = expected_allowed(profile, namespaces, persistent_volume_names)
    identifiers: set[str] = set()
    for group, resource, subresource in CLUSTER_RESOURCES:
        for verb in (*READING, *MUTATING):
            identifiers.add(check_id(verb, group, resource, subresource, ""))
    for group, resource in IMPERSONATION:
        identifiers.add(check_id("impersonate", group, resource, "", ""))
    for resource in ("roles", "clusterroles"):
        for verb in ("bind", "escalate"):
            identifiers.add(check_id(verb, "rbac.authorization.k8s.io", resource, "", ""))
    for namespace in namespaces:
        for group, resource, subresource in NAMESPACED_RESOURCES:
            for verb in (*READING, *MUTATING):
                identifiers.add(check_id(verb, group, resource, subresource, namespace))
    for verb, group, resource, subresource, namespace, name in (
        ("create", "", "serviceaccounts", "token", "fs2-system", "fs2-pod-security-rollout-custodian"),
        ("create", "", "serviceaccounts", "token", "fs2-system", "fs2-pod-security-metadata-reader"),
        ("get", "", "configmaps", "", "fs2-system", "fs2-pod-security-rollout-ledger"),
        ("update", "", "configmaps", "", "fs2-system", "fs2-pod-security-rollout-ledger"),
        ("patch", "", "configmaps", "", "fs2-system", "fs2-pod-security-rollout-ledger"),
        ("delete", "", "configmaps", "", "fs2-system", "fs2-pod-security-rollout-ledger"),
        ("list", "", "secrets", "", "fs2-models", ""),
    ):
        identifiers.add(check_id(verb, group, resource, subresource, namespace, name))
    for name in persistent_volume_names:
        identifiers.add(check_id("get", "", "persistentvolumes", "", "", name))
    for resource, names in (
        ("clusterroles", ("fs2-pod-security-external-custody-audit", "fs2-pod-security-rollout-reader")),
        ("clusterrolebindings", ("fs2-pod-security-external-custody-audit", "fs2-pod-security-rollout-custodian-reader", "fs2-pod-security-rollout-reader")),
    ):
        for name in names:
            identifiers.add(check_id("get", "rbac.authorization.k8s.io", resource, "", "", name))
    return {identifier: identifier in allowed for identifier in sorted(identifiers)}


def _rule_vector(
    rule: dict[str, Any],
    key: str,
    label: str,
    *,
    required: bool = True,
    allow_empty_strings: bool = False,
) -> list[str]:
    value = rule.get(key)
    if value is None and not required:
        return []
    if (
        not isinstance(value, list)
        or (required and not value)
        or any(
            not isinstance(item, str) or (not item and not allow_empty_strings)
            for item in value
        )
        or len(value) != len(set(value))
    ):
        raise AuditError(f"{label}.{key} must be a nonempty unique string vector")
    return value


def _expected_resource_atoms(
    profile: str,
    namespace: str,
    namespaces: list[str],
    persistent_volume_names: list[str],
) -> set[tuple[str, str, str, str, str]]:
    atoms: set[tuple[str, str, str, str, str]] = set()
    for identifier in expected_allowed(profile, namespaces, persistent_volume_names):
        verb, api, target, scope, name = identifier.split(":", 4)
        if scope not in {"_cluster", namespace}:
            continue
        resource, separator, subresource = target.partition("/")
        atoms.add(
            (
                verb,
                "" if api == "v1" else api,
                resource,
                subresource if separator else "",
                "" if name == "*" else name,
            )
        )
    return atoms


def validate_exact_rule_closure(
    profile: str,
    namespace: str,
    namespaces: list[str],
    persistent_volume_names: list[str],
    status: object,
    expected_closure: object | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Reject every effective rule outside or missing from the exact profile.

    SelfSubjectRulesReview is the exhaustive discovery surface here. The
    separate SSAR matrix remains defense in depth for exact named denials, but
    no finite SSAR resource catalog is treated as proof that another API group,
    CRD, subresource or URL is absent.
    """

    if not isinstance(status, dict) or status.get("incomplete") is not False:
        raise AuditError(f"SelfSubjectRulesReview is incomplete in {namespace}")
    if set(status) - {
        "evaluationError",
        "incomplete",
        "nonResourceRules",
        "resourceRules",
    }:
        raise AuditError(f"SelfSubjectRulesReview has unknown status fields in {namespace}")
    if status.get("evaluationError") not in (None, ""):
        raise AuditError(f"SelfSubjectRulesReview evaluation failed in {namespace}")
    resource_rules = status.get("resourceRules")
    non_resource_rules = status.get("nonResourceRules")
    if not isinstance(resource_rules, list) or not isinstance(non_resource_rules, list):
        raise AuditError(f"SelfSubjectRulesReview omits complete rule vectors in {namespace}")

    observed_resources: set[tuple[str, str, str, str, str]] = set()
    for index, raw_rule in enumerate(resource_rules):
        label = f"SelfSubjectRulesReview[{namespace}].resourceRules[{index}]"
        if not isinstance(raw_rule, dict) or set(raw_rule) - {
            "apiGroups",
            "resourceNames",
            "resources",
            "verbs",
        }:
            raise AuditError(f"{label} is malformed or has unknown fields")
        verbs = _rule_vector(raw_rule, "verbs", label)
        groups = _rule_vector(
            raw_rule, "apiGroups", label, allow_empty_strings=True
        )
        resources = _rule_vector(raw_rule, "resources", label)
        names = _rule_vector(raw_rule, "resourceNames", label, required=False) or [""]
        if "*" in verbs or "*" in groups or "*" in resources or "*" in names:
            raise AuditError(f"{label} contains wildcard authority")
        for verb in verbs:
            for group in groups:
                for combined in resources:
                    resource, separator, subresource = combined.partition("/")
                    if not resource or (separator and not subresource):
                        raise AuditError(f"{label} contains a malformed resource/subresource")
                    for name in names:
                        observed_resources.add(
                            (verb, group, resource, subresource if separator else "", name)
                        )

    if expected_closure is None:
        expected_resources = _expected_resource_atoms(
            profile, namespace, namespaces, persistent_volume_names
        )
        expected_non_resources = DISCOVERY_NON_RESOURCE_RULES
    else:
        if not isinstance(expected_closure, dict) or set(expected_closure) != {
            "non_resource_rules",
            "resource_rules",
        }:
            raise AuditError(f"expected effective-rule closure is malformed in {namespace}")
        if not isinstance(expected_closure["resource_rules"], list) or not isinstance(
            expected_closure["non_resource_rules"], list
        ):
            raise AuditError(
                f"expected effective-rule closure vectors are malformed in {namespace}"
            )
        expected_resources = set()
        for index, raw in enumerate(expected_closure["resource_rules"]):
            if not isinstance(raw, dict) or set(raw) != {
                "api_group",
                "name",
                "resource",
                "subresource",
                "verb",
            } or not all(isinstance(value, str) for value in raw.values()):
                raise AuditError(
                    f"expected resource-rule closure entry {index} is malformed in {namespace}"
                )
            expected_resources.add(
                (
                    raw["verb"],
                    raw["api_group"],
                    raw["resource"],
                    raw["subresource"],
                    raw["name"],
                )
            )
        expected_non_resources = set()
        for index, raw in enumerate(expected_closure["non_resource_rules"]):
            if (
                not isinstance(raw, dict)
                or set(raw) != {"url", "verb"}
                or not all(isinstance(value, str) and value for value in raw.values())
            ):
                raise AuditError(
                    f"expected non-resource closure entry {index} is malformed in {namespace}"
                )
            expected_non_resources.add((raw["verb"], raw["url"]))
    if observed_resources != expected_resources:
        additional = sorted(observed_resources - expected_resources)
        missing = sorted(expected_resources - observed_resources)
        raise AuditError(
            f"effective resource-rule closure differs in {namespace}: "
            f"additional={additional[:8]!r}, missing={missing[:8]!r}"
        )

    observed_non_resources: set[tuple[str, str]] = set()
    for index, raw_rule in enumerate(non_resource_rules):
        label = f"SelfSubjectRulesReview[{namespace}].nonResourceRules[{index}]"
        if not isinstance(raw_rule, dict) or set(raw_rule) != {
            "nonResourceURLs",
            "verbs",
        }:
            raise AuditError(f"{label} is malformed or has unknown fields")
        verbs = _rule_vector(raw_rule, "verbs", label)
        urls = _rule_vector(raw_rule, "nonResourceURLs", label)
        if "*" in verbs or "*" in urls:
            raise AuditError(f"{label} contains wildcard authority")
        observed_non_resources.update((verb, url) for verb in verbs for url in urls)
    if observed_non_resources != expected_non_resources:
        additional = sorted(observed_non_resources - expected_non_resources)
        missing = sorted(expected_non_resources - observed_non_resources)
        raise AuditError(
            f"effective non-resource URL closure differs in {namespace}: "
            f"additional={additional[:8]!r}, missing={missing[:8]!r}"
        )

    return {
        "resource_rules": [
            {
                "api_group": group,
                "name": name,
                "resource": resource,
                "subresource": subresource,
                "verb": verb,
            }
            for verb, group, resource, subresource, name in sorted(observed_resources)
        ],
        "non_resource_rules": [
            {"url": url, "verb": verb}
            for verb, url in sorted(observed_non_resources)
        ],
    }


def validate_unimpersonated_platform_transport(
    kubeconfig: Path, context: str, kubectl_path: Path
) -> dict[str, str | bool]:
    """Prove the selected platform transport does not use kubectl impersonation.

    ``kubectl config view`` is intentionally invoked without ``--raw`` so token,
    certificate and key material remains redacted.  SelfSubjectReview then
    authenticates the effective username/groups.  Combining the two prevents a
    privileged underlying credential from hiding behind an impersonated
    low-authority subject whose SSRR happens to match the signed closure.
    """

    if (
        not kubeconfig.is_absolute()
        or ".." in kubeconfig.parts
        or not context
        or not kubectl_path.is_absolute()
        or ".." in kubectl_path.parts
    ):
        raise AuditError("platform kubeconfig/context transport is malformed")
    completed = subprocess.run(
        [
            str(kubectl_path),
            "--kubeconfig",
            str(kubeconfig),
            "--context",
            context,
            "config",
            "view",
            "--minify",
            "-o",
            "json",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if completed.returncode != 0 or len(completed.stdout) > 1024 * 1024:
        raise AuditError("platform kubeconfig redacted projection failed")
    try:
        rendered = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuditError("platform kubeconfig redacted projection is malformed") from error
    if not isinstance(rendered, dict) or set(rendered) - {
        "apiVersion",
        "clusters",
        "contexts",
        "current-context",
        "kind",
        "preferences",
        "users",
    }:
        raise AuditError("platform kubeconfig redacted projection has unknown fields")
    users = rendered.get("users")
    contexts = rendered.get("contexts")
    if (
        rendered.get("current-context") != context
        or not isinstance(users, list)
        or len(users) != 1
        or not isinstance(users[0], dict)
        or set(users[0]) != {"name", "user"}
        or not isinstance(users[0].get("user"), dict)
        or not isinstance(contexts, list)
        or len(contexts) != 1
        or not isinstance(contexts[0], dict)
        or contexts[0].get("name") != context
    ):
        raise AuditError("platform kubeconfig does not select one exact context/user")
    user = users[0]["user"]
    forbidden = {
        "as",
        "as-groups",
        "as-uid",
        "as-user-extra",
        "impersonate",
        "impersonate-groups",
        "impersonate-uid",
        "impersonate-user-extra",
    }
    if forbidden.intersection(user):
        raise AuditError("platform kubeconfig contains an impersonation directive")
    return {
        "context": context,
        "impersonation_free": True,
        "redacted_projection": True,
    }


def review(client: Client, reviews: list[dict[str, Any]], verb: str, group: str, resource: str, subresource: str, namespace: str, name: str, expected: bool) -> None:
    identifier = check_id(verb, group, resource, subresource, namespace, name)
    allowed = client.allowed(verb, group, resource, namespace, subresource, name)
    reviews.append({"check": identifier, "allowed": allowed, "expected_allowed": expected})
    if allowed is not expected:
        raise AuditError(f"effective authority differs for {identifier}")


def run(args: argparse.Namespace, client: Any | None = None) -> dict[str, Any]:
    # The external executor injects the API client backed by its inherited,
    # epoch-bound token. CLI users retain the kubeconfig client for the other
    # read-only profiles; the executor never falls back to it.
    platform_transport: dict[str, str | bool] | None = None
    if client is None:
        if args.profile == "platform":
            platform_transport = validate_unimpersonated_platform_transport(
                args.kubeconfig, args.context, args.kubectl_path
            )
        client = Client(args.kubeconfig, args.context, args.kubectl_path)
    elif args.profile == "platform":
        raise AuditError("platform audit requires the exact supplied kubeconfig transport")
    identity_review = client.review(
        "/apis/authentication.k8s.io/v1beta1/selfsubjectreviews",
        {"apiVersion": "authentication.k8s.io/v1beta1", "kind": "SelfSubjectReview", "spec": {}},
    )
    identity = identity_review.get("status", {}).get("userInfo", {})
    username = identity.get("username") if isinstance(identity, dict) else None
    groups = sorted(identity.get("groups", [])) if isinstance(identity, dict) else []
    expected_groups = json.loads(args.expected_groups_json)
    compared_groups = groups
    if args.profile == "external-executor":
        if "system:authenticated" not in groups:
            raise AuditError("external epoch identity lacks system:authenticated")
        compared_groups = [group for group in groups if group != "system:authenticated"]
    if (
        username != args.expected_username
        or compared_groups != expected_groups
        or "system:masters" in groups
    ):
        raise AuditError("authenticated identity differs from the exact claimed subject")
    if args.profile in {
        "external-executor",
        "receipt-service-account",
        "metadata-reader",
    }:
        if not args.bound_jti_sha256 or not SHA256_RE.fullmatch(args.bound_jti_sha256):
            raise AuditError("short-lived identity audit requires an exact token JTI digest")
    elif args.bound_jti_sha256 is not None:
        raise AuditError("external identity audit may not claim a service-account token JTI")

    namespaces = json.loads(args.namespace_inventory_json)
    if not isinstance(namespaces, list) or not namespaces or namespaces != sorted(set(namespaces)) or not all(isinstance(name, str) and name for name in namespaces):
        raise AuditError("signed namespace inventory is malformed")
    persistent_volume_names = json.loads(args.persistent_volume_names_json)
    if (
        not isinstance(persistent_volume_names, list)
        or persistent_volume_names != sorted(set(persistent_volume_names))
        or len(persistent_volume_names) != 8
        or not FIXED_PERSISTENT_VOLUMES.issubset(persistent_volume_names)
        or not all(isinstance(name, str) and name for name in persistent_volume_names)
    ):
        raise AuditError("exact persistent-volume inventory is malformed")
    expected_closure_by_namespace: dict[str, dict[str, Any]] = {}
    supplied_closure_json = getattr(args, "expected_rule_closure_json", None)
    if args.profile == "platform":
        if not isinstance(supplied_closure_json, str):
            raise AuditError("platform audit requires a repository-pinned exact rule closure")
        supplied_closure = json.loads(supplied_closure_json)
        if canonical(supplied_closure).decode() != supplied_closure_json or not isinstance(
            supplied_closure, list
        ):
            raise AuditError("platform expected rule closure is not canonical JSON")
        for entry in supplied_closure:
            if (
                not isinstance(entry, dict)
                or set(entry) != {
                    "namespace",
                    "non_resource_rules",
                    "resource_rules",
                }
                or entry["namespace"] in expected_closure_by_namespace
            ):
                raise AuditError("platform expected rule closure has malformed entries")
            expected_closure_by_namespace[entry["namespace"]] = {
                "non_resource_rules": entry["non_resource_rules"],
                "resource_rules": entry["resource_rules"],
            }
        if sorted(expected_closure_by_namespace) != namespaces:
            raise AuditError("platform expected rule closure omits the exact namespace inventory")
    elif supplied_closure_json is not None:
        raise AuditError("only the platform profile may consume an external rule closure")
    if args.profile == "token-issuer":
        namespace_collection = client.raw("/api/v1/namespaces")
        live_namespaces = sorted(item.get("metadata", {}).get("name", "") for item in namespace_collection.get("items", []))
        if live_namespaces != namespaces:
            raise AuditError("token issuer did not observe the exact complete namespace inventory")
    kube_system = client.raw("/api/v1/namespaces/kube-system")
    if kube_system.get("metadata", {}).get("uid") != args.kube_system_uid:
        raise AuditError("selected cluster identity differs")

    reviews: list[dict[str, Any]] = []
    expected_matrix = expected_reviews(args.profile, namespaces, persistent_volume_names)

    def expected_for(
        verb: str,
        group: str,
        resource: str,
        subresource: str,
        namespace: str,
        name: str = "",
    ) -> bool:
        identifier = check_id(verb, group, resource, subresource, namespace, name)
        if args.profile != "platform":
            return expected_matrix[identifier]
        closure_namespace = namespace or namespaces[0]
        closure = expected_closure_by_namespace[closure_namespace]
        for atom in closure["resource_rules"]:
            if (
                isinstance(atom, dict)
                and atom.get("verb") == verb
                and atom.get("api_group") == group
                and atom.get("resource") == resource
                and atom.get("subresource") == subresource
                and (atom.get("name") == "" or (name and atom.get("name") == name))
            ):
                return True
        return False

    for group, resource, subresource in CLUSTER_RESOURCES:
        for verb in (*READING, *MUTATING):
            review(
                client,
                reviews,
                verb,
                group,
                resource,
                subresource,
                "",
                "",
                expected_for(verb, group, resource, subresource, ""),
            )
    for group, resource in IMPERSONATION:
        review(
            client,
            reviews,
            "impersonate",
            group,
            resource,
            "",
            "",
            "",
            expected_for("impersonate", group, resource, "", ""),
        )
    for resource in ("roles", "clusterroles"):
        for verb in ("bind", "escalate"):
            review(
                client,
                reviews,
                verb,
                "rbac.authorization.k8s.io",
                resource,
                "",
                "",
                "",
                expected_for(
                    verb, "rbac.authorization.k8s.io", resource, "", ""
                ),
            )
    for namespace in namespaces:
        for group, resource, subresource in NAMESPACED_RESOURCES:
            for verb in (*READING, *MUTATING):
                review(
                    client,
                    reviews,
                    verb,
                    group,
                    resource,
                    subresource,
                    namespace,
                    "",
                    expected_for(
                        verb, group, resource, subresource, namespace
                    ),
                )

    # Exact names are tested independently from the unbounded edges above.
    named_edges = (
        ("create", "", "serviceaccounts", "token", "fs2-system", "fs2-pod-security-rollout-custodian"),
        ("create", "", "serviceaccounts", "token", "fs2-system", "fs2-pod-security-metadata-reader"),
        ("get", "", "configmaps", "", "fs2-system", "fs2-pod-security-rollout-ledger"),
        ("update", "", "configmaps", "", "fs2-system", "fs2-pod-security-rollout-ledger"),
        ("patch", "", "configmaps", "", "fs2-system", "fs2-pod-security-rollout-ledger"),
        ("delete", "", "configmaps", "", "fs2-system", "fs2-pod-security-rollout-ledger"),
        ("list", "", "secrets", "", "fs2-models", ""),
    )
    for verb, group, resource, subresource, namespace, name in named_edges:
        review(
            client,
            reviews,
            verb,
            group,
            resource,
            subresource,
            namespace,
            name,
            expected_for(verb, group, resource, subresource, namespace, name),
        )
    for name in persistent_volume_names:
        review(
            client,
            reviews,
            "get",
            "",
            "persistentvolumes",
            "",
            "",
            name,
            expected_for("get", "", "persistentvolumes", "", "", name),
        )
    for resource, names in (
        ("clusterroles", ("fs2-pod-security-external-custody-audit", "fs2-pod-security-rollout-reader")),
        ("clusterrolebindings", ("fs2-pod-security-external-custody-audit", "fs2-pod-security-rollout-custodian-reader", "fs2-pod-security-rollout-reader")),
    ):
        for name in names:
            review(
                client,
                reviews,
                "get",
                "rbac.authorization.k8s.io",
                resource,
                "",
                "",
                name,
                expected_for(
                    "get", "rbac.authorization.k8s.io", resource, "", "", name
                ),
            )

    rules: list[dict[str, Any]] = []
    exact_rule_closure: list[dict[str, Any]] = []
    for namespace in namespaces:
        result = client.review(
            "/apis/authorization.k8s.io/v1/selfsubjectrulesreviews",
            {"apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectRulesReview", "spec": {"namespace": namespace}},
        )
        status = result.get("status")
        closure = validate_exact_rule_closure(
            args.profile,
            namespace,
            namespaces,
            persistent_volume_names,
            status,
            expected_closure_by_namespace.get(namespace)
            if args.profile == "platform"
            else None,
        )
        rules.append({"namespace": namespace, "status": status})
        exact_rule_closure.append({"namespace": namespace, **closure})
    result = {
        "schema": SCHEMA,
        "matrix_version": MATRIX_VERSION,
        "profile": args.profile,
        "username": username,
        "groups": compared_groups,
        "credential_jti_sha256": args.bound_jti_sha256,
        "cluster_id": args.cluster_id,
        "kube_system_uid": args.kube_system_uid,
        "namespace_inventory": namespaces,
        "namespace_inventory_sha256": hashlib.sha256(canonical(namespaces)).hexdigest(),
        "persistent_volume_names": persistent_volume_names,
        "self_subject_rules_reviews": rules,
        "self_subject_rules_reviews_sha256": hashlib.sha256(canonical(rules)).hexdigest(),
        "exact_rule_closure": exact_rule_closure,
        "exact_rule_closure_sha256": hashlib.sha256(
            canonical(exact_rule_closure)
        ).hexdigest(),
        "self_subject_access_reviews": reviews,
        "self_subject_access_reviews_sha256": hashlib.sha256(canonical(reviews)).hexdigest(),
        "observed_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    if args.profile == "external-executor":
        result["authenticated_groups"] = groups
    if args.profile == "platform":
        if platform_transport is None:
            raise AuditError("platform audit omitted its unimpersonated transport proof")
        result["transport"] = platform_transport
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--kubeconfig", required=True, type=Path)
    result.add_argument("--context", required=True)
    result.add_argument("--kubectl-path", required=True, type=Path)
    result.add_argument("--cluster-id", required=True)
    result.add_argument("--kube-system-uid", required=True)
    result.add_argument("--profile", required=True, choices=sorted(PROFILES))
    result.add_argument("--expected-username", required=True)
    result.add_argument("--expected-groups-json", required=True)
    result.add_argument("--namespace-inventory-json", required=True)
    result.add_argument("--persistent-volume-names-json", required=True)
    result.add_argument("--bound-jti-sha256")
    result.add_argument("--expected-rule-closure-json")
    return result


def main() -> int:
    try:
        result = run(parser().parse_args())
    except (AuditError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"SAI-07 effective-authority v2 audit rejected: {error}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
