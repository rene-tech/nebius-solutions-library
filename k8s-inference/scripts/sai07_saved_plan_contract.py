#!/usr/bin/env python3
"""Derive a deletion-free SAI-07 contract from one exact saved Terraform plan.

The caller supplies a repository-pinned Terraform executable and an immutable
saved-plan path. Both are opened with O_NOFOLLOW, hashed from their open file
descriptors, and executed/read through /proc/self/fd so path replacement cannot
change the bytes after verification. The complete ``terraform show -json``
document and its configuration, planned values, drift, outputs and resource
changes receive independent canonical digests. No plan/apply/state operation is
performed by this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

SCHEMA = "fs2-serve.nebius.ai/sai07-saved-plan-contract/v1"
ZERO_SHA256 = "0" * 64
MAX_TERRAFORM_BYTES = 256 * 1024 * 1024
MAX_PLAN_BYTES = 512 * 1024 * 1024
MAX_PLAN_JSON_BYTES = 512 * 1024 * 1024
ALLOWED_ACTIONS = {("create",), ("no-op",), ("read",), ("update",)}
SHA256_HEX = frozenset("0123456789abcdef")


class SavedPlanError(ValueError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _open_regular(path: Path, label: str, maximum: int) -> tuple[int, os.stat_result]:
    if not path.is_absolute() or ".." in path.parts:
        raise SavedPlanError(f"{label} path must be absolute without traversal")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0 or metadata.st_size > maximum:
        os.close(descriptor)
        raise SavedPlanError(f"{label} is not a bounded nonempty regular file")
    return descriptor, metadata


def _hash_descriptor(descriptor: int, metadata: os.stat_result, label: str) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    remaining = metadata.st_size
    while remaining:
        chunk = os.read(descriptor, min(1024 * 1024, remaining))
        if not chunk:
            break
        digest.update(chunk)
        remaining -= len(chunk)
    after = os.fstat(descriptor)
    if remaining or (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise SavedPlanError(f"{label} changed during descriptor-fenced hashing")
    return digest.hexdigest()


def _run_bounded(command: list[str], descriptors: tuple[int, ...], label: str) -> bytes:
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        pass_fds=descriptors,
        close_fds=True,
        env={"PATH": "/usr/bin:/bin", "TF_IN_AUTOMATION": "1"},
    )
    assert process.stdout is not None
    output = process.stdout.read(MAX_PLAN_JSON_BYTES + 1)
    if len(output) > MAX_PLAN_JSON_BYTES:
        process.kill()
        process.wait(timeout=30)
        raise SavedPlanError(f"{label} exceeded the bounded output size")
    return_code = process.wait(timeout=300)
    if return_code != 0:
        raise SavedPlanError(f"{label} failed")
    return output


def _digest_member(document: dict[str, Any], name: str) -> str:
    value = document.get(name)
    if value is None:
        return ZERO_SHA256
    return hashlib.sha256(canonical(value)).hexdigest()


def _root_variable(plan: dict[str, Any], name: str) -> Any:
    variables = plan.get("variables")
    entry = variables.get(name) if isinstance(variables, dict) else None
    if not isinstance(entry, dict) or set(entry) != {"value"}:
        raise SavedPlanError(f"saved plan omits exact root variable {name}")
    return entry["value"]


def inspect_saved_plan(
    plan_path: Path,
    terraform_path: Path,
    expected_terraform_sha256: str,
    expected_terraform_version: str,
) -> dict[str, str]:
    terraform_fd, terraform_metadata = _open_regular(
        terraform_path, "Terraform executable", MAX_TERRAFORM_BYTES
    )
    plan_fd, plan_metadata = _open_regular(plan_path, "saved Terraform plan", MAX_PLAN_BYTES)
    try:
        if _hash_descriptor(
            terraform_fd, terraform_metadata, "Terraform executable"
        ) != expected_terraform_sha256:
            raise SavedPlanError("Terraform executable differs from the repository pin")
        saved_plan_sha256 = _hash_descriptor(plan_fd, plan_metadata, "saved Terraform plan")
        executable = f"/proc/self/fd/{terraform_fd}"
        version_bytes = _run_bounded(
            [executable, "version", "-json"], (terraform_fd,), "Terraform version query"
        )
        try:
            version_document = json.loads(version_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SavedPlanError("Terraform version response is not JSON") from error
        if (
            not isinstance(version_document, dict)
            or version_document.get("terraform_version") != expected_terraform_version
        ):
            raise SavedPlanError("Terraform version differs from the repository pin")
        plan_bytes = _run_bounded(
            [executable, "show", "-json", f"/proc/self/fd/{plan_fd}"],
            (terraform_fd, plan_fd),
            "Terraform saved-plan projection",
        )
        try:
            plan = json.loads(plan_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SavedPlanError("Terraform saved-plan projection is not JSON") from error
        if (
            not isinstance(plan, dict)
            or not isinstance(plan.get("format_version"), str)
            or plan.get("terraform_version") != expected_terraform_version
            or not isinstance(plan.get("configuration"), dict)
            or not isinstance(plan.get("planned_values"), dict)
            or not isinstance(plan.get("resource_changes"), list)
        ):
            raise SavedPlanError("Terraform saved-plan projection is incomplete")
        kubeconfig_path = _root_variable(plan, "kubeconfig_path")
        kube_context = _root_variable(plan, "kube_context")
        rollout_phase = _root_variable(plan, "pod_security_rollout_phase")
        rollout_receipt = _root_variable(plan, "pod_security_rollout_receipt")
        if (
            not isinstance(kubeconfig_path, str)
            or not kubeconfig_path.startswith("/")
            or ".." in Path(kubeconfig_path).parts
            or not isinstance(kube_context, str)
            or not kube_context
            or not isinstance(rollout_phase, str)
            or not rollout_phase
            or not isinstance(rollout_receipt, dict)
        ):
            raise SavedPlanError("saved plan platform identity/rollout variables are malformed")
        external_handoff_path = rollout_receipt.get("external_handoff_path")
        custody_epoch_sha256 = rollout_receipt.get("custody_epoch_sha256")
        if (
            not isinstance(external_handoff_path, str)
            or not external_handoff_path.startswith("/")
            or ".." in Path(external_handoff_path).parts
            or not isinstance(custody_epoch_sha256, str)
            or len(custody_epoch_sha256) != 64
            or any(character not in SHA256_HEX for character in custody_epoch_sha256)
            or custody_epoch_sha256 == ZERO_SHA256
        ):
            raise SavedPlanError(
                "saved plan omits the exact handoff path or active custody epoch"
            )
        changes: list[dict[str, Any]] = plan["resource_changes"]
        addresses: set[str] = set()
        normalized_changes: list[dict[str, Any]] = []
        for index, raw in enumerate(changes):
            if not isinstance(raw, dict):
                raise SavedPlanError(f"resource_changes[{index}] is malformed")
            address = raw.get("address")
            change = raw.get("change")
            actions = change.get("actions") if isinstance(change, dict) else None
            if (
                not isinstance(address, str)
                or not address
                or address in addresses
                or not isinstance(actions, list)
                or tuple(actions) not in ALLOWED_ACTIONS
            ):
                raise SavedPlanError(
                    "saved plan has a duplicate address, delete/replacement, or unsupported action"
                )
            addresses.add(address)
            normalized_changes.append(
                {
                    "actions": actions,
                    "address": address,
                    "change_sha256": hashlib.sha256(canonical(change)).hexdigest(),
                    "mode": raw.get("mode"),
                    "provider_name": raw.get("provider_name"),
                    "type": raw.get("type"),
                }
            )
        normalized_changes.sort(key=lambda item: item["address"])
        canonical_plan = canonical(plan)
        return {
            "change_count": str(len(normalized_changes)),
            "configuration_sha256": _digest_member(plan, "configuration"),
            "custody_epoch_sha256": custody_epoch_sha256,
            "external_handoff_path_sha256": hashlib.sha256(
                external_handoff_path.encode()
            ).hexdigest(),
            "format_version": plan["format_version"],
            "output_changes_sha256": _digest_member(plan, "output_changes"),
            "plan_json_sha256": hashlib.sha256(canonical_plan).hexdigest(),
            "planned_values_sha256": _digest_member(plan, "planned_values"),
            "platform_kube_context": kube_context,
            "platform_kubeconfig_path_sha256": hashlib.sha256(
                kubeconfig_path.encode()
            ).hexdigest(),
            "prior_state_sha256": _digest_member(plan, "prior_state"),
            "resource_changes_sha256": hashlib.sha256(
                canonical(normalized_changes)
            ).hexdigest(),
            "resource_drift_sha256": _digest_member(plan, "resource_drift"),
            "saved_plan_sha256": saved_plan_sha256,
            "schema": SCHEMA,
            "terraform_version": expected_terraform_version,
            "rollout_phase": rollout_phase,
            "variables_sha256": _digest_member(plan, "variables"),
        }
    finally:
        os.close(plan_fd)
        os.close(terraform_fd)
