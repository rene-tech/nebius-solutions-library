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
import fcntl
import re
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
SYNCHRONOUS_SETTLEMENT_PROVIDERS = {
    "registry.terraform.io/hashicorp/kubernetes",
    "terraform.io/builtin/terraform",
}
SHA256_HEX = frozenset("0123456789abcdef")
CAPSULE_FD_RE = re.compile(r"^/proc/1/fd/(?:19[1-4]|197|198|201|202)$")
REQUIRED_MEMFD_SEALS = (
    fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
)


class SavedPlanError(ValueError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _open_regular(path: Path, label: str, maximum: int) -> tuple[int, os.stat_result]:
    if not path.is_absolute() or ".." in path.parts:
        raise SavedPlanError(f"{label} path must be absolute without traversal")
    capsule_descriptor = CAPSULE_FD_RE.fullmatch(str(path)) is not None
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_CLOEXEC | (0 if capsule_descriptor else os.O_NOFOLLOW),
    )
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


def _digest_regular_file(path: Path, label: str, maximum: int) -> str:
    descriptor, metadata = _open_regular(path, label, maximum)
    try:
        if (
            str(path) == "/proc/1/fd/198"
            and fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) & REQUIRED_MEMFD_SEALS
            != REQUIRED_MEMFD_SEALS
        ):
            raise SavedPlanError("capsule platform-kubeconfig descriptor is not write sealed")
        return _hash_descriptor(descriptor, metadata, label)
    finally:
        os.close(descriptor)


def _root_variable(plan: dict[str, Any], name: str) -> Any:
    variables = plan.get("variables")
    entry = variables.get(name) if isinstance(variables, dict) else None
    if not isinstance(entry, dict) or set(entry) != {"value"}:
        raise SavedPlanError(f"saved plan omits exact root variable {name}")
    return entry["value"]


def _known_projection(value: Any, unknown: Any) -> Any:
    """Retain exact planned values while representing provider-computed leaves."""

    if unknown is True:
        return {"provider_computed": True}
    if isinstance(value, dict):
        unknown_fields = unknown if isinstance(unknown, dict) else {}
        return {
            key: _known_projection(item, unknown_fields.get(key, False))
            for key, item in sorted(value.items())
        }
    if isinstance(value, list):
        unknown_items = unknown if isinstance(unknown, list) else []
        return [
            _known_projection(
                item, unknown_items[index] if index < len(unknown_items) else False
            )
            for index, item in enumerate(value)
        ]
    return value


def _show_plan_document(
    executable: str, terraform_fd: int, plan_fd: int, label: str
) -> dict[str, Any]:
    payload = _run_bounded(
        [executable, "show", "-json", f"/proc/self/fd/{plan_fd}"],
        (terraform_fd, plan_fd),
        label,
    )
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SavedPlanError(f"{label} is not JSON") from error
    if (
        not isinstance(document, dict)
        or not isinstance(document.get("configuration"), dict)
        or not isinstance(document.get("planned_values"), dict)
        or not isinstance(document.get("resource_changes"), list)
    ):
        raise SavedPlanError(f"{label} is incomplete")
    return document


def _resource_change_map(
    plan: dict[str, Any], label: str
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(plan["resource_changes"]):
        if not isinstance(raw, dict):
            raise SavedPlanError(f"{label} resource_changes[{index}] is malformed")
        address = raw.get("address")
        change = raw.get("change")
        actions = change.get("actions") if isinstance(change, dict) else None
        if (
            not isinstance(address, str)
            or not address
            or address in result
            or not isinstance(actions, list)
        ):
            raise SavedPlanError(f"{label} contains a malformed or duplicate change")
        result[address] = raw
    return result


def verify_settled_plans(
    original_plan_path: Path,
    settlement_plan_a_path: Path,
    settlement_plan_b_path: Path,
    terraform_path: Path,
    expected_terraform_sha256: str,
    expected_terraform_version: str,
) -> dict[str, str]:
    """Prove two fresh provider reads converge on the exact planned postconditions."""

    terraform_fd, terraform_metadata = _open_regular(
        terraform_path, "Terraform executable", MAX_TERRAFORM_BYTES
    )
    opened: list[tuple[int, os.stat_result, str]] = []
    try:
        if _hash_descriptor(
            terraform_fd, terraform_metadata, "Terraform executable"
        ) != expected_terraform_sha256:
            raise SavedPlanError("Terraform executable differs from the capsule pin")
        for path, label in (
            (original_plan_path, "authorized saved plan"),
            (settlement_plan_a_path, "first settlement plan"),
            (settlement_plan_b_path, "second settlement plan"),
        ):
            descriptor, metadata = _open_regular(path, label, MAX_PLAN_BYTES)
            if (
                str(path).startswith("/proc/1/fd/")
                and fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) & REQUIRED_MEMFD_SEALS
                != REQUIRED_MEMFD_SEALS
            ):
                raise SavedPlanError(f"{label} descriptor is not write sealed")
            opened.append((descriptor, metadata, label))
        executable = f"/proc/self/fd/{terraform_fd}"
        version_bytes = _run_bounded(
            [executable, "version", "-json"],
            (terraform_fd,),
            "Terraform version query",
        )
        try:
            version_document = json.loads(version_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SavedPlanError("Terraform version response is not JSON") from error
        if version_document.get("terraform_version") != expected_terraform_version:
            raise SavedPlanError("Terraform version differs from the capsule pin")
        documents = [
            _show_plan_document(executable, terraform_fd, descriptor, label)
            for descriptor, _metadata, label in opened
        ]
        original, first, second = documents
        configuration_sha256 = _digest_member(original, "configuration")
        variables_sha256 = _digest_member(original, "variables")
        if any(
            document.get("terraform_version") != expected_terraform_version
            or _digest_member(document, "configuration") != configuration_sha256
            or _digest_member(document, "variables") != variables_sha256
            for document in (first, second)
        ):
            raise SavedPlanError(
                "settlement plans differ from the authorized configuration or variables"
            )
        original_changes = _resource_change_map(original, "authorized plan")
        settlement_maps = (
            _resource_change_map(first, "first settlement plan"),
            _resource_change_map(second, "second settlement plan"),
        )
        postconditions: list[dict[str, Any]] = []
        for address, raw in sorted(original_changes.items()):
            if raw.get("mode") != "managed":
                continue
            provider = raw.get("provider_name")
            change = raw["change"]
            actions = tuple(change.get("actions", []))
            if (
                actions not in {("create",), ("update",), ("no-op",)}
                or (
                    actions != ("no-op",)
                    and provider not in SYNCHRONOUS_SETTLEMENT_PROVIDERS
                )
            ):
                raise SavedPlanError(
                    "authorized mutation lacks synchronous Kubernetes settlement semantics"
                )
            expected = _known_projection(
                change.get("after"), change.get("after_unknown", False)
            )
            observations: list[Any] = []
            for settlement in settlement_maps:
                observed = settlement.get(address)
                observed_change = (
                    observed.get("change") if isinstance(observed, dict) else None
                )
                if (
                    not isinstance(observed_change, dict)
                    or tuple(observed_change.get("actions", [])) != ("no-op",)
                    or observed.get("mode") != "managed"
                    or observed.get("provider_name") != provider
                ):
                    raise SavedPlanError(
                        "refreshed settlement plan has an absent or non-no-op managed object"
                    )
                projection = _known_projection(
                    observed_change.get("before"), change.get("after_unknown", False)
                )
                if projection != expected:
                    raise SavedPlanError(
                        "refreshed live state differs from an exact planned-object postcondition"
                    )
                observations.append(projection)
            if observations[0] != observations[1]:
                raise SavedPlanError("planned-object postcondition did not settle twice")
            postconditions.append(
                {
                    "address": address,
                    "known_after_sha256": hashlib.sha256(
                        canonical(expected)
                    ).hexdigest(),
                    "provider_name": provider,
                    "type": raw.get("type"),
                }
            )
        if not any(
            item["provider_name"] == "registry.terraform.io/hashicorp/kubernetes"
            for item in postconditions
        ):
            raise SavedPlanError(
                "settlement proof contains no authoritative Kubernetes object"
            )
        for settlement in settlement_maps:
            for raw in settlement.values():
                if raw.get("mode") == "managed" and tuple(
                    raw.get("change", {}).get("actions", [])
                ) != ("no-op",):
                    raise SavedPlanError(
                        "authoritative refreshed state has an unsettled managed action"
                    )
        first_state = {
            "planned_values": first.get("planned_values"),
            "resource_changes": first.get("resource_changes"),
            "resource_drift": first.get("resource_drift"),
        }
        second_state = {
            "planned_values": second.get("planned_values"),
            "resource_changes": second.get("resource_changes"),
            "resource_drift": second.get("resource_drift"),
        }
        if first_state != second_state:
            raise SavedPlanError(
                "two authoritative provider refreshes did not reach the same state"
            )
        return {
            "authoritative_refreshed_state_sha256": hashlib.sha256(
                canonical(second_state)
            ).hexdigest(),
            "first_settlement_plan_sha256": _hash_descriptor(
                opened[1][0], opened[1][1], opened[1][2]
            ),
            "planned_object_postconditions_sha256": hashlib.sha256(
                canonical(postconditions)
            ).hexdigest(),
            "second_settlement_plan_sha256": _hash_descriptor(
                opened[2][0], opened[2][1], opened[2][2]
            ),
            "settled_object_count": str(len(postconditions)),
            "status": "authoritative-provider-settlement-proved",
        }
    finally:
        for descriptor, _metadata, _label in opened:
            os.close(descriptor)
        os.close(terraform_fd)


def inspect_saved_plan(
    plan_path: Path,
    terraform_path: Path,
    expected_terraform_sha256: str,
    expected_terraform_version: str,
    expected_platform_kubeconfig_sha256: str | None = None,
) -> dict[str, str]:
    terraform_fd, terraform_metadata = _open_regular(
        terraform_path, "Terraform executable", MAX_TERRAFORM_BYTES
    )
    plan_fd, plan_metadata = _open_regular(plan_path, "saved Terraform plan", MAX_PLAN_BYTES)
    try:
        if (
            str(plan_path) == "/proc/1/fd/197"
            and fcntl.fcntl(plan_fd, fcntl.F_GET_SEALS) & REQUIRED_MEMFD_SEALS
            != REQUIRED_MEMFD_SEALS
        ):
            raise SavedPlanError("capsule saved-plan descriptor is not write sealed")
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
        execution_capsule_contract_sha256 = rollout_receipt.get(
            "execution_capsule_contract_sha256"
        )
        execution_runtime_attestation_sha256 = rollout_receipt.get(
            "execution_runtime_attestation_sha256"
        )
        execution_external_runtime_attestation_sha256 = rollout_receipt.get(
            "execution_external_runtime_attestation_sha256"
        )
        execution_source_bundle_sha256 = rollout_receipt.get(
            "execution_source_bundle_sha256"
        )
        execution_platform_kubeconfig_sha256 = rollout_receipt.get(
            "execution_platform_kubeconfig_sha256"
        )
        execution_context_sha256 = rollout_receipt.get(
            "execution_expected_context_sha256"
        )
        execution_receipt_bundle_sha256 = rollout_receipt.get(
            "execution_receipt_bundle_sha256"
        )
        execution_cluster_id = rollout_receipt.get("execution_cluster_id")
        execution_kube_system_uid = rollout_receipt.get(
            "execution_kube_system_uid"
        )
        execution_action = rollout_receipt.get("execution_action")
        execution_consumer = rollout_receipt.get("execution_consumer")
        execution_digests = (
            execution_capsule_contract_sha256,
            execution_runtime_attestation_sha256,
            execution_external_runtime_attestation_sha256,
            execution_source_bundle_sha256,
            execution_platform_kubeconfig_sha256,
            execution_context_sha256,
            execution_receipt_bundle_sha256,
        )
        if (
            not isinstance(external_handoff_path, str)
            or not external_handoff_path.startswith("/")
            or ".." in Path(external_handoff_path).parts
            or not isinstance(custody_epoch_sha256, str)
            or len(custody_epoch_sha256) != 64
            or any(character not in SHA256_HEX for character in custody_epoch_sha256)
            or custody_epoch_sha256 == ZERO_SHA256
            or any(
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in SHA256_HEX for character in value)
                for value in execution_digests
            )
            or any(
                value == ZERO_SHA256
                for value in execution_digests[:-1]
            )
            or not isinstance(execution_cluster_id, str)
            or not execution_cluster_id
            or not isinstance(execution_kube_system_uid, str)
            or not execution_kube_system_uid
            or execution_action not in {"authorize", "acknowledge"}
            or execution_consumer not in {"owner", "downstream"}
        ):
            raise SavedPlanError(
                "saved plan omits the exact context, handoff, custody epoch, or execution capsule session"
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
        if expected_platform_kubeconfig_sha256 is None:
            platform_kubeconfig_sha256 = _digest_regular_file(
                Path(kubeconfig_path),
                "platform kubeconfig",
                4 * 1024 * 1024,
            )
        else:
            if (
                not isinstance(expected_platform_kubeconfig_sha256, str)
                or len(expected_platform_kubeconfig_sha256) != 64
                or any(
                    character not in SHA256_HEX
                    for character in expected_platform_kubeconfig_sha256
                )
            ):
                raise SavedPlanError(
                    "externally attested platform kubeconfig digest is malformed"
                )
            platform_kubeconfig_sha256 = expected_platform_kubeconfig_sha256
        if platform_kubeconfig_sha256 != execution_platform_kubeconfig_sha256:
            raise SavedPlanError(
                "saved plan receipt does not bind the exact sealed platform kubeconfig"
            )
        return {
            "change_count": str(len(normalized_changes)),
            "configuration_sha256": _digest_member(plan, "configuration"),
            "custody_epoch_sha256": custody_epoch_sha256,
            "action": execution_action,
            "cluster_id": execution_cluster_id,
            "consumer": execution_consumer,
            "context_sha256": execution_context_sha256,
            "execution_capsule_contract_sha256": execution_capsule_contract_sha256,
            "execution_runtime_attestation_sha256": execution_runtime_attestation_sha256,
            "execution_external_runtime_attestation_sha256": execution_external_runtime_attestation_sha256,
            "execution_source_bundle_sha256": execution_source_bundle_sha256,
            "external_handoff_path_sha256": hashlib.sha256(
                external_handoff_path.encode()
            ).hexdigest(),
            "format_version": plan["format_version"],
            "output_changes_sha256": _digest_member(plan, "output_changes"),
            "plan_json_sha256": hashlib.sha256(canonical_plan).hexdigest(),
            "planned_values_sha256": _digest_member(plan, "planned_values"),
            "platform_kube_context": kube_context,
            "platform_kubeconfig_path": kubeconfig_path,
            "platform_kubeconfig_path_sha256": hashlib.sha256(
                kubeconfig_path.encode()
            ).hexdigest(),
            "platform_kubeconfig_sha256": platform_kubeconfig_sha256,
            "execution_platform_kubeconfig_sha256": execution_platform_kubeconfig_sha256,
            "kube_system_uid": execution_kube_system_uid,
            "prior_state_sha256": _digest_member(plan, "prior_state"),
            "resource_changes_sha256": hashlib.sha256(
                canonical(normalized_changes)
            ).hexdigest(),
            "resource_drift_sha256": _digest_member(plan, "resource_drift"),
            "saved_plan_sha256": saved_plan_sha256,
            "schema": SCHEMA,
            "terraform_version": expected_terraform_version,
            "rollout_phase": rollout_phase,
            "receipt_bundle_sha256": execution_receipt_bundle_sha256,
            "variables_sha256": _digest_member(plan, "variables"),
        }
    finally:
        os.close(plan_fd)
        os.close(terraform_fd)
