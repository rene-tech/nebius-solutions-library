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
from datetime import UTC, datetime
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
    }
    return {
        key: item
        for key, item in value.items()
        if key in allowed and isinstance(item, str) and item
    }


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
            result.append(
                {
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
            )
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
        state = command_json(
            [terraform, f"-chdir={configuration}", "state", "pull"],
            label=f"{root_name} canonical Terraform backend",
            environment={"TF_WORKSPACE": root["workspace"]},
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
        if (
            not isinstance(metadata, dict)
            or not isinstance(annotations, dict)
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
                    },
                },
                "authorityContentSha256": secret_commitment(item.get("data", {})),
                "authorityEvidenceId": canonical_sha256(
                    [metadata["uid"], metadata["resourceVersion"]]
                ),
                "authorityObservedAt": observed_at(),
                "immutable": item.get("immutable") is True,
            }
        )
    return sorted(result, key=lambda value: (value["metadata"]["namespace"], value["metadata"]["name"]))


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
    common = [
        "--profile",
        policy["profile"],
        "--parent-id",
        policy["project_id"],
        "--all",
        "--format",
        "json",
    ]
    binary = executable(policy, "nebius")
    commands = {
        "service_accounts": [binary, "iam", "service-account", "list", *common],
        "access_keys": [binary, "iam", "access-key", "list", *common],
        "auth_public_keys": [binary, "iam", "auth-public-key", "list", *common],
        "groups": [binary, "iam", "group", "list", *common],
        "group_memberships": [binary, "iam", "group-membership", "list", *common],
        "access_permits": [binary, "iam", "access-permit", "list", *common],
    }
    return {
        kind: normalize_nebius_items(
            command_json(command, label=f"Nebius {kind} inventory"), kind=kind
        )
        for kind, command in commands.items()
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
    return [
        item
        for item in registry["credentials"]
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
    policies = {item["id"]: item for item in registry["credentials"]}
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
    for policy_entry in registry["credentials"]:
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
        "role": "viewer",
        "key_id": key_id,
        "expires_at": expiry,
        "public_key_sha256": key.get("public_key_sha256"),
        "provider_bindings_sha256": canonical_sha256(
            [key, account, group, memberships[0], permits[0]]
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
    document = command_json(
        [
            executable(policy, "nebius"),
            "mk8s",
            "v1",
            "cluster",
            "get",
            "--profile",
            policy["profile"],
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


def operation_result(request: dict[str, Any]) -> dict[str, Any]:
    policy = request["policy"]
    operation = request["operation"]
    parameters = request["parameters"]
    states = terraform_inventory(policy)
    if operation == "artifact-inventory":
        return artifact_inventory(policy)
    secrets = kubernetes_secrets(policy)
    provider_inventory = nebius_inventory(policy)
    registry = load_registry(policy)
    registry_sha256 = canonical_sha256(registry)
    if operation == "custody-snapshot":
        return {
            "registry_sha256": registry_sha256,
            "terraform_states": states,
            "kubernetes_secrets": secrets,
            "nebius_inventory": provider_inventory,
            "credential_inventory": credential_inventory(
                policy, states, provider_inventory
            ),
        }
    if operation == "credential-inventory":
        return {
            **credential_inventory(policy, states, provider_inventory),
            "registry_sha256": registry_sha256,
        }
    if operation == "planned-generation-admission":
        return plan_admission(
            policy, str(parameters["phase"]), secrets, registry
        )
    if operation == "viewer-handoff-inventory":
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
    # These operations require class-specific accepted producers from SAI-06,
    # SAI-08, and SAI-09.  Failing here is intentional: a generic or locally
    # self-attested substitute must never authorize rotation or rollout.
    raise ProviderError(
        f"{operation} requires an accepted class-specific production adapter"
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
