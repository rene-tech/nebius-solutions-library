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

MAX_KUBECONFIG_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024


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
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > MAX_KUBECONFIG_BYTES
        or metadata.st_mode & 0o077
    ):
        os.close(descriptor)
        raise ValueError("kubeconfig must be a bounded owner-only regular file")
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


def _dangerous_permissions(path: Path, context: str) -> list[str]:
    probes = {
        "impersonate-users": ("impersonate", "users.authentication.k8s.io"),
        "impersonate-groups": ("impersonate", "groups.authentication.k8s.io"),
        "impersonate-serviceaccounts": (
            "impersonate",
            "serviceaccounts.authentication.k8s.io",
        ),
        "impersonate-uids": ("impersonate", "uids.authentication.k8s.io"),
        "impersonate-userextras": (
            "impersonate",
            "userextras.authentication.k8s.io",
        ),
        "serviceaccount-tokenrequest": ("create", "serviceaccounts/token"),
        "bind-clusterroles": ("bind", "clusterroles.rbac.authorization.k8s.io"),
        "escalate-clusterroles": (
            "escalate",
            "clusterroles.rbac.authorization.k8s.io",
        ),
        "bind-roles": ("bind", "roles.rbac.authorization.k8s.io", "--all-namespaces"),
        "escalate-roles": (
            "escalate",
            "roles.rbac.authorization.k8s.io",
            "--all-namespaces",
        ),
        "create-clusterrolebindings": (
            "create",
            "clusterrolebindings.rbac.authorization.k8s.io",
        ),
        "create-rolebindings": (
            "create",
            "rolebindings.rbac.authorization.k8s.io",
            "--all-namespaces",
        ),
    }
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
        "contract": ("configmaps", names["contract"], namespace),
        "trust": ("configmaps", names["trust"], namespace),
        "network-policy": (
            "networkpolicies.networking.k8s.io",
            names["network_policy"],
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
    owner_path = Path(query["security_owner_kubeconfig_path"])
    workloads_path = Path(query["workloads_kubeconfig_path"])
    owner = _identity(owner_path, query["security_owner_kube_context"])
    workloads = _identity(workloads_path, query["workloads_kube_context"])
    owner_group = query["security_owner_group"]
    names = json.loads(query["protected_names_json"])
    other_identities = json.loads(query["non_owner_identities_json"])
    if not isinstance(names, dict) or not isinstance(other_identities, dict):
        raise ValueError("identity inventory or protected names are invalid")

    if owner["username"] == workloads["username"]:
        raise ValueError("security-owner and workloads subjects must differ")
    if owner_group not in owner["groups"]:
        raise ValueError("security-owner credential lacks the dedicated owner group")
    if owner_group in workloads["groups"]:
        raise ValueError("workloads credential is a member of the security-owner group")
    owner_permissions = _protected_permissions(
        owner_path, query["security_owner_kube_context"], names
    )
    if not all(value for key, value in owner_permissions.items() if key.startswith("create:")):
        raise ValueError("security-owner credential cannot add every protected object")
    if any(value for key, value in owner_permissions.items() if not key.startswith("create:")):
        raise ValueError("security-owner credential can mutate or delete an existing generation")
    if _dangerous_permissions(owner_path, query["security_owner_kube_context"]):
        raise ValueError("security-owner credential has impersonation, token, bind or escalation authority")

    checked_nonowners: list[dict[str, str]] = []
    candidates: dict[str, dict[str, str]] = {
        "workloads": {
            "path": query["workloads_kubeconfig_path"],
            "context": query["workloads_kube_context"],
            "username": workloads["username"],
        }
    }
    for name, item in other_identities.items():
        if (
            not isinstance(name, str)
            or name == "workloads"
            or not isinstance(item, dict)
            or set(item) != {"kubeconfig_path", "kube_context", "username", "category"}
            or any(not isinstance(value, str) or not value for value in item.values())
            or item["category"] not in {"release", "human", "break-glass", "other"}
        ):
            raise ValueError("non-owner identity inventory is malformed")
        candidates[name] = {
            "path": item["kubeconfig_path"],
            "context": item["kube_context"],
            "username": item["username"],
            "category": item["category"],
        }
    for name, candidate in candidates.items():
        path = Path(candidate["path"])
        context = candidate["context"]
        identity = _identity(path, context)
        if identity["username"] != candidate["username"]:
            raise ValueError(f"{name} identity differs from its declared username")
        if owner_group in identity["groups"] or identity["username"] == owner["username"]:
            raise ValueError(f"{name} identity aliases the security owner")
        protected = _protected_permissions(path, context, names)
        forbidden = {
            key: value
            for key, value in protected.items()
            if not key.startswith("create:")
            or key in {"create:policy", "create:binding"}
        }
        if any(forbidden.values()):
            raise ValueError(f"{name} identity can mutate a protected generation")
        dangerous = _dangerous_permissions(path, context)
        if dangerous:
            raise ValueError(f"{name} identity has delegation authority: {dangerous[0]}")
        if name != "workloads":
            checked_nonowners.append(
                {"category": candidate["category"], "username": identity["username"]}
            )

    checked_nonowners.sort(key=lambda item: (item["category"], item["username"]))
    if {item["category"] for item in checked_nonowners} != {
        "release",
        "human",
        "break-glass",
        "other",
    } or len({item["username"] for item in checked_nonowners}) != len(
        checked_nonowners
    ):
        raise ValueError("non-owner identity categories or subjects are incomplete")

    return {
        "authorized": "true",
        "security_owner_subject_sha256": hashlib.sha256(
            owner["username"].encode()
        ).hexdigest(),
        "workloads_subject_sha256": hashlib.sha256(
            workloads["username"].encode()
        ).hexdigest(),
        "non_owner_inventory_sha256": hashlib.sha256(
            json.dumps(checked_nonowners, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
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
