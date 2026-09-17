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
        "operator-read-context",
        "operator-proxy-context",
        "scoped-credential-context",
        "backend-custody",
        "state-migration-readiness",
    }
)
CALLER_PURPOSES = frozenset(
    {
        "release-automation",
        "operator-read",
        "operator-proxy",
        "credential-delivery-general",
        "credential-delivery-scientific",
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
            "source_trust",
        }
    ),
    "credential-inventory": frozenset(),
    "rotation-readiness": frozenset(
        {
            "credential_class",
            "predecessor_id",
            "successor_id",
            "bindings_sha256",
            "credential_bindings",
            "source_trust",
        }
    ),
    "viewer-handoff-inventory": frozenset({"key_id"}),
    "ciphertext-migration": frozenset(
        {
            "credential_class",
            "from",
            "to",
            "bindings_sha256",
            "credential_bindings",
            "source_trust",
        }
    ),
    "authentication-continuity": frozenset(
        {
            "credential_class",
            "predecessor_id",
            "successor_id",
            "bindings_sha256",
            "credential_bindings",
            "source_trust",
        }
    ),
    "release-identity": frozenset(),
    "operator-read-context": frozenset(),
    "operator-proxy-context": frozenset(),
    "scoped-credential-context": frozenset({"credential_kind"}),
    "backend-custody": frozenset({"terraform_root_name"}),
    "state-migration-readiness": frozenset({"terraform_root_name"}),
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
    for parent in path.parents:
        parent_metadata = parent.stat()
        if (
            parent.is_symlink()
            or not parent.is_dir()
            or parent_metadata.st_uid != 0
            or stat.S_IMODE(parent_metadata.st_mode) & 0o022
        ):
            raise AuthorityServiceError(f"{label} parent chain is mutable")
    return metadata


def root_reader_file(
    path: Path, *, label: str, reader_gid: int
) -> os.stat_result:
    """Allow one kernel-authorized client group to read a root-custodied file."""

    if path.is_symlink() or not path.is_file():
        raise AuthorityServiceError(f"{label} must be a regular file")
    metadata = path.stat()
    if (
        metadata.st_uid != 0
        or metadata.st_gid != reader_gid
        or stat.S_IMODE(metadata.st_mode) != 0o640
    ):
        raise AuthorityServiceError(
            f"{label} must be root-owned, reader-group-owned and mode 0640"
        )
    for parent in path.parents:
        parent_metadata = parent.stat()
        if (
            parent.is_symlink()
            or not parent.is_dir()
            or parent_metadata.st_uid != 0
            or stat.S_IMODE(parent_metadata.st_mode) & 0o022
        ):
            raise AuthorityServiceError(f"{label} parent chain is mutable")
    return metadata


def process_cgroup(pid: int) -> str:
    """Return one exact unified-cgroup path for a kernel-authenticated peer."""

    try:
        lines = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise AuthorityServiceError("client cgroup identity is unavailable") from error
    unified = [line[3:] for line in lines if line.startswith("0::/")]
    if len(unified) != 1 or not unified[0].startswith("/"):
        raise AuthorityServiceError("client does not have one exact unified cgroup")
    return unified[0]


def authorize_operation_caller(
    *,
    config: dict[str, Any],
    operation: str,
    pid: int,
    uid: int,
    gid: int,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Bind an operation to a distinct UID/GID, cgroup and pinned client process."""

    allowed = config["operation_callers"][operation]
    matches = [
        (caller_id, config["caller_identities"][caller_id])
        for caller_id in allowed
        if config["caller_identities"][caller_id]["uid"] == uid
        and config["caller_identities"][caller_id]["gid"] == gid
    ]
    if len(matches) != 1:
        raise AuthorityServiceError(
            "client uid/gid is not authorized for this authority operation"
        )
    caller_id, caller = matches[0]
    if process_cgroup(pid) != caller["cgroup_path"]:
        raise AuthorityServiceError("client cgroup is not authorized for this operation")
    try:
        executable_path = Path(os.readlink(f"/proc/{pid}/exe")).resolve()
        command = [
            value.decode("utf-8")
            for value in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            if value
        ]
    except (OSError, UnicodeDecodeError) as error:
        raise AuthorityServiceError("client process identity is unavailable") from error
    expected_executable = Path(caller["executable_path"]).resolve()
    expected_script = Path(caller["client_script_path"])
    if (
        executable_path != expected_executable
        or file_sha256(executable_path) != caller["executable_sha256"]
        or len(command) != 2
        or Path(command[1]) != expected_script
        or file_sha256(expected_script) != caller["client_script_sha256"]
    ):
        raise AuthorityServiceError(
            "client executable/script identity is not authorized for this operation"
        )
    if operation == "scoped-credential-context":
        expected_kind = {
            "credential-delivery-general": "general-access",
            "credential-delivery-scientific": "scientific-access",
        }.get(caller_id)
        if request.get("credential_kind") != expected_kind:
            raise AuthorityServiceError(
                "credential delivery caller is not authorized for this credential kind"
            )
    return {
        "id": caller_id,
        "purpose": caller["purpose"],
        "uid": uid,
        "gid": gid,
        "pid": pid,
        "cgroup_path": caller["cgroup_path"],
        "executable_sha256": caller["executable_sha256"],
        "client_script_sha256": caller["client_script_sha256"],
    }


def _validate_scoped_kubernetes_identity(
    identity: Any,
    *,
    label: str,
    reader_uid: int,
    reader_gid: int,
    audience: str,
    allowed_permissions: set[str] | None = None,
    maximum_lifetime_seconds: int,
    delivery: bool = False,
) -> None:
    """Validate one root-custodied projected-token identity contract."""

    fields = {
        "service_account_id",
        "namespace",
        "name",
        "uid",
        "service_account_resource_version",
        "credential_id",
        "issuer",
        "audience",
        "maximum_lifetime_seconds",
        "allowed_permissions",
        "denied_permissions",
        "kubeconfig",
        "kubeconfig_sha256",
        "context_name",
        "reader_uid",
        "reader_gid",
        "rbac_binding",
    }
    delivery_fields = {
        "credential_class",
        "allowed_secret_namespace",
        "allowed_secret_name",
        "secret_key",
    }
    if delivery:
        fields |= delivery_fields
    rbac = identity.get("rbac_binding") if isinstance(identity, dict) else None
    rbac_fields = {
        "namespace",
        "role_name",
        "role_uid",
        "role_resource_version",
        "role_rules_sha256",
        "role_binding_name",
        "role_binding_uid",
        "role_binding_resource_version",
        "role_ref_kind",
        "role_ref_name",
        "service_account_uid",
        "service_account_resource_version",
        "subjects_sha256",
    }
    if (
        not isinstance(identity, dict)
        or set(identity) != fields
        or identity.get("audience") != audience
        or identity.get("maximum_lifetime_seconds") != maximum_lifetime_seconds
        or identity.get("reader_uid") != reader_uid
        or identity.get("reader_gid") != reader_gid
        or not all(
            isinstance(identity.get(field), str) and identity[field]
            for field in fields
            - {
                "maximum_lifetime_seconds",
                "allowed_permissions",
                "denied_permissions",
                "reader_uid",
                "reader_gid",
                "rbac_binding",
            }
        )
        or not isinstance(identity.get("allowed_permissions"), list)
        or not identity["allowed_permissions"]
        or not all(
            isinstance(permission, str) and permission
            for permission in identity["allowed_permissions"]
        )
        or len(identity["allowed_permissions"])
        != len(set(identity["allowed_permissions"]))
        or (
            allowed_permissions is not None
            and set(identity["allowed_permissions"]) != allowed_permissions
        )
        or not isinstance(identity.get("denied_permissions"), list)
        or not identity["denied_permissions"]
        or not all(
            isinstance(permission, str) and permission
            for permission in identity["denied_permissions"]
        )
        or len(identity["denied_permissions"])
        != len(set(identity["denied_permissions"]))
        or not isinstance(rbac, dict)
        or set(rbac) != rbac_fields
        or not all(isinstance(rbac.get(field), str) and rbac[field] for field in rbac_fields)
        or rbac["namespace"] != identity["namespace"]
        or rbac["service_account_uid"] != identity["uid"]
        or rbac["service_account_resource_version"]
        != identity["service_account_resource_version"]
        or rbac["role_ref_kind"] != "Role"
        or rbac["role_ref_name"] != rbac["role_name"]
    ):
        raise AuthorityServiceError(f"{label} identity contract is incomplete")
    if delivery:
        expected_permission = (
            f"secrets:get:{identity['allowed_secret_namespace']}/"
            f"{identity['allowed_secret_name']}"
        )
        if (
            identity["allowed_permissions"] != [expected_permission]
            or identity["credential_class"] not in {"pat-bootstrap", "pat-scientific"}
            or identity["secret_key"] != "token"
        ):
            raise AuthorityServiceError(
                f"{label} is not bound to one exact scoped credential Secret"
            )
    required_denials = {
        "impersonate:*",
        "pods:create",
        "pods:delete",
        "pods:exec",
        "secrets:create",
        "secrets:delete",
        "secrets:list",
        "secrets:patch",
        "secrets:update",
        "serviceaccounts/token:create",
        "workloads:create",
        "workloads:delete",
        "workloads:patch",
        "workloads:update",
    }
    if delivery:
        required_denials |= {"pods/portforward:create", "secrets:get:other"}
    else:
        required_denials |= {"secrets:get", "secrets:watch"}
    if not required_denials <= set(identity["denied_permissions"]):
        raise AuthorityServiceError(f"{label} RBAC denials are incomplete")
    kubeconfig = Path(identity["kubeconfig"])
    root_reader_file(kubeconfig, label=f"{label} kubeconfig", reader_gid=reader_gid)
    if file_sha256(kubeconfig) != identity["kubeconfig_sha256"]:
        raise AuthorityServiceError(f"{label} kubeconfig differs")


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
        "operator_identity",
        "operator_proxy_identity",
        "credential_delivery_identities",
        "cluster_id",
        "kubeconfig",
        "kubeconfig_sha256",
        "cluster_inventory_identity",
        "handoff_kubeconfig",
        "namespaces",
        "approved_control_plane_cidrs",
        "terraform_roots",
        "artifact_inventory_scopes",
        "credential_registry_path",
        "consumer_contracts_path",
        "provider_executables",
        "class_adapters",
        "backend_access_identities",
        "backend_access_identity_adapter",
        "backend_custody_adapter",
        "release_identity_adapter",
        "authorization_closure_adapter",
        "cluster_authorization_adapter",
        "operator_proxy_authorization_adapter",
        "credential_delivery_authorization_adapter",
        "controller_inventory_adapters",
        "state_migration_adapter",
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
    backend_identities = policy.get("backend_access_identities")
    backend_purposes = {"authority", *CALLER_PURPOSES}
    backend_fields = {
        "config_path",
        "config_sha256",
        "profile",
        "project_id",
        "service_account_id",
        "credential_kind",
        "audience",
        "provider_issuer",
        "token_exchange_source",
        "maximum_lifetime_seconds",
        "reader_uid",
        "reader_gid",
    }
    if not isinstance(backend_identities, dict) or set(backend_identities) != backend_purposes:
        raise AuthorityServiceError("purpose-bound backend identities are incomplete")
    for purpose, identity in backend_identities.items():
        if (
            not isinstance(identity, dict)
            or set(identity) != backend_fields
            or identity.get("project_id") != policy["project_id"]
            or identity.get("credential_kind") != "workload_identity_session"
            or identity.get("audience") != f"terraform-backend:{purpose}"
            or identity.get("maximum_lifetime_seconds") != 3600
            or not all(
                isinstance(identity.get(field), str) and identity[field]
                for field in backend_fields
                - {"maximum_lifetime_seconds", "reader_uid", "reader_gid"}
            )
            or not isinstance(identity.get("reader_uid"), int)
            or not isinstance(identity.get("reader_gid"), int)
            or (purpose != "authority" and identity["reader_uid"] < 1)
            or (purpose != "authority" and identity["reader_gid"] < 1)
        ):
            raise AuthorityServiceError(
                f"purpose-bound backend identity is malformed: {purpose}"
            )
        config_path = Path(identity["config_path"])
        if purpose == "authority":
            if identity["reader_uid"] != 0 or identity["reader_gid"] != 0:
                raise AuthorityServiceError("authority backend identity must be root-only")
            root_private_file(config_path, label="authority backend identity configuration")
        else:
            root_reader_file(
                config_path,
                label=f"{purpose} backend identity configuration",
                reader_gid=identity["reader_gid"],
            )
        if file_sha256(config_path) != identity["config_sha256"]:
            raise AuthorityServiceError(
                f"purpose-bound backend identity config differs: {purpose}"
            )
    automation = policy.get("evidence_identity")
    if (
        not isinstance(automation, dict)
        or set(automation)
        != {
            "config_path",
            "config_sha256",
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
                "config_sha256",
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
    root_private_file(config_path, label="read-only evidence identity configuration")
    if file_sha256(config_path) != automation["config_sha256"]:
        raise AuthorityServiceError("read-only evidence identity configuration differs")
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
        "config_sha256",
        "profile",
        "project_id",
        "service_account_id",
        "credential_kind",
        "audience",
        "provider_issuer",
        "token_exchange_source",
        "maximum_lifetime_seconds",
        "allowed_roles",
        "allowed_commands",
        "interactive_login_allowed",
        "human_principal_allowed",
        "reader_uid",
        "reader_gid",
    }
    if (
        not isinstance(release, dict)
        or set(release) != release_fields
        or release.get("project_id") != policy["project_id"]
        or release.get("audience") != "terraform-release"
        or release.get("interactive_login_allowed") is not False
        or release.get("human_principal_allowed") is not False
        or release.get("credential_kind")
        != "workload_identity_session"
        or not all(
            isinstance(release.get(field), str) and release[field]
            for field in release_fields
            - {
                "allowed_roles",
                "allowed_commands",
                "maximum_lifetime_seconds",
                "interactive_login_allowed",
                "human_principal_allowed",
                "reader_uid",
                "reader_gid",
            }
        )
        or not isinstance(release.get("allowed_roles"), list)
        or not release["allowed_roles"]
        or not set(release["allowed_roles"]) <= {"editor", "admin"}
        or not isinstance(release.get("allowed_commands"), list)
        or set(release["allowed_commands"]) != {"preflight", "plan", "apply"}
        or release.get("maximum_lifetime_seconds") != 3600
    ):
        raise AuthorityServiceError("automation-only release identity is incomplete")
    release_path = Path(release["config_path"])
    if not release_path.is_absolute():
        raise AuthorityServiceError("release identity config path must be absolute")
    root_reader_file(
        release_path,
        label="release workload identity configuration",
        reader_gid=release["reader_gid"],
    )
    if file_sha256(release_path) != release["config_sha256"]:
        raise AuthorityServiceError("release workload identity configuration differs")
    operator = policy.get("operator_identity")
    operator_fields = {
        "project_id",
        "service_account_id",
        "credential_kind",
        "credential_id",
        "kubeconfig",
        "kubeconfig_sha256",
        "context_name",
        "maximum_lifetime_seconds",
        "reader_uid",
        "reader_gid",
    }
    if (
        not isinstance(operator, dict)
        or set(operator) != operator_fields
        or operator.get("project_id") != policy["project_id"]
        or operator.get("credential_kind") != "auth_public_keys"
        or operator.get("maximum_lifetime_seconds") != 86400
        or not all(
            isinstance(operator.get(field), str) and operator[field]
            for field in operator_fields
            - {"maximum_lifetime_seconds", "reader_uid", "reader_gid"}
        )
    ):
        raise AuthorityServiceError("operator viewer identity is incomplete")
    operator_kubeconfig = Path(operator["kubeconfig"])
    root_reader_file(
        operator_kubeconfig,
        label="operator viewer kubeconfig",
        reader_gid=operator["reader_gid"],
    )
    if file_sha256(operator_kubeconfig) != operator["kubeconfig_sha256"]:
        raise AuthorityServiceError("operator viewer kubeconfig differs")
    _validate_scoped_kubernetes_identity(
        policy.get("operator_proxy_identity"),
        label="operator port-forward",
        reader_uid=policy["operator_proxy_identity"].get("reader_uid", -1),
        reader_gid=policy["operator_proxy_identity"].get("reader_gid", -1),
        audience="operator-proxy",
        allowed_permissions={
            "pods:get:fs2-system",
            "pods:list:fs2-system",
            "pods/portforward:create:fs2-system",
            "services:get:fs2-system",
        },
        maximum_lifetime_seconds=900,
    )
    deliveries = policy.get("credential_delivery_identities")
    if not isinstance(deliveries, dict) or set(deliveries) != {
        "general-access",
        "scientific-access",
    }:
        raise AuthorityServiceError("scoped credential delivery identities are incomplete")
    expected_classes = {
        "general-access": "pat-bootstrap",
        "scientific-access": "pat-scientific",
    }
    for kind, identity in deliveries.items():
        _validate_scoped_kubernetes_identity(
            identity,
            label=f"{kind} credential delivery",
            reader_uid=identity.get("reader_uid", -1),
            reader_gid=identity.get("reader_gid", -1),
            audience=f"scoped-credential-delivery:{kind}",
            maximum_lifetime_seconds=300,
            delivery=True,
        )
        if identity["credential_class"] != expected_classes[kind]:
            raise AuthorityServiceError(
                f"{kind} delivery is bound to the wrong credential class"
            )
    cluster_identity = policy.get("cluster_inventory_identity")
    cluster_identity_fields = {
        "service_account_id",
        "namespace",
        "name",
        "uid",
        "credential_id",
        "issuer",
        "audience",
        "maximum_lifetime_seconds",
        "allowed_permissions",
        "denied_permissions",
    }
    if (
        not isinstance(cluster_identity, dict)
        or set(cluster_identity) != cluster_identity_fields
        or not all(
            isinstance(cluster_identity.get(field), str) and cluster_identity[field]
            for field in cluster_identity_fields
            - {
                "maximum_lifetime_seconds",
                "allowed_permissions",
                "denied_permissions",
            }
        )
        or cluster_identity.get("audience") != "credential-inventory"
        or cluster_identity.get("maximum_lifetime_seconds") != 3600
        or not isinstance(cluster_identity.get("allowed_permissions"), list)
        or not all(
            isinstance(value, str)
            for value in cluster_identity["allowed_permissions"]
        )
        or sorted(cluster_identity["allowed_permissions"])
        != ["serviceaccounts:list", "secrets:get", "secrets:list"]
        or not isinstance(cluster_identity.get("denied_permissions"), list)
        or not all(
            isinstance(value, str)
            for value in cluster_identity["denied_permissions"]
        )
        or set(cluster_identity["denied_permissions"])
        != {
            "impersonate:*",
            "pods:create",
            "pods:exec",
            "secrets:create",
            "secrets:delete",
            "secrets:patch",
            "secrets:update",
            "serviceaccounts/token:create",
            "workloads:create",
            "workloads:delete",
            "workloads:patch",
            "workloads:update",
        }
    ):
        raise AuthorityServiceError(
            "global Secret inventory identity or RBAC closure is incomplete"
        )
    global_kubeconfig = Path(policy["kubeconfig"])
    root_private_file(global_kubeconfig, label="global Secret inventory kubeconfig")
    if file_sha256(global_kubeconfig) != policy.get("kubeconfig_sha256"):
        raise AuthorityServiceError("global Secret inventory kubeconfig differs")
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
    if policy["handoff_kubeconfig"] != operator["kubeconfig"]:
        raise AuthorityServiceError(
            "operator viewer kubeconfig differs from the handoff inventory identity"
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
                "backend_expectation",
                "legacy_state_source",
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
            or not isinstance(root.get("backend_expectation"), dict)
            or set(root["backend_expectation"])
            != {
                "project_id",
                "bucket_id",
                "bucket_parent_id",
                "bucket_project_id",
                "bucket_owner_service_account_id",
                "object_key",
                "endpoint",
                "kms_key_id",
                "kms_key_parent_id",
                "kms_key_project_id",
                "logging_destination_bucket_id",
                "logging_destination_parent_id",
                "logging_destination_project_id",
                "access_log_prefix",
                "object_lock_mode",
                "object_lock_retention_days",
            }
            or root["backend_expectation"].get("project_id") != policy["project_id"]
            or root["backend_expectation"].get("bucket_project_id")
            != policy["project_id"]
            or root["backend_expectation"].get("kms_key_project_id")
            != policy["project_id"]
            or root["backend_expectation"].get("logging_destination_project_id")
            != policy["project_id"]
            or not all(
                isinstance(value, str) and value
                for key, value in root["backend_expectation"].items()
                if key != "object_lock_retention_days"
            )
            or not isinstance(
                root["backend_expectation"].get("object_lock_retention_days"), int
            )
            or root["backend_expectation"]["object_lock_retention_days"] < 1
            or not isinstance(root.get("legacy_state_source"), dict)
            or set(root["legacy_state_source"])
            != {
                "path",
                "sha256",
                "canonical_state_sha256",
                "lineage",
                "serial",
                "retained_reason",
                "retention_expires_at",
            }
            or not isinstance(root["legacy_state_source"].get("path"), str)
            or not Path(root["legacy_state_source"]["path"]).is_absolute()
            or not all(
                isinstance(root["legacy_state_source"].get(field), str)
                and root["legacy_state_source"][field]
                for field in (
                    "sha256",
                    "canonical_state_sha256",
                    "lineage",
                    "retained_reason",
                    "retention_expires_at",
                )
            )
            or not isinstance(root["legacy_state_source"].get("serial"), int)
            or root["legacy_state_source"]["serial"] < 1
        ):
            raise AuthorityServiceError(f"authority Terraform root is malformed: {name}")
        backend_config = Path(root["backend_config_path"])
        terraform_data_dir = Path(root["terraform_data_dir"])
        backend_metadata = backend_config.stat()
        if (
            backend_config.is_symlink()
            or not backend_config.is_file()
            or backend_metadata.st_uid != 0
            or stat.S_IMODE(backend_metadata.st_mode) != 0o644
            or any(
                parent.is_symlink()
                or not parent.is_dir()
                or parent.stat().st_uid != 0
                or stat.S_IMODE(parent.stat().st_mode) & 0o022
                for parent in backend_config.parents
            )
        ):
            raise AuthorityServiceError(
                f"{name} Terraform backend configuration must be root-owned mode 0644"
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
        "crane",
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
    if contracts.get("pending_contract_ids") != []:
        raise AuthorityServiceError(
            "every one of the 21 credential classes must be admitted by an exact adapter"
        )
    contract_ids = set(contracts.get("contracts", {}))
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
            or set(class_adapter["operations"])
            != set(contracts["contracts"][credential_class].get("required_operations", []))
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
        policy.get("backend_access_identity_adapter"),
        label="purpose-bound backend workload identity",
    )
    _validate_adapter(
        policy.get("backend_custody_adapter"), label="Terraform backend custody"
    )
    _validate_adapter(
        policy.get("release_identity_adapter"), label="release workload identity"
    )
    _validate_adapter(
        policy.get("authorization_closure_adapter"),
        label="provider authorization closure",
    )
    _validate_adapter(
        policy.get("cluster_authorization_adapter"),
        label="global Secret inventory identity and RBAC closure",
    )
    _validate_adapter(
        policy.get("operator_proxy_authorization_adapter"),
        label="operator port-forward identity and RBAC closure",
    )
    _validate_adapter(
        policy.get("credential_delivery_authorization_adapter"),
        label="scoped credential delivery identity and RBAC closure",
    )
    controller_adapters = policy.get("controller_inventory_adapters")
    if not isinstance(controller_adapters, dict) or set(controller_adapters) != {
        "helm_release_records"
    }:
        raise AuthorityServiceError("controller-owned Secret adapters are incomplete")
    _validate_adapter(
        controller_adapters["helm_release_records"],
        label="Helm release storage inventory",
    )
    _validate_adapter(
        policy.get("state_migration_adapter"),
        label="Terraform legacy-to-remote state copy custody",
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


def _validate_caller_identities(document: dict[str, Any]) -> None:
    callers = document.get("caller_identities")
    operation_callers = document.get("operation_callers")
    if (
        not isinstance(callers, dict)
        or set(callers) != CALLER_PURPOSES
        or not isinstance(operation_callers, dict)
        or set(operation_callers) != READ_ONLY_OPERATIONS
    ):
        raise AuthorityServiceError("purpose-bound authority callers are incomplete")
    uids: set[int] = set()
    gids: set[int] = set()
    cgroups: set[str] = set()
    for caller_id, caller in callers.items():
        fields = {
            "purpose",
            "uid",
            "gid",
            "cgroup_path",
            "executable_path",
            "executable_sha256",
            "client_script_path",
            "client_script_sha256",
        }
        if (
            not isinstance(caller, dict)
            or set(caller) != fields
            or caller.get("purpose") != caller_id
            or not isinstance(caller.get("uid"), int)
            or caller["uid"] < 1
            or not isinstance(caller.get("gid"), int)
            or caller["gid"] < 1
            or not isinstance(caller.get("cgroup_path"), str)
            or not caller["cgroup_path"].startswith("/")
            or any(
                not isinstance(caller.get(field), str) or not caller[field]
                for field in (
                    "executable_path",
                    "executable_sha256",
                    "client_script_path",
                    "client_script_sha256",
                )
            )
        ):
            raise AuthorityServiceError(f"authority caller is malformed: {caller_id}")
        if (
            caller["uid"] in uids
            or caller["gid"] in gids
            or caller["cgroup_path"] in cgroups
        ):
            raise AuthorityServiceError("authority caller identities must be distinct")
        uids.add(caller["uid"])
        gids.add(caller["gid"])
        cgroups.add(caller["cgroup_path"])
        for path_field, digest_field in (
            ("executable_path", "executable_sha256"),
            ("client_script_path", "client_script_sha256"),
        ):
            configured_path = Path(caller[path_field])
            candidate = configured_path.resolve()
            candidate_metadata = candidate.stat() if candidate.is_file() else None
            if (
                not candidate.is_absolute()
                or configured_path.is_symlink()
                or not candidate.is_file()
                or candidate_metadata is None
                or candidate_metadata.st_uid != 0
                or stat.S_IMODE(candidate_metadata.st_mode) & 0o022
                or len(caller[digest_field]) != 64
                or file_sha256(candidate) != caller[digest_field]
            ):
                raise AuthorityServiceError(
                    f"authority caller process pin differs: {caller_id}"
                )
    for operation, allowed in operation_callers.items():
        if (
            not isinstance(allowed, list)
            or not allowed
            or len(allowed) != len(set(allowed))
            or not set(allowed) <= set(callers)
        ):
            raise AuthorityServiceError(
                f"authority operation caller set is malformed: {operation}"
            )
    release_operations = READ_ONLY_OPERATIONS - {
        "operator-read-context",
        "operator-proxy-context",
        "scoped-credential-context",
    }
    expected_operation_callers = {
        **{operation: {"release-automation"} for operation in release_operations},
        "operator-read-context": {"operator-read"},
        "operator-proxy-context": {"operator-proxy"},
        "scoped-credential-context": {
            "credential-delivery-general",
            "credential-delivery-scientific",
        },
    }
    expected_operation_callers["backend-custody"] = set(CALLER_PURPOSES)
    expected_operation_callers["state-migration-readiness"] = set(CALLER_PURPOSES)
    if any(
        set(operation_callers[operation]) != expected
        for operation, expected in expected_operation_callers.items()
    ):
        raise AuthorityServiceError("authority operation-to-purpose map is not exact")


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    root_private_file(path, label="authority configuration")
    document = json.loads(path.read_text(encoding="utf-8"))
    policy = document.get("policy") if isinstance(document, dict) else None
    required = {
        "schema",
        "caller_identities",
        "operation_callers",
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
        != "fs2-serve.nebius.ai/credential-authority-config/v3"
        or not isinstance(document.get("configuration_id"), str)
        or not document["configuration_id"]
        or not isinstance(policy, dict)
        or not isinstance(document.get("operations"), dict)
        or set(document["operations"]) != READ_ONLY_OPERATIONS
        or not isinstance(document.get("evidence_producer_key_id"), str)
        or not document["evidence_producer_key_id"]
    ):
        raise AuthorityServiceError("authority configuration is incomplete")
    _validate_caller_identities(document)
    _validate_policy(policy)
    expected_readers = {
        "release-automation": policy["release_identity"],
        "operator-read": policy["operator_identity"],
        "operator-proxy": policy["operator_proxy_identity"],
        "credential-delivery-general": policy["credential_delivery_identities"]["general-access"],
        "credential-delivery-scientific": policy["credential_delivery_identities"]["scientific-access"],
    }
    for caller_id, identity in expected_readers.items():
        caller = document["caller_identities"][caller_id]
        if (caller["uid"], caller["gid"]) != (
            identity["reader_uid"],
            identity["reader_gid"],
        ):
            raise AuthorityServiceError(
                f"authority caller does not own its purpose-scoped reader: {caller_id}"
            )
        backend_identity = policy["backend_access_identities"][caller_id]
        if (caller["uid"], caller["gid"]) != (
            backend_identity["reader_uid"],
            backend_identity["reader_gid"],
        ):
            raise AuthorityServiceError(
                f"authority caller does not own its backend reader: {caller_id}"
            )
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


def receive_request(connection: socket.socket) -> tuple[int, int, int, dict[str, Any]]:
    if not hasattr(socket, "SO_PEERCRED"):
        raise AuthorityServiceError("kernel peer credentials are unavailable")
    pid, uid, gid = struct.unpack(
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
    return pid, uid, gid, envelope


def normalized_parameters(request: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Validate the complete client schema and return only bounded parameters."""

    operation = request["operation"]
    allowed = CLIENT_FIELDS[operation]
    expected = {"operation", "request_nonce", *allowed}
    if set(request) != expected or FORBIDDEN_CLIENT_FIELDS.intersection(request):
        raise AuthorityServiceError("credential authority request schema is invalid")
    parameters = {field: request[field] for field in sorted(allowed)}
    scalar_parameters = {
        key: value
        for key, value in parameters.items()
        if key not in {"credential_bindings", "source_trust"}
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
    if operation == "scoped-credential-context" and parameters["credential_kind"] not in {
        "general-access",
        "scientific-access",
    }:
        raise AuthorityServiceError("scoped credential kind is invalid")
    if operation == "consumer-readiness" and parameters["phase"] not in {
        "predecessor-ready",
        "dual-read-ready",
        "current-write-ready",
    }:
        raise AuthorityServiceError("consumer readiness phase is invalid")
    if operation in {"rotation-readiness", "authentication-continuity"} and (
        parameters["predecessor_id"] == parameters["successor_id"]
    ):
        raise AuthorityServiceError("credential generations must use distinct identities")
    if operation == "ciphertext-migration" and (
        not isinstance(parameters["from"], int)
        or not isinstance(parameters["to"], int)
        or parameters["from"] < 1
        or parameters["to"] != parameters["from"] + 1
    ):
        raise AuthorityServiceError("ciphertext migration generations are not contiguous")
    if operation in {
        "consumer-readiness",
        "rotation-readiness",
        "ciphertext-migration",
        "authentication-continuity",
    }:
        bindings = parameters.get("credential_bindings")
        source_trust = parameters.get("source_trust")
        binding_fields = {
            "namespace",
            "name",
            "uid",
            "resource_version",
            "content_sha256",
            "authority_evidence_id",
            "credential_class",
            "generation",
            "immutable",
        }
        if (
            not isinstance(bindings, dict)
            or len(bindings) > 256
            or not isinstance(parameters.get("bindings_sha256"), str)
            or len(parameters["bindings_sha256"]) != 64
            or canonical_sha256(bindings) != parameters["bindings_sha256"]
        ):
            raise AuthorityServiceError("consumer readiness bindings are incomplete")
        source_trust_fields = {
            "credential_class",
            "generation",
            "retained_generations",
            "registry_sha256",
            "credential_identities_sha256",
            "terraform_bindings_sha256",
            "secret_bindings_sha256",
        }
        if (
            not isinstance(source_trust, dict)
            or set(source_trust) != source_trust_fields
            or source_trust.get("credential_class")
            != parameters.get("credential_class")
            or not isinstance(source_trust.get("generation"), int)
            or source_trust["generation"] < 1
            or not isinstance(source_trust.get("retained_generations"), list)
            or not source_trust["retained_generations"]
            or source_trust["retained_generations"]
            != list(range(1, source_trust["generation"] + 1))
            or any(
                not isinstance(source_trust.get(field), str)
                or len(source_trust[field]) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in source_trust[field]
                )
                for field in (
                    "registry_sha256",
                    "credential_identities_sha256",
                    "terraform_bindings_sha256",
                    "secret_bindings_sha256",
                )
            )
            or source_trust["secret_bindings_sha256"]
            != parameters["bindings_sha256"]
        ):
            raise AuthorityServiceError(
                "consumer readiness source trust is incomplete"
            )
        if (
            operation == "consumer-readiness"
            and source_trust["generation"] != parameters["generation"]
        ):
            raise AuthorityServiceError(
                "consumer readiness generation differs from source trust"
            )
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
    if operation in {
        "consumer-readiness",
        "rotation-readiness",
        "ciphertext-migration",
        "authentication-continuity",
    }:
        contracts = json.loads(
            Path(config["policy"]["consumer_contracts_path"]).read_text(
                encoding="utf-8"
            )
        )
        contract = contracts.get("contracts", {}).get(parameters["credential_class"])
        if (
            not isinstance(contract, dict)
            or operation not in contract.get("required_operations", [])
        ):
            raise AuthorityServiceError(
                "credential class has no contract for this authority operation"
            )
    if operation in {"backend-custody", "state-migration-readiness"} and parameters["terraform_root_name"] not in config[
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
    config: dict[str, Any],
    request: dict[str, Any],
    *,
    parameters: dict[str, Any],
    caller_authorization: dict[str, Any],
) -> dict[str, Any]:
    operation = request["operation"]
    policy = config["policy"]
    provider_request = {
        "schema": "fs2-serve.nebius.ai/credential-provider-read/v2",
        "operation": operation,
        "request_nonce": request["request_nonce"],
        "parameters": parameters,
        "caller_authorization": caller_authorization,
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
        "caller_authorization_sha256",
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
        or payload.get("caller_authorization_sha256")
        != canonical_sha256(caller_authorization)
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
    gid: int,
    caller_authorization: dict[str, Any],
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
        "client_gid": gid,
        "caller_id": caller_authorization["id"],
        "caller_purpose": caller_authorization["purpose"],
        "caller_authorization_sha256": canonical_sha256(caller_authorization),
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
    pid, uid, gid, envelope = receive_request(connection)
    request = envelope["request"]
    caller_authorization = authorize_operation_caller(
        config=config,
        operation=request["operation"],
        pid=pid,
        uid=uid,
        gid=gid,
        request=request,
    )
    parameters = normalized_parameters(request, config)
    payload = provider_call(
        config,
        request,
        parameters=parameters,
        caller_authorization=caller_authorization,
    )
    response = externally_anchored_response(
        config=config,
        request=request,
        request_sha256=envelope["request_sha256"],
        payload=payload,
    )
    audit_event(
        config=config,
        uid=uid,
        gid=gid,
        caller_authorization=caller_authorization,
        request=request,
        evidence=response,
    )
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
