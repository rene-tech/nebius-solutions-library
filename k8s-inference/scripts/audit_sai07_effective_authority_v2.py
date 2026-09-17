#!/usr/bin/env python3
"""Emit a complete, identity-bound SAI-07 effective-authority audit.

Unlike the retained v1 audit, this artifact contains every SSRR rule and every
SSAR decision.  It covers the exact cluster namespace inventory plus admission,
RBAC, workload, Secret, token, proxy and impersonation pivots.  This command is
read-only apart from Kubernetes review APIs, which do not mutate resources.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from audit_sai07_effective_authority import Client, AuditError, canonical

SCHEMA = "fs2-serve.nebius.ai/sai07-effective-authority-audit/v2"
MATRIX_VERSION = "sai07-authority-matrix-2026-09-17-v2"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
PROFILES = {"platform", "inactive-owner", "token-issuer", "receipt-service-account", "metadata-reader"}
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
IMPERSONATION = (
    ("authentication.k8s.io", "users"),
    ("authentication.k8s.io", "groups"),
    ("authentication.k8s.io", "userextras"),
    ("", "serviceaccounts"),
)


def check_id(verb: str, group: str, resource: str, subresource: str, namespace: str, name: str = "") -> str:
    api = group or "v1"
    target = resource + (f"/{subresource}" if subresource else "")
    scope = namespace or "_cluster"
    return f"{verb}:{api}:{target}:{scope}:{name or '*'}"


def expected_allowed(profile: str, namespaces: list[str], persistent_volume_names: list[str]) -> set[str]:
    result = {
        check_id("create", "authorization.k8s.io", "selfsubjectaccessreviews", "", ""),
        check_id("create", "authorization.k8s.io", "selfsubjectrulesreviews", "", ""),
    }
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


def review(client: Client, reviews: list[dict[str, Any]], verb: str, group: str, resource: str, subresource: str, namespace: str, name: str, expected: bool) -> None:
    identifier = check_id(verb, group, resource, subresource, namespace, name)
    allowed = client.allowed(verb, group, resource, namespace, subresource, name)
    reviews.append({"check": identifier, "allowed": allowed, "expected_allowed": expected})
    if allowed is not expected:
        raise AuditError(f"effective authority differs for {identifier}")


def run(args: argparse.Namespace) -> dict[str, Any]:
    client = Client(args.kubeconfig, args.context)
    identity_review = client.review(
        "/apis/authentication.k8s.io/v1beta1/selfsubjectreviews",
        {"apiVersion": "authentication.k8s.io/v1beta1", "kind": "SelfSubjectReview", "spec": {}},
    )
    identity = identity_review.get("status", {}).get("userInfo", {})
    username = identity.get("username") if isinstance(identity, dict) else None
    groups = sorted(identity.get("groups", [])) if isinstance(identity, dict) else []
    expected_groups = json.loads(args.expected_groups_json)
    if username != args.expected_username or groups != expected_groups or "system:masters" in groups:
        raise AuditError("authenticated identity differs from the exact claimed subject")
    if args.profile in {"receipt-service-account", "metadata-reader"}:
        if not args.bound_jti_sha256 or not SHA256_RE.fullmatch(args.bound_jti_sha256):
            raise AuditError("short-lived service-account audit requires an exact token JTI digest")
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
    for group, resource, subresource in CLUSTER_RESOURCES:
        for verb in (*READING, *MUTATING):
            identifier = check_id(verb, group, resource, subresource, "")
            review(client, reviews, verb, group, resource, subresource, "", "", expected_matrix[identifier])
    for group, resource in IMPERSONATION:
        identifier = check_id("impersonate", group, resource, "", "")
        review(client, reviews, "impersonate", group, resource, "", "", "", expected_matrix[identifier])
    for resource in ("roles", "clusterroles"):
        for verb in ("bind", "escalate"):
            identifier = check_id(verb, "rbac.authorization.k8s.io", resource, "", "")
            review(client, reviews, verb, "rbac.authorization.k8s.io", resource, "", "", "", expected_matrix[identifier])
    for namespace in namespaces:
        for group, resource, subresource in NAMESPACED_RESOURCES:
            for verb in (*READING, *MUTATING):
                identifier = check_id(verb, group, resource, subresource, namespace)
                review(client, reviews, verb, group, resource, subresource, namespace, "", expected_matrix[identifier])

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
        identifier = check_id(verb, group, resource, subresource, namespace, name)
        review(client, reviews, verb, group, resource, subresource, namespace, name, expected_matrix[identifier])
    for name in persistent_volume_names:
        identifier = check_id("get", "", "persistentvolumes", "", "", name)
        review(client, reviews, "get", "", "persistentvolumes", "", "", name, expected_matrix[identifier])
    for resource, names in (
        ("clusterroles", ("fs2-pod-security-external-custody-audit", "fs2-pod-security-rollout-reader")),
        ("clusterrolebindings", ("fs2-pod-security-external-custody-audit", "fs2-pod-security-rollout-custodian-reader", "fs2-pod-security-rollout-reader")),
    ):
        for name in names:
            identifier = check_id("get", "rbac.authorization.k8s.io", resource, "", "", name)
            review(client, reviews, "get", "rbac.authorization.k8s.io", resource, "", "", name, expected_matrix[identifier])

    rules: list[dict[str, Any]] = []
    for namespace in namespaces:
        result = client.review(
            "/apis/authorization.k8s.io/v1/selfsubjectrulesreviews",
            {"apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectRulesReview", "spec": {"namespace": namespace}},
        )
        status = result.get("status")
        if not isinstance(status, dict) or status.get("incomplete") is not False:
            raise AuditError(f"SelfSubjectRulesReview is incomplete in {namespace}")
        rules.append({"namespace": namespace, "status": status})
    return {
        "schema": SCHEMA,
        "matrix_version": MATRIX_VERSION,
        "profile": args.profile,
        "username": username,
        "groups": groups,
        "credential_jti_sha256": args.bound_jti_sha256,
        "cluster_id": args.cluster_id,
        "kube_system_uid": args.kube_system_uid,
        "namespace_inventory": namespaces,
        "namespace_inventory_sha256": hashlib.sha256(canonical(namespaces)).hexdigest(),
        "persistent_volume_names": persistent_volume_names,
        "self_subject_rules_reviews": rules,
        "self_subject_rules_reviews_sha256": hashlib.sha256(canonical(rules)).hexdigest(),
        "self_subject_access_reviews": reviews,
        "self_subject_access_reviews_sha256": hashlib.sha256(canonical(reviews)).hexdigest(),
        "observed_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--kubeconfig", required=True, type=Path)
    result.add_argument("--context", required=True)
    result.add_argument("--cluster-id", required=True)
    result.add_argument("--kube-system-uid", required=True)
    result.add_argument("--profile", required=True, choices=sorted(PROFILES))
    result.add_argument("--expected-username", required=True)
    result.add_argument("--expected-groups-json", required=True)
    result.add_argument("--namespace-inventory-json", required=True)
    result.add_argument("--persistent-volume-names-json", required=True)
    result.add_argument("--bound-jti-sha256")
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
