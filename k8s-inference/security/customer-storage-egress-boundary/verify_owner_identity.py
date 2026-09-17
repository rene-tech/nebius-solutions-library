#!/usr/bin/env python3
"""Verify that security and workloads kubeconfigs are distinct authorities."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

SECURITY_ROOT = Path(__file__).resolve().parents[1]
if os.fspath(SECURITY_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(SECURITY_ROOT))

from rbac_authority import (  # noqa: E402
    reject_unapproved_dangerous,
    subject_authority,
    verify_subject_inventory,
)

MAX_KUBECONFIG_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024


def _open_regular_nofollow(path: Path) -> tuple[int, os.stat_result]:
    absolute = Path(os.path.abspath(os.fspath(path)))
    parts = absolute.parts[1:]
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("kubeconfig path is invalid")
    directory_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in parts[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        descriptor = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd
        )
    finally:
        os.close(directory_fd)
    metadata = os.fstat(descriptor)
    filesystem = os.fstatvfs(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_size <= 0
        or metadata.st_size > MAX_KUBECONFIG_BYTES
        or metadata.st_mode & 0o077
        or not filesystem.f_flag & getattr(os, "ST_RDONLY", 1)
    ):
        os.close(descriptor)
        raise ValueError(
            "kubeconfig must be a bounded root-owned private file on a read-only filesystem"
        )
    return descriptor, metadata


def _kubectl(path: Path, context: str, *arguments: str) -> str:
    descriptor, before = _open_regular_nofollow(path)
    try:
        command = [
            "kubectl",
            "--kubeconfig",
            f"/proc/self/fd/{descriptor}",
            "--context",
            context,
            *arguments,
        ]
        result = subprocess.run(  # noqa: S603 - fixed kubectl and bounded arguments.
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            pass_fds=(descriptor,),
        )
        after = os.fstat(descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise ValueError("kubeconfig changed during the descriptor-bound check")
        if result.returncode != 0:
            raise ValueError("Kubernetes identity preflight failed")
        if len(result.stdout.encode()) > MAX_OUTPUT_BYTES:
            raise ValueError("Kubernetes identity response exceeds its bound")
        return result.stdout.strip()
    finally:
        os.close(descriptor)


def _credential_sha256(path: Path) -> str:
    descriptor, before = _open_regular_nofollow(path)
    try:
        payload = b""
        while len(payload) <= MAX_KUBECONFIG_BYTES:
            chunk = os.read(
                descriptor,
                min(65536, MAX_KUBECONFIG_BYTES + 1 - len(payload)),
            )
            if not chunk:
                break
            payload += chunk
        after = os.fstat(descriptor)
        if (
            len(payload) > MAX_KUBECONFIG_BYTES
            or len(payload) != before.st_size
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise ValueError("kubeconfig changed during its descriptor-bound digest")
        return hashlib.sha256(payload).hexdigest()
    finally:
        os.close(descriptor)


def _identity(path: Path, context: str) -> dict[str, Any]:
    try:
        response = json.loads(_kubectl(path, context, "auth", "whoami", "-o", "json"))
        identity = response["status"]["userInfo"]
        username = identity["username"]
        groups = identity.get("groups", [])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Kubernetes identity response is invalid") from exc
    if not isinstance(username, str) or not username or not isinstance(groups, list):
        raise ValueError("Kubernetes identity response is incomplete")
    if any(not isinstance(group, str) for group in groups):
        raise ValueError("Kubernetes identity groups are invalid")
    return {"username": username, "groups": groups}


def _can_i(path: Path, context: str, *arguments: str) -> bool:
    return _kubectl(path, context, "auth", "can-i", *arguments) == "yes"


def _rbac_inventory(
    path: Path, context: str
) -> tuple[str, list[dict[str, str]], list[dict[str, Any]]]:
    """Hash RBAC and derive the exact binding-to-rule authority graph."""

    inventory: list[dict[str, Any]] = []
    subjects: set[tuple[str, str, str]] = set()
    for resource, namespaced in (
        ("roles.rbac.authorization.k8s.io", True),
        ("rolebindings.rbac.authorization.k8s.io", True),
        ("clusterroles.rbac.authorization.k8s.io", False),
        ("clusterrolebindings.rbac.authorization.k8s.io", False),
    ):
        arguments = ["get", resource]
        if namespaced:
            arguments.append("--all-namespaces")
        arguments.extend(("-o", "json"))
        try:
            response = json.loads(_kubectl(path, context, *arguments))
            items = response["items"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Kubernetes RBAC inventory response is invalid") from exc
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError("Kubernetes RBAC inventory items are invalid")
        for item in items:
            metadata = item.get("metadata", {})
            projection = {
                "apiVersion": item.get("apiVersion"),
                "kind": item.get("kind"),
                "metadata": {
                    "name": metadata.get("name"),
                    "namespace": metadata.get("namespace", ""),
                    "uid": metadata.get("uid"),
                    "resourceVersion": metadata.get("resourceVersion"),
                },
            }
            for field in ("aggregationRule", "roleRef", "rules", "subjects"):
                if field in item:
                    projection[field] = item[field]
            for subject in item.get("subjects", []):
                if not isinstance(subject, dict):
                    raise ValueError("Kubernetes RBAC subject is invalid")
                kind = subject.get("kind")
                name = subject.get("name")
                namespace = subject.get("namespace", "")
                if kind == "ServiceAccount" and not namespace:
                    namespace = metadata.get("namespace", "")
                if (
                    kind not in {"User", "Group", "ServiceAccount"}
                    or not isinstance(name, str)
                    or not name
                    or not isinstance(namespace, str)
                    or (kind == "ServiceAccount" and not namespace)
                    or (kind != "ServiceAccount" and namespace)
                ):
                    raise ValueError("Kubernetes RBAC subject identity is incomplete")
                subjects.add((kind, namespace, name))
            if any(
                not isinstance(projection["metadata"].get(field), str)
                or not projection["metadata"][field]
                for field in ("name", "uid", "resourceVersion")
            ):
                raise ValueError("Kubernetes RBAC inventory metadata is incomplete")
            inventory.append(projection)
    inventory.sort(
        key=lambda item: (
            str(item["kind"]),
            item["metadata"]["namespace"],
            item["metadata"]["name"],
        )
    )
    inventory_sha256 = hashlib.sha256(
        json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    subject_rows = [
        {"kind": kind, "namespace": namespace, "name": name}
        for kind, namespace, name in sorted(subjects)
    ]
    role_rules: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in inventory:
        if item["kind"] not in {"Role", "ClusterRole"}:
            continue
        metadata = item["metadata"]
        role_rules[(item["kind"], metadata["namespace"], metadata["name"])] = item.get(
            "rules", []
        )
    authority: list[dict[str, Any]] = []
    for item in inventory:
        if item["kind"] not in {"RoleBinding", "ClusterRoleBinding"}:
            continue
        metadata = item["metadata"]
        role_ref = item.get("roleRef", {})
        role_kind = role_ref.get("kind")
        role_name = role_ref.get("name")
        role_namespace = metadata["namespace"] if role_kind == "Role" else ""
        key = (str(role_kind), role_namespace, str(role_name))
        if key not in role_rules:
            raise ValueError("Kubernetes RBAC binding references an absent role")
        binding_subjects = item.get("subjects", [])
        for subject in binding_subjects:
            subject_namespace = subject.get("namespace", "")
            if subject.get("kind") == "ServiceAccount" and not subject_namespace:
                subject_namespace = metadata["namespace"]
            authority.append(
                {
                    "subject": {
                        "kind": subject.get("kind"),
                        "namespace": subject_namespace,
                        "name": subject.get("name"),
                    },
                    "scope": metadata["namespace"] if item["kind"] == "RoleBinding" else "*",
                    "binding": {
                        "kind": item["kind"],
                        "namespace": metadata["namespace"],
                        "name": metadata["name"],
                        "uid": metadata["uid"],
                    },
                    "roleRef": {
                        "kind": role_kind,
                        "namespace": role_namespace,
                        "name": role_name,
                    },
                    "rules": role_rules[key],
                }
            )
    authority.sort(
        key=lambda item: (
            item["subject"]["kind"],
            item["subject"]["namespace"],
            item["subject"]["name"],
            item["binding"]["kind"],
            item["binding"]["namespace"],
            item["binding"]["name"],
        )
    )
    return inventory_sha256, subject_rows, authority


def _rbac_inventory_sha256(path: Path, context: str) -> str:
    return _rbac_inventory(path, context)[0]


def _namespaces(path: Path, context: str) -> list[str]:
    try:
        response = json.loads(_kubectl(path, context, "get", "namespaces", "-o", "json"))
        values = sorted(
            item["metadata"]["name"] for item in response["items"]
        )
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Kubernetes namespace inventory is invalid") from exc
    if not values or values != sorted(set(values)) or any(not value for value in values):
        raise ValueError("Kubernetes namespace inventory is incomplete")
    return values


def _dangerous_permissions(
    path: Path, context: str, namespaces: list[str]
) -> list[str]:
    probes: dict[str, tuple[str, ...]] = {
        "impersonate-users": ("impersonate", "users"),
        "impersonate-groups": ("impersonate", "groups"),
        "impersonate-serviceaccounts": ("impersonate", "serviceaccounts"),
        "impersonate-uids": ("impersonate", "uids.authentication.k8s.io"),
        "impersonate-userextras": (
            "impersonate",
            "userextras.authentication.k8s.io",
        ),
        "bind-clusterroles": ("bind", "clusterroles.rbac.authorization.k8s.io"),
        "escalate-clusterroles": (
            "escalate",
            "clusterroles.rbac.authorization.k8s.io",
        ),
        "create-clusterrolebindings": (
            "create",
            "clusterrolebindings.rbac.authorization.k8s.io",
        ),
    }
    resource_verbs = {
        "secrets": (
            "get",
            "list",
            "watch",
            "create",
            "update",
            "patch",
            "delete",
            "deletecollection",
        ),
        "pods/exec": ("create",),
        "pods/attach": ("create",),
        "pods/portforward": ("create",),
        "pods/ephemeralcontainers": ("update", "patch"),
        "pods/binding": ("create",),
        "pods/eviction.policy": ("create",),
        "namespaces": ("create", "update", "patch", "delete"),
        "nodes": ("get", "list", "watch", "update", "patch", "delete"),
        "nodes/proxy": ("get", "create"),
        "nodes/status": ("update", "patch"),
        "serviceaccounts": ("create", "update", "patch", "delete", "deletecollection"),
        "serviceaccounts/token": ("create",),
        "pods": ("create", "update", "patch", "delete", "deletecollection"),
        "deployments.apps": ("create", "update", "patch", "delete", "deletecollection"),
        "replicasets.apps": ("create", "update", "patch", "delete", "deletecollection"),
        "daemonsets.apps": ("create", "update", "patch", "delete", "deletecollection"),
        "statefulsets.apps": ("create", "update", "patch", "delete", "deletecollection"),
        "jobs.batch": ("create", "update", "patch", "delete", "deletecollection"),
        "cronjobs.batch": ("create", "update", "patch", "delete", "deletecollection"),
        "networkpolicies.networking.k8s.io": (
            "update",
            "patch",
            "delete",
            "deletecollection",
        ),
        "configmaps": ("create", "update", "patch", "delete", "deletecollection"),
        "roles.rbac.authorization.k8s.io": (
            "create",
            "update",
            "patch",
            "delete",
            "deletecollection",
        ),
        "rolebindings.rbac.authorization.k8s.io": (
            "create",
            "update",
            "patch",
            "delete",
            "deletecollection",
        ),
        "clusterroles.rbac.authorization.k8s.io": (
            "create",
            "update",
            "patch",
            "delete",
            "deletecollection",
        ),
        "clusterrolebindings.rbac.authorization.k8s.io": (
            "create",
            "update",
            "patch",
            "delete",
            "deletecollection",
        ),
        "validatingadmissionpolicies.admissionregistration.k8s.io": (
            "create",
            "update",
            "patch",
            "delete",
            "deletecollection",
        ),
        "validatingadmissionpolicybindings.admissionregistration.k8s.io": (
            "create",
            "update",
            "patch",
            "delete",
            "deletecollection",
        ),
        "validatingwebhookconfigurations.admissionregistration.k8s.io": (
            "create",
            "update",
            "patch",
            "delete",
            "deletecollection",
        ),
        "mutatingwebhookconfigurations.admissionregistration.k8s.io": (
            "create",
            "update",
            "patch",
            "delete",
            "deletecollection",
        ),
        "certificatesigningrequests.certificates.k8s.io": (
            "create",
            "update",
            "patch",
            "delete",
            "deletecollection",
        ),
    }
    for resource, verbs in resource_verbs.items():
        for verb in verbs:
            cluster_scoped = resource.startswith(
                (
                    "cluster",
                    "validating",
                    "mutating",
                    "certificate",
                    "namespaces",
                    "nodes",
                )
            )
            if cluster_scoped:
                probes[f"{verb}-{resource}"] = (verb, resource)
            else:
                for namespace in namespaces:
                    probes[f"{verb}-{resource}:{namespace}"] = (
                        verb,
                        resource,
                        "--namespace",
                        namespace,
                    )
    for namespace in namespaces:
        probes[f"bind-roles:{namespace}"] = (
            "bind",
            "roles.rbac.authorization.k8s.io",
            "--namespace",
            namespace,
        )
        probes[f"escalate-roles:{namespace}"] = (
            "escalate",
            "roles.rbac.authorization.k8s.io",
            "--namespace",
            namespace,
        )
        probes[f"create-rolebindings:{namespace}"] = (
            "create",
            "rolebindings.rbac.authorization.k8s.io",
            "--namespace",
            namespace,
        )
    probes.update(
        {
            "approve-certificate-signing-requests": (
                "approve",
                "certificatesigningrequests/approval.certificates.k8s.io",
            ),
            "sign-kubernetes-signers": ("sign", "signers.certificates.k8s.io"),
            "approve-kubernetes-signers": ("approve", "signers.certificates.k8s.io"),
        }
    )
    return [name for name, arguments in probes.items() if _can_i(path, context, *arguments)]


def _protected_permissions(
    path: Path,
    context: str,
    names: dict[str, str],
) -> dict[str, bool]:
    namespace = names["namespace"]
    resources = {
        "policy": (
            "validatingadmissionpolicies.admissionregistration.k8s.io",
            names["boundary_policy"],
            None,
        ),
        "binding": (
            "validatingadmissionpolicybindings.admissionregistration.k8s.io",
            names["boundary_policy"],
            None,
        ),
        "workload-policy": (
            "validatingadmissionpolicies.admissionregistration.k8s.io",
            names["workload_policy"],
            None,
        ),
        "workload-binding": (
            "validatingadmissionpolicybindings.admissionregistration.k8s.io",
            names["workload_policy"],
            None,
        ),
        "contract": ("configmaps", names["contract"], namespace),
        "trust": ("configmaps", names["trust"], namespace),
        "network-policy": (
            "networkpolicies.networking.k8s.io",
            names["network_policy"],
            namespace,
        ),
        "release-role": (
            "roles.rbac.authorization.k8s.io",
            names["release_role"],
            namespace,
        ),
        "release-binding": (
            "rolebindings.rbac.authorization.k8s.io",
            names["release_role"],
            namespace,
        ),
        "release-serviceaccount": (
            "serviceaccounts",
            names["release_workload"],
            namespace,
        ),
        "release-deployment": (
            "deployments.apps",
            names["release_workload"],
            namespace,
        ),
        "release-record": (
            "configmaps",
            names["release_record"],
            namespace,
        ),
    }
    result: dict[str, bool] = {}
    for label, (resource, name, resource_namespace) in resources.items():
        for verb in ("create", "update", "patch", "delete"):
            arguments = [verb, resource]
            if verb != "create":
                arguments.extend(("--resource-name", name))
            if resource_namespace is not None:
                arguments.extend(("--namespace", resource_namespace))
            result[f"{verb}:{label}"] = _can_i(path, context, *arguments)
    return result


def verify(query: dict[str, str]) -> dict[str, str]:
    owner_group = query["security_owner_group"]
    names = json.loads(query["protected_names_json"])
    declared_identities = json.loads(query["identity_inventory_json"])
    declared_service_accounts = json.loads(query["service_account_inventory_json"])
    declared_system_subjects = json.loads(query["system_subject_inventory_json"])
    controller_identities = json.loads(query["controller_identities_json"])
    if (
        not isinstance(names, dict)
        or not isinstance(declared_identities, dict)
        or not isinstance(declared_service_accounts, list)
        or not isinstance(declared_system_subjects, list)
        or not isinstance(controller_identities, dict)
    ):
        raise ValueError("identity inventory or protected names are invalid")
    owner_declarations = [
        item
        for item in declared_identities.values()
        if isinstance(item, dict) and item.get("category") == "owner"
    ]
    if len(owner_declarations) != 1:
        raise ValueError("identity inventory must declare exactly one owner")
    owner_declaration = owner_declarations[0]
    namespaces = _namespaces(
        Path(owner_declaration["kubeconfig_path"]),
        owner_declaration["kube_context"],
    )
    checked: list[dict[str, str]] = []
    categories: list[str] = []
    for name, item in declared_identities.items():
        if (
            not isinstance(name, str)
            or not isinstance(item, dict)
            or set(item)
            != {
                "kubeconfig_path",
                "kube_context",
                "username",
                "groups",
                "category",
                "credential_sha256",
                "provider_principal_id",
            }
            or any(
                not isinstance(item.get(field), str) or not item[field]
                for field in (
                    "kubeconfig_path",
                    "kube_context",
                    "username",
                    "category",
                    "credential_sha256",
                    "provider_principal_id",
                )
            )
            or not isinstance(item.get("groups"), list)
            or not item["groups"]
            or item["groups"] != sorted(set(item["groups"]))
            or item["category"]
            not in {"owner", "workloads", "release", "human", "break-glass", "other"}
        ):
            raise ValueError("Kubernetes identity inventory is malformed")
        path = Path(item["kubeconfig_path"])
        context = item["kube_context"]
        if _credential_sha256(path) != item["credential_sha256"]:
            raise ValueError(f"{name} credential bytes differ from the signed inventory")
        identity = _identity(path, context)
        if identity["username"] != item["username"]:
            raise ValueError(f"{name} identity differs from its declared username")
        if sorted(identity["groups"]) != item["groups"]:
            raise ValueError(f"{name} authenticated groups differ from the signed inventory")
        protected = _protected_permissions(path, context, names)
        dangerous = _dangerous_permissions(path, context, namespaces)
        if item["category"] == "owner":
            if owner_group not in identity["groups"]:
                raise ValueError("security-owner credential lacks the dedicated owner group")
            owner_create_labels = {
                "policy", "binding", "workload-policy", "workload-binding",
                "contract", "trust", "network-policy", "release-role",
                "release-binding",
            }
            if not all(
                protected[f"create:{label}"] for label in owner_create_labels
            ):
                raise ValueError("security-owner credential cannot add every protected object")
            if any(
                value
                for key, value in protected.items()
                if key.startswith(("update:", "delete:"))
            ):
                raise ValueError(
                    "security-owner credential can update or delete an existing generation"
                )
        else:
            if owner_group in identity["groups"]:
                raise ValueError(f"{name} identity aliases the security-owner group")
            admission_mediated = {
                "create:contract",
                "create:trust",
                "create:network-policy",
            }
            if item["category"] == "release":
                # The already-active boundary matches every ConfigMap request
                # by this identity and admits only the content-named Helm v1
                # record. This is an exact admission-mediated permission, not
                # a namespace-wide ConfigMap exemption.
                admission_mediated.update(
                    {
                        "create:release-serviceaccount",
                        "create:release-deployment",
                        "create:release-record",
                        "update:release-record",
                        "patch:release-record",
                    }
                )
            forbidden = {
                key: value
                for key, value in protected.items()
                if key not in admission_mediated
            }
            if any(forbidden.values()):
                raise ValueError(f"{name} identity can mutate a protected generation")
        if item["category"] == "owner":
            owner_cluster_create_or_apply = {
                "create-validatingadmissionpolicies.admissionregistration.k8s.io",
                "patch-validatingadmissionpolicies.admissionregistration.k8s.io",
                "create-validatingadmissionpolicybindings.admissionregistration.k8s.io",
                "patch-validatingadmissionpolicybindings.admissionregistration.k8s.io",
            }
            owner_namespace_create_or_apply = {
                "create-configmaps",
                "create-roles.rbac.authorization.k8s.io",
                "create-rolebindings.rbac.authorization.k8s.io",
                "create-rolebindings",
            }
            dangerous = [
                permission
                for permission in dangerous
                if permission not in owner_cluster_create_or_apply
                and not (
                    permission.rsplit(":", 1)[-1] == names["namespace"]
                    and permission.rsplit(":", 1)[0]
                    in owner_namespace_create_or_apply
                )
            ]
        elif item["category"] == "release":
            release_namespace_create = {
                "create-serviceaccounts",
                "create-deployments.apps",
                "create-configmaps",
            }
            dangerous = [
                permission
                for permission in dangerous
                if not (
                    permission.rsplit(":", 1)[-1] == names["namespace"]
                    and permission.rsplit(":", 1)[0]
                    in release_namespace_create
                )
            ]
        if dangerous:
            raise ValueError(f"{name} identity has dangerous authority: {dangerous[0]}")
        categories.append(item["category"])
        checked.append(
            {
                "name": name,
                "category": item["category"],
                "username": identity["username"],
                "groups": item["groups"],
                "credential_sha256": item["credential_sha256"],
                "provider_principal_id": item["provider_principal_id"],
            }
        )

    required_categories = {"owner", "workloads", "release", "human", "break-glass", "other"}
    if (
        set(categories) != required_categories
        or categories.count("owner") != 1
        or categories.count("workloads") != 1
        or len({item["username"] for item in checked}) != len(checked)
        or len({item["credential_sha256"] for item in checked}) != len(checked)
        or len({item["provider_principal_id"] for item in checked}) != len(checked)
    ):
        raise ValueError("Kubernetes identity inventory is not exhaustive and disjoint")

    checked.sort(key=lambda item: item["name"])
    owner = next(item for item in checked if item["category"] == "owner")
    workloads = next(item for item in checked if item["category"] == "workloads")
    rbac_inventory_sha256, rbac_subjects, effective_authority = _rbac_inventory(
        Path(owner_declaration["kubeconfig_path"]),
        owner_declaration["kube_context"],
    )
    if rbac_inventory_sha256 != query["expected_rbac_inventory_sha256"]:
        raise ValueError("live cluster RBAC inventory differs from the signed receipt")
    effective_authority_sha256 = hashlib.sha256(
        json.dumps(effective_authority, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if effective_authority_sha256 != query["expected_effective_authority_sha256"]:
        raise ValueError("live RBAC effective authority differs from the signed receipt")

    for identity in checked:
        _, semantic_dangerous = subject_authority(
            effective_authority,
            kind="User",
            namespace="",
            name=identity["username"],
            groups=identity["groups"],
        )
        allowed: set[str] = set()
        if identity["category"] == "owner":
            policy_names = ",".join(
                sorted({names["boundary_policy"], names["workload_policy"]})
            )
            allowed.update(
                {
                    "*|admissionregistration.k8s.io|validatingadmissionpolicies|create|names=*",
                    "*|admissionregistration.k8s.io|validatingadmissionpolicybindings|create|names=*",
                    f"*|admissionregistration.k8s.io|validatingadmissionpolicies|patch|names={policy_names}",
                    f"*|admissionregistration.k8s.io|validatingadmissionpolicybindings|patch|names={policy_names}",
                    f"{names['namespace']}|core|configmaps|create|names=*",
                    f"{names['namespace']}|rbac.authorization.k8s.io|roles|create|names=*",
                    f"{names['namespace']}|rbac.authorization.k8s.io|rolebindings|create|names=*",
                }
            )
        elif identity["category"] == "release":
            allowed.update(
                {
                    f"{names['namespace']}|core|serviceaccounts|create|names=*",
                    f"{names['namespace']}|apps|deployments|create|names=*",
                    f"{names['namespace']}|core|configmaps|create|names=*",
                    f"{names['namespace']}|core|configmaps|update|names={names['release_record']}",
                    f"{names['namespace']}|core|configmaps|patch|names={names['release_record']}",
                }
            )
        reject_unapproved_dangerous(
            semantic_dangerous,
            explicitly_allowed=allowed,
        )

    authorized_users = {item["username"] for item in checked}
    authorized_groups = {group for item in checked for group in item["groups"]}
    authorized_service_accounts: set[tuple[str, str]] = set()
    for subject in declared_service_accounts:
        if (
            not isinstance(subject, dict)
            or set(subject)
            != {
                "namespace",
                "name",
                "uid",
                "owner",
                "groups",
                "effective_authority_sha256",
                "dangerous_permissions",
            }
            or any(
                not isinstance(subject.get(field), str) or not subject[field]
                for field in ("namespace", "name", "uid", "owner")
            )
            or not isinstance(subject.get("groups"), list)
            or subject["groups"] != sorted(set(subject["groups"]))
            or not isinstance(subject.get("effective_authority_sha256"), str)
            or len(subject["effective_authority_sha256"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in subject["effective_authority_sha256"]
            )
            or not isinstance(subject.get("dangerous_permissions"), list)
            or subject["dangerous_permissions"]
            != sorted(set(subject["dangerous_permissions"]))
            or any(
                not isinstance(permission, str) or not permission
                for permission in subject["dangerous_permissions"]
            )
        ):
            raise ValueError("signed ServiceAccount inventory is malformed")
        authorized_service_accounts.add((subject["namespace"], subject["name"]))
        authorized_groups.update(subject["groups"])
    if len(authorized_service_accounts) != len(declared_service_accounts):
        raise ValueError("signed ServiceAccount inventory contains duplicate subjects")
    seen_system_subjects: set[tuple[str, str]] = set()
    for subject in declared_system_subjects:
        if (
            not isinstance(subject, dict)
            or set(subject)
            != {
                "kind",
                "name",
                "uid",
                "namespace",
                "owner",
                "groups",
                "effective_authority_sha256",
                "dangerous_permissions",
            }
            or subject.get("kind") not in {"User", "Group"}
            or not isinstance(subject.get("name"), str)
            or not subject["name"].startswith("system:")
            or subject.get("namespace") != ""
            or not isinstance(subject.get("uid"), str)
            or not subject["uid"]
            or not isinstance(subject.get("owner"), str)
            or not subject["owner"]
            or not isinstance(subject.get("groups"), list)
            or subject["groups"] != sorted(set(subject["groups"]))
            or not isinstance(subject.get("effective_authority_sha256"), str)
            or len(subject["effective_authority_sha256"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in subject["effective_authority_sha256"]
            )
            or not isinstance(subject.get("dangerous_permissions"), list)
            or subject["dangerous_permissions"]
            != sorted(set(subject["dangerous_permissions"]))
            or any(
                not isinstance(permission, str) or not permission
                for permission in subject["dangerous_permissions"]
            )
        ):
            raise ValueError("signed Kubernetes system-subject inventory is malformed")
        identity = (subject["kind"], subject["name"])
        if identity in seen_system_subjects:
            raise ValueError("signed Kubernetes system-subject inventory has duplicates")
        seen_system_subjects.add(identity)
        if subject["kind"] == "User":
            authorized_users.add(subject["name"])
            authorized_groups.update(subject["groups"])
        else:
            authorized_groups.add(subject["name"])
    verify_subject_inventory(
        declared_service_accounts,
        declared_system_subjects,
        effective_authority,
        controller_identities=controller_identities,
    )
    for subject in rbac_subjects:
        if subject["kind"] == "User" and subject["name"] not in authorized_users:
            raise ValueError("live RBAC has an undeclared User subject")
        if subject["kind"] == "Group" and subject["name"] not in authorized_groups:
            raise ValueError("live RBAC has an undeclared Group subject")
        if subject["kind"] == "ServiceAccount" and (
            subject["namespace"], subject["name"]
        ) not in authorized_service_accounts:
            raise ValueError("live RBAC has an undeclared ServiceAccount subject")

    return {
        "authorized": "true",
        "security_owner_subject_sha256": hashlib.sha256(
            owner["username"].encode()
        ).hexdigest(),
        "workloads_subject_sha256": hashlib.sha256(
            workloads["username"].encode()
        ).hexdigest(),
        "identity_inventory_sha256": hashlib.sha256(
            json.dumps(checked, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "rbac_inventory_sha256": rbac_inventory_sha256,
        "rbac_subjects_sha256": hashlib.sha256(
            json.dumps(rbac_subjects, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "rbac_effective_authority_sha256": effective_authority_sha256,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--terraform-external", action="store_true")
    args = parser.parse_args()
    if not args.terraform_external:
        parser.error("--terraform-external is required")
    try:
        query = json.load(sys.stdin)
        if not isinstance(query, dict) or any(
            not isinstance(value, str) for value in query.values()
        ):
            raise ValueError("identity query must contain string values")
        print(json.dumps(verify(query), sort_keys=True))
        return 0
    except (KeyError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(
            f"customer-storage security-owner identity rejected: {exc}", file=sys.stderr
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
