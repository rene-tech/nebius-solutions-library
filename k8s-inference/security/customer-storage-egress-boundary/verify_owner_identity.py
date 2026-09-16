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


def _admission_permissions(path: Path, context: str) -> dict[tuple[str, str], bool]:
    resources = (
        "validatingadmissionpolicies.admissionregistration.k8s.io",
        "validatingadmissionpolicybindings.admissionregistration.k8s.io",
    )
    return {
        (verb, resource): _kubectl(path, context, "auth", "can-i", verb, resource)
        == "yes"
        for resource in resources
        for verb in ("create", "update", "delete")
    }


def verify(query: dict[str, str]) -> dict[str, str]:
    owner_path = Path(query["security_owner_kubeconfig_path"])
    workloads_path = Path(query["workloads_kubeconfig_path"])
    owner = _identity(owner_path, query["security_owner_kube_context"])
    workloads = _identity(workloads_path, query["workloads_kube_context"])
    owner_group = query["security_owner_group"]

    if owner["username"] == workloads["username"]:
        raise ValueError("security-owner and workloads subjects must differ")
    if owner_group not in owner["groups"]:
        raise ValueError("security-owner credential lacks the dedicated owner group")
    if owner_group in workloads["groups"]:
        raise ValueError("workloads credential is a member of the security-owner group")
    workloads_permissions = _admission_permissions(
        workloads_path, query["workloads_kube_context"]
    )
    owner_permissions = _admission_permissions(
        owner_path, query["security_owner_kube_context"]
    )
    if any(workloads_permissions.values()):
        raise ValueError(
            "workloads credential can mutate the external admission boundary"
        )
    if not all(
        allowed
        for (verb, _resource), allowed in owner_permissions.items()
        if verb in {"create", "update"}
    ):
        raise ValueError(
            "security-owner credential cannot maintain the external admission boundary"
        )

    return {
        "authorized": "true",
        "security_owner_subject_sha256": hashlib.sha256(
            owner["username"].encode()
        ).hexdigest(),
        "workloads_subject_sha256": hashlib.sha256(
            workloads["username"].encode()
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
