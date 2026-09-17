#!/usr/bin/env python3
"""Root-owned, read-only credential evidence authority.

The service owns the provider adapter selection.  Clients can select only a
reviewed read operation and its bounded parameters; they cannot select a
binary, profile, project, kubeconfig, state root, or evidence file.  Provider
commands and every command file are pinned by SHA-256 in a root-owned mode-0600
configuration.  Responses are request-bound over a root-owned Unix socket and
every completed request is recorded in an append-only hash chain.

This service never creates, updates, disables, revokes, replaces, or deletes a
credential or infrastructure object.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import socket
import stat
import struct
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from append_only_evidence import append_event, load_stream

CONFIG_PATH = Path("/etc/fs2-credential-authority/config.json")
SOCKET_PATH = Path("/run/fs2-credential-authority/v1.sock")
AUDIT_ROOT = Path("/var/lib/fs2-credential-authority/audit")
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
AUTHORITATIVE_ARTIFACT_ROOTS = {
    "operator-receipts": "/srv/fs2-credential-custody/operator-receipts",
    "secure-handoff": "/srv/fs2-credential-custody/secure-handoff",
    "task-run-roots": "/srv/fs2-credential-custody/task-run-roots",
    "terraform-workdirs": "/srv/fs2-credential-custody/terraform-workdirs",
}
READ_ONLY_OPERATIONS = frozenset(
    {
        "custody-snapshot",
        "planned-generation-admission",
        "artifact-inventory",
        "consumer-readiness",
        "credential-inventory",
        "rotation-readiness",
        "viewer-handoff-inventory",
        "ciphertext-migration",
        "authentication-continuity",
        "release-identity",
        "backend-custody",
    }
)
CLIENT_FIELDS: dict[str, frozenset[str]] = {
    "custody-snapshot": frozenset(),
    "planned-generation-admission": frozenset({"phase"}),
    "artifact-inventory": frozenset(),
    "consumer-readiness": frozenset(
        {
            "credential_class",
            "generation",
            "phase",
            "bindings_sha256",
            "credential_bindings",
        }
    ),
    "credential-inventory": frozenset(),
    "rotation-readiness": frozenset(
        {"credential_class", "predecessor_id", "successor_id"}
    ),
    "viewer-handoff-inventory": frozenset({"key_id"}),
    "ciphertext-migration": frozenset({"credential_class", "from", "to"}),
    "authentication-continuity": frozenset(
        {"credential_class", "predecessor_id", "successor_id"}
    ),
    "release-identity": frozenset(),
    "backend-custody": frozenset({"terraform_root_name"}),
}
FORBIDDEN_CLIENT_FIELDS = frozenset(
    {
        "command",
        "configuration",
        "evidence",
        "executable",
        "kubeconfig",
        "path",
        "profile",
        "project_id",
        "provider",
        "root",
        "state",
        "terraform_root",
    }
)


class AuthorityServiceError(RuntimeError):
    pass


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise AuthorityServiceError(
                f"authority command file is not regular: {path}"
            )
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def root_private_file(path: Path, *, label: str) -> os.stat_result:
    if path.is_symlink() or not path.is_file():
        raise AuthorityServiceError(f"{label} must be a regular file")
    metadata = path.stat()
    if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise AuthorityServiceError(f"{label} must be root-owned and mode 0600")
    return metadata


def _validate_adapter(adapter: Any, *, label: str) -> None:
    if (
        not isinstance(adapter, dict)
        or set(adapter) != {"command", "file_sha256", "timeout_seconds"}
        or not isinstance(adapter.get("command"), list)
        or not 1 <= len(adapter["command"]) <= 2
        or not all(isinstance(value, str) and value for value in adapter["command"])
        or not all(Path(value).is_absolute() for value in adapter["command"])
        or any(value in {"-c", "-m"} or value.startswith("-") for value in adapter["command"])
        or not isinstance(adapter.get("file_sha256"), dict)
        or not isinstance(adapter.get("timeout_seconds"), int)
        or not 1 <= adapter["timeout_seconds"] <= 120
    ):
        raise AuthorityServiceError(f"authority adapter is malformed: {label}")
    resolved_command = [str(Path(value).resolve()) for value in adapter["command"]]
    if len(set(resolved_command)) != len(resolved_command):
        raise AuthorityServiceError(f"authority adapter repeats a command file: {label}")
    if set(adapter["file_sha256"]) != set(resolved_command):
        raise AuthorityServiceError(
            f"every authority executable and script must be digest-pinned: {label}"
        )
    for resolved in resolved_command:
        command_file = Path(resolved)
        if command_file.is_symlink() or not command_file.is_file():
            raise AuthorityServiceError(f"authority command must be a real file: {label}")
        metadata = command_file.stat()
        expected = adapter["file_sha256"].get(resolved)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or not isinstance(expected, str)
            or len(expected) != 64
            or file_sha256(command_file) != expected
        ):
            raise AuthorityServiceError(
                f"authority command identity differs from root policy: {label}"
            )


def _validate_policy(policy: Any) -> None:
    required = {
        "schema",
        "project_id",
        "evidence_identity",
        "release_identity",
        "cluster_id",
        "kubeconfig",
        "handoff_kubeconfig",
        "namespaces",
        "approved_control_plane_cidrs",
        "terraform_roots",
        "artifact_inventory_scopes",
        "credential_registry_path",
        "consumer_contracts_path",
        "provider_executables",
        "class_adapters",
        "backend_custody_adapter",
    }
    if (
        not isinstance(policy, dict)
        or set(policy) != required
        or policy.get("schema")
        != "fs2-serve.nebius.ai/credential-authority-policy/v2"
        or not all(
            isinstance(policy.get(field), str) and policy[field]
            for field in ("project_id", "cluster_id")
        )
        or not isinstance(policy.get("namespaces"), list)
        or not policy["namespaces"]
        or not all(isinstance(item, str) and item for item in policy["namespaces"])
        or len(policy["namespaces"]) != len(set(policy["namespaces"]))
    ):
        raise AuthorityServiceError("authority production policy is incomplete")
    automation = policy.get("evidence_identity")
    if (
        not isinstance(automation, dict)
        or set(automation)
        != {
            "config_path",
            "profile",
            "service_account_id",
            "credential_kind",
            "credential_id",
            "expires_at",
            "interactive_login_allowed",
        }
        or automation.get("interactive_login_allowed") is not False
        or not all(
            isinstance(automation.get(field), str) and automation[field]
            for field in (
                "config_path",
                "profile",
                "service_account_id",
                "credential_kind",
                "credential_id",
                "expires_at",
            )
        )
        or automation.get("credential_kind")
        not in {"access_keys", "auth_public_keys"}
    ):
        raise AuthorityServiceError("read-only evidence identity is incomplete")
    config_path = Path(automation["config_path"])
    if not config_path.is_absolute():
        raise AuthorityServiceError("read-only evidence config path must be absolute")
    try:
        automation_expiry = datetime.fromisoformat(
            automation["expires_at"].replace("Z", "+00:00")
        ).astimezone(UTC)
    except ValueError as error:
        raise AuthorityServiceError("read-only evidence identity expiry is invalid") from error
    remaining = automation_expiry - datetime.now(UTC)
    if remaining <= timedelta(0) or remaining > timedelta(hours=24):
        raise AuthorityServiceError(
            "read-only evidence identity must have a provider-enforced expiry within 24 hours"
        )
    release = policy.get("release_identity")
    release_fields = {
        "config_path",
        "profile",
        "project_id",
        "service_account_id",
        "credential_kind",
        "credential_id",
        "issued_at",
        "expires_at",
        "audience",
        "allowed_roles",
        "allowed_commands",
        "interactive_login_allowed",
        "human_principal_allowed",
    }
    if (
        not isinstance(release, dict)
        or set(release) != release_fields
        or release.get("project_id") != policy["project_id"]
        or release.get("audience") != "terraform-release"
        or release.get("interactive_login_allowed") is not False
        or release.get("human_principal_allowed") is not False
        or release.get("credential_kind")
        not in {"access_keys", "auth_public_keys"}
        or not all(
            isinstance(release.get(field), str) and release[field]
            for field in release_fields
            - {
                "allowed_roles",
                "allowed_commands",
                "interactive_login_allowed",
                "human_principal_allowed",
            }
        )
        or not isinstance(release.get("allowed_roles"), list)
        or not release["allowed_roles"]
        or not set(release["allowed_roles"]) <= {"editor", "admin"}
        or not isinstance(release.get("allowed_commands"), list)
        or set(release["allowed_commands"]) != {"preflight", "plan", "apply"}
    ):
        raise AuthorityServiceError("automation-only release identity is incomplete")
    release_path = Path(release["config_path"])
    if not release_path.is_absolute():
        raise AuthorityServiceError("release identity config path must be absolute")
    try:
        release_issued = datetime.fromisoformat(
            release["issued_at"].replace("Z", "+00:00")
        ).astimezone(UTC)
        release_expiry = datetime.fromisoformat(
            release["expires_at"].replace("Z", "+00:00")
        ).astimezone(UTC)
    except ValueError as error:
        raise AuthorityServiceError("release identity lifetime is invalid") from error
    now = datetime.now(UTC)
    if (
        release_issued > now
        or release_expiry <= now
        or release_expiry - release_issued > timedelta(hours=1)
    ):
        raise AuthorityServiceError(
            "release identity must have a provider-enforced lifetime of at most one hour"
        )
    cidrs = policy.get("approved_control_plane_cidrs")
    if not isinstance(cidrs, list) or not cidrs or len(cidrs) != len(set(cidrs)):
        raise AuthorityServiceError("authority approved CIDR set is incomplete")
    try:
        networks = [ipaddress.ip_network(value, strict=True) for value in cidrs]
    except (TypeError, ValueError) as error:
        raise AuthorityServiceError("authority approved CIDR set is invalid") from error
    collapsed = list(ipaddress.collapse_addresses(networks))
    if len(collapsed) != len(networks) or any(
        network.prefixlen != network.max_prefixlen for network in collapsed
    ):
        raise AuthorityServiceError("authority approved CIDRs are semantically overbroad")
    for field in (
        "kubeconfig",
        "handoff_kubeconfig",
        "credential_registry_path",
        "consumer_contracts_path",
    ):
        candidate = Path(policy[field])
        if not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_file():
            raise AuthorityServiceError(f"authority policy {field} must be a real absolute file")
        metadata = candidate.stat()
        if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
            raise AuthorityServiceError(
                f"authority policy {field} must be root-owned and immutable to clients"
            )
    roots = policy.get("terraform_roots")
    required_roots = {
        "configuration",
        "foundation",
        "infrastructure",
        "model-artifacts",
        "reference-data",
        "workloads",
    }
    if not isinstance(roots, dict) or set(roots) != required_roots:
        raise AuthorityServiceError("authority must own every Terraform state root")
    for name, root in roots.items():
        if (
            not isinstance(root, dict)
            or set(root)
            != {
                "configuration_dir",
                "backend_type",
                "backend_config_path",
                "terraform_data_dir",
                "workspace",
                "saved_plan_paths",
                "lineage_id",
            }
            or not isinstance(root.get("configuration_dir"), str)
            or not Path(root["configuration_dir"]).is_absolute()
            or root.get("backend_type") not in {"remote", "s3"}
            or not isinstance(root.get("backend_config_path"), str)
            or not Path(root["backend_config_path"]).is_absolute()
            or not isinstance(root.get("terraform_data_dir"), str)
            or not Path(root["terraform_data_dir"]).is_absolute()
            or not isinstance(root.get("workspace"), str)
            or not root["workspace"]
            or not isinstance(root.get("saved_plan_paths"), list)
            or not root["saved_plan_paths"]
            or not all(
                isinstance(value, str) and Path(value).is_absolute()
                for value in root["saved_plan_paths"]
            )
            or len(root["saved_plan_paths"]) != len(set(root["saved_plan_paths"]))
            or not isinstance(root.get("lineage_id"), str)
            or not root["lineage_id"]
        ):
            raise AuthorityServiceError(f"authority Terraform root is malformed: {name}")
        backend_config = Path(root["backend_config_path"])
        terraform_data_dir = Path(root["terraform_data_dir"])
        if (
            backend_config.is_symlink()
            or not backend_config.is_file()
            or backend_config.stat().st_uid != 0
            or stat.S_IMODE(backend_config.stat().st_mode) != 0o600
        ):
            raise AuthorityServiceError(
                f"authority backend config must be root-owned mode 0600: {name}"
            )
        if (
            terraform_data_dir.is_symlink()
            or not terraform_data_dir.is_dir()
            or terraform_data_dir.stat().st_uid != 0
            or stat.S_IMODE(terraform_data_dir.stat().st_mode) & 0o077
        ):
            raise AuthorityServiceError(
                f"authority Terraform data directory must be root-only: {name}"
            )
        configuration = Path(root["configuration_dir"])
        if configuration.is_symlink() or not configuration.is_dir():
            raise AuthorityServiceError(
                f"authority Terraform root must be a real directory: {name}"
            )
        metadata = configuration.stat()
        if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
            raise AuthorityServiceError(
                f"authority Terraform root must be root-owned and immutable: {name}"
            )
        for saved_plan_value in root["saved_plan_paths"]:
            saved_plan = Path(saved_plan_value)
            if saved_plan.is_symlink() or not saved_plan.is_file():
                raise AuthorityServiceError(
                    f"authority saved plan must be a real file: {name}"
                )
            saved_metadata = saved_plan.stat()
            if (
                saved_metadata.st_uid != 0
                or stat.S_IMODE(saved_metadata.st_mode) != 0o600
            ):
                raise AuthorityServiceError(
                    f"authority saved plan must be root-owned mode 0600: {name}"
                )
    executables = policy.get("provider_executables")
    if not isinstance(executables, dict) or set(executables) != {
        "kubectl",
        "nebius",
        "terraform",
    }:
        raise AuthorityServiceError("authority provider executable registry is incomplete")
    for name, executable in executables.items():
        if (
            not isinstance(executable, dict)
            or set(executable) != {"path", "sha256"}
            or not isinstance(executable.get("path"), str)
            or not Path(executable["path"]).is_absolute()
            or not isinstance(executable.get("sha256"), str)
            or len(executable["sha256"]) != 64
        ):
            raise AuthorityServiceError(f"authority provider executable is malformed: {name}")
    contracts = json.loads(Path(policy["consumer_contracts_path"]).read_text(encoding="utf-8"))
    pending = set(contracts.get("pending_contract_ids", []))
    contract_ids = set(contracts.get("contracts", {})) - pending
    class_adapters = policy.get("class_adapters")
    if not isinstance(class_adapters, dict) or set(class_adapters) != contract_ids:
        raise AuthorityServiceError(
            "authority must configure one pinned adapter for every active credential class"
        )
    allowed_adapter_operations = {
        "consumer-readiness",
        "rotation-readiness",
        "ciphertext-migration",
        "authentication-continuity",
    }
    for credential_class, class_adapter in class_adapters.items():
        if (
            not isinstance(class_adapter, dict)
            or set(class_adapter) != {"operations", "adapter"}
            or not isinstance(class_adapter.get("operations"), list)
            or not class_adapter["operations"]
            or len(class_adapter["operations"])
            != len(set(class_adapter["operations"]))
            or not set(class_adapter["operations"]) <= allowed_adapter_operations
        ):
            raise AuthorityServiceError(
                f"credential class adapter operations are malformed: {credential_class}"
            )
        _validate_adapter(
            class_adapter.get("adapter"),
            label=f"credential class {credential_class}",
        )
    _validate_adapter(
        policy.get("backend_custody_adapter"), label="Terraform backend custody"
    )
    scopes = policy.get("artifact_inventory_scopes")
    if not isinstance(scopes, list) or not scopes:
        raise AuthorityServiceError("authority artifact inventory is absent")
    scope_roots: set[str] = set()
    for scope in scopes:
        if (
            not isinstance(scope, dict)
            or set(scope)
            != {
                "id",
                "category",
                "root",
                "owner",
                "purpose",
                "expires_at",
                "readers",
            }
            or not all(
                isinstance(scope.get(field), str) and scope[field]
                for field in (
                    "id",
                    "category",
                    "root",
                    "owner",
                    "purpose",
                    "expires_at",
                )
            )
            or not Path(scope["root"]).is_absolute()
            or not isinstance(scope.get("readers"), list)
            or not scope["readers"]
            or scope["root"] in scope_roots
        ):
            raise AuthorityServiceError("authority artifact inventory scope is malformed")
        expiry = datetime.fromisoformat(scope["expires_at"].replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            raise AuthorityServiceError("artifact inventory expiry needs a timezone")
        scope_roots.add(scope["root"])
    if (
        len(scopes) != len(AUTHORITATIVE_ARTIFACT_ROOTS)
        or {
            scope["category"]: scope["root"]
            for scope in scopes
        }
        != AUTHORITATIVE_ARTIFACT_ROOTS
    ):
        raise AuthorityServiceError(
            "authority artifact scopes must equal every source-owned custody mount"
        )


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    root_private_file(path, label="authority configuration")
    document = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "schema",
        "allowed_client_uids",
        "operations",
        "configuration_id",
        "policy",
        "evidence_signer",
        "evidence_producer_key_id",
        "external_anchor",
    }
    if (
        not isinstance(document, dict)
        or set(document) != required
        or document.get("schema")
        != "fs2-serve.nebius.ai/credential-authority-config/v2"
        or not isinstance(document.get("configuration_id"), str)
        or not document["configuration_id"]
        or not isinstance(document.get("allowed_client_uids"), list)
        or len(document["allowed_client_uids"]) != 1
        or not isinstance(document["allowed_client_uids"][0], int)
        or document["allowed_client_uids"][0] < 1
        or not isinstance(document.get("operations"), dict)
        or set(document["operations"]) != READ_ONLY_OPERATIONS
        or not isinstance(document.get("evidence_producer_key_id"), str)
        or not document["evidence_producer_key_id"]
    ):
        raise AuthorityServiceError("authority configuration is incomplete")
    _validate_policy(document["policy"])
    for operation, adapter in document["operations"].items():
        _validate_adapter(adapter, label=operation)
    _validate_adapter(document["evidence_signer"], label="evidence signer")
    _validate_adapter(document["external_anchor"], label="external anchor")
    return document


def recv_exact(connection: socket.socket, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise AuthorityServiceError("client closed an incomplete request")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def receive_request(connection: socket.socket) -> tuple[int, dict[str, Any]]:
    if not hasattr(socket, "SO_PEERCRED"):
        raise AuthorityServiceError("kernel peer credentials are unavailable")
    _pid, uid, _gid = struct.unpack(
        "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    )
    size = struct.unpack("!I", recv_exact(connection, 4))[0]
    if not 1 <= size <= MAX_MESSAGE_BYTES:
        raise AuthorityServiceError("authority request size is invalid")
    try:
        envelope = json.loads(recv_exact(connection, size))
    except json.JSONDecodeError as error:
        raise AuthorityServiceError("authority request is invalid JSON") from error
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"schema", "nonce", "request_sha256", "request"}
        or envelope.get("schema")
        != "fs2-serve.nebius.ai/credential-authority-request/v1"
        or not isinstance(envelope.get("nonce"), str)
        or not envelope["nonce"]
        or not isinstance(envelope.get("request"), dict)
        or envelope.get("request_sha256") != canonical_sha256(envelope["request"])
        or envelope["request"].get("operation") not in READ_ONLY_OPERATIONS
        or envelope["request"].get("request_nonce") != envelope["nonce"]
    ):
        raise AuthorityServiceError("authority request binding is invalid")
    return uid, envelope


def normalized_parameters(request: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Validate the complete client schema and return only bounded parameters."""

    operation = request["operation"]
    allowed = CLIENT_FIELDS[operation]
    expected = {"operation", "request_nonce", *allowed}
    if set(request) != expected or FORBIDDEN_CLIENT_FIELDS.intersection(request):
        raise AuthorityServiceError("credential authority request schema is invalid")
    parameters = {field: request[field] for field in sorted(allowed)}
    scalar_parameters = {
        key: value for key, value in parameters.items() if key != "credential_bindings"
    }
    if any(not isinstance(value, (str, int)) for value in scalar_parameters.values()):
        raise AuthorityServiceError("credential authority parameters must be scalar")
    if any(isinstance(value, str) and (not value or len(value) > 256) for value in scalar_parameters.values()):
        raise AuthorityServiceError("credential authority parameter is invalid")
    if "generation" in parameters and (
        not isinstance(parameters["generation"], int)
        or parameters["generation"] < 1
    ):
        raise AuthorityServiceError("credential generation is invalid")
    if operation == "planned-generation-admission" and parameters["phase"] not in {
        "secret-stage",
        "consumer-rollout",
    }:
        raise AuthorityServiceError("credential admission phase is invalid")
    if operation == "consumer-readiness" and parameters["phase"] not in {
        "predecessor-ready",
        "dual-read-ready",
        "current-write-ready",
    }:
        raise AuthorityServiceError("consumer readiness phase is invalid")
    if operation == "consumer-readiness":
        bindings = parameters.get("credential_bindings")
        binding_fields = {
            "namespace",
            "name",
            "uid",
            "resource_version",
            "content_sha256",
            "authority_evidence_id",
            "authority_observed_at",
            "credential_class",
            "generation",
            "immutable",
        }
        if (
            not isinstance(bindings, dict)
            or not bindings
            or len(bindings) > 256
            or not isinstance(parameters.get("bindings_sha256"), str)
            or len(parameters["bindings_sha256"]) != 64
            or canonical_sha256(bindings) != parameters["bindings_sha256"]
        ):
            raise AuthorityServiceError("consumer readiness bindings are incomplete")
        for address, binding in bindings.items():
            if (
                not isinstance(address, str)
                or not address.startswith("kubernetes_secret_v1.")
                or not isinstance(binding, dict)
                or set(binding) != binding_fields
                or not all(isinstance(value, str) and value for value in binding.values())
                or len(binding["content_sha256"]) != 64
                or binding["immutable"] not in {"true", "false"}
            ):
                raise AuthorityServiceError(
                    "consumer readiness binding identity is malformed"
                )
    registry = json.loads(
        Path(config["policy"]["credential_registry_path"]).read_text(encoding="utf-8")
    )
    classes = {
        item["id"]
        for item in registry.get("credentials", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if "credential_class" in parameters and parameters["credential_class"] not in classes:
        raise AuthorityServiceError("credential class is not registered")
    if operation == "ciphertext-migration" and parameters["credential_class"] not in {
        "payload-keyring",
        "customer-storage-cipher-keyring",
        "customer-storage-name-keyring",
        "ledger-keyring",
        "pat-pepper-keyring",
        "route-attestors",
    }:
        raise AuthorityServiceError("credential class has no ciphertext migration contract")
    if operation == "authentication-continuity" and parameters["credential_class"] not in {
        "admin-token",
        "pat-bootstrap",
        "pat-scientific",
        "pat-website",
    }:
        raise AuthorityServiceError("credential class has no authentication continuity contract")
    if operation == "backend-custody" and parameters["terraform_root_name"] not in config[
        "policy"
    ]["terraform_roots"]:
        raise AuthorityServiceError("Terraform backend custody root is not registered")
    return parameters


def contains_forbidden_value_field(value: Any) -> bool:
    forbidden = {
        "data",
        "stringData",
        "secret_value",
        "credential_value",
        "private_key",
        "token",
        "password",
    }
    if isinstance(value, dict):
        return bool(forbidden.intersection(value)) or any(
            contains_forbidden_value_field(item) for item in value.values()
        )
    if isinstance(value, list):
        return any(contains_forbidden_value_field(item) for item in value)
    return False


def adapter_call(adapter: dict[str, Any], request: dict[str, Any], *, label: str) -> dict[str, Any]:
    completed = subprocess.run(
        adapter["command"],
        input=json.dumps(request, sort_keys=True, separators=(",", ":")),
        text=True,
        capture_output=True,
        check=True,
        timeout=adapter["timeout_seconds"],
        env={
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
        },
    )
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise AuthorityServiceError(f"{label} returned invalid JSON") from error
    if not isinstance(response, dict):
        raise AuthorityServiceError(f"{label} returned malformed JSON")
    return response


def provider_call(
    config: dict[str, Any], request: dict[str, Any], *, parameters: dict[str, Any]
) -> dict[str, Any]:
    operation = request["operation"]
    policy = config["policy"]
    provider_request = {
        "schema": "fs2-serve.nebius.ai/credential-provider-read/v2",
        "operation": operation,
        "request_nonce": request["request_nonce"],
        "parameters": parameters,
        "policy": policy,
        "policy_sha256": canonical_sha256(policy),
    }
    payload = adapter_call(
        config["operations"][operation],
        provider_request,
        label=f"{operation} provider",
    )
    if contains_forbidden_value_field(payload):
        raise AuthorityServiceError("authority provider returned forbidden values")
    required = {
        "schema",
        "operation",
        "policy_sha256",
        "observed_at",
        "complete",
        "data_fields_returned",
        "result",
    }
    if (
        set(payload) != required
        or payload.get("schema")
        != "fs2-serve.nebius.ai/credential-provider-observation/v2"
        or payload.get("operation") != operation
        or payload.get("policy_sha256") != canonical_sha256(policy)
        or payload.get("complete") is not True
        or payload.get("data_fields_returned") != 0
        or not isinstance(payload.get("result"), dict)
    ):
        raise AuthorityServiceError("authority provider did not return a complete observation")
    return payload


def externally_anchored_response(
    *,
    config: dict[str, Any],
    request: dict[str, Any],
    request_sha256: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    now = datetime.now(UTC).replace(microsecond=0)
    evidence_id = str(__import__("uuid").uuid4())
    policy_sha256 = canonical_sha256(config["policy"])
    reservation = adapter_call(
        config["external_anchor"],
        {
            "schema": "fs2-serve.nebius.ai/external-evidence-anchor-request/v1",
            "action": "reserve",
            "evidence_id": evidence_id,
            "operation": request["operation"],
            "request_sha256": request_sha256,
            "payload_sha256": canonical_sha256(payload),
            "policy_sha256": policy_sha256,
        },
        label="external evidence anchor reservation",
    )
    if (
        set(reservation)
        != {
            "schema",
            "reservation_id",
            "log_id",
            "endpoint",
            "entry_index",
            "sequence",
            "prior_checkpoint_sha256",
            "expires_at",
        }
        or reservation.get("schema")
        != "fs2-serve.nebius.ai/external-evidence-reservation/v2"
        or not all(
            isinstance(reservation.get(field), str) and reservation[field]
            for field in (
                "reservation_id",
                "log_id",
                "endpoint",
                "prior_checkpoint_sha256",
                "expires_at",
            )
        )
        or not isinstance(reservation.get("entry_index"), int)
        or reservation["entry_index"] < 0
        or reservation.get("sequence") != reservation["entry_index"] + 1
    ):
        raise AuthorityServiceError("external evidence reservation is malformed")
    claim = {
        "schema": "fs2-serve.nebius.ai/credential-evidence-claim/v1",
        "evidence_id": evidence_id,
        "operation": request["operation"],
        "request_sha256": request_sha256,
        "request_nonce": request["request_nonce"],
        "payload_sha256": canonical_sha256(payload),
        "policy_sha256": policy_sha256,
        "producer_key_id": config["evidence_producer_key_id"],
        "sequence": reservation["sequence"],
        "observed_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
    }
    signer = adapter_call(
        config["evidence_signer"],
        {
            "schema": "fs2-serve.nebius.ai/credential-evidence-sign-request/v1",
            "claim": claim,
        },
        label="credential evidence signer",
    )
    if (
        set(signer) != {"schema", "producer_key_id", "signature"}
        or signer.get("schema")
        != "fs2-serve.nebius.ai/credential-evidence-signature/v1"
        or signer.get("producer_key_id") != claim["producer_key_id"]
        or not isinstance(signer.get("signature"), str)
        or not signer["signature"]
    ):
        raise AuthorityServiceError("credential evidence signer output is invalid")
    record = {
        "schema": "fs2-serve.nebius.ai/credential-evidence-record/v1",
        "claim_sha256": canonical_sha256(claim),
        "producer_signature": signer["signature"],
    }
    anchor = adapter_call(
        config["external_anchor"],
        {
            "schema": "fs2-serve.nebius.ai/external-evidence-anchor-request/v1",
            "action": "commit",
            "reservation": reservation,
            "record": record,
        },
        label="external evidence anchor commit",
    )
    anchor_fields = {
        "schema",
        "endpoint",
        "log_id",
        "entry_index",
        "claim_sha256",
        "record_sha256",
        "leaf_sha256",
        "anchored_at",
        "retention_until",
        "checkpoint",
        "inclusion_proof",
        "consistency_proof",
        "witnesses",
    }
    if (
        set(anchor) != anchor_fields
        or anchor.get("schema")
        != "fs2-serve.nebius.ai/external-evidence-anchor/v2"
        or anchor.get("claim_sha256") != record["claim_sha256"]
        or anchor.get("record_sha256") != canonical_sha256(record)
        or anchor.get("log_id") != reservation["log_id"]
        or anchor.get("endpoint") != reservation["endpoint"]
        or anchor.get("entry_index") != reservation["entry_index"]
        or not isinstance(anchor.get("checkpoint"), dict)
        or anchor["checkpoint"].get("previous_checkpoint_sha256")
        != reservation["prior_checkpoint_sha256"]
        or not isinstance(anchor.get("inclusion_proof"), list)
        or not isinstance(anchor.get("consistency_proof"), list)
        or not isinstance(anchor.get("witnesses"), list)
        or not all(
            isinstance(anchor.get(field), str) and anchor[field]
            for field in (
                "anchored_at",
                "retention_until",
                "leaf_sha256",
            )
        )
    ):
        raise AuthorityServiceError("external anchor does not bind the signed record")
    return {
        "schema": "fs2-serve.nebius.ai/externally-anchored-credential-evidence/v1",
        "claim": claim,
        "producer_signature": signer["signature"],
        "external_anchor": anchor,
        "payload": payload,
    }


def audit_event(
    *,
    config: dict[str, Any],
    uid: int,
    request: dict[str, Any],
    evidence: dict[str, Any],
) -> None:
    if AUDIT_ROOT.is_symlink():
        raise AuthorityServiceError("authority audit root must not be a symlink")
    AUDIT_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = AUDIT_ROOT.stat()
    if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise AuthorityServiceError("authority audit root must be root-only")
    # One lifetime stream prevents daily chain resets from hiding omission or
    # reordering.  The externally anchored evidence ID/checkpoint provides the
    # independent durability boundary for every local journal entry.
    stream = AUDIT_ROOT / "global"
    if not stream.exists():
        stream.mkdir(mode=0o700)
    event = {
        "schema": "fs2-serve.nebius.ai/credential-authority-audit/v2",
        "observed_at": datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "configuration_id": config["configuration_id"],
        "configuration_sha256": canonical_sha256(config),
        "client_uid": uid,
        "operation": request["operation"],
        "request_sha256": canonical_sha256(request),
        "evidence_id": evidence["claim"]["evidence_id"],
        "evidence_sha256": canonical_sha256(evidence),
        "external_log_id": evidence["external_anchor"]["log_id"],
        "external_checkpoint_sha256": canonical_sha256(
            evidence["external_anchor"]["checkpoint"]
        ),
        "external_entry_index": evidence["external_anchor"]["entry_index"],
    }
    append_event(
        stream,
        stream="credential-authority-audit",
        event=request["operation"],
        state=event,
    )
    load_stream(stream, stream="credential-authority-audit")


def handle(connection: socket.socket, config: dict[str, Any]) -> None:
    uid, envelope = receive_request(connection)
    if (
        len(config["allowed_client_uids"]) != 1
        or uid != config["allowed_client_uids"][0]
    ):
        raise AuthorityServiceError("client uid is not authorized by root policy")
    request = envelope["request"]
    parameters = normalized_parameters(request, config)
    payload = provider_call(config, request, parameters=parameters)
    response = externally_anchored_response(
        config=config,
        request=request,
        request_sha256=envelope["request_sha256"],
        payload=payload,
    )
    audit_event(config=config, uid=uid, request=request, evidence=response)
    encoded = json.dumps(response, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise AuthorityServiceError("authority response is too large")
    connection.sendall(struct.pack("!I", len(encoded)) + encoded)


def serve() -> None:
    if os.geteuid() != 0:
        raise AuthorityServiceError("credential authority must run as root")
    config = load_config()
    if (
        os.environ.get("LISTEN_PID") != str(os.getpid())
        or os.environ.get("LISTEN_FDS") != "1"
        or os.environ.get("LISTEN_FDNAMES") not in {None, "credential-authority"}
    ):
        raise AuthorityServiceError(
            "credential authority requires its systemd-owned activation socket"
        )
    # systemd owns the socket across process crashes/restarts, so this service
    # never unlinks or replaces a stale socket path. Descriptor 3 is the sole
    # LISTEN_FDS entry and remains the stable client endpoint.
    with socket.socket(fileno=3) as listener:
        if listener.family != socket.AF_UNIX or listener.type != socket.SOCK_STREAM:
            raise AuthorityServiceError("systemd passed an unexpected listener")
        while True:
            connection, _address = listener.accept()
            with connection:
                try:
                    handle(connection, config)
                except (
                    AuthorityServiceError,
                    KeyError,
                    OSError,
                    TypeError,
                    ValueError,
                    subprocess.SubprocessError,
                ):
                    # One malformed or unauthorized request must not terminate
                    # the authority.  Return a content-free error and continue.
                    encoded = json.dumps(
                        {
                            "schema": "fs2-serve.nebius.ai/credential-authority-error/v1",
                            "error": "request-rejected",
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                    try:
                        connection.sendall(struct.pack("!I", len(encoded)) + encoded)
                    except OSError:
                        pass


if __name__ == "__main__":
    try:
        serve()
    except (
        AuthorityServiceError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        print(f"error: {error}", file=os.sys.stderr)
        raise SystemExit(2) from error
