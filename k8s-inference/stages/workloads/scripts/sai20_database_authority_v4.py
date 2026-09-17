#!/usr/bin/env python3
"""Verify source-rooted SAI-20 evidence at plan and re-observe it at apply.

No trust root is accepted from Terraform, the environment or the packet.  The
only roots are in the committed registry.  Plan verification consumes raw API
transcripts signed by distinct collector and reviewer roots.  Apply verification
replays the safe reads and performs fresh authenticated Kubernetes API reads
through the exact kubeconfig/context used by the Kubernetes provider.
"""

from __future__ import annotations

import base64
import copy
import fcntl
import hashlib
import json
import os
import re
import stat
import struct
import subprocess
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

import sai20_database_authority as v3


SCHEMA = "fs2-serve.nebius.ai/sai20-database-authority/v4"
REGISTRY_SCHEMA = "fs2-serve.nebius.ai/sai20-authority-root-registry/v1"
REQUIRED_ROLES = {"evidence-collector", "independent-reviewer"}
REJECTED_COMMITS = {*v3.REJECTED_COMMITS, "ffd8674063314f876b1e0b00b73a76fdb4ea27af"}
WORKLOAD_ENDPOINTS = {
    (namespace, resource): (
        f"/api/v1/namespaces/{namespace}/{resource}"
        if resource in {"pods", "replicationcontrollers"}
        else f"/apis/apps/v1/namespaces/{namespace}/{resource}"
        if resource in {"deployments", "statefulsets", "daemonsets", "replicasets"}
        else f"/apis/batch/v1/namespaces/{namespace}/{resource}"
    )
    for namespace, resource in v3.RESOURCE_LISTS
}
WORKLOAD_TYPES = {
    "pods": ("v1", "Pod"),
    "replicationcontrollers": ("v1", "ReplicationController"),
    "deployments": ("apps/v1", "Deployment"),
    "statefulsets": ("apps/v1", "StatefulSet"),
    "daemonsets": ("apps/v1", "DaemonSet"),
    "replicasets": ("apps/v1", "ReplicaSet"),
    "jobs": ("batch/v1", "Job"),
    "cronjobs": ("batch/v1", "CronJob"),
}
V4_RBAC_LISTS = v3.RBAC_LISTS | {
    ("fs2-data", "roles"),
    ("fs2-data", "rolebindings"),
    ("cnpg-system", "roles"),
    ("cnpg-system", "rolebindings"),
}
NAMESPACE_ENDPOINT = "/api/v1/namespaces"
NAMESPACED_RBAC_RESOURCES = ("roles", "rolebindings")
CLUSTER_RBAC_RESOURCES = ("clusterroles", "clusterrolebindings")
SECRET_METADATA_ACCEPT = "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1"
NETWORK_POLICY_ENDPOINT = "/apis/networking.k8s.io/v1/namespaces/fs2-data/networkpolicies"
DATABASE_POD_ENDPOINT = (
    "/api/v1/namespaces/fs2-data/pods?labelSelector="
    + urllib.parse.quote("cnpg.io/cluster=fs2-control-db", safe="")
)
SELF_SUBJECT_REVIEW_ENDPOINT = "/apis/authentication.k8s.io/v1/selfsubjectreviews"
SELF_SUBJECT_RULES_REVIEW_ENDPOINT = "/apis/authorization.k8s.io/v1/selfsubjectrulesreviews"
SELF_SUBJECT_ACCESS_REVIEW_ENDPOINT = "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews"
SUBJECT_ACCESS_REVIEW_ENDPOINT = "/apis/authorization.k8s.io/v1/subjectaccessreviews"
DANGEROUS_REVIEWS = {
    "impersonate-users": {"group": "", "resource": "users", "verb": "impersonate"},
    "impersonate-groups": {"group": "", "resource": "groups", "verb": "impersonate"},
    "impersonate-serviceaccounts": {"group": "", "resource": "serviceaccounts", "verb": "impersonate"},
    "impersonate-uids": {"group": "authentication.k8s.io", "resource": "uids", "verb": "impersonate"},
    "impersonate-userextras": {"group": "authentication.k8s.io", "resource": "userextras", "verb": "impersonate"},
    "create-certificate-signing-requests": {"group": "certificates.k8s.io", "resource": "certificatesigningrequests", "verb": "create"},
    "update-certificate-signing-request-approval": {"group": "certificates.k8s.io", "resource": "certificatesigningrequests/approval", "verb": "update"},
    "patch-certificate-signing-request-approval": {"group": "certificates.k8s.io", "resource": "certificatesigningrequests/approval", "verb": "patch"},
    "approve-certificate-signers": {"group": "certificates.k8s.io", "resource": "signers", "verb": "approve"},
    "sign-certificate-signers": {"group": "certificates.k8s.io", "resource": "signers", "verb": "sign"},
    "escalate-roles": {"group": "rbac.authorization.k8s.io", "resource": "roles", "verb": "escalate"},
    "escalate-clusterroles": {"group": "rbac.authorization.k8s.io", "resource": "clusterroles", "verb": "escalate"},
    "bind-roles": {"group": "rbac.authorization.k8s.io", "resource": "roles", "verb": "bind"},
    "bind-clusterroles": {"group": "rbac.authorization.k8s.io", "resource": "clusterroles", "verb": "bind"},
}
CREDENTIAL_PIVOT_ACTIONS = {
    "pods/exec": ("create", "get"),
    "pods/attach": ("create", "get"),
    "pods/portforward": ("create", "get"),
    "pods/proxy": ("create", "delete", "get", "patch", "update"),
    "pods/ephemeralcontainers": ("patch", "update"),
    "nodes/proxy": ("create", "delete", "get", "patch", "update"),
}
CREDENTIAL_PIVOT_RESOURCES = set(CREDENTIAL_PIVOT_ACTIONS)


def credential_pivot_rule(rule: dict[str, Any]) -> bool:
    groups = set(rule.get("apiGroups", []))
    resources = set(rule.get("resources", []))
    verbs = set(rule.get("verbs", []))
    if not ({"", "*"} & groups):
        return False
    return any(
        (pivot_resource in resources or "*" in resources)
        and (bool(set(actions) & verbs) or "*" in verbs)
        for pivot_resource, actions in CREDENTIAL_PIVOT_ACTIONS.items()
    )


def scoped_pod_connection_review(review_name: str) -> bool:
    """Return whether v5 must classify this Pod target before denying it.

    Pod connect authority is not intrinsically custodian-only: exact,
    credential-free tenant targets and short-lived audited debug leases remain
    supported.  Node proxy and every non-connect authority stay dangerous in
    v4 without deferral.
    """

    return review_name.startswith("credential-pivot/") and "/pods/" in review_name


def rbac_endpoints(namespaces: list[str]) -> dict[tuple[str, str], str]:
    endpoints = {
        ("", resource): f"/apis/rbac.authorization.k8s.io/v1/{resource}"
        for resource in CLUSTER_RBAC_RESOURCES
    }
    endpoints.update(
        {
            (namespace, resource): f"/apis/rbac.authorization.k8s.io/v1/namespaces/{namespace}/{resource}"
            for namespace in namespaces
            for resource in NAMESPACED_RBAC_RESOURCES
        }
    )
    return endpoints


def service_account_endpoints(namespaces: list[str]) -> dict[str, str]:
    return {
        namespace: f"/api/v1/namespaces/{namespace}/serviceaccounts"
        for namespace in namespaces
    }


def secret_metadata_endpoints(namespaces: list[str]) -> dict[str, str]:
    return {
        namespace: f"/api/v1/namespaces/{namespace}/secrets"
        for namespace in namespaces
    }


def dangerous_reviews(
    namespaces: list[str],
    service_accounts: list[dict[str, str]],
    secrets: list[dict[str, str]],
    custodians: list[dict[str, Any]],
    entries: dict[str, dict[str, Any]],
) -> dict[str, dict[str, str]]:
    reviews = dict(DANGEROUS_REVIEWS)
    secret_resource_names: set[tuple[str, str, str]] = set()
    service_account_resource_names: set[tuple[str, str, str]] = set()
    pivot_resource_names: set[tuple[str, str, str, str]] = set()
    signer_resource_names: set[str] = set()
    for namespace, resource in sorted(rbac_endpoints(namespaces)):
        if resource not in {"roles", "clusterroles"}:
            continue
        items = entries[f"k8s/rbac/{namespace or '_cluster'}/{resource}"]["body"]["items"]
        target_namespaces = [namespace] if resource == "roles" else namespaces
        for role in items:
            for rule in role.get("rules", []):
                groups = set(rule.get("apiGroups", []))
                resources = set(rule.get("resources", []))
                verbs = set(rule.get("verbs", []))
                if {"", "*"} & groups and {"secrets", "*"} & resources:
                    secret_verbs = ({"get", "list", "watch"} & verbs) | (
                        {"get", "list", "watch"} if "*" in verbs else set()
                    )
                    for verb in sorted(secret_verbs):
                        for name in rule.get("resourceNames", []):
                            if isinstance(name, str) and name:
                                secret_resource_names.update(
                                    (target_namespace, verb, name)
                                    for target_namespace in target_namespaces
                                )
                if {"", "*"} & groups and {"serviceaccounts", "*"} & resources:
                    service_account_verbs = ({"create", "update", "patch"} & verbs) | (
                        {"create", "update", "patch"} if "*" in verbs else set()
                    )
                    for verb in sorted(service_account_verbs):
                        for name in rule.get("resourceNames", []):
                            if isinstance(name, str) and name:
                                service_account_resource_names.update(
                                    (target_namespace, verb, name)
                                    for target_namespace in target_namespaces
                                )
                if {"", "*"} & groups:
                    for pivot_resource, pivot_actions in CREDENTIAL_PIVOT_ACTIONS.items():
                        if pivot_resource not in resources and "*" not in resources:
                            continue
                        pivot_verbs = set(pivot_actions) & verbs
                        if "*" in verbs:
                            pivot_verbs = set(pivot_actions)
                        pivot_namespaces = (
                            [""]
                            if pivot_resource == "nodes/proxy"
                            else target_namespaces
                        )
                        for verb in sorted(pivot_verbs):
                            for name in rule.get("resourceNames", []):
                                if isinstance(name, str) and name:
                                    pivot_resource_names.update(
                                        (target_namespace, pivot_resource, verb, name)
                                        for target_namespace in pivot_namespaces
                                    )
                if (
                    {"certificates.k8s.io", "*"} & groups
                    and {"signers", "*"} & resources
                    and {"sign", "*"} & verbs
                ):
                    signer_resource_names.update(
                        name
                        for name in rule.get("resourceNames", [])
                        if isinstance(name, str) and name
                    )
    for name in sorted(signer_resource_names):
        reviews[f"sign-certificate-signer/{digest(name)[:16]}"] = {
            "group": "certificates.k8s.io",
            "resource": "signers",
            "verb": "sign",
            "name": name,
        }
    for namespace in namespaces:
        for verb in ("get", "list", "watch"):
            reviews[f"read-secrets/{namespace}/{verb}/_all"] = {
                "group": "",
                "resource": "secrets",
                "verb": verb,
                "namespace": namespace,
            }
    secret_resource_names.update(
        (secret["namespace"], "get", secret["name"])
        for secret in secrets
    )
    for namespace, verb, name in sorted(secret_resource_names):
        reviews[f"read-secret/{namespace}/{verb}/{digest(name)[:16]}"] = {
            "group": "",
            "resource": "secrets",
            "verb": verb,
            "namespace": namespace,
            "name": name,
        }
    for namespace in namespaces:
        for verb in ("create", "update", "patch"):
            reviews[f"mutate-serviceaccounts/{namespace}/{verb}/_all"] = {
                "group": "",
                "resource": "serviceaccounts",
                "verb": verb,
                "namespace": namespace,
            }
        reviews[f"create-serviceaccount-tokens/{namespace}/_all"] = {
            "group": "",
            "resource": "serviceaccounts",
            "subresource": "token",
            "verb": "create",
            "namespace": namespace,
        }
    service_account_resource_names.update(
        (account["namespace"], verb, account["name"])
        for account in service_accounts
        for verb in ("update", "patch")
    )
    for namespace, verb, name in sorted(service_account_resource_names):
        reviews[f"mutate-serviceaccount/{namespace}/{verb}/{digest(name)[:16]}"] = {
            "group": "",
            "resource": "serviceaccounts",
            "verb": verb,
            "namespace": namespace,
            "name": name,
        }
    for pivot_resource, pivot_actions in sorted(CREDENTIAL_PIVOT_ACTIONS.items()):
        resource, subresource = pivot_resource.split("/", 1)
        target_namespaces = [""] if resource == "nodes" else namespaces
        for namespace in target_namespaces:
            for verb in pivot_actions:
                review = {
                    "group": "",
                    "resource": resource,
                    "subresource": subresource,
                    "verb": verb,
                }
                if namespace:
                    review["namespace"] = namespace
                reviews[
                    f"credential-pivot/{namespace or '_cluster'}/{pivot_resource}/{verb}/_all"
                ] = review
    for namespace, pivot_resource, verb, name in sorted(pivot_resource_names):
        resource, subresource = pivot_resource.split("/", 1)
        review = {
            "group": "",
            "resource": resource,
            "subresource": subresource,
            "verb": verb,
            "name": name,
        }
        if namespace:
            review["namespace"] = namespace
        reviews[
            f"credential-pivot/{namespace or '_cluster'}/{pivot_resource}/{verb}/{digest(name)[:16]}"
        ] = review
    for account in service_accounts:
        reviews[f"create-serviceaccount-token/{account['namespace']}/{account['name']}"] = {
            "group": "",
            "resource": "serviceaccounts",
            "subresource": "token",
            "verb": "create",
            "namespace": account["namespace"],
            "name": account["name"],
        }
    for custodian in custodians:
        principal_id = custodian["id"]
        reviews[f"impersonate-exact-custodian-user/{principal_id}"] = {
            "group": "",
            "resource": "users",
            "verb": "impersonate",
            "name": custodian["username"],
        }
        reviews[f"impersonate-exact-custodian-uid/{principal_id}"] = {
            "group": "authentication.k8s.io",
            "resource": "uids",
            "verb": "impersonate",
            "name": custodian["uid"],
        }
        for group in custodian["groups"]:
            reviews[f"impersonate-exact-custodian-group/{principal_id}/{digest(group)[:16]}"] = {
                "group": "",
                "resource": "groups",
                "verb": "impersonate",
                "name": group,
            }
        for extra_key in custodian["extra"]:
            reviews[f"impersonate-exact-custodian-extra/{principal_id}/{digest(extra_key)[:16]}"] = {
                "group": "authentication.k8s.io",
                "resource": "userextras",
                "verb": "impersonate",
                "name": extra_key,
            }
        subject = custodian["subject"]
        if subject["kind"] == "ServiceAccount":
            reviews[f"impersonate-exact-custodian-serviceaccount/{principal_id}"] = {
                "group": "",
                "resource": "serviceaccounts",
                "verb": "impersonate",
                "namespace": subject["namespace"],
                "name": subject["name"],
            }
    return dict(sorted(reviews.items()))
REQUIRED_SOURCE_PATHS = {
    "k8s-inference/inference-stack",
    "k8s-inference/security/sai20/README.md",
    "k8s-inference/security/sai20/authority-roots-v1.json",
    "k8s-inference/stages/workloads/contracts/sai20-control-db-ingress-v4.json",
    "k8s-inference/stages/workloads/cluster_contract.tf",
    "k8s-inference/stages/workloads/locals.tf",
    "k8s-inference/stages/workloads/outputs.tf",
    "k8s-inference/stages/workloads/providers.tf",
    "k8s-inference/stages/workloads/sai20_database_authority_v3.tf",
    "k8s-inference/stages/workloads/sai20_database_authority_v4.tf",
    "k8s-inference/stages/workloads/sai20_database_custody.tf",
    "k8s-inference/stages/workloads/sai20_network_isolation.tf",
    "k8s-inference/stages/workloads/scripts/sai20_database_authority.py",
    "k8s-inference/stages/workloads/scripts/sai20_database_authority_v4.py",
    "k8s-inference/stages/workloads/versions.tf",
    "k8s-inference/stages/workloads/variables.tf",
    "k8s-inference/tests/test_sai20_database_authority_v4.py",
    "k8s-inference/docs/SAI-20-NETWORK-ISOLATION.md",
}


def require(condition: bool, message: str) -> None:
    v3.require(condition, message)


def exact_keys(value: dict[str, Any], keys: set[str], where: str) -> None:
    v3.exact_keys(value, keys, where)


def text(value: Any, where: str) -> str:
    return v3.string(value, where)


def digest(value: Any) -> str:
    return v3.digest(value)


def sha256(value: Any, where: str) -> str:
    return v3.sha256(value, where)


def oid(value: Any, where: str) -> str:
    return v3.oid(value, where)


def decode_base64(value: Any, where: str) -> bytes:
    raw = text(value, where)
    try:
        return base64.b64decode(raw, validate=True)
    except ValueError as exc:
        raise v3.ContractError(f"{where} must be canonical base64") from exc


def decode_base64url(value: Any, where: str) -> bytes:
    raw = text(value, where)
    require(re.fullmatch(r"[A-Za-z0-9_-]{43}", raw) is not None, f"{where} must be raw Ed25519 base64url")
    return base64.urlsafe_b64decode(raw + "=")


def parse_json_bytes(value: bytes, where: str) -> Any:
    return v3.parse_json(value, where)


def git(repository: Path, *args: str) -> str:
    return v3.git(repository, *args)


def git_is_ancestor(repository: Path, ancestor: str, descendant: str) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(repository), "merge-base", "--is-ancestor", ancestor, descendant],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.returncode == 0


def verify_root_registry(registry: dict[str, Any], repository: Path, head: str) -> dict[str, dict[str, Any]]:
    exact_keys(
        registry,
        {"schema", "status", "required_roles", "minimum_distinct_signatures", "roots", "enrollment"},
        "root registry",
    )
    require(registry["schema"] == REGISTRY_SCHEMA, "root registry schema mismatch")
    require(registry["status"] == "ACTIVE", "root registry is not ACTIVE; source-owned enrollment is required")
    require(
        registry["required_roles"] == sorted(REQUIRED_ROLES),
        "root registry role closure must be exact and duplicate-free",
    )
    require(registry["minimum_distinct_signatures"] == 2, "root registry must require two signatures")
    roots = registry["roots"]
    require(isinstance(roots, list) and len(roots) >= 2, "collector and reviewer roots must be source-enrolled")
    result: dict[str, dict[str, Any]] = {}
    principals: set[str] = set()
    groups: set[str] = set()
    for index, root in enumerate(roots):
        where = f"root registry.roots[{index}]"
        require(isinstance(root, dict), f"{where} must be an object")
        exact_keys(
            root,
            {"key_id", "public_key", "role", "principal_id", "group_id", "enabled", "provenance"},
            where,
        )
        require(root["enabled"] is True, f"{where} must be enabled")
        require(root["role"] in REQUIRED_ROLES, f"{where}.role invalid")
        principal = text(root["principal_id"], f"{where}.principal_id")
        group = text(root["group_id"], f"{where}.group_id")
        require(principal not in principals and group not in groups, "authority roles must have distinct principals and groups")
        principals.add(principal)
        groups.add(group)
        raw_key = decode_base64url(root["public_key"], f"{where}.public_key")
        require(len(raw_key) == 32, f"{where}.public_key must be Ed25519")
        derived = "sha256:" + hashlib.sha256(raw_key).hexdigest()
        require(root["key_id"] == derived and derived not in result, f"{where}.key_id mismatch or duplicate")
        provenance = root["provenance"]
        require(isinstance(provenance, dict), f"{where}.provenance must be an object")
        exact_keys(
            provenance,
            {
                "accepted_source_commit", "accepted_source_tree", "public_key_source_path",
                "public_key_source_blob", "independent_review_receipt_sha256",
            },
            f"{where}.provenance",
        )
        commit = oid(provenance["accepted_source_commit"], f"{where}.provenance.accepted_source_commit")
        tree = oid(provenance["accepted_source_tree"], f"{where}.provenance.accepted_source_tree")
        path = text(provenance["public_key_source_path"], f"{where}.provenance.public_key_source_path")
        blob = oid(provenance["public_key_source_blob"], f"{where}.provenance.public_key_source_blob")
        sha256(provenance["independent_review_receipt_sha256"], f"{where}.provenance.independent_review_receipt_sha256")
        require(git(repository, "rev-parse", f"{commit}^{{tree}}") == tree, f"{where} provenance tree mismatch")
        require(git(repository, "rev-parse", f"{commit}:{path}") == blob, f"{where} provenance blob mismatch")
        require(git_is_ancestor(repository, commit, head), f"{where} provenance is not an ancestor of source HEAD")
        provenance_document = parse_json_bytes(
            subprocess.run(
                ["git", "-C", str(repository), "show", f"{commit}:{path}"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).stdout,
            f"{where} provenance document",
        )
        require(isinstance(provenance_document, dict), f"{where} provenance document must be an object")
        exact_keys(
            provenance_document,
            {"key_id", "public_key", "role", "principal_id", "group_id", "acceptance"},
            f"{where} provenance document",
        )
        require(
            provenance_document.get("key_id") == derived
            and provenance_document.get("public_key") == root["public_key"]
            and provenance_document.get("role") == root["role"]
            and provenance_document.get("principal_id") == principal
            and provenance_document.get("group_id") == group,
            f"{where} provenance document does not enroll this exact root",
        )
        acceptance = provenance_document["acceptance"]
        require(isinstance(acceptance, dict), f"{where} acceptance must be an object")
        exact_keys(
            acceptance,
            {
                "verdict", "reviewer_principal_id", "reviewed_source_commit",
                "reviewed_source_tree", "reviewed_at", "findings_sha256",
            },
            f"{where} acceptance",
        )
        reviewed_commit = oid(acceptance["reviewed_source_commit"], f"{where} acceptance.reviewed_source_commit")
        reviewed_tree = oid(acceptance["reviewed_source_tree"], f"{where} acceptance.reviewed_source_tree")
        require(
            acceptance["verdict"] == "ACCEPTED"
            and text(acceptance["reviewer_principal_id"], f"{where} acceptance.reviewer_principal_id") != principal
            and git(repository, "rev-parse", f"{reviewed_commit}^{{tree}}") == reviewed_tree
            and git_is_ancestor(repository, reviewed_commit, commit),
            f"{where} root was not independently accepted before enrollment",
        )
        v3.parse_time(acceptance["reviewed_at"], f"{where} acceptance.reviewed_at")
        sha256(acceptance["findings_sha256"], f"{where} acceptance.findings_sha256")
        require(
            digest(acceptance) == provenance["independent_review_receipt_sha256"],
            f"{where} independent review receipt is not content-derived",
        )
        result[derived] = {**root, "raw_key": raw_key}
    require({root["role"] for root in result.values()} == REQUIRED_ROLES, "both authority roles need enrolled roots")
    return result


def stable_object(item: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(item)
    metadata = value.get("metadata", {})
    if isinstance(metadata, dict):
        metadata.pop("managedFields", None)
    return value


def list_body(entry: dict[str, Any], expected_path: str, where: str) -> dict[str, Any]:
    require(entry["origin"] == "kubernetes-api", f"{where} origin must be Kubernetes")
    request = entry["request"]
    response = entry["response"]
    require(request["method"] == "GET" and request["path"] == expected_path, f"{where} request identity mismatch")
    require(entry["request_body"] is None, f"{where} GET request body must be empty")
    body = entry["body"]
    require(isinstance(body, dict) and isinstance(body.get("items"), list), f"{where} body must be a Kubernetes List")
    metadata = body.get("metadata")
    require(isinstance(metadata, dict), f"{where} list metadata missing")
    require(text(metadata.get("resourceVersion"), f"{where}.metadata.resourceVersion"), f"{where} resourceVersion missing")
    require(metadata.get("continue", "") == "", f"{where} pagination is incomplete")
    require(metadata.get("remainingItemCount", 0) in {0, None}, f"{where} remaining items exist")
    require(response["status"] == 200, f"{where} response status must be 200")
    return body


def verify_transcript_entry(entry: Any, cluster: dict[str, Any], where: str) -> dict[str, Any]:
    require(isinstance(entry, dict), f"{where} must be an object")
    exact_keys(entry, {"name", "origin", "transport", "request", "response"}, where)
    name = text(entry["name"], f"{where}.name")
    require(entry["origin"] in {"kubernetes-api", "nebius-iam"}, f"{where}.origin invalid")
    transport = entry["transport"]
    request = entry["request"]
    response = entry["response"]
    require(isinstance(transport, dict) and isinstance(request, dict) and isinstance(response, dict), f"{where} transcript sections invalid")
    exact_keys(transport, {"endpoint_sha256", "ca_sha256", "tls_spki_sha256"}, f"{where}.transport")
    exact_keys(request, {"method", "path", "headers", "body_base64", "body_sha256", "credential_subject_sha256"}, f"{where}.request")
    exact_keys(response, {"status", "content_type", "audit_id", "request_id", "observed_at", "body_base64", "body_sha256"}, f"{where}.response")
    for key in ("endpoint_sha256", "ca_sha256", "tls_spki_sha256"):
        sha256(transport[key], f"{where}.transport.{key}")
    if entry["origin"] == "kubernetes-api":
        require(transport["endpoint_sha256"] == cluster["api_server_sha256"], f"{where} API server mismatch")
        require(transport["ca_sha256"] == cluster["ca_sha256"], f"{where} Kubernetes CA mismatch")
    else:
        require(transport["endpoint_sha256"] == cluster["provider_iam_endpoint_sha256"], f"{where} provider IAM endpoint mismatch")
        require(transport["ca_sha256"] == cluster["provider_iam_ca_sha256"], f"{where} provider IAM CA mismatch")
    request_body_bytes = decode_base64(request["body_base64"], f"{where}.request.body_base64")
    headers = request["headers"]
    require(
        isinstance(headers, dict)
        and all(
            isinstance(key, str)
            and key == key.lower()
            and isinstance(value, str)
            and value
            for key, value in headers.items()
        ),
        f"{where}.request.headers must be a lowercase string map",
    )
    require(hashlib.sha256(request_body_bytes).hexdigest() == request["body_sha256"], f"{where}.request body digest mismatch")
    sha256(request["credential_subject_sha256"], f"{where}.request.credential_subject_sha256")
    require(request["method"] in {"GET", "POST"}, f"{where}.request.method invalid")
    require(isinstance(response["status"], int) and response["status"] in {200, 201}, f"{where}.response.status invalid")
    require(response["content_type"].startswith("application/json"), f"{where}.response.content_type invalid")
    text(response["audit_id"], f"{where}.response.audit_id")
    text(response["request_id"], f"{where}.response.request_id")
    observed = v3.parse_time(response["observed_at"], f"{where}.response.observed_at")
    body_bytes = decode_base64(response["body_base64"], f"{where}.response.body_base64")
    require(hashlib.sha256(body_bytes).hexdigest() == response["body_sha256"], f"{where}.response body digest mismatch")
    body = parse_json_bytes(body_bytes, f"{where}.response body")
    request_body = parse_json_bytes(request_body_bytes, f"{where}.request body") if request_body_bytes else None
    return {
        **entry,
        "name": name,
        "body": body,
        "request_body": request_body,
        "observed": observed,
        "body_bytes": body_bytes,
    }


def verify_namespace_inventory(entries: dict[str, dict[str, Any]]) -> tuple[list[str], str]:
    name = "k8s/namespaces/_cluster"
    require(name in entries, "authoritative Namespace list is missing")
    body = list_body(entries[name], NAMESPACE_ENDPOINT, name)
    normalized = []
    for index, item in enumerate(body["items"]):
        metadata = item.get("metadata", {})
        where = f"Namespace list item[{index}]"
        require(isinstance(metadata, dict), f"{where} metadata missing")
        normalized.append(
            {
                "name": text(metadata.get("name"), f"{where}.name"),
                "uid": text(metadata.get("uid"), f"{where}.uid"),
                "resource_version": text(metadata.get("resourceVersion"), f"{where}.resourceVersion"),
                "content_sha256": digest(stable_object(item)),
            }
        )
    normalized.sort(key=lambda item: (item["name"], item["uid"]))
    namespaces = [item["name"] for item in normalized]
    require(namespaces and len(namespaces) == len(set(namespaces)), "Namespace inventory must be non-empty and unique")
    return namespaces, digest(normalized)


def verify_service_account_inventory(
    entries: dict[str, dict[str, Any]],
    namespaces: list[str],
) -> tuple[list[dict[str, Any]], str]:
    normalized: list[dict[str, Any]] = []
    for namespace, endpoint in sorted(service_account_endpoints(namespaces).items()):
        entry_name = f"k8s/serviceaccounts/{namespace}"
        require(entry_name in entries, f"authoritative ServiceAccount list is missing for {namespace}")
        body = list_body(entries[entry_name], endpoint, entry_name)
        for index, item in enumerate(body["items"]):
            metadata = item.get("metadata", {})
            where = f"{entry_name}.items[{index}]"
            require(isinstance(metadata, dict), f"{where} metadata missing")
            require(metadata.get("namespace") == namespace, f"{where} namespace mismatch")
            normalized.append(
                {
                    "namespace": namespace,
                    "name": text(metadata.get("name"), f"{where}.name"),
                    "uid": text(metadata.get("uid"), f"{where}.uid"),
                    "resource_version": text(metadata.get("resourceVersion"), f"{where}.resourceVersion"),
                    "automount_service_account_token": item.get(
                        "automountServiceAccountToken", True
                    ),
                    "content_sha256": digest(stable_object(item)),
                }
            )
            require(
                type(normalized[-1]["automount_service_account_token"]) is bool,
                f"{where}.automountServiceAccountToken must be a boolean",
            )
    normalized.sort(key=lambda item: (item["namespace"], item["name"], item["uid"]))
    identities = [(item["namespace"], item["name"]) for item in normalized]
    require(len(identities) == len(set(identities)), "ServiceAccount inventory contains duplicate identities")
    return normalized, digest(normalized)


def verify_secret_metadata_inventory(
    entries: dict[str, dict[str, Any]],
    namespaces: list[str],
) -> tuple[list[dict[str, str]], str]:
    normalized: list[dict[str, str]] = []
    for namespace, endpoint in sorted(secret_metadata_endpoints(namespaces).items()):
        entry_name = f"k8s/secret-metadata/{namespace}"
        require(entry_name in entries, f"metadata-only Secret list is missing for {namespace}")
        entry = entries[entry_name]
        require(
            entry["request"]["headers"] == {"accept": SECRET_METADATA_ACCEPT},
            f"{entry_name} did not request PartialObjectMetadataList",
        )
        body = list_body(entry, endpoint, entry_name)
        require(
            body.get("apiVersion") == "meta.k8s.io/v1"
            and body.get("kind") == "PartialObjectMetadataList",
            f"{entry_name} returned secret-bearing objects instead of metadata-only objects",
        )
        for index, item in enumerate(body["items"]):
            where = f"{entry_name}.items[{index}]"
            require(
                isinstance(item, dict)
                and set(item) == {"apiVersion", "kind", "metadata"}
                and item.get("apiVersion") == "meta.k8s.io/v1"
                and item.get("kind") == "PartialObjectMetadata",
                f"{where} is not strict metadata-only Secret evidence",
            )
            metadata = item["metadata"]
            require(isinstance(metadata, dict) and metadata.get("namespace") == namespace, f"{where} namespace mismatch")
            normalized.append(
                {
                    "namespace": namespace,
                    "name": text(metadata.get("name"), f"{where}.name"),
                    "uid": text(metadata.get("uid"), f"{where}.uid"),
                    "resource_version": text(metadata.get("resourceVersion"), f"{where}.resourceVersion"),
                }
            )
    normalized.sort(key=lambda item: (item["namespace"], item["name"], item["uid"]))
    identities = [(item["namespace"], item["name"]) for item in normalized]
    require(len(identities) == len(set(identities)), "Secret metadata inventory contains duplicate identities")
    return normalized, digest(normalized)


def custodian_identities(
    entries: dict[str, dict[str, Any]],
    principals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    custodians: list[dict[str, Any]] = []
    for principal in principals:
        if principal["class"] != "custodian":
            continue
        identity_name = f"k8s/identity/{principal['id']}/selfsubjectreview"
        require(identity_name in entries, f"custodian identity transcript is missing: {principal['id']}")
        identity = subject_from_review(
            entries[identity_name]["body"],
            f"custodian {principal['id']}",
        )
        custodians.append({"id": principal["id"], "subject": principal["subject"], **identity})
    require(custodians, "principal inventory has no exact custodian")
    return custodians


def expected_transcript_names(
    principals: list[dict[str, Any]],
    namespaces: list[str],
    review_names: set[str],
) -> set[str]:
    names = {
        "k8s/database-pods/fs2-data",
        "k8s/networkpolicies/fs2-data",
        "k8s/namespaces/_cluster",
        "k8s/collector/selfsubjectreview",
        "nebius/legacy-group-membership",
    }
    names.update(
        f"k8s/workloads/{namespace}/{resource}"
        for namespace, resource in WORKLOAD_ENDPOINTS
    )
    names.update(
        f"k8s/rbac/{namespace or '_cluster'}/{resource}"
        for namespace, resource in rbac_endpoints(namespaces)
    )
    names.update(f"k8s/serviceaccounts/{namespace}" for namespace in namespaces)
    names.update(f"k8s/secret-metadata/{namespace}" for namespace in namespaces)
    for principal in principals:
        principal_id = principal["id"]
        names.add(f"k8s/identity/{principal_id}/selfsubjectreview")
        names.update(
            f"k8s/identity/{principal_id}/selfsubjectrulesreview/{namespace}"
            for namespace in namespaces
        )
        names.update(
            f"k8s/identity/{principal_id}/selfsubjectaccessreview/{review_name}"
            for review_name in review_names
        )
    names.update(
        f"k8s/collector/selfsubjectrulesreview/{namespace}"
        for namespace in namespaces
    )
    names.update(
        f"k8s/collector/selfsubjectaccessreview/{review_name}"
        for review_name in review_names
    )
    return names


def require_collector_credential(
    entries: dict[str, dict[str, Any]],
    collector: dict[str, Any],
) -> None:
    subject = {
        "uid": collector["uid"],
        "username": collector["username"],
        "groups": sorted(collector["groups"]),
        "extra_sha256": collector["extra_sha256"],
    }
    expected = digest(subject)
    require(collector["credential_subject_sha256"] == expected, "collector credential digest is not content-derived")
    for name, entry in entries.items():
        if name.startswith("k8s/identity/"):
            continue
        require(
            entry["request"]["credential_subject_sha256"] == expected,
            f"{name} was not collected by the signed collector identity",
        )


def verify_collector_identity(
    entries: dict[str, dict[str, Any]],
    collector: dict[str, Any],
    namespaces: list[str],
    reviews: dict[str, dict[str, str]],
) -> None:
    subject = {
        "uid": collector["uid"],
        "username": collector["username"],
        "groups": collector["groups"],
        "extra_sha256": collector["extra_sha256"],
    }
    subject_digest = digest(subject)
    identity_name = "k8s/collector/selfsubjectreview"
    identity_entry = entries[identity_name]
    require(
        identity_entry["origin"] == "kubernetes-api"
        and identity_entry["request"]["method"] == "POST"
        and identity_entry["request"]["path"] == SELF_SUBJECT_REVIEW_ENDPOINT
        and identity_entry["request_body"]
        == {"apiVersion": "authentication.k8s.io/v1", "kind": "SelfSubjectReview"}
        and identity_entry["request"]["credential_subject_sha256"] == subject_digest,
        "collector SelfSubjectReview request identity mismatch",
    )
    observed = subject_from_review(identity_entry["body"], identity_name)
    require(
        observed["username"] == collector["username"]
        and observed["uid"] == collector["uid"]
        and observed["groups"] == collector["groups"]
        and observed["extra_sha256"] == collector["extra_sha256"],
        "collector identity is not authenticator-derived",
    )
    for namespace in namespaces:
        name = f"k8s/collector/selfsubjectrulesreview/{namespace}"
        entry = entries[name]
        request = {
            "apiVersion": "authorization.k8s.io/v1",
            "kind": "SelfSubjectRulesReview",
            "spec": {"namespace": namespace},
        }
        require(
            entry["origin"] == "kubernetes-api"
            and entry["request"]["method"] == "POST"
            and entry["request"]["path"] == SELF_SUBJECT_RULES_REVIEW_ENDPOINT
            and entry["request_body"] == request
            and entry["request"]["credential_subject_sha256"] == subject_digest
            and entry["body"].get("status", {}).get("incomplete") is False,
            f"collector SSRR is incomplete or unauthenticated in {namespace}",
        )
    for review_name, attributes in reviews.items():
        name = f"k8s/collector/selfsubjectaccessreview/{review_name}"
        entry = entries[name]
        request = {
            "apiVersion": "authorization.k8s.io/v1",
            "kind": "SelfSubjectAccessReview",
            "spec": {"resourceAttributes": attributes},
        }
        require(
            entry["origin"] == "kubernetes-api"
            and entry["request"]["method"] == "POST"
            and entry["request"]["path"] == SELF_SUBJECT_ACCESS_REVIEW_ENDPOINT
            and entry["request_body"] == request
            and entry["request"]["credential_subject_sha256"] == subject_digest
            and type(entry["body"].get("status", {}).get("allowed")) is bool,
            f"collector SSAR is incomplete or unauthenticated: {review_name}",
        )
        require(
            entry["body"]["status"]["allowed"] is False
            and not entry["body"]["status"].get("evaluationError"),
            f"evidence collector retains dangerous authority: {review_name}",
        )


def normalize_workload(item: dict[str, Any], namespace: str, resource: str) -> dict[str, Any]:
    metadata = item.get("metadata", {})
    require(isinstance(metadata, dict), "workload metadata missing")
    if resource == "pods":
        labels = metadata.get("labels", {})
    elif resource == "cronjobs":
        labels = item.get("spec", {}).get("jobTemplate", {}).get("spec", {}).get("template", {}).get("metadata", {}).get("labels", {})
    else:
        labels = item.get("spec", {}).get("template", {}).get("metadata", {}).get("labels", {})
    require(isinstance(labels, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in labels.items()), "workload labels invalid")
    profile = v3.database_profile(labels)
    return {
        "namespace": namespace,
        "resource": resource,
        "name": text(metadata.get("name"), "workload name"),
        "uid": text(metadata.get("uid"), "workload UID"),
        "resource_version": text(metadata.get("resourceVersion"), "workload resourceVersion"),
        "effective_labels": labels,
        "effective_labels_sha256": digest(labels),
        "database_peer": profile is not None,
        "client_profile": profile,
        "disposition": "approved-current" if profile is not None else "not-a-database-peer",
    }


def receipt_from_items(body: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    metadata = body["metadata"]
    ordered = sorted(items, key=lambda item: (item["name"], item["uid"]))
    return {
        "resource_version": metadata["resourceVersion"],
        "continue_token": metadata.get("continue", ""),
        "remaining_item_count": metadata.get("remainingItemCount", 0) or 0,
        "item_count": len(ordered),
        "items_sha256": digest(ordered),
    }


def verify_workloads(entries: dict[str, dict[str, Any]], legacy: dict[str, Any]) -> list[dict[str, Any]]:
    expected_inventory = legacy["workload_inventory"]
    normalized_all: list[dict[str, Any]] = []
    expected_lists = {(item["namespace"], item["resource"]): item["list"] for item in expected_inventory["lists"]}
    for key, endpoint in sorted(WORKLOAD_ENDPOINTS.items()):
        namespace, resource = key
        name = f"k8s/workloads/{namespace}/{resource}"
        body = list_body(entries[name], endpoint, name)
        normalized = [normalize_workload(item, namespace, resource) for item in body["items"]]
        normalized_all.extend(normalized)
        require(receipt_from_items(body, normalized) == expected_lists[key], f"{name} receipt differs from raw API response")
    expected_objects = sorted(expected_inventory["objects"], key=lambda item: (item["namespace"], item["resource"], item["name"], item["uid"]))
    actual_objects = sorted(normalized_all, key=lambda item: (item["namespace"], item["resource"], item["name"], item["uid"]))
    require(actual_objects == expected_objects, "normalized workload inventory differs from raw API responses")
    for generation in expected_inventory["storage_generations"]:
        profile = v3.STORAGE_GENERATIONS[generation["component"]]
        selected = sorted(
            (item for item in actual_objects if item["client_profile"] == profile),
            key=lambda item: (item["namespace"], item["resource"], item["name"], item["uid"]),
        )
        require(generation["object_count"] == len(selected), f"{generation['component']} count is opaque")
        require(generation["receipt_sha256"] == digest(selected), f"{generation['component']} receipt is not content-derived")
    return actual_objects


def normalize_selector(value: Any) -> dict[str, Any]:
    selector = value if isinstance(value, dict) else {}
    return {
        "matchLabels": selector.get("matchLabels", {}),
        "matchExpressions": [
            {
                "key": expression["key"],
                "operator": expression["operator"],
                "values": expression.get("values", []),
            }
            for expression in selector.get("matchExpressions", [])
        ],
    }


def verify_network(entries: dict[str, dict[str, Any]], legacy: dict[str, Any], expected_spec_sha256: str) -> None:
    inventory = legacy["network_policy_inventory"]
    pod_body = list_body(entries["k8s/database-pods/fs2-data"], DATABASE_POD_ENDPOINT, "database Pod list")
    database_pods: list[dict[str, Any]] = []
    labels_by_uid: dict[str, dict[str, str]] = {}
    for pod in pod_body["items"]:
        metadata = pod["metadata"]
        labels = metadata.get("labels", {})
        require(labels.get("cnpg.io/cluster") == "fs2-control-db", "database Pod selector response contains another cluster")
        normalized = {
            "name": metadata["name"],
            "uid": metadata["uid"],
            "resource_version": metadata["resourceVersion"],
            "labels": labels,
            "labels_sha256": digest(labels),
        }
        database_pods.append(normalized)
        labels_by_uid[metadata["uid"]] = labels
    require(receipt_from_items(pod_body, database_pods) == inventory["database_pod_list"], "database Pod receipt is not API-derived")
    require(sorted(database_pods, key=lambda item: (item["name"], item["uid"])) == sorted(inventory["database_pods"], key=lambda item: (item["name"], item["uid"])), "database Pods differ")

    policy_body = list_body(entries["k8s/networkpolicies/fs2-data"], NETWORK_POLICY_ENDPOINT, "NetworkPolicy list")
    policies: list[dict[str, Any]] = []
    overlaps: list[dict[str, Any]] = []
    for policy in policy_body["items"]:
        metadata = policy["metadata"]
        spec = policy.get("spec", {})
        selector = normalize_selector(spec.get("podSelector", {}))
        selected = sorted(uid for uid, labels in labels_by_uid.items() if v3.selector_matches(selector, labels, metadata["name"]))
        overlaps_database = bool(selected)
        normalized = {
            "name": metadata["name"],
            "uid": metadata["uid"],
            "resource_version": metadata["resourceVersion"],
            "spec_sha256": digest(spec),
            "pod_selector": selector,
            "selector_sha256": digest(selector),
            "selected_database_pod_uids": selected,
            "selects_control_database": overlaps_database,
            "disposition": "canonical-exact" if overlaps_database and metadata["name"] == "fs2-control-db-ingress" else "non-overlapping",
        }
        if overlaps_database:
            overlaps.append(normalized)
        else:
            require(v3.selector_explicitly_excludes_database(selector), f"{metadata['name']} lacks permanent DB exclusion")
        policies.append(normalized)
    require(all(item["name"] == "fs2-control-db-ingress" for item in overlaps), "alternate policy selects the database")
    require(receipt_from_items(policy_body, policies) == inventory["list"], "NetworkPolicy receipt is not API-derived")
    require(sorted(policies, key=lambda item: (item["name"], item["uid"])) == sorted(inventory["items"], key=lambda item: (item["name"], item["uid"])), "NetworkPolicy inventory differs")
    require(inventory["planned_policy"]["spec_sha256"] == expected_spec_sha256, "planned policy digest is not source-derived")
    if overlaps:
        require(len(overlaps) == 1 and overlaps[0]["spec_sha256"] == expected_spec_sha256, "live canonical ingress spec differs")


def normalize_rbac(item: dict[str, Any], namespace: str, resource: str) -> dict[str, Any]:
    metadata = item["metadata"]
    return {
        "namespace": namespace,
        "resource": resource,
        "name": metadata["name"],
        "uid": metadata["uid"],
        "resource_version": metadata["resourceVersion"],
        "content_sha256": digest(stable_object(item)),
    }


def dangerous_rbac_subjects(raw_objects: dict[tuple[str, str], list[dict[str, Any]]]) -> list[dict[str, str]]:
    roles: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for (namespace, resource), items in raw_objects.items():
        if resource not in {"roles", "clusterroles"}:
            continue
        for item in items:
            scope = namespace if resource == "roles" else ""
            roles[(resource, scope, item["metadata"]["name"])] = item.get("rules", [])
    subjects: set[tuple[str, str, str]] = set()
    for (namespace, resource), items in raw_objects.items():
        if resource != "rolebindings":
            continue
        for binding in items:
            role_ref = binding.get("roleRef", {})
            role_resource = "roles" if role_ref.get("kind") == "Role" else "clusterroles"
            role_scope = namespace if role_resource == "roles" else ""
            rules = roles.get((role_resource, role_scope, role_ref.get("name", "")), [])
            dangerous = any(
                ({"impersonate", "bind", "escalate", "approve", "*"} & set(rule.get("verbs", [])))
                or (
                    {"create", "*"} & set(rule.get("verbs", []))
                    and {"serviceaccounts/token", "certificatesigningrequests", "*"}
                    & set(rule.get("resources", []))
                )
                or (
                    {"update", "patch", "*"} & set(rule.get("verbs", []))
                    and {"certificatesigningrequests/approval", "*"}
                    & set(rule.get("resources", []))
                )
                or (
                    {"sign", "*"} & set(rule.get("verbs", []))
                    and {"signers", "*"} & set(rule.get("resources", []))
                    and {"certificates.k8s.io", "*"}
                    & set(rule.get("apiGroups", []))
                )
                or (
                    {"get", "list", "watch", "*"} & set(rule.get("verbs", []))
                    and {"secrets", "*"} & set(rule.get("resources", []))
                    and {"", "*"} & set(rule.get("apiGroups", []))
                )
                or (
                    {"create", "update", "patch", "*"} & set(rule.get("verbs", []))
                    and {"serviceaccounts", "*"} & set(rule.get("resources", []))
                    and {"", "*"} & set(rule.get("apiGroups", []))
                )
                or credential_pivot_rule(rule)
                for rule in rules
            )
            if not dangerous:
                continue
            for subject in binding.get("subjects", []):
                subjects.add((subject.get("kind", ""), subject.get("namespace", ""), subject.get("name", "")))
    return [
        {"kind": kind, "namespace": namespace, "name": name}
        for kind, namespace, name in sorted(subjects)
    ]


def sensitive_mutation_subjects(raw_objects: dict[tuple[str, str], list[dict[str, Any]]]) -> list[dict[str, str]]:
    roles: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for (namespace, resource), items in raw_objects.items():
        if resource not in {"roles", "clusterroles"}:
            continue
        for item in items:
            scope = namespace if resource == "roles" else ""
            roles[(resource, scope, item["metadata"]["name"])] = item.get("rules", [])
    sensitive_resources = {
        "pods", "replicationcontrollers", "deployments", "statefulsets",
        "daemonsets", "replicasets", "jobs", "cronjobs", "networkpolicies",
        "secrets", "serviceaccounts", "serviceaccounts/token",
        *CREDENTIAL_PIVOT_RESOURCES,
        "roles", "rolebindings", "clusterroles", "clusterrolebindings",
        "validatingadmissionpolicies", "validatingadmissionpolicybindings",
    }
    mutation_verbs = {"create", "update", "patch", "delete", "deletecollection", "*"}
    subjects: set[tuple[str, str, str]] = set()
    for (namespace, resource), items in raw_objects.items():
        if resource != "rolebindings":
            continue
        for binding in items:
            role_ref = binding.get("roleRef", {})
            role_resource = "roles" if role_ref.get("kind") == "Role" else "clusterroles"
            role_scope = namespace if role_resource == "roles" else ""
            rules = roles.get((role_resource, role_scope, role_ref.get("name", "")), [])
            grants_mutation = any(
                mutation_verbs & set(rule.get("verbs", []))
                and ({"*"} | sensitive_resources) & set(rule.get("resources", []))
                for rule in rules
            )
            if not grants_mutation:
                continue
            for subject in binding.get("subjects", []):
                subjects.add((subject.get("kind", ""), subject.get("namespace", ""), subject.get("name", "")))
    return [
        {"kind": kind, "namespace": namespace, "name": name}
        for kind, namespace, name in sorted(subjects)
    ]


def verify_rbac(
    entries: dict[str, dict[str, Any]],
    legacy: dict[str, Any],
    authorization: dict[str, Any],
    namespaces: list[str],
) -> None:
    inventory = legacy["rbac_inventory"]
    expected_lists = {(item["namespace"], item["resource"]): item["list"] for item in inventory["lists"]}
    raw_objects: dict[tuple[str, str], list[dict[str, Any]]] = {}
    normalized_all: list[dict[str, Any]] = []
    for key, endpoint in sorted(rbac_endpoints(namespaces).items()):
        namespace, resource = key
        name = f"k8s/rbac/{namespace or '_cluster'}/{resource}"
        body = list_body(entries[name], endpoint, name)
        raw_objects[key] = body["items"]
        normalized = [normalize_rbac(item, namespace, resource) for item in body["items"]]
        normalized_all.extend(normalized)
        if key in expected_lists:
            require(receipt_from_items(body, normalized) == expected_lists[key], f"{name} receipt differs from raw API response")
    legacy_normalized = [item for item in normalized_all if (item["namespace"], item["resource"]) in v3.RBAC_LISTS]
    require(sorted(legacy_normalized, key=lambda item: (item["namespace"], item["resource"], item["name"], item["uid"])) == sorted(inventory["objects"], key=lambda item: (item["namespace"], item["resource"], item["name"], item["uid"])), "RBAC inventory differs from raw API responses")
    ordered_rbac = sorted(normalized_all, key=lambda item: (item["namespace"], item["resource"], item["name"], item["uid"]))
    require(digest(ordered_rbac) == authorization["rbac_inventory_sha256"], "v4 RBAC inventory digest is not content-derived")
    dangerous = dangerous_rbac_subjects(raw_objects)
    require(dangerous == authorization["dangerous_rbac_subjects"], "dangerous RBAC subject closure differs")
    sensitive = sensitive_mutation_subjects(raw_objects)
    require(sensitive == authorization["sensitive_mutation_subjects"], "sensitive mutation subject closure differs")
    admitted_subjects = {
        (principal["subject"]["kind"], principal["subject"]["namespace"], principal["subject"]["name"])
        for principal in inventory["principals"]
    }
    legacy_group = ("Group", "", inventory["legacy_group"])
    require(
        all((item["kind"], item["namespace"], item["name"]) in admitted_subjects | {legacy_group} for item in dangerous),
        "dangerous RBAC is bound to a subject outside the exact principal inventory",
    )
    require(
        all((item["kind"], item["namespace"], item["name"]) in admitted_subjects | {legacy_group} for item in sensitive),
        "sensitive workload, policy or RBAC mutation is bound to a broad or unknown subject",
    )


def subject_from_review(
    body: dict[str, Any], where: str, *, allow_empty_uid: bool = False
) -> dict[str, Any]:
    require(body.get("kind") == "SelfSubjectReview", f"{where} is not SelfSubjectReview")
    user_info = body.get("status", {}).get("userInfo", {})
    username = text(user_info.get("username"), f"{where}.username")
    uid = user_info.get("uid", "")
    require(
        isinstance(uid, str) and (allow_empty_uid or bool(uid)),
        f"{where}.uid invalid",
    )
    groups = sorted(v3.unique_strings(user_info.get("groups", []), f"{where}.groups"))
    extra = user_info.get("extra", {})
    require(isinstance(extra, dict), f"{where}.extra invalid")
    require(
        all(
            isinstance(key, str)
            and isinstance(values, list)
            and all(isinstance(value, str) for value in values)
            for key, values in extra.items()
        ),
        f"{where}.extra values must be string lists",
    )
    normalized_extra = {key: sorted(values) for key, values in sorted(extra.items())}
    return {
        "uid": uid,
        "username": username,
        "groups": groups,
        "extra": normalized_extra,
        "extra_sha256": digest(normalized_extra),
    }


def verify_identities(
    entries: dict[str, dict[str, Any]],
    legacy: dict[str, Any],
    authorization: dict[str, Any],
    namespaces: list[str],
    reviews: dict[str, dict[str, str]],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, dict[str, bool]]]:
    principals = legacy["rbac_inventory"]["principals"]
    rules_evidence: dict[str, Any] = {}
    access_evidence: dict[str, Any] = {}
    verified_identities: dict[str, dict[str, Any]] = {}
    signed_decisions: dict[str, dict[str, bool]] = {}
    capable: list[dict[str, Any]] = []
    for principal in principals:
        principal_id = principal["id"]
        identity_name = f"k8s/identity/{principal_id}/selfsubjectreview"
        identity_entry = entries[identity_name]
        require(identity_entry["origin"] == "kubernetes-api", f"{identity_name} origin mismatch")
        require(
            identity_entry["request"]["method"] == "POST"
            and identity_entry["request"]["path"] == SELF_SUBJECT_REVIEW_ENDPOINT
            and identity_entry["request_body"]
            == {"apiVersion": "authentication.k8s.io/v1", "kind": "SelfSubjectReview"},
            f"{identity_name} request is not the exact SelfSubjectReview",
        )
        identity = subject_from_review(
            identity_entry["body"],
            identity_name,
            allow_empty_uid=principal["class"] == "controller",
        )
        require(identity["username"] == principal["username"], f"{principal_id} username is not authenticator-derived")
        require(identity["groups"] == sorted(principal["groups"]), f"{principal_id} groups are not authenticator-derived")
        subject_digest = digest(
            {
                "uid": identity["uid"],
                "username": identity["username"],
                "groups": identity["groups"],
                "extra_sha256": identity["extra_sha256"],
            }
        )
        require(identity_entry["request"]["credential_subject_sha256"] == subject_digest, f"{principal_id} transcript credential mismatch")
        verified_identities[principal_id] = {
            "id": principal_id,
            "class": principal["class"],
            "subject": principal["subject"],
            **identity,
        }
        rules_evidence[principal_id] = {}
        for namespace in namespaces:
            rules_name = f"k8s/identity/{principal_id}/selfsubjectrulesreview/{namespace}"
            rules_entry = entries[rules_name]
            require(
                rules_entry["origin"] == "kubernetes-api"
                and rules_entry["request"]["method"] == "POST"
                and rules_entry["request"]["path"] == SELF_SUBJECT_RULES_REVIEW_ENDPOINT
                and rules_entry["request_body"]
                == {
                    "apiVersion": "authorization.k8s.io/v1",
                    "kind": "SelfSubjectRulesReview",
                    "spec": {"namespace": namespace},
                }
                and rules_entry["request"]["credential_subject_sha256"] == subject_digest,
                f"{rules_name} request identity mismatch",
            )
            rules = rules_entry["body"]
            require(rules.get("kind") == "SelfSubjectRulesReview", f"{rules_name} kind mismatch")
            require(rules.get("spec", {}).get("namespace") == namespace, f"{rules_name} namespace mismatch")
            status = rules.get("status", {})
            require(status.get("incomplete") is False, f"{rules_name} is incomplete")
            rules_evidence[principal_id][namespace] = status
        allowed: list[str] = []
        review_bodies: list[dict[str, Any]] = []
        signed_decisions[principal_id] = {}
        for review_name, attributes in reviews.items():
            access_name = f"k8s/identity/{principal_id}/selfsubjectaccessreview/{review_name}"
            access_entry = entries[access_name]
            require(
                access_entry["origin"] == "kubernetes-api"
                and access_entry["request"]["method"] == "POST"
                and access_entry["request"]["path"] == SELF_SUBJECT_ACCESS_REVIEW_ENDPOINT
                and access_entry["request_body"]
                == {
                    "apiVersion": "authorization.k8s.io/v1",
                    "kind": "SelfSubjectAccessReview",
                    "spec": {"resourceAttributes": attributes},
                }
                and access_entry["request"]["credential_subject_sha256"] == subject_digest,
                f"{access_name} request identity mismatch",
            )
            body = access_entry["body"]
            require(body.get("kind") == "SelfSubjectAccessReview", f"{access_name} kind mismatch")
            spec_attributes = body.get("spec", {}).get("resourceAttributes", {})
            require(spec_attributes == attributes, f"{access_name} request attributes mismatch")
            require(type(body.get("status", {}).get("allowed")) is bool, f"{access_name} has no authoritative decision")
            require(not body.get("status", {}).get("evaluationError"), f"{access_name} has an authorization evaluation error")
            signed_decisions[principal_id][review_name] = body["status"]["allowed"]
            if body["status"]["allowed"]:
                allowed.append(review_name)
            review_bodies.append(body)
        if principal["class"] != "custodian":
            unscoped = [name for name in allowed if not scoped_pod_connection_review(name)]
            require(
                not unscoped,
                f"non-custodian principal {principal_id} retains token, certificate, node-proxy or impersonation authority",
            )
        if allowed:
            capable.append(
                {
                    "principal_id": principal_id,
                    "guarded": True,
                    "receipt_sha256": digest(review_bodies),
                }
            )
        access_evidence[principal_id] = review_bodies
    require(digest(rules_evidence) == legacy["rbac_inventory"]["effective_permissions_sha256"], "effective permissions digest is opaque")
    require(digest(rules_evidence) == authorization["effective_permissions_sha256"], "v4 effective permissions digest is not content-derived")
    require(digest(access_evidence) == authorization["impersonation_receipts_sha256"], "v4 impersonation receipt digest is not content-derived")
    require(
        sorted(capable, key=lambda item: item["principal_id"])
        == sorted(legacy["rbac_inventory"]["impersonation"]["capable"], key=lambda item: item["principal_id"]),
        "impersonation receipts are not SSAR-derived",
    )
    require(legacy["rbac_inventory"]["impersonation"]["unaccounted"] == [], "unaccounted impersonation authority exists")
    provider_entry = entries["nebius/legacy-group-membership"]
    require(provider_entry["origin"] == "nebius-iam", "legacy group membership lacks provider origin")
    require(
        provider_entry["request"]["method"] == "POST"
        and provider_entry["request"]["path"] == "/nebius.iam.v1.GroupMembershipService/List"
        and provider_entry["request_body"]
        == {"group_id": legacy["rbac_inventory"]["legacy_group"], "page_size": 1000},
        "legacy group membership request is not authoritative or complete",
    )
    provider_body = provider_entry["body"]
    require(digest(provider_body) == authorization["provider_group_response_sha256"], "provider group receipt is not content-derived")
    require(
        provider_body.get("group") == legacy["rbac_inventory"]["legacy_group"]
        and provider_body.get("members") == []
        and provider_body.get("next_page_token", "") == "",
        "legacy broad group is not authoritatively empty",
    )
    require(legacy["rbac_inventory"]["legacy_group_members"] == [], "legacy packet contradicts provider membership")
    executor_id = authorization["executor_principal_id"]
    by_id = {principal["id"]: principal for principal in principals}
    require(executor_id in by_id and by_id[executor_id]["class"] == "custodian", "executor must be the exact signed custodian")
    return verified_identities[executor_id], verified_identities, signed_decisions


def verify_authorized_parents(parents: Any, workloads: list[dict[str, Any]], legacy: dict[str, Any]) -> list[dict[str, str]]:
    require(isinstance(parents, list) and parents, "authorized parent inventory must not be empty")
    live = {(item["namespace"], item["resource"], item["name"], item["uid"]): item for item in workloads}
    controller_usernames = {principal["username"] for principal in legacy["rbac_inventory"]["principals"] if principal["class"] == "controller"}
    expected_parent_keys = {
        (item["namespace"], item["resource"], item["name"], item["uid"])
        for item in workloads
        if item["database_peer"] is True and item["resource"] != "pods"
    }
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for index, parent in enumerate(parents):
        where = f"authorized_parents[{index}]"
        require(isinstance(parent, dict), f"{where} must be an object")
        exact_keys(parent, {"namespace", "resource", "api_version", "kind", "name", "uid", "controller_username"}, where)
        key = (parent["namespace"], parent["resource"], parent["name"], parent["uid"])
        require(key in live and key not in seen, f"{where} does not resolve to one unique live parent")
        require(live[key]["database_peer"] is True, f"{where} is not a database-client parent")
        require(parent["resource"] != "pods", f"{where} cannot authorize a Pod as an owner")
        require(
            (parent["api_version"], parent["kind"]) == WORKLOAD_TYPES[parent["resource"]],
            f"{where} API type does not match its inventoried resource",
        )
        require(parent["controller_username"] in controller_usernames, f"{where} controller identity is not exact")
        seen.add(key)
        normalized.append(parent)
    require(seen == expected_parent_keys, "authorized parent list is not the exact live database-controller closure")
    return sorted(normalized, key=lambda item: (item["namespace"], item["resource"], item["name"], item["uid"]))


def verify_source(payload: dict[str, Any], repository: Path) -> tuple[str, str]:
    source = payload["source"]
    require(isinstance(source, dict), "source must be an object")
    exact_keys(source, {"commit", "tree", "blobs"}, "source")
    commit = oid(source["commit"], "source.commit")
    tree = oid(source["tree"], "source.tree")
    require(commit not in REJECTED_COMMITS, "source commit is a preserved rejected candidate")
    require(git(repository, "rev-parse", "HEAD") == commit, "source commit differs from checkout HEAD")
    require(git(repository, "rev-parse", "HEAD^{tree}") == tree, "source tree differs from checkout tree")
    require(git(repository, "diff", "--name-only") == "", "tracked checkout differs from source commit")
    require(git(repository, "diff", "--cached", "--name-only") == "", "index differs from source commit")
    blobs = source["blobs"]
    require(isinstance(blobs, list), "source.blobs must be a list")
    bound = {item["path"]: item["oid"] for item in blobs if isinstance(item, dict) and set(item) == {"path", "oid"}}
    require(set(bound) == REQUIRED_SOURCE_PATHS, "v4 source blob closure mismatch")
    for path, expected in bound.items():
        require(git(repository, "rev-parse", f"{commit}:{path}") == oid(expected, f"source blob {path}"), f"source blob mismatch: {path}")
    return commit, tree


def verify_collector(
    collector: Any,
    roots: dict[str, dict[str, Any]],
    repository: Path,
    commit: str,
    tree: str,
) -> dict[str, Any]:
    require(isinstance(collector, dict), "collector must be an object")
    exact_keys(
        collector,
        {
            "root_key_id", "principal_id", "uid", "username", "groups", "extra_sha256",
            "credential_subject_sha256", "software_commit", "software_tree",
            "software_blob", "kubectl_sha256",
        },
        "collector",
    )
    root_key_id = text(collector["root_key_id"], "collector.root_key_id")
    require(root_key_id in roots and roots[root_key_id]["role"] == "evidence-collector", "collector root mismatch")
    require(collector["principal_id"] == roots[root_key_id]["principal_id"], "collector principal mismatch")
    text(collector["uid"], "collector.uid")
    text(collector["username"], "collector.username")
    groups = v3.unique_strings(collector["groups"], "collector.groups")
    require(groups == sorted(groups), "collector.groups must be sorted")
    require("system:authenticated" in groups, "collector must be authenticated")
    require(roots[root_key_id]["group_id"] in groups, "collector is not in its source-enrolled authority group")
    sha256(collector["extra_sha256"], "collector.extra_sha256")
    sha256(collector["credential_subject_sha256"], "collector.credential_subject_sha256")
    require(collector["software_commit"] == commit and collector["software_tree"] == tree, "collector software source mismatch")
    require(
        git(repository, "rev-parse", f"{commit}:k8s-inference/stages/workloads/scripts/sai20_database_authority_v4.py")
        == oid(collector["software_blob"], "collector.software_blob"),
        "collector software blob mismatch",
    )
    sha256(collector["kubectl_sha256"], "collector.kubectl_sha256")
    return collector


def verify_bundle(query: dict[str, str]) -> tuple[dict[str, str], dict[str, Any], dict[str, dict[str, Any]]]:
    repository = Path(query["repository_root"])
    registry_bytes = v3.safe_read(query["root_registry_path"], "root_registry_path", 1024 * 1024)
    require(hashlib.sha256(registry_bytes).hexdigest() == query["expected_root_registry_sha256"], "root registry bytes differ from source")
    registry = parse_json_bytes(registry_bytes, "root registry")
    head = git(repository, "rev-parse", "HEAD")
    roots = verify_root_registry(registry, repository, head)

    packet_bytes = v3.safe_read(query["authority_bundle_path"], "authority_bundle_path", 32 * 1024 * 1024)
    packet = parse_json_bytes(packet_bytes, "v4 authority bundle")
    require(isinstance(packet, dict), "v4 authority bundle must be an object")
    exact_keys(packet, {"payload", "payload_sha256", "signatures"}, "v4 authority bundle")
    payload = packet["payload"]
    require(isinstance(payload, dict), "v4 payload must be an object")
    exact_keys(
        payload,
        {
            "schema", "status", "observed_at", "valid_until", "source", "cluster", "collector",
            "legacy_v3_packet_sha256", "ingress_spec_sha256", "authorization", "authorized_parents",
            "independent_review", "transcript",
        },
        "v4 payload",
    )
    require(payload["schema"] == SCHEMA and payload["status"] == "ACCEPTED", "v4 bundle is not accepted")
    payload_sha = digest(payload)
    require(packet["payload_sha256"] == payload_sha, "v4 payload digest mismatch")
    signatures = packet["signatures"]
    require(isinstance(signatures, list) and len(signatures) == 2, "exactly two source-rooted signatures are required")
    signed_roles: set[str] = set()
    signed_principals: set[str] = set()
    role_principals: dict[str, str] = {}
    for index, signature in enumerate(signatures):
        where = f"signatures[{index}]"
        require(isinstance(signature, dict), f"{where} must be an object")
        exact_keys(signature, {"key_id", "role", "signature"}, where)
        key_id = text(signature["key_id"], f"{where}.key_id")
        require(key_id in roots, f"{where} key is not source-enrolled")
        root = roots[key_id]
        require(signature["role"] == root["role"], f"{where} role mismatch")
        require(root["role"] not in signed_roles and root["principal_id"] not in signed_principals, "signature roles/principals must be distinct")
        try:
            Ed25519PublicKey.from_public_bytes(root["raw_key"]).verify(
                decode_base64(signature["signature"], f"{where}.signature"),
                v3.canonical(payload),
            )
        except InvalidSignature as exc:
            raise v3.ContractError(f"{where} signature invalid") from exc
        signed_roles.add(root["role"])
        signed_principals.add(root["principal_id"])
        role_principals[root["role"]] = root["principal_id"]
    require(signed_roles == REQUIRED_ROLES, "collector and reviewer signatures are both required")

    observed = v3.parse_time(payload["observed_at"], "payload.observed_at")
    valid_until = v3.parse_time(payload["valid_until"], "payload.valid_until")
    now = datetime.now(timezone.utc)
    require(observed <= now <= valid_until, "v4 bundle is expired or not yet valid")
    require(valid_until - observed <= timedelta(minutes=30), "v4 bundle validity exceeds thirty minutes")
    require(now - observed <= timedelta(minutes=10), "v4 evidence is older than ten minutes")
    commit, tree = verify_source(payload, repository)

    legacy_bytes = v3.safe_read(query["legacy_v3_handoff_path"], "legacy_v3_handoff_path", 8 * 1024 * 1024)
    legacy_sha = hashlib.sha256(legacy_bytes).hexdigest()
    require(legacy_sha == payload["legacy_v3_packet_sha256"], "legacy v3 packet is not bound by v4 roots")
    legacy_packet = parse_json_bytes(legacy_bytes, "legacy v3 packet")
    require(isinstance(legacy_packet, dict), "legacy v3 packet must be an object")
    exact_keys(legacy_packet, {"payload", "payload_sha256", "signature"}, "legacy v3 packet")
    legacy = legacy_packet["payload"]
    require(isinstance(legacy, dict), "legacy v3 payload must be an object")
    require(legacy.get("schema") == v3.SCHEMA and legacy.get("status") == "ACCEPTED", "legacy v3 payload is not accepted")
    require(legacy_packet["payload_sha256"] == digest(legacy), "legacy v3 payload digest mismatch")
    require(legacy["source"]["commit"] == commit and legacy["source"]["tree"] == tree, "legacy packet source differs from v4")

    contract_bytes = v3.safe_read(query["ingress_contract_path"], "ingress_contract_path", 1024 * 1024)
    require(hashlib.sha256(contract_bytes).hexdigest() == query["expected_ingress_contract_file_sha256"], "ingress contract file differs from source")
    contract_text = contract_bytes.decode("utf-8").replace("${run_id}", query["run_id"])
    ingress_contract = parse_json_bytes(contract_text.encode(), "normalized ingress contract")
    ingress_sha = digest(ingress_contract)
    require(ingress_sha == payload["ingress_spec_sha256"], "ingress spec digest is not source-derived")

    cluster = payload["cluster"]
    require(isinstance(cluster, dict), "cluster must be an object")
    exact_keys(
        cluster,
        {
            "project_id", "cluster_id", "context_sha256", "api_server_sha256", "ca_sha256",
            "provider_iam_endpoint_sha256", "provider_iam_ca_sha256",
        },
        "cluster",
    )
    require(cluster["project_id"] == query["expected_project_id"] and cluster["cluster_id"] == query["expected_cluster_id"], "cluster identity mismatch")
    for field in ("context_sha256", "api_server_sha256", "ca_sha256", "provider_iam_endpoint_sha256", "provider_iam_ca_sha256"):
        sha256(cluster[field], f"cluster.{field}")
    require(
        all(legacy["cluster"][field] == cluster[field] for field in ("project_id", "cluster_id", "context_sha256", "api_server_sha256")),
        "legacy and v4 packets identify different Kubernetes targets",
    )
    collector = verify_collector(payload["collector"], roots, repository, commit, tree)

    transcript = payload["transcript"]
    require(isinstance(transcript, list), "transcript must be a list")
    entries: dict[str, dict[str, Any]] = {}
    observed_times: list[datetime] = []
    for index, raw_entry in enumerate(transcript):
        entry = verify_transcript_entry(raw_entry, cluster, f"transcript[{index}]")
        require(entry["name"] not in entries, f"duplicate transcript entry {entry['name']}")
        entries[entry["name"]] = entry
        observed_times.append(entry["observed"])
    require(observed_times and min(observed_times) >= observed and max(observed_times) <= valid_until, "transcript observations fall outside bundle window")
    namespaces, namespace_inventory_sha256 = verify_namespace_inventory(entries)
    service_accounts, service_account_inventory_sha256 = verify_service_account_inventory(entries, namespaces)
    secrets, secret_metadata_inventory_sha256 = verify_secret_metadata_inventory(entries, namespaces)
    custodians = custodian_identities(entries, legacy["rbac_inventory"]["principals"])
    reviews = dangerous_reviews(namespaces, service_accounts, secrets, custodians, entries)
    require(
        set(entries) == expected_transcript_names(
            legacy["rbac_inventory"]["principals"],
            namespaces,
            set(reviews),
        ),
        "transcript does not have the exact source-defined request closure",
    )
    require_collector_credential(entries, collector)
    verify_collector_identity(entries, collector, namespaces, reviews)

    workloads = verify_workloads(entries, legacy)
    verify_network(entries, legacy, ingress_sha)
    authorization = payload["authorization"]
    require(isinstance(authorization, dict), "authorization must be an object")
    exact_keys(
        authorization,
        {
            "executor_principal_id", "dangerous_rbac_subjects", "sensitive_mutation_subjects",
            "namespace_inventory_sha256", "service_account_inventory_sha256",
            "secret_metadata_inventory_sha256",
            "rbac_inventory_sha256", "effective_permissions_sha256",
            "impersonation_receipts_sha256", "provider_group_response_sha256",
        },
        "authorization",
    )
    for field in (
        "namespace_inventory_sha256", "service_account_inventory_sha256",
        "secret_metadata_inventory_sha256",
        "rbac_inventory_sha256", "effective_permissions_sha256",
        "impersonation_receipts_sha256", "provider_group_response_sha256",
    ):
        sha256(authorization[field], f"authorization.{field}")
    require(
        namespace_inventory_sha256 == authorization["namespace_inventory_sha256"],
        "Namespace inventory digest is not content-derived",
    )
    require(
        service_account_inventory_sha256 == authorization["service_account_inventory_sha256"],
        "ServiceAccount inventory digest is not content-derived",
    )
    require(
        secret_metadata_inventory_sha256 == authorization["secret_metadata_inventory_sha256"],
        "metadata-only Secret inventory digest is not content-derived",
    )
    verify_rbac(entries, legacy, authorization, namespaces)
    executor, principal_identities, signed_decisions = verify_identities(
        entries,
        legacy,
        authorization,
        namespaces,
        reviews,
    )
    parents = verify_authorized_parents(payload["authorized_parents"], workloads, legacy)

    review_body = payload["independent_review"]
    require(isinstance(review_body, dict), "independent_review must be an object")
    exact_keys(
        review_body,
        {"verdict", "commit", "tree", "reviewer_principal_id", "reviewed_at", "findings_sha256"},
        "independent_review",
    )
    require(
        review_body.get("verdict") == "ACCEPTED"
        and review_body.get("commit") == commit
        and review_body.get("tree") == tree
        and review_body.get("reviewer_principal_id") == role_principals["independent-reviewer"],
        "independent review transcript does not accept exact source",
    )
    reviewed_at = v3.parse_time(review_body["reviewed_at"], "independent_review.reviewed_at")
    require(observed <= reviewed_at <= valid_until, "independent review falls outside the evidence window")
    sha256(review_body["findings_sha256"], "independent_review.findings_sha256")
    require(digest(review_body) == legacy["authority"]["review_receipt_sha256"], "review receipt is opaque")
    result = {
        "verified": "true",
        "bundle_sha256": hashlib.sha256(packet_bytes).hexdigest(),
        "payload_sha256": payload_sha,
        "legacy_v3_packet_sha256": legacy_sha,
        "source_commit": commit,
        "source_tree": tree,
        "ingress_spec_sha256": ingress_sha,
        "namespace_inventory_sha256": namespace_inventory_sha256,
        "namespace_names_json": json.dumps(namespaces, separators=(",", ":")),
        "service_account_inventory_sha256": service_account_inventory_sha256,
        "service_account_names_json": json.dumps(
            [
                {"namespace": account["namespace"], "name": account["name"], "uid": account["uid"]}
                for account in service_accounts
            ],
            sort_keys=True,
            separators=(",", ":"),
        ),
        "secret_metadata_inventory_sha256": secret_metadata_inventory_sha256,
        "secret_names_json": json.dumps(
            [{"namespace": item["namespace"], "name": item["name"]} for item in secrets],
            sort_keys=True,
            separators=(",", ":"),
        ),
        "executor_principal_id": executor["id"],
        "executor_uid": executor["uid"],
        "executor_username": executor["username"],
        "executor_groups_json": json.dumps(executor["groups"], separators=(",", ":")),
        "executor_extra_json": json.dumps(executor["extra"], sort_keys=True, separators=(",", ":")),
        "executor_extra_sha256": executor["extra_sha256"],
        "authorized_parents_json": json.dumps(parents, sort_keys=True, separators=(",", ":")),
        "network_policy_specs_json": json.dumps(
            sorted(
                [
                    {"name": item["metadata"]["name"], "spec": item.get("spec", {})}
                    for item in entries["k8s/networkpolicies/fs2-data"]["body"]["items"]
                ]
                + (
                    []
                    if any(
                        item["metadata"]["name"] == "fs2-control-db-ingress"
                        for item in entries["k8s/networkpolicies/fs2-data"]["body"]["items"]
                    )
                    else [{"name": "fs2-control-db-ingress", "spec": ingress_contract}]
                ),
                key=lambda item: item["name"],
            ),
            sort_keys=True,
            separators=(",", ":"),
        ),
        "api_server_sha256": cluster["api_server_sha256"],
        "ca_sha256": cluster["ca_sha256"],
        "valid_until": payload["valid_until"],
    }
    return result, {
        "payload": payload,
        "legacy": legacy,
        "entries": entries,
        "executor": executor,
        "namespaces": namespaces,
        "secret_names": sorted((item["namespace"], item["name"]) for item in secrets),
        "service_accounts": service_accounts,
        "principal_identities": principal_identities,
        "authority_reviews": reviews,
        "signed_authority_decisions": signed_decisions,
    }, roots


def require_static_elf(descriptor: int, size: int, label: str) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    elf_header = os.read(descriptor, 64)
    require(
        len(elf_header) >= 52 and elf_header[:4] == b"\x7fELF",
        f"{label} must be a native ELF executable",
    )
    require(
        elf_header[4] in {1, 2} and elf_header[5] in {1, 2},
        f"{label} ELF class or byte order is invalid",
    )
    endian = "<" if elf_header[5] == 1 else ">"
    if elf_header[4] == 2:
        program_offset = struct.unpack_from(f"{endian}Q", elf_header, 32)[0]
        program_entry_size = struct.unpack_from(f"{endian}H", elf_header, 54)[0]
        program_count = struct.unpack_from(f"{endian}H", elf_header, 56)[0]
    else:
        program_offset = struct.unpack_from(f"{endian}I", elf_header, 28)[0]
        program_entry_size = struct.unpack_from(f"{endian}H", elf_header, 42)[0]
        program_count = struct.unpack_from(f"{endian}H", elf_header, 44)[0]
    require(
        0 < program_count <= 1024 and program_entry_size >= 4,
        f"{label} ELF program table is invalid",
    )
    require(
        program_offset + program_entry_size * program_count <= size,
        f"{label} ELF program table exceeds the authenticated snapshot",
    )
    for index in range(program_count):
        os.lseek(descriptor, program_offset + index * program_entry_size, os.SEEK_SET)
        program_header = os.read(descriptor, 4)
        require(len(program_header) == 4, f"{label} ELF program header is truncated")
        program_type = struct.unpack(f"{endian}I", program_header)[0]
        require(program_type != 3, f"{label} must be a static ELF with no external interpreter")
    os.lseek(descriptor, 0, os.SEEK_SET)


def validate_self_contained_kubeconfig(config_bytes: bytes, expected_context: str) -> None:
    try:
        config = yaml.safe_load(config_bytes)
    except yaml.YAMLError as exc:
        raise v3.ContractError("kubeconfig is not valid YAML") from exc
    require(isinstance(config, dict), "kubeconfig must be a YAML object")
    require(
        config.get("apiVersion") == "v1"
        and config.get("kind") == "Config"
        and config.get("current-context") == expected_context,
        "kubeconfig API identity or current context mismatch",
    )
    require(
        set(config) <= {
            "apiVersion", "kind", "preferences", "clusters", "contexts",
            "users", "current-context",
        },
        "kubeconfig contains an external extension or unsupported top-level field",
    )
    require(not config.get("extensions"), "kubeconfig extensions are forbidden")

    def named_items(field: str) -> dict[str, dict[str, Any]]:
        items = config.get(field)
        require(isinstance(items, list), f"kubeconfig {field} must be a list")
        normalized: dict[str, dict[str, Any]] = {}
        singular = {"clusters": "cluster", "contexts": "context", "users": "user"}[field]
        for index, item in enumerate(items):
            require(isinstance(item, dict), f"kubeconfig {field}[{index}] must be an object")
            require(set(item) == {"name", singular}, f"kubeconfig {field}[{index}] contains an external extension")
            name = text(item.get("name"), f"kubeconfig {field}[{index}].name")
            value = item.get(singular)
            require(isinstance(value, dict), f"kubeconfig {field}[{index}].{singular} must be an object")
            require(name not in normalized, f"kubeconfig {field} contains duplicate {name}")
            normalized[name] = value
        return normalized

    clusters = named_items("clusters")
    contexts = named_items("contexts")
    users = named_items("users")
    require(expected_context in contexts, "kubeconfig does not contain the selected context")
    selected_context = contexts[expected_context]
    require(
        set(selected_context) <= {"cluster", "user", "namespace"}
        and isinstance(selected_context.get("cluster"), str)
        and isinstance(selected_context.get("user"), str),
        "selected kubeconfig context contains an external extension or incomplete identity",
    )
    require(
        selected_context["cluster"] in clusters and selected_context["user"] in users,
        "selected kubeconfig context references an unknown cluster or user",
    )
    cluster = clusters[selected_context["cluster"]]
    require(
        set(cluster) == {"server", "certificate-authority-data"},
        "selected kubeconfig cluster must use only an inline CA and direct server",
    )
    server = text(cluster["server"], "kubeconfig cluster server")
    require(server.startswith("https://"), "kubeconfig cluster server must use HTTPS")
    ca_data = text(cluster["certificate-authority-data"], "kubeconfig inline CA")
    try:
        base64.b64decode(ca_data, validate=True)
    except ValueError as exc:
        raise v3.ContractError("kubeconfig inline CA must be canonical base64") from exc

    user = users[selected_context["user"]]
    token_identity = set(user) == {"token"} and isinstance(user.get("token"), str) and bool(user["token"])
    certificate_identity = set(user) == {"client-certificate-data", "client-key-data"}
    require(
        token_identity or certificate_identity,
        "selected kubeconfig user must use only an inline token or inline client certificate/key",
    )
    if certificate_identity:
        for field in ("client-certificate-data", "client-key-data"):
            value = text(user[field], f"kubeconfig user {field}")
            try:
                base64.b64decode(value, validate=True)
            except ValueError as exc:
                raise v3.ContractError(f"kubeconfig user {field} must be canonical base64") from exc


def prepare_kubectl(query: dict[str, str], context: dict[str, Any]) -> None:
    if "_kubectl_fd" in context:
        return
    binary = Path(query["kubectl_path"])
    require(binary.is_absolute(), "kubectl_path must be absolute")
    source_fd = os.open(str(binary), os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    sealed_fd = -1
    kubeconfig_source_fd = -1
    kubeconfig_fd = -1
    try:
        before = os.fstat(source_fd)
        require(stat.S_ISREG(before.st_mode), "kubectl must be a regular file")
        require(before.st_uid == 0, "kubectl must be owned by root")
        require(before.st_mode & 0o022 == 0, "kubectl must not be group- or world-writable")
        require(before.st_mode & 0o111 != 0, "kubectl must be executable")
        require(before.st_size > 0 and before.st_size <= 256 * 1024 * 1024, "kubectl size is invalid")
        sealed_fd = os.memfd_create("sai20-kubectl", os.MFD_ALLOW_SEALING)
        kubectl_hash = hashlib.sha256()
        remaining = before.st_size
        while remaining > 0:
            chunk = os.read(source_fd, min(1024 * 1024, remaining))
            require(chunk != b"", "kubectl changed during authenticated read")
            kubectl_hash.update(chunk)
            offset = 0
            while offset < len(chunk):
                written = os.write(sealed_fd, chunk[offset:])
                require(written > 0, "kubectl sealed copy write failed")
                offset += written
            remaining -= len(chunk)
        require(os.read(source_fd, 1) == b"", "kubectl exceeds its authenticated size")
        require(
            kubectl_hash.hexdigest() == context["payload"]["collector"]["kubectl_sha256"],
            "apply-time kubectl differs from the signed collector tool",
        )
        require_static_elf(sealed_fd, before.st_size, "kubectl")
        after = os.fstat(source_fd)
        require(
            (before.st_dev, before.st_ino, before.st_mode, before.st_uid, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_dev, after.st_ino, after.st_mode, after.st_uid, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
            "kubectl source changed during authenticated copy",
        )
        os.fchmod(sealed_fd, before.st_mode & 0o555)
        seal_mask = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
        fcntl.fcntl(sealed_fd, fcntl.F_ADD_SEALS, seal_mask)
        require(fcntl.fcntl(sealed_fd, fcntl.F_GET_SEALS) == seal_mask, "kubectl snapshot is not immutable")
        os.lseek(sealed_fd, 0, os.SEEK_SET)

        kubeconfig_path = query["kubeconfig_path"]
        require(
            re.fullmatch(r"/proc/[1-9][0-9]*/fd/[0-9]+", kubeconfig_path) is not None,
            "provider kubeconfig must be a launcher-owned descriptor path",
        )
        # /proc/<launcher>/fd/<fd> is necessarily a magic link. The exact
        # syntax is constrained above; the opened object, seals, owner and
        # memfd identity are checked below before any bytes are trusted.
        kubeconfig_source_fd = os.open(kubeconfig_path, os.O_RDONLY | os.O_CLOEXEC)
        kubeconfig_before = os.fstat(kubeconfig_source_fd)
        require(stat.S_ISREG(kubeconfig_before.st_mode), "kubeconfig must be a regular file")
        require(kubeconfig_before.st_uid == os.geteuid(), "provider kubeconfig memfd must be owned by the Terraform launcher user")
        require(kubeconfig_before.st_mode & 0o777 == 0o400, "provider kubeconfig memfd must be mode 0400")
        require(0 < kubeconfig_before.st_size <= 4 * 1024 * 1024, "kubeconfig size is invalid")
        seal_mask = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
        try:
            provider_seals = fcntl.fcntl(kubeconfig_source_fd, fcntl.F_GET_SEALS)
        except OSError as exc:
            raise v3.ContractError("provider kubeconfig is not a sealable memfd") from exc
        require(provider_seals == seal_mask, "provider kubeconfig descriptor is not immutable")
        provider_target = os.readlink(f"/proc/self/fd/{kubeconfig_source_fd}")
        require(
            provider_target.startswith("/memfd:sai20-provider-kubeconfig"),
            "provider kubeconfig is not the source-owned sealed memfd",
        )
        kubeconfig_fd = os.memfd_create("sai20-kubeconfig", os.MFD_ALLOW_SEALING)
        kubeconfig_hash = hashlib.sha256()
        kubeconfig_bytes = bytearray()
        remaining = kubeconfig_before.st_size
        while remaining > 0:
            chunk = os.read(kubeconfig_source_fd, min(1024 * 1024, remaining))
            require(chunk != b"", "kubeconfig changed during authenticated read")
            kubeconfig_hash.update(chunk)
            kubeconfig_bytes.extend(chunk)
            offset = 0
            while offset < len(chunk):
                written = os.write(kubeconfig_fd, chunk[offset:])
                require(written > 0, "kubeconfig sealed copy write failed")
                offset += written
            remaining -= len(chunk)
        require(os.read(kubeconfig_source_fd, 1) == b"", "kubeconfig exceeds its authenticated size")
        validate_self_contained_kubeconfig(bytes(kubeconfig_bytes), query["kube_context"])
        kubeconfig_bytes.clear()
        kubeconfig_after = os.fstat(kubeconfig_source_fd)
        require(
            (
                kubeconfig_before.st_dev,
                kubeconfig_before.st_ino,
                kubeconfig_before.st_mode,
                kubeconfig_before.st_uid,
                kubeconfig_before.st_size,
                kubeconfig_before.st_mtime_ns,
                kubeconfig_before.st_ctime_ns,
            )
            == (
                kubeconfig_after.st_dev,
                kubeconfig_after.st_ino,
                kubeconfig_after.st_mode,
                kubeconfig_after.st_uid,
                kubeconfig_after.st_size,
                kubeconfig_after.st_mtime_ns,
                kubeconfig_after.st_ctime_ns,
            ),
            "kubeconfig source changed during authenticated copy",
        )
        require(
            fcntl.fcntl(kubeconfig_source_fd, fcntl.F_GET_SEALS) == seal_mask,
            "provider kubeconfig descriptor lost its immutable seals",
        )
        os.fchmod(kubeconfig_fd, 0o400)
        fcntl.fcntl(kubeconfig_fd, fcntl.F_ADD_SEALS, seal_mask)
        require(fcntl.fcntl(kubeconfig_fd, fcntl.F_GET_SEALS) == seal_mask, "kubeconfig snapshot is not immutable")
        os.lseek(kubeconfig_fd, 0, os.SEEK_SET)
        context["_kubectl_fd"] = sealed_fd
        context["_kubeconfig_fd"] = kubeconfig_fd
        context["_kubeconfig_sha256"] = kubeconfig_hash.hexdigest()
        sealed_fd = -1
        kubeconfig_fd = -1
    finally:
        if kubeconfig_fd >= 0:
            os.close(kubeconfig_fd)
        if kubeconfig_source_fd >= 0:
            os.close(kubeconfig_source_fd)
        if sealed_fd >= 0:
            os.close(sealed_fd)
        os.close(source_fd)


def close_kubectl(context: dict[str, Any]) -> None:
    for field in ("_kubectl_fd", "_kubeconfig_fd"):
        descriptor = context.pop(field, None)
        if isinstance(descriptor, int):
            os.close(descriptor)


def kubectl(
    query: dict[str, str],
    context: dict[str, Any],
    args: list[str],
    stdin: bytes | None = None,
) -> bytes:
    prepare_kubectl(query, context)
    descriptor = context["_kubectl_fd"]
    kubeconfig_descriptor = context["_kubeconfig_fd"]
    completed = subprocess.run(
        [
            f"/proc/self/fd/{descriptor}",
            "--kubeconfig", f"/proc/self/fd/{kubeconfig_descriptor}",
            "--context", query["kube_context"],
            *args,
        ],
        input=stdin,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(descriptor, kubeconfig_descriptor),
        env={"HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C"},
    )
    require(completed.returncode == 0, f"apply-time Kubernetes re-observation failed for {' '.join(args[:2])}")
    return completed.stdout


def live_get(query: dict[str, str], context: dict[str, Any], path: str) -> bytes:
    return kubectl(query, context, ["get", "--raw", path])


def live_post(query: dict[str, str], context: dict[str, Any], path: str, body: dict[str, Any]) -> bytes:
    return kubectl(query, context, ["create", f"--raw={path}", "-f", "-"], v3.canonical(body))


def live_secret_names(query: dict[str, str], context: dict[str, Any]) -> list[tuple[str, str]]:
    output = kubectl(
        query,
        context,
        ["get", "secrets", "--all-namespaces", "--server-print=true", "--no-headers", "-o", "wide"],
    ).decode("utf-8")
    names: list[tuple[str, str]] = []
    for index, line in enumerate(output.splitlines()):
        if not line.strip():
            continue
        fields = line.split()
        require(len(fields) >= 2, f"metadata-only Secret table row {index} is malformed")
        names.append((fields[0], fields[1]))
    require(len(names) == len(set(names)), "metadata-only Secret table contains duplicate identities")
    return sorted(names)


def verify_apply_identity(query: dict[str, str], context: dict[str, Any]) -> None:
    payload = context["payload"]
    prepare_kubectl(query, context)

    config = parse_json_bytes(kubectl(query, context, ["config", "view", "--minify", "--flatten", "-o", "json"]), "apply kubeconfig view")
    require(config.get("current-context") == query["kube_context"], "apply kubeconfig current context mismatch")
    require(
        hashlib.sha256(query["kube_context"].encode()).hexdigest() == payload["cluster"]["context_sha256"],
        "apply kubeconfig context differs from the signed context",
    )
    clusters = config.get("clusters", [])
    require(isinstance(clusters, list) and len(clusters) == 1, "apply kubeconfig must select one cluster")
    cluster = clusters[0].get("cluster", {})
    require(hashlib.sha256(text(cluster.get("server"), "kubeconfig server").encode()).hexdigest() == payload["cluster"]["api_server_sha256"], "apply API server differs")
    ca_data = text(cluster.get("certificate-authority-data"), "kubeconfig CA")
    require(hashlib.sha256(base64.b64decode(ca_data)).hexdigest() == payload["cluster"]["ca_sha256"], "apply cluster CA differs")

    self_review_request = {"apiVersion": "authentication.k8s.io/v1", "kind": "SelfSubjectReview"}
    live_identity = subject_from_review(parse_json_bytes(live_post(query, context, SELF_SUBJECT_REVIEW_ENDPOINT, self_review_request), "apply SelfSubjectReview"), "apply executor")
    require(
        live_identity["uid"] == context["executor"]["uid"]
        and live_identity["username"] == context["executor"]["username"]
        and live_identity["groups"] == context["executor"]["groups"]
        and live_identity["extra_sha256"] == context["executor"]["extra_sha256"],
        "Terraform executor is not the exact signed custodian identity",
    )


def verify_apply(query: dict[str, str], context: dict[str, Any], _roots: dict[str, dict[str, Any]]) -> None:
    verify_apply_identity(query, context)

    for name, entry in context["entries"].items():
        if entry["origin"] != "kubernetes-api" or entry["request"]["method"] != "GET":
            continue
        if name.startswith("k8s/secret-metadata/"):
            continue
        path = entry["request"]["path"]
        current = parse_json_bytes(live_get(query, context, path), f"apply {name}")
        require(digest(stable_object(current)) == digest(stable_object(entry["body"])), f"apply-time re-observation differs: {name}")
    require(
        live_secret_names(query, context) == context["secret_names"],
        "metadata-only Secret inventory changed at apply",
    )

    for namespace in context["namespaces"]:
        request = {
            "apiVersion": "authorization.k8s.io/v1",
            "kind": "SelfSubjectRulesReview",
            "spec": {"namespace": namespace},
        }
        body = parse_json_bytes(live_post(query, context, SELF_SUBJECT_RULES_REVIEW_ENDPOINT, request), f"apply SSRR {namespace}")
        require(body.get("status", {}).get("incomplete") is False, f"apply SSRR {namespace} is incomplete")
        signed = context["entries"][f"k8s/identity/{context['executor']['id']}/selfsubjectrulesreview/{namespace}"]["body"]
        require(digest(body.get("status", {})) == digest(signed.get("status", {})), f"apply SSRR changed in {namespace}")
    for principal_id, identity in sorted(context["principal_identities"].items()):
        for review_name, attributes in context["authority_reviews"].items():
            request = {
                "apiVersion": "authorization.k8s.io/v1",
                "kind": "SubjectAccessReview",
                "spec": {
                    "user": identity["username"],
                    "uid": identity["uid"],
                    "groups": identity["groups"],
                    "extra": identity["extra"],
                    "resourceAttributes": attributes,
                },
            }
            body = parse_json_bytes(
                live_post(query, context, SUBJECT_ACCESS_REVIEW_ENDPOINT, request),
                f"apply SAR {principal_id}/{review_name}",
            )
            require(body.get("kind") == "SubjectAccessReview", f"apply SAR kind mismatch: {principal_id}/{review_name}")
            require(body.get("spec") == request["spec"], f"apply SAR subject or attributes changed: {principal_id}/{review_name}")
            status = body.get("status", {})
            require(type(status.get("allowed")) is bool, f"apply SAR has no decision: {principal_id}/{review_name}")
            require(not status.get("evaluationError"), f"apply SAR evaluation failed: {principal_id}/{review_name}")
            require(
                status["allowed"] == context["signed_authority_decisions"][principal_id][review_name],
                f"apply dangerous authority changed: {principal_id}/{review_name}",
            )
            if identity["class"] != "custodian" and not scoped_pod_connection_review(review_name):
                require(not status["allowed"], f"non-custodian gained dangerous authority at apply: {principal_id}/{review_name}")


def main() -> int:
    try:
        query = parse_json_bytes(sys.stdin.buffer.read(), "Terraform external query")
        require(isinstance(query, dict), "Terraform external query must be an object")
        common_keys = {
            "mode", "authority_bundle_path", "legacy_v3_handoff_path", "root_registry_path",
            "ingress_contract_path", "expected_root_registry_sha256",
            "expected_ingress_contract_file_sha256", "repository_root", "expected_project_id",
            "expected_cluster_id", "run_id",
        }
        apply_keys = {"kubeconfig_path", "kube_context", "kubectl_path", "apply_nonce"}
        require(set(query) == common_keys | (apply_keys if query.get("mode") in {"identity", "apply"} else set()), "Terraform external query keys invalid")
        require(query["mode"] in {"plan", "identity", "apply"}, "mode must be plan, identity or apply")
        result, context, roots = verify_bundle(query)
        if query["mode"] == "identity":
            verify_apply_identity(query, context)
            result["apply_nonce"] = text(query["apply_nonce"], "apply_nonce")
            result["sealed_kubeconfig_sha256"] = context["_kubeconfig_sha256"]
            result["identity_reobserved"] = "true"
            result["apply_reobserved"] = "false"
        elif query["mode"] == "apply":
            verify_apply(query, context, roots)
            result["apply_nonce"] = text(query["apply_nonce"], "apply_nonce")
            result["sealed_kubeconfig_sha256"] = context["_kubeconfig_sha256"]
            result["identity_reobserved"] = "true"
            result["apply_reobserved"] = "true"
        else:
            result["identity_reobserved"] = "false"
            result["apply_reobserved"] = "false"
        close_kubectl(context)
        json.dump(result, sys.stdout, sort_keys=True, separators=(",", ":"))
        sys.stdout.write("\n")
        return 0
    except (v3.ContractError, OSError, UnicodeError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        json.dump({"error": str(exc)}, sys.stderr, sort_keys=True)
        sys.stderr.write("\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
