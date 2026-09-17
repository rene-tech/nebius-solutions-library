#!/usr/bin/env python3
"""Provider-native, metadata-only observations for the credential authority.

The root authority injects the complete immutable policy.  This adapter accepts
no caller paths, profiles, projects, commands, Terraform roots, kubeconfigs, or
evidence files.  It reads every configured Terraform lineage, every cluster
Secret, and project IAM inventories, but emits only identities and irreversible
SHA-256 commitments--never credential material.

All commands in this module are read-only.  Mutation, key creation, rotation,
revocation, deletion, apply, and rollout operations are intentionally absent.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


class ProviderError(RuntimeError):
    pass


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def observed_at() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def provider_time(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise ProviderError(f"{label} is not RFC3339")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProviderError(f"{label} is not RFC3339") from error
    if parsed.tzinfo is None:
        raise ProviderError(f"{label} has no timezone")
    return parsed.astimezone(UTC)


def file_sha256(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    digest = hashlib.sha256()
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ProviderError("provider file is not regular")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def root_private_file(path: Path, *, label: str, expected_sha256: str | None = None) -> str:
    """Bind a provider input to one root-owned regular file and secure parent chain."""

    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ProviderError(f"{label} is absent or unsafe")
    metadata = path.stat()
    if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ProviderError(f"{label} is not root-owned mode 0600")
    for parent in path.parents:
        parent_metadata = parent.stat()
        if (
            parent.is_symlink()
            or not parent.is_dir()
            or parent_metadata.st_uid != 0
            or stat.S_IMODE(parent_metadata.st_mode) & 0o022
        ):
            raise ProviderError(f"{label} parent chain is mutable")
    digest = file_sha256(path)
    if expected_sha256 is not None and digest != expected_sha256:
        raise ProviderError(f"{label} digest differs from root policy")
    return digest


def executable(policy: dict[str, Any], name: str) -> str:
    configured = policy["provider_executables"][name]
    path = Path(configured["path"])
    if path.is_symlink() or not path.is_file():
        raise ProviderError(f"{name} executable is absent or unsafe")
    metadata = path.stat()
    if (
        metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or file_sha256(path) != configured["sha256"]
    ):
        raise ProviderError(f"{name} executable differs from root policy")
    return str(path)


def command_json(
    command: list[str],
    *,
    label: str,
    timeout: int = 60,
    environment: dict[str, str] | None = None,
) -> Any:
    if not command or not Path(command[0]).is_absolute() or any(
        value in {"-c", "-m"} for value in command
    ):
        raise ProviderError(f"{label} command is unsafe")
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=True,
            timeout=timeout,
            env={
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "LANG": "C.UTF-8",
                **(environment or {}),
            },
        )
        return json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise ProviderError(f"{label} did not return authoritative JSON") from error


def command_success(
    command: list[str], *, label: str, environment: dict[str, str]
) -> None:
    if not command or not Path(command[0]).is_absolute():
        raise ProviderError(f"{label} command is unsafe")
    try:
        subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=True,
            timeout=120,
            env={
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "LANG": "C.UTF-8",
                **environment,
            },
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ProviderError(f"{label} failed") from error


def command_json_input(
    command: list[str],
    *,
    label: str,
    payload: dict[str, Any],
    timeout: int,
) -> Any:
    """Run one policy-pinned adapter with a schema-bounded JSON request."""

    if not command or not Path(command[0]).is_absolute() or any(
        value in {"-c", "-m"} or value.startswith("-") for value in command
    ):
        raise ProviderError(f"{label} command is unsafe")
    try:
        completed = subprocess.run(
            command,
            input=json.dumps(payload, sort_keys=True, separators=(",", ":")),
            text=True,
            capture_output=True,
            check=True,
            timeout=timeout,
            env={
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "LANG": "C.UTF-8",
            },
        )
        return json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise ProviderError(f"{label} did not return authoritative JSON") from error


def verified_adapter_command(adapter: dict[str, Any], *, label: str) -> list[str]:
    if (
        not isinstance(adapter, dict)
        or set(adapter) != {"command", "file_sha256", "timeout_seconds"}
        or not isinstance(adapter.get("command"), list)
        or not 1 <= len(adapter["command"]) <= 2
        or not isinstance(adapter.get("file_sha256"), dict)
        or not isinstance(adapter.get("timeout_seconds"), int)
        or not 1 <= adapter["timeout_seconds"] <= 120
    ):
        raise ProviderError(f"{label} adapter policy is malformed")
    command = [str(Path(value).resolve()) for value in adapter["command"]]
    if (
        any(not Path(value).is_absolute() for value in adapter["command"])
        or len(set(command)) != len(command)
        or set(adapter["file_sha256"]) != set(command)
    ):
        raise ProviderError(f"{label} adapter command is not exactly pinned")
    for value in command:
        path = Path(value)
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_uid != 0
            or stat.S_IMODE(path.stat().st_mode) & 0o022
            or file_sha256(path) != adapter["file_sha256"][value]
        ):
            raise ProviderError(f"{label} adapter executable differs from policy")
    return command


def stable_json(path: Path, *, label: str) -> dict[str, Any]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ProviderError(f"{label} is absent or unsafe")
    before = path.stat()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ProviderError(f"{label} is invalid JSON") from error
    after = path.stat()
    stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable):
        raise ProviderError(f"{label} changed while read")
    if not isinstance(document, dict):
        raise ProviderError(f"{label} is not an object")
    return document


def safe_labels(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "purpose",
        "retention",
        "handoff_lineage",
        "handoff_generation",
        "credential_class",
        "credential_generation",
        "audience",
        "owner",
        "name",
        "status",
        "version",
    }
    return {
        key: item
        for key, item in value.items()
        if key in allowed and isinstance(item, str) and item
    }


def state_provider_binding(terraform_type: Any, attributes: dict[str, Any]) -> dict[str, Any] | None:
    """Extract only non-secret provider identity fields from Terraform state."""

    metadata_value = attributes.get("metadata")
    metadata = (
        metadata_value[0]
        if isinstance(metadata_value, list)
        and len(metadata_value) == 1
        and isinstance(metadata_value[0], dict)
        else metadata_value
        if isinstance(metadata_value, dict)
        else {}
    )
    if terraform_type == "kubernetes_secret_v1":
        annotations = metadata.get("annotations", {})
        if not isinstance(annotations, dict):
            annotations = {}
        return {
            "namespace": metadata.get("namespace"),
            "name": metadata.get("name"),
            "uid": metadata.get("uid"),
            "resource_version": metadata.get("resource_version"),
            "immutable": attributes.get("immutable") is True,
            "credential_class": annotations.get("fs2.nebius.ai/credential-class"),
            "credential_generation": annotations.get(
                "fs2.nebius.ai/credential-generation"
            ),
            "content_sha256": annotations.get("fs2.nebius.ai/content-sha256"),
        }
    if terraform_type == "helm_release":
        release_metadata = attributes.get("metadata")
        release = (
            release_metadata[0]
            if isinstance(release_metadata, list)
            and len(release_metadata) == 1
            and isinstance(release_metadata[0], dict)
            else {}
        )
        return {
            "namespace": attributes.get("namespace"),
            "name": attributes.get("name"),
            "revision": release.get("revision"),
            "status": release.get("status"),
        }
    return None


def state_addresses(state: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for resource in state.get("resources", []):
        if not isinstance(resource, dict) or resource.get("mode", "managed") != "managed":
            continue
        module = f"{resource['module']}." if isinstance(resource.get("module"), str) else ""
        base = f"{module}{resource.get('type')}.{resource.get('name')}"
        for instance in resource.get("instances", []):
            if not isinstance(instance, dict):
                raise ProviderError("Terraform state contains a malformed resource instance")
            index = instance.get("index_key")
            suffix = "" if index is None else f"[{json.dumps(index, separators=(',', ':'))}]"
            attributes = instance.get("attributes")
            if not isinstance(attributes, dict):
                raise ProviderError("Terraform state resource lacks attributes")
            provider_id = attributes.get("id")
            parent_id = attributes.get("parent_id")
            item = {
                    "address": f"{base}{suffix}",
                    "type": resource.get("type"),
                    "identity_sha256": canonical_sha256(attributes),
                    "provider_id": (
                        str(provider_id) if provider_id not in (None, "") else None
                    ),
                    "parent_id": (
                        str(parent_id) if parent_id not in (None, "") else None
                    ),
                    "labels": safe_labels(attributes.get("labels")),
                }
            binding = state_provider_binding(resource.get("type"), attributes)
            if binding is not None:
                item["provider_binding"] = binding
            result.append(item)
    return sorted(result, key=lambda item: item["address"])


def terraform_inventory(policy: dict[str, Any]) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    terraform = executable(policy, "terraform")
    for root_name, root in sorted(policy["terraform_roots"].items()):
        configuration = Path(root["configuration_dir"])
        if configuration.is_symlink() or not configuration.is_dir():
            raise ProviderError(f"{root_name} Terraform configuration is unsafe")
        metadata = configuration.stat()
        if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
            raise ProviderError(
                f"{root_name} Terraform configuration is not root-owned immutable input"
            )
        backend_declarations: list[str] = []
        for source_path in sorted(configuration.glob("*.tf")):
            if source_path.is_symlink() or not source_path.is_file():
                raise ProviderError(
                    f"{root_name} Terraform configuration contains an unsafe file"
                )
            source_metadata = source_path.stat()
            if (
                source_metadata.st_uid != 0
                or stat.S_IMODE(source_metadata.st_mode) & 0o022
            ):
                raise ProviderError(
                    f"{root_name} Terraform source is not root-owned immutable input"
                )
            backend_declarations.extend(
                re.findall(
                    r'\bbackend\s+"([A-Za-z0-9_-]+)"\s*\{',
                    source_path.read_text(encoding="utf-8"),
                )
            )
        if backend_declarations != [root["backend_type"]]:
            raise ProviderError(
                f"{root_name} Terraform backend differs from root policy"
            )
        terraform_environment = {
            "TF_DATA_DIR": root["terraform_data_dir"],
            "TF_WORKSPACE": root["workspace"],
            "TF_IN_AUTOMATION": "1",
        }
        command_success(
            [
                terraform,
                f"-chdir={configuration}",
                "init",
                "-input=false",
                "-reconfigure",
                "-lockfile=readonly",
                f"-backend-config={root['backend_config_path']}",
            ],
            label=f"{root_name} canonical Terraform backend initialization",
            environment=terraform_environment,
        )
        state = command_json(
            [terraform, f"-chdir={configuration}", "state", "pull"],
            label=f"{root_name} canonical Terraform backend",
            environment=terraform_environment,
        )
        lineage = state.get("lineage") if isinstance(state, dict) else None
        serial = state.get("serial") if isinstance(state, dict) else None
        if (
            not isinstance(state, dict)
            or not isinstance(lineage, str)
            or lineage != root["lineage_id"]
            or not isinstance(serial, int)
            or serial < 1
        ):
            raise ProviderError(f"{root_name} Terraform lineage or serial is invalid")
        inventory.append(
            {
                "root": root_name,
                "configuration_sha256": canonical_sha256(
                    {
                        path.name: file_sha256(path)
                        for path in sorted(configuration.glob("*.tf"))
                        if path.is_file() and not path.is_symlink()
                    }
                ),
                "backend_type": root["backend_type"],
                "workspace": root["workspace"],
                "lineage": lineage,
                "serial": serial,
                "state_json_sha256": canonical_sha256(state),
                "resources": state_addresses(state),
            }
        )
    return inventory


def secret_commitment(data: Any) -> str:
    if not isinstance(data, dict):
        raise ProviderError("Kubernetes Secret data map is malformed")
    commitments: dict[str, str] = {}
    for key, encoded in data.items():
        if not isinstance(key, str) or not isinstance(encoded, str):
            raise ProviderError("Kubernetes Secret data entry is malformed")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise ProviderError("Kubernetes Secret data is not canonical base64") from error
        commitments[key] = hashlib.sha256(decoded).hexdigest()
    return canonical_sha256(commitments)


def kubernetes_secrets(policy: dict[str, Any], *, kubeconfig: str | None = None) -> list[dict[str, Any]]:
    document = command_json(
        [
            executable(policy, "kubectl"),
            "--kubeconfig",
            kubeconfig or policy["kubeconfig"],
            "get",
            "secrets",
            "--all-namespaces",
            "-o",
            "json",
        ],
        label="global Kubernetes Secret inventory",
    )
    if not isinstance(document, dict) or not isinstance(document.get("items"), list):
        raise ProviderError("Kubernetes returned an incomplete global Secret inventory")
    result: list[dict[str, Any]] = []
    for item in document["items"]:
        metadata = item.get("metadata") if isinstance(item, dict) else None
        annotations = metadata.get("annotations", {}) if isinstance(metadata, dict) else None
        owners = metadata.get("ownerReferences", []) if isinstance(metadata, dict) else None
        if (
            not isinstance(metadata, dict)
            or not isinstance(annotations, dict)
            or not isinstance(owners, list)
            or not all(
                isinstance(metadata.get(field), str) and metadata[field]
                for field in ("namespace", "name", "uid", "resourceVersion")
            )
        ):
            raise ProviderError("Kubernetes Secret lacks exact provider identity")
        result.append(
            {
                "metadata": {
                    "namespace": metadata["namespace"],
                    "name": metadata["name"],
                    "uid": metadata["uid"],
                    "resourceVersion": metadata["resourceVersion"],
                    "annotations": {
                        key: value
                        for key, value in annotations.items()
                        if key.startswith("fs2.nebius.ai/")
                        or key
                        in {
                            "kubernetes.io/service-account.name",
                            "kubernetes.io/service-account.uid",
                        }
                    },
                    "labels": safe_labels(metadata.get("labels", {})),
                    "owners": sorted(
                        [
                            {
                                "apiVersion": owner.get("apiVersion"),
                                "kind": owner.get("kind"),
                                "name": owner.get("name"),
                                "uid": owner.get("uid"),
                                "controller": owner.get("controller") is True,
                            }
                            for owner in owners
                            if isinstance(owner, dict)
                            and all(
                                isinstance(owner.get(field), str) and owner[field]
                                for field in ("apiVersion", "kind", "name", "uid")
                            )
                        ],
                        key=lambda value: (
                            str(value["kind"]), str(value["name"]), str(value["uid"])
                        ),
                    ),
                },
                "type": item.get("type", "Opaque"),
                "authorityContentSha256": secret_commitment(item.get("data", {})),
                "authorityEvidenceId": canonical_sha256(
                    [metadata["uid"], metadata["resourceVersion"]]
                ),
                "authorityObservedAt": observed_at(),
                "immutable": item.get("immutable") is True,
            }
        )
    return sorted(result, key=lambda value: (value["metadata"]["namespace"], value["metadata"]["name"]))


def kubernetes_service_accounts(policy: dict[str, Any]) -> list[dict[str, str]]:
    """Return exact ServiceAccount identities without token or Secret data."""

    document = command_json(
        [
            executable(policy, "kubectl"),
            "--kubeconfig",
            policy["kubeconfig"],
            "get",
            "serviceaccounts",
            "--all-namespaces",
            "-o",
            "json",
        ],
        label="global Kubernetes ServiceAccount inventory",
    )
    if not isinstance(document, dict) or not isinstance(document.get("items"), list):
        raise ProviderError("Kubernetes returned an incomplete ServiceAccount inventory")
    result: list[dict[str, str]] = []
    for item in document["items"]:
        metadata = item.get("metadata") if isinstance(item, dict) else None
        if not isinstance(metadata, dict) or not all(
            isinstance(metadata.get(field), str) and metadata[field]
            for field in ("namespace", "name", "uid", "resourceVersion")
        ):
            raise ProviderError("Kubernetes ServiceAccount lacks exact provider identity")
        result.append(
            {
                "namespace": metadata["namespace"],
                "name": metadata["name"],
                "uid": metadata["uid"],
                "resourceVersion": metadata["resourceVersion"],
            }
        )
    return sorted(result, key=lambda value: (value["namespace"], value["name"]))


def helm_release_secret_inventory(
    policy: dict[str, Any], secrets: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Classify Helm storage only through an independently pinned provider adapter."""

    adapter = policy["controller_inventory_adapters"]["helm_release_records"]
    command = verified_adapter_command(adapter, label="Helm release record inventory")
    response = command_json_input(
        command,
        label="Helm release record inventory",
        payload={
            "schema": "fs2-serve.nebius.ai/helm-release-inventory-request/v1",
            "cluster_id": policy["cluster_id"],
            "namespaces": policy["namespaces"],
        },
        timeout=adapter["timeout_seconds"],
    )
    fields = {
        "schema",
        "cluster_id",
        "records",
        "observed_at",
        "complete",
        "data_fields_returned",
    }
    records = response.get("records") if isinstance(response, dict) else None
    if (
        not isinstance(response, dict)
        or set(response) != fields
        or response.get("schema")
        != "fs2-serve.nebius.ai/helm-release-inventory/v1"
        or response.get("cluster_id") != policy["cluster_id"]
        or response.get("complete") is not True
        or response.get("data_fields_returned") != 0
        or not isinstance(records, list)
    ):
        raise ProviderError("Helm release inventory is incomplete")
    live = {
        (item["metadata"]["namespace"], item["metadata"]["name"]): item
        for item in secrets
    }
    verified: dict[str, dict[str, Any]] = {}
    record_fields = {
        "namespace",
        "name",
        "uid",
        "resource_version",
        "content_sha256",
        "release_name",
        "revision",
        "status",
    }
    for record in records:
        if not isinstance(record, dict) or set(record) != record_fields:
            raise ProviderError("Helm release record binding is malformed")
        item = live.get((record.get("namespace"), record.get("name")))
        metadata = item.get("metadata") if isinstance(item, dict) else None
        labels = metadata.get("labels", {}) if isinstance(metadata, dict) else {}
        identity = f"{record.get('namespace')}/{record.get('name')}"
        if (
            not isinstance(metadata, dict)
            or item.get("type") != "helm.sh/release.v1"
            or metadata.get("uid") != record.get("uid")
            or metadata.get("resourceVersion") != record.get("resource_version")
            or item.get("authorityContentSha256") != record.get("content_sha256")
            or labels.get("owner") != "helm"
            or labels.get("name") != record.get("release_name")
            or labels.get("version") != str(record.get("revision"))
            or labels.get("status") != record.get("status")
            or identity in verified
        ):
            raise ProviderError("Helm release record differs from live Secret identity")
        verified[identity] = record
    return verified


def list_items(document: Any, *, label: str) -> list[dict[str, Any]]:
    if isinstance(document, list):
        items = document
    elif isinstance(document, dict):
        candidates = [value for value in document.values() if isinstance(value, list)]
        items = candidates[0] if len(candidates) == 1 else None
    else:
        items = None
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ProviderError(f"{label} inventory is malformed")
    return items


def nested_id(value: Any, *path: str) -> str | None:
    current = value
    for item in path:
        if not isinstance(current, dict):
            return None
        current = current.get(item)
    return current if isinstance(current, str) and current else None


def resource_metadata(item: dict[str, Any], *, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    metadata = item.get("metadata")
    spec = item.get("spec", {})
    if not isinstance(metadata, dict) or not isinstance(spec, dict):
        raise ProviderError(f"{label} lacks metadata or spec")
    if not isinstance(metadata.get("id"), str) or not metadata["id"]:
        raise ProviderError(f"{label} lacks a provider ID")
    return metadata, spec


def normalize_nebius_items(document: Any, *, kind: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in list_items(document, label=kind):
        metadata, spec = resource_metadata(item, label=kind)
        normalized: dict[str, Any] = {
            "id": metadata["id"],
            "parent_id": metadata.get("parent_id"),
            "name": metadata.get("name"),
            "labels": safe_labels(metadata.get("labels")),
            "provider_object_sha256": canonical_sha256(item),
        }
        if kind in {"access_keys", "auth_public_keys"}:
            normalized["service_account_id"] = nested_id(
                spec, "account", "service_account", "id"
            )
            normalized["expires_at"] = spec.get("expires_at")
        if kind == "access_keys":
            reference = spec.get("secret_reference_id") or nested_id(
                item, "status", "secret_reference_id"
            )
            normalized["secret_reference_id_sha256"] = (
                hashlib.sha256(reference.encode()).hexdigest()
                if isinstance(reference, str) and reference
                else None
            )
        elif kind == "auth_public_keys":
            public = spec.get("data")
            normalized["public_key_sha256"] = (
                hashlib.sha256(public.encode()).hexdigest()
                if isinstance(public, str) and public
                else None
            )
        elif kind == "group_memberships":
            normalized["group_id"] = (
                spec.get("group_id") or metadata.get("parent_id")
            )
            normalized["member_id"] = spec.get("member_id") or item.get("member_id")
        elif kind == "access_permits":
            normalized["group_id"] = metadata.get("parent_id")
            normalized["resource_id"] = spec.get("resource_id") or item.get(
                "resource_id"
            )
            normalized["role"] = spec.get("role") or item.get("role")
        result.append(normalized)
    return sorted(result, key=lambda value: value["id"])


def nebius_inventory(policy: dict[str, Any]) -> dict[str, Any]:
    automation = policy["evidence_identity"]
    root_private_file(
        Path(automation["config_path"]),
        label="read-only evidence identity configuration",
        expected_sha256=automation["config_sha256"],
    )
    common = [
        "--profile",
        automation["profile"],
        "--parent-id",
        policy["project_id"],
        "--all",
        "--format",
        "json",
    ]
    binary = executable(policy, "nebius")
    prefix = [binary, "--config", automation["config_path"]]
    commands = {
        "service_accounts": [*prefix, "iam", "service-account", "list", *common],
        "access_keys": [*prefix, "iam", "access-key", "list", *common],
        "auth_public_keys": [*prefix, "iam", "auth-public-key", "list", *common],
        "groups": [*prefix, "iam", "group", "list", *common],
        "group_memberships": [*prefix, "iam", "group-membership", "list", *common],
        "access_permits": [*prefix, "iam", "access-permit", "list", *common],
    }
    return {
        kind: normalize_nebius_items(
            command_json(command, label=f"Nebius {kind} inventory"), kind=kind
        )
        for kind, command in commands.items()
    }


def authorization_closure_proof(
    policy: dict[str, Any],
    *,
    service_account_id: str,
    expected_group_id: str,
    expected_permits: list[dict[str, str]],
    label: str,
) -> dict[str, Any]:
    """Require a provider-derived closure across inherited and cross-resource grants."""

    adapter = policy["authorization_closure_adapter"]
    command = verified_adapter_command(adapter, label=f"{label} authorization closure")
    request = {
        "schema": "fs2-serve.nebius.ai/authorization-closure-request/v1",
        "project_id": policy["project_id"],
        "service_account_id": service_account_id,
    }
    response = command_json_input(
        command,
        label=f"{label} authorization closure",
        payload=request,
        timeout=adapter["timeout_seconds"],
    )
    required = {
        "schema",
        "project_id",
        "service_account_id",
        "scope",
        "memberships",
        "permits",
        "direct_permits",
        "impersonation_permits",
        "observed_at",
        "complete",
        "data_fields_returned",
    }
    memberships = response.get("memberships") if isinstance(response, dict) else None
    permits = response.get("permits") if isinstance(response, dict) else None
    if (
        not isinstance(response, dict)
        or set(response) != required
        or response.get("schema")
        != "fs2-serve.nebius.ai/authorization-closure/v1"
        or response.get("project_id") != policy["project_id"]
        or response.get("service_account_id") != service_account_id
        or response.get("scope") != "organization-and-descendants"
        or response.get("complete") is not True
        or response.get("data_fields_returned") != 0
        or memberships != [expected_group_id]
        or not isinstance(permits, list)
        or sorted(permits, key=canonical_sha256)
        != sorted(expected_permits, key=canonical_sha256)
        or response.get("direct_permits") != []
        or response.get("impersonation_permits") != []
    ):
        raise ProviderError(
            f"{label} has unproved, inherited, cross-resource, direct, or impersonation access"
        )
    return {
        "scope": response["scope"],
        "observed_at": response["observed_at"],
        "closure_sha256": canonical_sha256(response),
    }


def evidence_identity_proof(
    policy: dict[str, Any], inventory: dict[str, Any]
) -> dict[str, Any]:
    """Bind every provider read to one expiring viewer-only automation lineage."""

    configured = policy["evidence_identity"]
    remaining = provider_time(
        configured["expires_at"], label="evidence identity expiry"
    ) - datetime.now(UTC)
    if remaining <= timedelta(0) or remaining > timedelta(hours=24):
        raise ProviderError("read-only evidence identity lifetime is invalid")
    accounts = [
        item
        for item in inventory["service_accounts"]
        if item["id"] == configured["service_account_id"]
    ]
    if len(accounts) != 1 or accounts[0].get("parent_id") != policy["project_id"]:
        raise ProviderError("read-only evidence service account is absent or ambiguous")
    labels = accounts[0].get("labels", {})
    if labels.get("purpose") != "credential-evidence-reader":
        raise ProviderError("read-only evidence service account has the wrong purpose")
    credentials = [
        item
        for item in inventory[configured["credential_kind"]]
        if item["id"] == configured["credential_id"]
    ]
    if (
        len(credentials) != 1
        or credentials[0].get("parent_id") != policy["project_id"]
        or credentials[0].get("service_account_id")
        != configured["service_account_id"]
        or credentials[0].get("expires_at") != configured["expires_at"]
    ):
        raise ProviderError("read-only evidence credential lineage or expiry differs")
    memberships = [
        item
        for item in inventory["group_memberships"]
        if item.get("member_id") == configured["service_account_id"]
    ]
    if len(memberships) != 1:
        raise ProviderError("read-only evidence identity has an ambiguous group set")
    groups = [
        item
        for item in inventory["groups"]
        if item["id"] == memberships[0].get("group_id")
    ]
    if (
        len(groups) != 1
        or groups[0].get("parent_id") != policy["project_id"]
        or groups[0].get("labels", {}).get("purpose")
        != "credential-evidence-reader"
    ):
        raise ProviderError("read-only evidence viewer group is not exact")
    permits = [
        item
        for item in inventory["access_permits"]
        if item.get("group_id") == groups[0]["id"]
    ]
    if len(permits) != 1 or (
        permits[0].get("resource_id"), permits[0].get("role")
    ) != (policy["project_id"], "viewer"):
        raise ProviderError("read-only evidence identity is not viewer-only")
    closure = authorization_closure_proof(
        policy,
        service_account_id=configured["service_account_id"],
        expected_group_id=groups[0]["id"],
        expected_permits=[
            {
                "permit_id": permits[0]["id"],
                "group_id": groups[0]["id"],
                "resource_id": policy["project_id"],
                "role": "viewer",
            }
        ],
        label="read-only evidence identity",
    )
    return {
        "service_account_id": configured["service_account_id"],
        "credential_kind": configured["credential_kind"],
        "credential_id": configured["credential_id"],
        "expires_at": configured["expires_at"],
        "group_id": groups[0]["id"],
        "membership_id": memberships[0]["id"],
        "permit_id": permits[0]["id"],
        "role": "viewer",
        "authorization_closure": closure,
        "provider_identity_sha256": canonical_sha256(
            [accounts[0], credentials[0], groups[0], memberships[0], permits[0]]
        ),
    }


def release_identity_proof(
    policy: dict[str, Any], inventory: dict[str, Any]
) -> dict[str, Any]:
    """Prove one provider-enforced workload session and its complete grant closure."""

    configured = policy["release_identity"]
    config_sha256 = root_private_file(
        Path(configured["config_path"]),
        label="release workload identity configuration",
        expected_sha256=configured["config_sha256"],
    )
    adapter = policy["release_identity_adapter"]
    command = verified_adapter_command(adapter, label="release workload identity")
    session = command_json_input(
        command,
        label="release workload identity",
        payload={
            "schema": "fs2-serve.nebius.ai/release-session-observation-request/v1",
            "project_id": policy["project_id"],
            "service_account_id": configured["service_account_id"],
            "audience": configured["audience"],
            "provider_issuer": configured["provider_issuer"],
            "token_exchange_source": configured["token_exchange_source"],
            "config_path": configured["config_path"],
            "config_sha256": config_sha256,
        },
        timeout=adapter["timeout_seconds"],
    )
    required = {
        "schema",
        "provider",
        "provider_session_id",
        "project_id",
        "service_account_id",
        "credential_kind",
        "audience",
        "provider_issuer",
        "token_exchange_source",
        "issued_at",
        "expires_at",
        "actor_type",
        "human_principal",
        "interactive_login",
        "impersonation_allowed",
        "config_sha256",
        "observed_at",
        "complete",
        "data_fields_returned",
    }
    if (
        not isinstance(session, dict)
        or set(session) != required
        or session.get("schema")
        != "fs2-serve.nebius.ai/release-session-observation/v1"
        or session.get("provider") != "nebius-iam"
        or session.get("project_id") != policy["project_id"]
        or session.get("service_account_id") != configured["service_account_id"]
        or session.get("credential_kind") != "workload_identity_session"
        or session.get("audience") != configured["audience"]
        or session.get("provider_issuer") != configured["provider_issuer"]
        or session.get("token_exchange_source")
        != configured["token_exchange_source"]
        or session.get("actor_type") != "service_account"
        or session.get("human_principal") is not False
        or session.get("interactive_login") is not False
        or session.get("impersonation_allowed") is not False
        or session.get("config_sha256") != config_sha256
        or session.get("complete") is not True
        or session.get("data_fields_returned") != 0
        or not isinstance(session.get("provider_session_id"), str)
        or not session["provider_session_id"]
    ):
        raise ProviderError("release identity is not a provider-enforced workload session")
    issued = provider_time(session["issued_at"], label="release identity issue time")
    expires = provider_time(session["expires_at"], label="release identity expiry")
    now = datetime.now(UTC)
    if (
        issued > now
        or expires <= now
        or expires - issued
        > timedelta(seconds=configured["maximum_lifetime_seconds"])
    ):
        raise ProviderError("release automation identity lifetime is invalid")
    accounts = [
        item
        for item in inventory["service_accounts"]
        if item["id"] == configured["service_account_id"]
    ]
    if (
        len(accounts) != 1
        or accounts[0].get("parent_id") != policy["project_id"]
        or accounts[0].get("labels", {}).get("purpose")
        != "credential-release-automation"
        or accounts[0].get("labels", {}).get("audience")
        != configured["audience"]
    ):
        raise ProviderError("release automation service account lineage differs")
    memberships = [
        item
        for item in inventory["group_memberships"]
        if item.get("member_id") == configured["service_account_id"]
    ]
    if len(memberships) != 1:
        raise ProviderError("release automation identity has an ambiguous group set")
    groups = [
        item
        for item in inventory["groups"]
        if item["id"] == memberships[0].get("group_id")
    ]
    if (
        len(groups) != 1
        or groups[0].get("parent_id") != policy["project_id"]
        or groups[0].get("labels", {}).get("purpose")
        != "credential-release-automation"
        or groups[0].get("labels", {}).get("audience")
        != configured["audience"]
    ):
        raise ProviderError("release automation group lineage differs")
    permits = [
        item
        for item in inventory["access_permits"]
        if item.get("group_id") == groups[0]["id"]
    ]
    observed_roles = sorted(
        item["role"]
        for item in permits
        if item.get("resource_id") == policy["project_id"]
        and isinstance(item.get("role"), str)
    )
    if (
        observed_roles != sorted(configured["allowed_roles"])
        or len(permits) != len(observed_roles)
        or any(item.get("resource_id") != policy["project_id"] for item in permits)
    ):
        raise ProviderError("release automation role set differs from policy")
    expected_permits = [
        {
            "permit_id": item["id"],
            "group_id": groups[0]["id"],
            "resource_id": policy["project_id"],
            "role": item["role"],
        }
        for item in sorted(permits, key=lambda value: value["id"])
    ]
    closure = authorization_closure_proof(
        policy,
        service_account_id=configured["service_account_id"],
        expected_group_id=groups[0]["id"],
        expected_permits=expected_permits,
        label="release workload identity",
    )
    return {
        "project_id": policy["project_id"],
        "service_account_id": configured["service_account_id"],
        "credential_kind": "workload_identity_session",
        "provider_session_id": session["provider_session_id"],
        "issued_at": session["issued_at"],
        "expires_at": session["expires_at"],
        "audience": session["audience"],
        "provider_issuer": session["provider_issuer"],
        "token_exchange_source": session["token_exchange_source"],
        "profile": configured["profile"],
        "config_path": configured["config_path"],
        "config_sha256": config_sha256,
        "allowed_roles": observed_roles,
        "allowed_commands": configured["allowed_commands"],
        "interactive_login_allowed": False,
        "human_principal_allowed": False,
        "impersonation_allowed": False,
        "group_id": groups[0]["id"],
        "membership_id": memberships[0]["id"],
        "permit_ids": sorted(item["id"] for item in permits),
        "authorization_closure": closure,
        "provider_executables": policy["provider_executables"],
        "provider_identity_sha256": canonical_sha256(
            [accounts[0], session, groups[0], memberships[0], permits, closure]
        ),
    }


def release_lineage_inventory(
    policy: dict[str, Any], inventory: dict[str, Any]
) -> dict[str, Any]:
    """Classify the dormant release service account without requiring an active session."""

    configured = policy["release_identity"]
    accounts = [
        item
        for item in inventory["service_accounts"]
        if item["id"] == configured["service_account_id"]
    ]
    memberships = [
        item
        for item in inventory["group_memberships"]
        if item.get("member_id") == configured["service_account_id"]
    ]
    groups = [
        item
        for item in inventory["groups"]
        if memberships and item["id"] == memberships[0].get("group_id")
    ]
    permits = [
        item
        for item in inventory["access_permits"]
        if groups and item.get("group_id") == groups[0]["id"]
    ]
    roles = sorted(
        item.get("role") for item in permits if isinstance(item.get("role"), str)
    )
    if (
        len(accounts) != 1
        or accounts[0].get("parent_id") != policy["project_id"]
        or accounts[0].get("labels", {}).get("purpose")
        != "credential-release-automation"
        or len(memberships) != 1
        or len(groups) != 1
        or groups[0].get("parent_id") != policy["project_id"]
        or groups[0].get("labels", {}).get("purpose")
        != "credential-release-automation"
        or roles != sorted(configured["allowed_roles"])
        or len(permits) != len(roles)
        or any(item.get("resource_id") != policy["project_id"] for item in permits)
    ):
        raise ProviderError("dormant release automation lineage is not exact")
    expected_permits = [
        {
            "permit_id": item["id"],
            "group_id": groups[0]["id"],
            "resource_id": policy["project_id"],
            "role": item["role"],
        }
        for item in sorted(permits, key=lambda value: value["id"])
    ]
    closure = authorization_closure_proof(
        policy,
        service_account_id=configured["service_account_id"],
        expected_group_id=groups[0]["id"],
        expected_permits=expected_permits,
        label="dormant release automation identity",
    )
    return {
        "service_account_id": configured["service_account_id"],
        "group_id": groups[0]["id"],
        "membership_id": memberships[0]["id"],
        "permit_ids": sorted(item["id"] for item in permits),
        "authorization_closure": closure,
    }


def load_registry(policy: dict[str, Any]) -> dict[str, Any]:
    registry = stable_json(
        Path(policy["credential_registry_path"]), label="durable credential registry"
    )
    if registry.get("schema") != "fs2-serve.nebius.ai/durable-credential-registry/v2":
        raise ProviderError("durable credential registry schema is unsupported")
    return registry


def classes_for_address(
    registry: dict[str, Any], root: str, address: str
) -> list[dict[str, Any]]:
    pending = set(registry.get("pending_credential_ids", []))
    return [
        item
        for item in registry["credentials"]
        if item.get("id") not in pending
        if item.get("terraform_root") == root
        and any(re.fullmatch(pattern, address) for pattern in item.get("address_regexes", []))
    ]


def generation_from_address(address: str) -> int:
    match = re.search(r"\[(?:\"?)([1-9][0-9]*)(?:\"?)\]$", address)
    return int(match.group(1)) if match else 1


def provider_kind(terraform_type: str) -> str | None:
    return {
        "nebius_iam_v1_service_account": "service_accounts",
        "nebius_iam_v1_group": "groups",
        "nebius_iam_v1_group_membership": "group_memberships",
        "nebius_iam_v1_access_permit": "access_permits",
        "nebius_iam_v2_access_key": "access_keys",
    }.get(terraform_type)


def base_address(address: str) -> str:
    return re.sub(r"\[.*\]$", "", address)


def reconcile_global_provider_inventory(
    *,
    states: list[dict[str, Any]],
    secrets: list[dict[str, Any]],
    service_accounts: list[dict[str, str]],
    helm_release_records: dict[str, dict[str, Any]],
    provider_inventory: dict[str, Any],
    registry: dict[str, Any],
    evidence_identity: dict[str, Any],
    release_identity: dict[str, Any],
    operator_identity: dict[str, Any],
    approved_namespaces: list[str],
) -> dict[str, Any]:
    """Reject any cluster Secret or project IAM object without exact custody."""

    declared = {
        (item["root"], item["address"])
        for field in (
            "terraform_resource_addresses",
            "provider_managed_resource_addresses",
        )
        for item in registry.get(field, [])
    }
    exemptions = registry.get("provider_inventory_exemptions")
    if not isinstance(exemptions, dict) or set(exemptions) != {
        "kubernetes_secrets",
        "nebius_iam",
    }:
        raise ProviderError("provider inventory classification registry is malformed")
    exemption_fields = {
        "id",
        "owner",
        "purpose",
        "expires_at",
        "readers",
        "source",
    }
    for family in ("kubernetes_secrets", "nebius_iam"):
        entries = exemptions[family]
        if (
            not isinstance(entries, list)
            or any(
                not isinstance(item, dict)
                or set(item) != exemption_fields
                or not all(
                    isinstance(item.get(field), str) and item[field]
                    for field in ("id", "owner", "purpose", "expires_at", "source")
                )
                or not isinstance(item.get("readers"), list)
                or not item["readers"]
                or not all(isinstance(reader, str) and reader for reader in item["readers"])
                for item in entries
            )
            or len({item["id"] for item in entries}) != len(entries)
        ):
            raise ProviderError(
                f"provider inventory classification is incomplete: {family}"
            )
        for item in entries:
            expiry = provider_time(
                item["expires_at"], label=f"{family} exemption expiry"
            )
            if expiry <= datetime.now(UTC) or expiry > datetime.now(UTC) + timedelta(days=30):
                raise ProviderError(
                    f"provider inventory exemption is expired or overlong: {item['id']}"
                )
    rules = registry.get("provider_inventory_rules")
    if not isinstance(rules, dict) or set(rules) != {
        "kubernetes_secrets",
        "nebius_iam",
    }:
        raise ProviderError("provider inventory rule registry is malformed")
    secret_rule_fields = {
        "id",
        "type",
        "name_regex",
        "namespace_scope",
        "owner",
        "purpose",
        "readers",
        "expiry",
        "source",
    }
    secret_rules = rules["kubernetes_secrets"]
    if (
        not isinstance(secret_rules, list)
        or not secret_rules
        or any(
            not isinstance(rule, dict)
            or set(rule) != secret_rule_fields
            or rule.get("namespace_scope") != "authority-approved"
            or not all(
                isinstance(rule.get(field), str) and rule[field]
                for field in secret_rule_fields - {"readers"}
            )
            or not isinstance(rule.get("readers"), list)
            or not rule["readers"]
            or re.fullmatch(rule["name_regex"], "") is not None
            for rule in secret_rules
        )
        or rules["nebius_iam"] != []
    ):
        raise ProviderError("provider inventory rules are incomplete")
    state_secret_ids: set[str] = set()
    state_secret_bindings: dict[str, dict[str, Any]] = {}
    state_nebius_ids: set[str] = set()
    for state in states:
        root = state["root"]
        for resource in state["resources"]:
            if (root, base_address(resource["address"])) not in declared:
                continue
            provider_id = resource.get("provider_id")
            if not isinstance(provider_id, str) or not provider_id:
                continue
            if resource.get("type") == "kubernetes_secret_v1":
                state_secret_ids.add(provider_id)
                binding = resource.get("provider_binding")
                if not isinstance(binding, dict) or provider_id in state_secret_bindings:
                    raise ProviderError(
                        f"Terraform Secret lacks one exact state binding: {resource['address']}"
                    )
                state_secret_bindings[provider_id] = binding
            elif provider_kind(str(resource.get("type", ""))) is not None:
                state_nebius_ids.add(provider_id)
    live_secret_ids = {
        f"{item['metadata']['namespace']}/{item['metadata']['name']}"
        for item in secrets
    }
    live_nebius_ids = {
        item["id"]
        for kind in (
            "service_accounts",
            "access_keys",
            "auth_public_keys",
            "groups",
            "group_memberships",
            "access_permits",
        )
        for item in provider_inventory[kind]
    }
    automation_ids = {
        evidence_identity["service_account_id"],
        evidence_identity["credential_id"],
        evidence_identity["group_id"],
        evidence_identity["membership_id"],
        evidence_identity["permit_id"],
        release_identity["service_account_id"],
        release_identity["group_id"],
        release_identity["membership_id"],
        *release_identity["permit_ids"],
        operator_identity["service_account_id"],
        operator_identity["key_id"],
        operator_identity["group_id"],
        operator_identity["membership_id"],
        operator_identity["permit_id"],
    }
    classified_secret_ids = {
        item["id"] for item in exemptions["kubernetes_secrets"]
    }
    classified_nebius_ids = {item["id"] for item in exemptions["nebius_iam"]}
    if not classified_secret_ids <= live_secret_ids or not classified_nebius_ids <= live_nebius_ids:
        raise ProviderError("provider classification names an absent live object")
    rule_classifications: dict[str, str] = {}
    approved_namespace_set = set(approved_namespaces)
    service_account_index = {
        (item["namespace"], item["name"]): item for item in service_accounts
    }
    for item in secrets:
        identity = f"{item['metadata']['namespace']}/{item['metadata']['name']}"
        if identity in state_secret_ids:
            binding = state_secret_bindings[identity]
            annotations = item["metadata"].get("annotations", {})
            if (
                binding.get("namespace") != item["metadata"]["namespace"]
                or binding.get("name") != item["metadata"]["name"]
                or binding.get("content_sha256")
                != item.get("authorityContentSha256")
                or binding.get("content_sha256")
                != annotations.get("fs2.nebius.ai/content-sha256")
                or binding.get("credential_class")
                != annotations.get("fs2.nebius.ai/credential-class")
                or str(binding.get("credential_generation"))
                != annotations.get("fs2.nebius.ai/credential-generation")
                or binding.get("immutable") != item.get("immutable")
                or (
                    binding.get("uid") not in (None, "")
                    and binding.get("uid") != item["metadata"]["uid"]
                )
                or (
                    binding.get("resource_version") not in (None, "")
                    and str(binding.get("resource_version"))
                    != item["metadata"]["resourceVersion"]
                )
            ):
                raise ProviderError(
                    f"Terraform Secret state differs from live UID/RV/content binding: {identity}"
                )
            continue
        if identity in classified_secret_ids:
            continue
        matches = [
            rule
            for rule in secret_rules
            if item["metadata"]["namespace"] in approved_namespace_set
            and item.get("type") == rule["type"]
            and re.fullmatch(rule["name_regex"], item["metadata"]["name"])
            is not None
        ]
        if len(matches) == 1 and matches[0]["id"] == "helm-release-records":
            if identity not in helm_release_records:
                matches = []
        if len(matches) == 1 and matches[0]["id"] == "bound-service-account-token":
            annotations = item["metadata"].get("annotations", {})
            owners = item["metadata"].get("owners", [])
            service_account = annotations.get("kubernetes.io/service-account.name")
            service_account_uid = annotations.get("kubernetes.io/service-account.uid")
            live_account = service_account_index.get(
                (item["metadata"]["namespace"], service_account)
            ) if isinstance(service_account, str) else None
            exact_owners = [
                owner
                for owner in owners
                if owner.get("apiVersion") == "v1"
                and owner.get("kind") == "ServiceAccount"
                and owner.get("name") == service_account
                and owner.get("uid") == service_account_uid
            ]
            if (
                not isinstance(live_account, dict)
                or live_account.get("uid") != service_account_uid
                or len(owners) != 1
                or len(exact_owners) != 1
            ):
                matches = []
        if len(matches) == 1:
            rule_classifications[identity] = matches[0]["id"]
    unmanaged_secrets = sorted(
        live_secret_ids
        - state_secret_ids
        - classified_secret_ids
        - set(rule_classifications)
    )
    unmanaged_nebius = sorted(
        live_nebius_ids - state_nebius_ids - automation_ids - classified_nebius_ids
    )
    if unmanaged_secrets or unmanaged_nebius:
        raise ProviderError(
            "global provider inventory is not fully reconciled: "
            f"{len(unmanaged_secrets)} Kubernetes Secrets and "
            f"{len(unmanaged_nebius)} Nebius IAM objects lack exact custody"
        )
    return {
        "scope": "all-cluster-secrets-and-all-project-iam",
        "kubernetes_secret_count": len(live_secret_ids),
        "nebius_iam_count": len(live_nebius_ids),
        "unmanaged_kubernetes_secret_count": 0,
        "unmanaged_nebius_iam_count": 0,
        "classified_inventory_sha256": canonical_sha256(
            {
                "exemptions": exemptions,
                "rules": rules,
                "matches": rule_classifications,
                "helm_release_records": helm_release_records,
                "service_accounts": service_accounts,
                "state_secret_bindings": state_secret_bindings,
            }
        ),
        "inventory_sha256": canonical_sha256(
            [sorted(live_secret_ids), sorted(live_nebius_ids)]
        ),
    }


def credential_inventory(
    policy: dict[str, Any],
    states: list[dict[str, Any]],
    provider_inventory: dict[str, Any],
) -> dict[str, Any]:
    registry = load_registry(policy)
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for state in states:
        for resource in state["resources"]:
            credential_classes = classes_for_address(
                registry, state["root"], resource["address"]
            )
            if not credential_classes:
                continue
            generation = generation_from_address(resource["address"])
            binding: dict[str, Any] | None = None
            kind = provider_kind(str(resource.get("type", "")))
            if kind is not None:
                provider_id = resource.get("provider_id")
                matches = [
                    item
                    for item in provider_inventory.get(kind, [])
                    if isinstance(item, dict) and item.get("id") == provider_id
                ]
                if len(matches) != 1:
                    raise ProviderError(
                        "Terraform durable identity is absent or ambiguous in provider inventory: "
                        f"{resource['address']}"
                    )
                binding = {
                    "kind": kind,
                    "provider_id": provider_id,
                    "provider_object_sha256": matches[0]["provider_object_sha256"],
                }
            for credential_class in credential_classes:
                grouped.setdefault((credential_class["id"], generation), []).append(
                    {
                        "terraform_address": resource["address"],
                        "terraform_root": state["root"],
                        "identity_sha256": resource["identity_sha256"],
                        "state_lineage": state["lineage"],
                        "state_serial": state["serial"],
                        "provider_binding": binding,
                    }
                )
    pending = set(registry.get("pending_credential_ids", []))
    policies = {
        item["id"]: item
        for item in registry["credentials"]
        if item["id"] not in pending
    }
    items: list[dict[str, Any]] = []
    for (credential_class, generation), resources in sorted(grouped.items()):
        resources = sorted(resources, key=lambda item: (item["terraform_root"], item["terraform_address"]))
        fingerprint = canonical_sha256(resources)
        policy_entry = policies[credential_class]
        items.append(
            {
                "id": canonical_sha256(
                    [policy["project_id"], credential_class, generation, fingerprint]
                ),
                "credential_class": credential_class,
                "owner_id": policy_entry["owner"],
                "project_id": policy["project_id"],
                "purpose": policy_entry["purpose"],
                "generation": generation,
                "fingerprint": fingerprint,
                "status": "observed",
                "provider_version": canonical_sha256(
                    [
                        [item["state_lineage"], item["state_serial"]]
                        for item in resources
                    ]
                ),
                # Required-expiry classes remain unready until a provider-native
                # source supplies an enforceable expiry.  Never synthesize one.
                "expires_at": None,
                "readers": policy_entry["readers"],
                "terraform_bindings": resources,
            }
        )
    classes: dict[str, dict[str, Any]] = {}
    for policy_entry in policies.values():
        matches = [item for item in items if item["credential_class"] == policy_entry["id"]]
        if not matches:
            raise ProviderError(
                f"authoritative states omit credential class {policy_entry['id']}"
            )
        generations = sorted({item["generation"] for item in matches})
        if generations != list(range(1, max(generations) + 1)):
            raise ProviderError(
                f"credential class has a reduced generation history: {policy_entry['id']}"
            )
        classes[policy_entry["id"]] = {
            "current_generation": max(generations),
            "retained_generations": generations,
            "identities_sha256": canonical_sha256(matches),
        }
    ids = [item["id"] for item in items]
    fingerprints = [item["fingerprint"] for item in items]
    if len(ids) != len(set(ids)) or len(fingerprints) != len(set(fingerprints)):
        raise ProviderError("credential IDs or fingerprints are reused")
    return {"items": items, "classes": classes}


def artifact_inventory(policy: dict[str, Any]) -> dict[str, Any]:
    artifacts: list[dict[str, Any]] = []
    observed_paths: set[tuple[int, int]] = set()
    for scope in policy["artifact_inventory_scopes"]:
        root = Path(scope["root"])
        if root.is_symlink() or not root.is_dir():
            raise ProviderError("configured artifact scope is absent or unsafe")
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ProviderError("artifact scope contains a symlink")
            if not path.is_file():
                continue
            identity = (path.stat().st_dev, path.stat().st_ino)
            if identity in observed_paths:
                raise ProviderError("artifact scopes overlap or alias the same file")
            observed_paths.add(identity)
            name = path.name.lower()
            classification = "unknown-sensitive"
            if ".tfstate" in name or name.endswith(".backup"):
                classification = "terraform-state"
            elif ".tfplan" in name or name.endswith(".plan.json"):
                classification = "terraform-plan"
            elif "cookie" in name:
                classification = "session-cookie"
            elif any(word in name for word in ("access", "credential", "handoff")):
                classification = "scoped-credential"
            artifacts.append(
                {
                    "artifact_id": canonical_sha256([scope["id"], str(path)]),
                    "path_sha256": hashlib.sha256(str(path).encode()).hexdigest(),
                    "sha256": file_sha256(path),
                    "classification": classification,
                    "owner": scope["owner"],
                    "purpose": scope["purpose"],
                    "expires_at": scope["expires_at"],
                    "readers": scope["readers"],
                    "storage": scope["id"],
                    "scope_category": scope["category"],
                    "local_present": True,
                    "disposition": "retained-for-migration",
                    "provider_version": "filesystem-v2",
                    "audit_event_id": canonical_sha256([path.stat().st_dev, path.stat().st_ino, path.stat().st_mtime_ns]),
                }
            )
    return {
        "schema": "fs2-serve.nebius.ai/authoritative-artifact-inventory/v1",
        "scope": "all-configured-product-operator-state",
        "configuration_sha256": canonical_sha256(policy),
        "scope_registry_sha256": canonical_sha256(policy["artifact_inventory_scopes"]),
        "scope_categories": sorted(
            scope["category"] for scope in policy["artifact_inventory_scopes"]
        ),
        "scope_count": len(policy["artifact_inventory_scopes"]),
        "evidence_id": canonical_sha256(artifacts),
        "observed_at": observed_at(),
        "artifacts": artifacts,
    }


def plan_admission(
    policy: dict[str, Any],
    phase: str,
    secrets: list[dict[str, Any]],
    registry: dict[str, Any],
) -> dict[str, Any]:
    terraform = executable(policy, "terraform")
    credential_classes = {
        item["id"]
        for item in registry["credentials"]
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    plans: list[dict[str, Any]] = []
    live = {
        (item["metadata"]["namespace"], item["metadata"]["name"]): item
        for item in secrets
    }
    post_create: list[dict[str, Any]] = []
    planned_creates: list[dict[str, Any]] = []
    for root_name, root in sorted(policy["terraform_roots"].items()):
        for path_value in root["saved_plan_paths"]:
            path = Path(path_value)
            if not path.is_absolute() or path.is_symlink() or not path.is_file():
                raise ProviderError(f"{root_name} saved plan is absent or unsafe")
            metadata = path.stat()
            if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise ProviderError(
                    f"{root_name} saved plan must be root-owned mode 0600"
                )
            document = command_json(
                [terraform, f"-chdir={root['configuration_dir']}", "show", "-json", str(path)],
                label=f"{root_name} saved plan",
            )
            changes = document.get("resource_changes")
            if not isinstance(changes, list):
                raise ProviderError("saved plan omits resource changes")
            for change in changes:
                if not isinstance(change, dict):
                    raise ProviderError("saved plan contains a malformed resource change")
                if change.get("type") != "kubernetes_secret_v1":
                    continue
                actions = change.get("change", {}).get("actions")
                if actions != ["create"]:
                    if actions not in (["no-op"], ["read"]):
                        raise ProviderError("saved plan mutates an existing credential Secret")
                    continue
                if phase == "consumer-rollout":
                    raise ProviderError("consumer rollout plan still creates a credential Secret")
                after = change.get("change", {}).get("after")
                metadata = after.get("metadata") if isinstance(after, dict) else None
                metadata = metadata[0] if isinstance(metadata, list) and metadata else metadata
                if not isinstance(metadata, dict):
                    raise ProviderError("planned Secret lacks metadata")
                identity = (metadata.get("namespace"), metadata.get("name"))
                observed = live.get(identity)
                annotations = metadata.get("annotations", {})
                if (
                    not all(isinstance(value, str) and value for value in identity)
                    or not isinstance(annotations, dict)
                    or not isinstance(
                        annotations.get("fs2.nebius.ai/credential-class"), str
                    )
                    or not isinstance(
                        annotations.get("fs2.nebius.ai/credential-generation"), str
                    )
                    or not isinstance(
                        annotations.get("fs2.nebius.ai/content-sha256"), str
                    )
                    or annotations.get("fs2.nebius.ai/credential-class")
                    not in credential_classes
                ):
                    raise ProviderError(
                        "planned credential Secret lacks class, generation, or commitment"
                    )
                if observed is not None:
                    raise ProviderError(
                        "planned immutable credential Secret already exists"
                    )
                planned_creates.append(
                    {
                        "root": root_name,
                        "namespace": identity[0],
                        "name": identity[1],
                        "credential_class": annotations[
                            "fs2.nebius.ai/credential-class"
                        ],
                        "generation": int(
                            annotations["fs2.nebius.ai/credential-generation"]
                        ),
                        "content_sha256": annotations[
                            "fs2.nebius.ai/content-sha256"
                        ],
                    }
                )
            plans.append(
                {
                    "root": root_name,
                    "path_sha256": hashlib.sha256(str(path).encode()).hexdigest(),
                    "plan_sha256": file_sha256(path),
                    "plan_json_sha256": canonical_sha256(document),
                    "resource_changes_sha256": canonical_sha256(changes),
                }
            )
    if phase == "consumer-rollout" and not post_create:
        # Consumer rollout is authorized by current live immutable Secret
        # bindings, not by planned commitments.  Return the complete global
        # inventory so the caller must bind every exact consumer separately.
        post_create = [
            item
            for item in secrets
            if item["immutable"] is True
            and "fs2.nebius.ai/credential-class"
            in item["metadata"]["annotations"]
            and "fs2.nebius.ai/credential-generation"
            in item["metadata"]["annotations"]
            and "fs2.nebius.ai/content-sha256"
            in item["metadata"]["annotations"]
        ]
        if not post_create:
            raise ProviderError(
                "consumer rollout has no provider-observed immutable credential generation"
            )
    return {
        "registry_sha256": canonical_sha256(registry),
        "phase": phase,
        "plans": plans,
        "planned_secret_commitments": planned_creates,
        "post_create_secret_bindings": post_create,
    }


def denial_probes() -> dict[str, list[str]]:
    probes: dict[str, list[str]] = {}
    namespaced = (
        "pods",
        "deployments.apps",
        "statefulsets.apps",
        "daemonsets.apps",
        "replicasets.apps",
        "jobs.batch",
        "cronjobs.batch",
        "configmaps",
        "services",
        "persistentvolumeclaims",
        "roles.rbac.authorization.k8s.io",
        "rolebindings.rbac.authorization.k8s.io",
    )
    for resource in namespaced:
        for verb in ("create", "update", "patch", "delete", "deletecollection"):
            probes[f"{verb}_{resource}"] = [verb, resource, "--all-namespaces"]
    for resource in (
        "clusterroles.rbac.authorization.k8s.io",
        "clusterrolebindings.rbac.authorization.k8s.io",
    ):
        for verb in ("create", "update", "patch", "delete", "deletecollection"):
            probes[f"{verb}_{resource}"] = [verb, resource]
    for resource in ("pods/exec", "pods/attach", "pods/portforward"):
        probes[f"create_{resource}"] = ["create", resource, "--all-namespaces"]
    for verb in ("get", "list", "watch", "create", "update", "patch", "delete"):
        probes[f"{verb}_secrets"] = [verb, "secrets", "--all-namespaces"]
    probes.update(
        {
            "create_serviceaccount_tokens": [
                "create",
                "serviceaccounts/token",
                "--all-namespaces",
            ],
            "create_tokenreviews": [
                "create",
                "tokenreviews.authentication.k8s.io",
            ],
            "escalate_roles": [
                "escalate",
                "roles.rbac.authorization.k8s.io",
                "--all-namespaces",
            ],
            "bind_roles": [
                "bind",
                "roles.rbac.authorization.k8s.io",
                "--all-namespaces",
            ],
            "escalate_clusterroles": [
                "escalate",
                "clusterroles.rbac.authorization.k8s.io",
            ],
            "bind_clusterroles": [
                "bind",
                "clusterroles.rbac.authorization.k8s.io",
            ],
            "impersonate_users": ["impersonate", "users"],
            "impersonate_groups": ["impersonate", "groups"],
            "impersonate_serviceaccounts": ["impersonate", "serviceaccounts"],
        }
    )
    return probes


def authorization_denials(policy: dict[str, Any]) -> dict[str, bool]:
    kubectl = executable(policy, "kubectl")
    common = [kubectl, "--kubeconfig", policy["handoff_kubeconfig"], "auth", "can-i"]
    result: dict[str, bool] = {}
    for name, probe in denial_probes().items():
        completed = subprocess.run(
            [*common, *probe], text=True, capture_output=True, check=False
        )
        answer = completed.stdout.strip().lower()
        if completed.returncode != 0 or answer not in {"yes", "no"}:
            raise ProviderError(f"handoff authorization probe failed: {name}")
        result[name] = answer == "no"
    if not all(result.values()):
        raise ProviderError("handoff identity retains mutation, Secret, or token access")
    return result


def exact_handoff_lineage(
    policy: dict[str, Any],
    inventory: dict[str, Any],
    *,
    key_id: str,
) -> dict[str, Any]:
    keys = [item for item in inventory["auth_public_keys"] if item["id"] == key_id]
    if len(keys) != 1:
        raise ProviderError("handoff key is absent or ambiguous in provider inventory")
    key = keys[0]
    if key.get("parent_id") != policy["project_id"]:
        raise ProviderError("handoff key belongs to another project")
    service_account_id = key.get("service_account_id")
    accounts = [
        item
        for item in inventory["service_accounts"]
        if item["id"] == service_account_id
    ]
    if len(accounts) != 1:
        raise ProviderError("handoff key service account is absent or ambiguous")
    account = accounts[0]
    labels = account.get("labels", {})
    lineage_id = labels.get("handoff_lineage")
    generation_text = labels.get("handoff_generation")
    if (
        account.get("parent_id") != policy["project_id"]
        or labels.get("purpose") != "operator-handoff-viewer"
        or not isinstance(lineage_id, str)
        or not lineage_id
        or not isinstance(generation_text, str)
        or not generation_text.isdigit()
        or int(generation_text) < 1
    ):
        raise ProviderError("handoff service account is outside the viewer lineage")
    generation = int(generation_text)
    groups = [
        item
        for item in inventory["groups"]
        if item.get("parent_id") == policy["project_id"]
        and item.get("labels", {}).get("purpose") == "operator-handoff-viewer"
        and item.get("labels", {}).get("handoff_lineage") == lineage_id
        and item.get("labels", {}).get("handoff_generation") == generation_text
    ]
    if len(groups) != 1:
        raise ProviderError("handoff viewer group is absent or ambiguous")
    group = groups[0]
    memberships = [
        item
        for item in inventory["group_memberships"]
        if item.get("group_id") == group["id"]
    ]
    if len(memberships) != 1 or memberships[0].get("member_id") != service_account_id:
        raise ProviderError("handoff group membership is not exact")
    account_memberships = [
        item
        for item in inventory["group_memberships"]
        if item.get("member_id") == service_account_id
    ]
    if len(account_memberships) != 1 or account_memberships[0].get("group_id") != group["id"]:
        raise ProviderError("handoff service account has an additional group membership")
    permits = [
        item
        for item in inventory["access_permits"]
        if item.get("group_id") == group["id"]
    ]
    if len(permits) != 1 or (
        permits[0].get("resource_id"), permits[0].get("role")
    ) != (policy["project_id"], "viewer"):
        raise ProviderError("handoff group does not have exactly project viewer")
    closure = authorization_closure_proof(
        policy,
        service_account_id=service_account_id,
        expected_group_id=group["id"],
        expected_permits=[
            {
                "permit_id": permits[0]["id"],
                "group_id": group["id"],
                "resource_id": policy["project_id"],
                "role": "viewer",
            }
        ],
        label="operator viewer identity",
    )
    expiry = key.get("expires_at")
    try:
        expires_at = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
    except ValueError as error:
        raise ProviderError("handoff key has no provider-enforced expiry") from error
    if expires_at.tzinfo is None or expires_at.astimezone(UTC) <= datetime.now(UTC):
        raise ProviderError("handoff key expiry is absent or elapsed")
    return {
        "project_id": policy["project_id"],
        "cluster_id": policy["cluster_id"],
        "lineage_id": lineage_id,
        "generation": generation,
        "service_account_id": service_account_id,
        "group_id": group["id"],
        "membership_id": memberships[0]["id"],
        "permit_id": permits[0]["id"],
        "role": "viewer",
        "key_id": key_id,
        "expires_at": expiry,
        "public_key_sha256": key.get("public_key_sha256"),
        "authorization_closure": closure,
        "provider_bindings_sha256": canonical_sha256(
            [key, account, group, memberships[0], permits[0], closure]
        ),
    }


def viewer_inventory_proof(policy: dict[str, Any]) -> dict[str, Any]:
    kubectl = executable(policy, "kubectl")
    completed = subprocess.run(
        [
            kubectl,
            "--kubeconfig",
            policy["handoff_kubeconfig"],
            "get",
            "pods",
            "--all-namespaces",
            "-o",
            "name",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ProviderError("handoff identity cannot inventory pods")
    names = sorted(line for line in completed.stdout.splitlines() if line)
    return {
        "allowed": True,
        "resource_count": len(names),
        "resource_names_sha256": canonical_sha256(names),
    }


def normalized_host_cidrs(values: Any) -> list[str]:
    if not isinstance(values, list) or not values:
        raise ProviderError("control-plane CIDR inventory is absent")
    try:
        networks = [ipaddress.ip_network(value, strict=True) for value in values]
    except (TypeError, ValueError) as error:
        raise ProviderError("control-plane CIDR inventory is invalid") from error
    collapsed = list(ipaddress.collapse_addresses(networks))
    if len(collapsed) != len(networks) or any(
        network.prefixlen != network.max_prefixlen for network in collapsed
    ):
        raise ProviderError("control-plane CIDRs are semantically broader than host egress")
    return sorted(network.with_prefixlen for network in collapsed)


def cluster_cidrs(policy: dict[str, Any]) -> list[str]:
    automation = policy["evidence_identity"]
    document = command_json(
        [
            executable(policy, "nebius"),
            "--config",
            automation["config_path"],
            "mk8s",
            "v1",
            "cluster",
            "get",
            "--profile",
            automation["profile"],
            "--id",
            policy["cluster_id"],
            "--format",
            "json",
        ],
        label="Nebius cluster CIDR inventory",
    )
    try:
        observed = document["spec"]["control_plane"]["endpoints"]["public_endpoint"]["allowed_cidrs"]
    except (KeyError, TypeError) as error:
        raise ProviderError("Nebius cluster omits control-plane CIDRs") from error
    normalized = normalized_host_cidrs(observed)
    approved = normalized_host_cidrs(policy["approved_control_plane_cidrs"])
    if normalized != approved:
        raise ProviderError("live control-plane CIDRs differ from the approved egress set")
    return normalized


def load_consumer_contracts(policy: dict[str, Any]) -> dict[str, Any]:
    contracts = stable_json(
        Path(policy["consumer_contracts_path"]), label="credential consumer contracts"
    )
    if (
        contracts.get("schema")
        != "fs2-serve.nebius.ai/credential-consumer-contracts/v1"
        or not isinstance(contracts.get("contracts"), dict)
        or not isinstance(contracts.get("pending_contract_ids"), list)
    ):
        raise ProviderError("credential consumer contracts are malformed")
    return contracts


def exact_requested_secret_bindings(
    parameters: dict[str, Any], secrets: list[dict[str, Any]]
) -> dict[str, Any]:
    supplied = parameters.get("credential_bindings")
    if not isinstance(supplied, dict) or not supplied:
        raise ProviderError("consumer readiness requires exact Secret bindings")
    by_identity = {
        (item["metadata"]["namespace"], item["metadata"]["name"]): item
        for item in secrets
    }
    verified: dict[str, Any] = {}
    for address, binding in supplied.items():
        if not isinstance(binding, dict):
            raise ProviderError("consumer Secret binding is malformed")
        live = by_identity.get((binding.get("namespace"), binding.get("name")))
        live_metadata = live.get("metadata") if isinstance(live, dict) else None
        if (
            not isinstance(live_metadata, dict)
            or live_metadata.get("uid") != binding.get("uid")
            or live_metadata.get("resourceVersion")
            != binding.get("resource_version")
            or live.get("authorityContentSha256") != binding.get("content_sha256")
            or live.get("authorityEvidenceId")
            != binding.get("authority_evidence_id")
            or binding.get("credential_class")
            != parameters.get("credential_class")
            or binding.get("generation") != str(parameters.get("generation"))
        ):
            raise ProviderError(
                f"consumer Secret binding is stale or belongs to another class: {address}"
            )
        verified[address] = binding
    if canonical_sha256(verified) != parameters.get("bindings_sha256"):
        raise ProviderError("consumer Secret binding digest differs after live comparison")
    return verified


def class_adapter_result(
    *,
    policy: dict[str, Any],
    operation: str,
    parameters: dict[str, Any],
    states: list[dict[str, Any]],
    secrets: list[dict[str, Any]],
    provider_inventory: dict[str, Any],
    evidence_identity: dict[str, Any],
    registry: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch to the exact class-specific, digest-pinned read-only adapter."""

    credential_class = parameters.get("credential_class")
    contracts = load_consumer_contracts(policy)
    pending = set(contracts["pending_contract_ids"])
    contract = contracts["contracts"].get(credential_class)
    configured = policy.get("class_adapters", {}).get(credential_class)
    if (
        not isinstance(credential_class, str)
        or credential_class in pending
        or not isinstance(contract, dict)
        or not isinstance(configured, dict)
        or set(configured) != {"operations", "adapter"}
        or operation not in configured.get("operations", [])
    ):
        raise ProviderError(
            f"{operation} has no accepted adapter for {credential_class}"
        )
    if operation == "consumer-readiness":
        exact_requested_secret_bindings(parameters, secrets)
    adapter = configured["adapter"]
    command = verified_adapter_command(
        adapter, label=f"{credential_class} {operation}"
    )
    request = {
        "schema": "fs2-serve.nebius.ai/credential-class-observation-request/v1",
        "operation": operation,
        "credential_class": credential_class,
        "parameters": parameters,
        "contract": contract,
        "contract_sha256": canonical_sha256(contract),
        "terraform_states": states,
        "kubernetes_secrets": secrets,
        "nebius_inventory": provider_inventory,
        "evidence_identity": evidence_identity,
        "registry_sha256": canonical_sha256(registry),
    }
    response = command_json_input(
        command,
        label=f"{credential_class} {operation}",
        payload=request,
        timeout=adapter["timeout_seconds"],
    )
    required = {
        "schema",
        "operation",
        "credential_class",
        "contract_sha256",
        "observed_at",
        "complete",
        "data_fields_returned",
        "result",
    }
    if (
        not isinstance(response, dict)
        or set(response) != required
        or response.get("schema")
        != "fs2-serve.nebius.ai/credential-class-observation/v1"
        or response.get("operation") != operation
        or response.get("credential_class") != credential_class
        or response.get("contract_sha256") != canonical_sha256(contract)
        or response.get("complete") is not True
        or response.get("data_fields_returned") != 0
        or not isinstance(response.get("result"), dict)
    ):
        raise ProviderError(
            f"{credential_class} adapter returned an incomplete observation"
        )
    return response


def backend_custody_result(
    policy: dict[str, Any], root_name: str
) -> dict[str, Any]:
    """Obtain provider-native encryption, logging and endpoint custody facts."""

    root = policy["terraform_roots"][root_name]
    config_path = Path(root["backend_config_path"])
    config_sha256 = root_private_file(
        config_path, label=f"{root_name} Terraform backend configuration"
    )
    adapter = policy["backend_custody_adapter"]
    command = verified_adapter_command(adapter, label="Terraform backend custody")
    request = {
        "schema": "fs2-serve.nebius.ai/backend-custody-request/v1",
        "terraform_root_name": root_name,
        "backend_type": root["backend_type"],
        "backend_config_path": str(config_path),
        "backend_config_sha256": config_sha256,
        "project_id": policy["project_id"],
        "expected_custody": root["backend_expectation"],
    }
    response = command_json_input(
        command,
        label=f"{root_name} backend custody",
        payload=request,
        timeout=adapter["timeout_seconds"],
    )
    fields = {
        "schema",
        "terraform_root_name",
        "backend_type",
        "backend_config_sha256",
        "bucket_id",
        "object_key",
        "endpoint",
        "encryption",
        "access_logging",
        "versioning_enabled",
        "object_lock_enabled",
        "object_lock_mode",
        "object_lock_retention_days",
        "bucket_parent_id",
        "bucket_project_id",
        "bucket_owner_service_account_id",
        "kms_key_parent_id",
        "kms_key_project_id",
        "logging_destination_parent_id",
        "logging_destination_project_id",
        "access_log_prefix",
        "observed_at",
        "complete",
        "data_fields_returned",
    }
    encryption = response.get("encryption") if isinstance(response, dict) else None
    logging = response.get("access_logging") if isinstance(response, dict) else None
    if (
        not isinstance(response, dict)
        or set(response) != fields
        or response.get("schema")
        != "fs2-serve.nebius.ai/backend-custody-observation/v1"
        or response.get("terraform_root_name") != root_name
        or response.get("backend_type") != root["backend_type"]
        or response.get("backend_config_sha256") != config_sha256
        or response.get("complete") is not True
        or response.get("data_fields_returned") != 0
        or not isinstance(encryption, dict)
        or encryption.get("enabled") is not True
        or not isinstance(encryption.get("kms_key_id"), str)
        or not encryption["kms_key_id"]
        or not isinstance(logging, dict)
        or logging.get("enabled") is not True
        or not isinstance(logging.get("destination_bucket_id"), str)
        or not logging["destination_bucket_id"]
        or response.get("versioning_enabled") is not True
        or response.get("object_lock_enabled") is not True
        or not isinstance(response.get("endpoint"), str)
        or not response["endpoint"].startswith("https://")
        or not isinstance(response.get("bucket_id"), str)
        or not response["bucket_id"]
        or not isinstance(response.get("object_key"), str)
        or not response["object_key"]
    ):
        raise ProviderError("Terraform backend custody is incomplete")
    expected = root["backend_expectation"]
    observed_binding = {
        "project_id": policy["project_id"],
        "bucket_id": response["bucket_id"],
        "bucket_parent_id": response["bucket_parent_id"],
        "bucket_project_id": response["bucket_project_id"],
        "bucket_owner_service_account_id": response[
            "bucket_owner_service_account_id"
        ],
        "object_key": response["object_key"],
        "endpoint": response["endpoint"],
        "kms_key_id": encryption["kms_key_id"],
        "kms_key_parent_id": response["kms_key_parent_id"],
        "kms_key_project_id": response["kms_key_project_id"],
        "logging_destination_bucket_id": logging["destination_bucket_id"],
        "logging_destination_parent_id": response[
            "logging_destination_parent_id"
        ],
        "logging_destination_project_id": response[
            "logging_destination_project_id"
        ],
        "access_log_prefix": response["access_log_prefix"],
        "object_lock_mode": response["object_lock_mode"],
        "object_lock_retention_days": response["object_lock_retention_days"],
    }
    if observed_binding != expected:
        raise ProviderError(
            "Terraform backend custody differs from the exact source-approved binding"
        )
    return response


def state_migration_readiness_result(
    policy: dict[str, Any], root_name: str
) -> dict[str, Any]:
    """Attest an additive legacy-state copy without moving, deleting or overwriting it."""

    root = policy["terraform_roots"][root_name]
    legacy = root["legacy_state_source"]
    legacy_path = Path(legacy["path"])
    digest = root_private_file(
        legacy_path,
        label=f"{root_name} quarantined legacy state",
        expected_sha256=legacy["sha256"],
    )
    legacy_state = stable_json(legacy_path, label=f"{root_name} quarantined legacy state")
    if (
        legacy_state.get("lineage") != legacy["lineage"]
        or legacy_state.get("serial") != legacy["serial"]
        or canonical_sha256(legacy_state) != legacy["canonical_state_sha256"]
    ):
        raise ProviderError("quarantined legacy state differs from the approved source")
    adapter = policy["state_migration_adapter"]
    command = verified_adapter_command(adapter, label="Terraform state copy custody")
    response = command_json_input(
        command,
        label=f"{root_name} Terraform state copy custody",
        payload={
            "schema": "fs2-serve.nebius.ai/state-copy-readiness-request/v1",
            "terraform_root_name": root_name,
            "source": {
                "sha256": digest,
                "canonical_state_sha256": legacy["canonical_state_sha256"],
                "lineage": legacy["lineage"],
                "serial": legacy["serial"],
            },
            "destination": root["backend_expectation"],
            "copy_semantics": "create-new-object-version-no-source-mutation",
        },
        timeout=adapter["timeout_seconds"],
    )
    fields = {
        "schema",
        "terraform_root_name",
        "source_sha256",
        "source_canonical_state_sha256",
        "source_lineage",
        "source_serial",
        "destination_binding_sha256",
        "destination_object_present",
        "destination_object_version_id",
        "destination_canonical_state_sha256",
        "source_retained",
        "overwrite_performed",
        "observed_at",
        "complete",
        "data_fields_returned",
    }
    expected_destination_sha256 = canonical_sha256(root["backend_expectation"])
    if (
        not isinstance(response, dict)
        or set(response) != fields
        or response.get("schema")
        != "fs2-serve.nebius.ai/state-copy-readiness/v1"
        or response.get("terraform_root_name") != root_name
        or response.get("source_sha256") != digest
        or response.get("source_canonical_state_sha256")
        != legacy["canonical_state_sha256"]
        or response.get("source_lineage") != legacy["lineage"]
        or response.get("source_serial") != legacy["serial"]
        or response.get("destination_binding_sha256")
        != expected_destination_sha256
        or response.get("source_retained") is not True
        or response.get("overwrite_performed") is not False
        or response.get("complete") is not True
        or response.get("data_fields_returned") != 0
    ):
        raise ProviderError("legacy-to-remote state copy evidence is incomplete")
    if response["destination_object_present"] is True:
        if (
            not isinstance(response.get("destination_object_version_id"), str)
            or not response["destination_object_version_id"]
            or response.get("destination_canonical_state_sha256")
            != legacy["canonical_state_sha256"]
        ):
            raise ProviderError("remote state copy is not byte-semantically verified")
        status = "copy-verified-source-retained"
    elif (
        response["destination_object_present"] is False
        and response.get("destination_object_version_id") is None
        and response.get("destination_canonical_state_sha256") is None
    ):
        status = "destination-empty-copy-pending"
    else:
        raise ProviderError("remote state destination has an ambiguous object state")
    return {**response, "status": status}


def operation_result(request: dict[str, Any]) -> dict[str, Any]:
    policy = request["policy"]
    operation = request["operation"]
    parameters = request["parameters"]
    if operation == "backend-custody":
        return backend_custody_result(
            policy, str(parameters["terraform_root_name"])
        )
    if operation == "state-migration-readiness":
        return state_migration_readiness_result(
            policy, str(parameters["terraform_root_name"])
        )
    if operation == "release-identity":
        provider_inventory = nebius_inventory(policy)
        return {
            "release_identity": release_identity_proof(policy, provider_inventory),
            "evidence_identity": evidence_identity_proof(policy, provider_inventory),
            "observed_at": observed_at(),
        }
    if operation == "operator-proxy-context":
        provider_inventory = nebius_inventory(policy)
        configured = policy["operator_identity"]
        lineage = exact_handoff_lineage(
            policy,
            provider_inventory,
            key_id=configured["credential_id"],
        )
        if (
            lineage["service_account_id"] != configured["service_account_id"]
            or lineage["project_id"] != policy["project_id"]
            or provider_time(lineage["expires_at"], label="operator viewer expiry")
            - datetime.now(UTC)
            > timedelta(seconds=configured["maximum_lifetime_seconds"])
        ):
            raise ProviderError("operator proxy identity differs from fixed viewer policy")
        kubeconfig = Path(configured["kubeconfig"])
        kubeconfig_sha256 = root_private_file(
            kubeconfig,
            label="operator viewer kubeconfig",
            expected_sha256=configured["kubeconfig_sha256"],
        )
        return {
            "operator_identity": lineage,
            "kubeconfig": str(kubeconfig),
            "kubeconfig_sha256": kubeconfig_sha256,
            "context_name": configured["context_name"],
            "provider_executables": policy["provider_executables"],
            "denials": authorization_denials(policy),
            "inventory": viewer_inventory_proof(policy),
            "allowed_cidrs": cluster_cidrs(policy),
            "observed_at": observed_at(),
        }
    states = terraform_inventory(policy)
    if operation == "artifact-inventory":
        return artifact_inventory(policy)
    secrets = kubernetes_secrets(policy)
    service_accounts = kubernetes_service_accounts(policy)
    helm_release_records = helm_release_secret_inventory(policy, secrets)
    provider_inventory = nebius_inventory(policy)
    evidence_identity = evidence_identity_proof(policy, provider_inventory)
    release_identity = release_lineage_inventory(policy, provider_inventory)
    operator_identity = exact_handoff_lineage(
        policy,
        provider_inventory,
        key_id=policy["operator_identity"]["credential_id"],
    )
    registry = load_registry(policy)
    registry_sha256 = canonical_sha256(registry)
    provider_reconciliation = reconcile_global_provider_inventory(
        states=states,
        secrets=secrets,
        service_accounts=service_accounts,
        helm_release_records=helm_release_records,
        provider_inventory=provider_inventory,
        registry=registry,
        evidence_identity=evidence_identity,
        release_identity=release_identity,
        operator_identity=operator_identity,
        approved_namespaces=policy["namespaces"],
    )
    if operation == "custody-snapshot":
        return {
            "registry_sha256": registry_sha256,
            "terraform_states": states,
            "kubernetes_secrets": secrets,
            "nebius_inventory": provider_inventory,
            "evidence_identity": evidence_identity,
            "provider_inventory_reconciliation": provider_reconciliation,
            "credential_inventory": credential_inventory(
                policy, states, provider_inventory
            ),
        }
    if operation == "credential-inventory":
        return {
            **credential_inventory(policy, states, provider_inventory),
            "registry_sha256": registry_sha256,
            "evidence_identity": evidence_identity,
            "provider_inventory_reconciliation": provider_reconciliation,
        }
    if operation == "planned-generation-admission":
        return plan_admission(
            policy, str(parameters["phase"]), secrets, registry
        )
    if operation == "viewer-handoff-inventory":
        if parameters["key_id"] != policy["operator_identity"]["credential_id"]:
            raise ProviderError(
                "handoff key is not the source-approved operator viewer credential"
            )
        lineage = exact_handoff_lineage(
            policy, provider_inventory, key_id=str(parameters["key_id"])
        )
        provider_handoff_id = canonical_sha256(lineage)
        return {
            "handoff_id": provider_handoff_id,
            "key_id": parameters["key_id"],
            "provider_lineage": lineage,
            "denials": authorization_denials(policy),
            "inventory": viewer_inventory_proof(policy),
            "allowed_cidrs": cluster_cidrs(policy),
            "observed_at": observed_at(),
        }
    if operation in {
        "consumer-readiness",
        "rotation-readiness",
        "ciphertext-migration",
        "authentication-continuity",
    }:
        return class_adapter_result(
            policy=policy,
            operation=operation,
            parameters=parameters,
            states=states,
            secrets=secrets,
            provider_inventory=provider_inventory,
            evidence_identity=evidence_identity,
            registry=registry,
        )
    raise ProviderError(
        f"unsupported read-only credential observation: {operation}"
    )


def main() -> int:
    request = json.loads(sys.stdin.read())
    required = {
        "schema",
        "operation",
        "request_nonce",
        "parameters",
        "policy",
        "policy_sha256",
    }
    if (
        not isinstance(request, dict)
        or set(request) != required
        or request.get("schema")
        != "fs2-serve.nebius.ai/credential-provider-read/v2"
        or not isinstance(request.get("parameters"), dict)
        or not isinstance(request.get("policy"), dict)
        or request.get("policy_sha256") != canonical_sha256(request["policy"])
    ):
        raise ProviderError("credential provider request is malformed")
    result = operation_result(request)
    response = {
        "schema": "fs2-serve.nebius.ai/credential-provider-observation/v2",
        "operation": request["operation"],
        "policy_sha256": request["policy_sha256"],
        "observed_at": observed_at(),
        "complete": True,
        "data_fields_returned": 0,
        "result": result,
    }
    print(json.dumps(response, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ProviderError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
