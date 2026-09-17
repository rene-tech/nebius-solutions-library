#!/usr/bin/env python3
"""Capture the authoritative Nebius IAM human directory for external signing.

The adapter accepts no caller-supplied transcript. It reads a root-owned trust
anchor and a dedicated read-only CLI configuration from source-fixed paths,
executes the pinned bounded IAM queries itself, and emits a canonical unsigned
snapshot. External security automation signs that exact output.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

ADAPTER_ID = "fs2-serve.nebius.ai/nebius-iam-human-directory/v3"
SNAPSHOT_SCHEMA = "fs2-serve.nebius.ai/security-subject-provider-snapshot/v3"
TRUST_SCHEMA = "fs2-serve.nebius.ai/security-provider-trust-anchor/v3"
TRUST_ANCHOR_PATH = Path("/etc/fs2/security/network-policy-provider-trust-anchor-v3.json")
NEBIUS_CLI_PATH = Path("/usr/local/bin/nebius")
NEBIUS_CONFIG_PATH = Path("/etc/fs2/security/nebius-directory-reader.yaml")
NEBIUS_CREDENTIAL_PATH = Path("/etc/fs2/security/nebius-directory-reader-credential.json")
PROVIDER_AUTHORITY_PATH = Path(
    "/etc/fs2/security/network-policy-provider-collection-authority-v1.json"
)


class AdapterError(RuntimeError):
    """The authoritative provider enumeration is unavailable or incomplete."""


def canonical(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _is_https_endpoint(value: Any) -> bool:
    return isinstance(value, str) and bool(
        re.fullmatch(
            r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*)?",
            value,
        )
    )


def _nested(value: Any, path: Any, *, label: str) -> Any:
    if (
        not isinstance(path, list)
        or not path
        or any(not isinstance(part, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", part) for part in path)
    ):
        raise AdapterError(f"{label} path is invalid")
    current = value
    for part in path:
        if not isinstance(current, dict) or part not in current:
            raise AdapterError(f"{label} path is absent")
        current = current[part]
    return current


def _root_owned_file(
    path: Path,
    *,
    modes: set[int],
    maximum: int,
    label: str,
    required_gid: int | None = None,
) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != 0
                or (required_gid is not None and metadata.st_gid != required_gid)
                or stat.S_IMODE(metadata.st_mode) not in modes
                or metadata.st_size > maximum
            ):
                raise AdapterError(f"{label} custody is not exact")
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = os.read(descriptor, min(65536, maximum + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > maximum:
                    raise AdapterError(f"{label} exceeds its bound")
            value = b"".join(chunks)
            if len(value) != metadata.st_size:
                raise AdapterError(f"{label} changed while it was read")
        finally:
            os.close(descriptor)
    except OSError as error:
        raise AdapterError(f"{label} custody is unavailable") from error
    if len(value) > maximum:
        raise AdapterError(f"{label} exceeds its bound")
    return value


def _provider_authority(trust: dict[str, Any]) -> tuple[dict[str, Any], str]:
    try:
        raw = _root_owned_file(
            PROVIDER_AUTHORITY_PATH,
            modes={0o400, 0o444},
            maximum=65536,
            label="provider collection authority",
            required_gid=0,
        )
        authority = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AdapterError("provider collection authority is invalid") from error
    authority_sha256 = hashlib.sha256(canonical(authority).encode()).hexdigest()
    if (
        not isinstance(authority, dict)
        or set(authority)
        != {
            "schema",
            "provider",
            "tenant_sha256",
            "api_endpoint_sha256",
            "snapshot_public_key",
            "snapshot_signer_key_id",
            "valid_from",
            "expires_at",
        }
        or authority.get("schema")
        != "fs2-serve.nebius.ai/security-provider-collection-authority/v1"
        or authority.get("provider") != "nebius-iam"
        or authority.get("tenant_sha256") != trust.get("tenant_sha256")
        or authority.get("api_endpoint_sha256")
        != trust.get("directory_execution", {}).get("api_endpoint_sha256")
        or authority_sha256 != trust.get("provider_collection_authority_sha256")
        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", str(authority.get("snapshot_public_key", "")))
        or authority.get("snapshot_signer_key_id")
        != hashlib.sha256(str(authority.get("snapshot_public_key", "")).encode()).hexdigest()
    ):
        raise AdapterError("provider collection authority is not independently pinned")
    now = dt.datetime.now(dt.UTC)
    try:
        valid_from = dt.datetime.fromisoformat(str(authority["valid_from"]).replace("Z", "+00:00"))
        expires_at = dt.datetime.fromisoformat(str(authority["expires_at"]).replace("Z", "+00:00"))
    except ValueError as error:
        raise AdapterError("provider collection authority validity is invalid") from error
    if (
        valid_from.tzinfo is None
        or expires_at.tzinfo is None
        or valid_from.astimezone(dt.UTC) > now
        or expires_at.astimezone(dt.UTC)
        <= now + dt.timedelta(seconds=trust["directory_query"]["snapshot_ttl_seconds"])
    ):
        raise AdapterError("provider collection authority is not valid for the trusted evidence lifetime")
    return authority, authority_sha256


def _trust_anchor() -> tuple[dict[str, Any], str]:
    try:
        raw = _root_owned_file(
            TRUST_ANCHOR_PATH,
            modes={0o400, 0o444},
            maximum=65536,
            label="provider trust anchor",
            required_gid=0,
        )
        trust = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AdapterError("provider trust anchor is invalid") from error
    expected = {
        "schema", "provider", "adapter", "directory_execution", "directory_query",
        "kubernetes_authentication", "execution_sha256", "kubernetes_authentication_sha256",
        "tenant_sha256", "query_sha256", "provider_collection_authority_sha256",
        "valid_from", "expires_at",
    }
    execution_fields = {
        "cli_path", "cli_sha256", "config_path", "config_sha256", "credential_path",
        "credential_sha256", "api_endpoint", "api_endpoint_sha256", "provider_issuer",
        "provider_issuer_sha256", "principal_type", "principal_id", "config_profiles_path",
        "config_endpoint_path", "config_credential_path", "config_principal_path",
        "credential_principal_path", "directory_reader_role",
    }
    query_fields = {
        "cli_path", "config_path", "profile", "tenant_id", "page_size", "max_pages",
        "max_records", "timeout_seconds", "snapshot_ttl_seconds", "consistency_passes",
    }
    authentication_fields = {
        "oidc_issuer", "oidc_issuer_sha256", "audiences", "audiences_sha256",
        "username_claim", "username_prefix", "groups_claim", "groups_prefix",
        "provider_subject_field", "provider_email_field", "provider_group_field",
        "probe_token_path", "probe_token_sha256",
    }
    execution = trust.get("directory_execution", {}) if isinstance(trust, dict) else {}
    query = trust.get("directory_query", {}) if isinstance(trust, dict) else {}
    authentication = trust.get("kubernetes_authentication", {}) if isinstance(trust, dict) else {}
    if (
        not isinstance(trust, dict)
        or set(trust) != expected
        or trust.get("schema") != TRUST_SCHEMA
        or trust.get("provider") != "nebius-iam"
        or not isinstance(execution, dict)
        or set(execution) != execution_fields
        or not isinstance(query, dict)
        or set(query) != query_fields
        or not isinstance(authentication, dict)
        or set(authentication) != authentication_fields
        or execution.get("cli_path") != str(NEBIUS_CLI_PATH)
        or execution.get("config_path") != str(NEBIUS_CONFIG_PATH)
        or execution.get("credential_path") != str(NEBIUS_CREDENTIAL_PATH)
        or query.get("cli_path") != str(NEBIUS_CLI_PATH)
        or query.get("config_path") != str(NEBIUS_CONFIG_PATH)
        or not re.fullmatch(r"[A-Za-z0-9._-]{3,128}", str(query.get("profile", "")))
        or not re.fullmatch(r"tenant-[A-Za-z0-9-]{8,128}", str(query.get("tenant_id", "")))
        or not isinstance(query.get("page_size"), int)
        or not 1 <= query["page_size"] <= 1000
        or not isinstance(query.get("max_pages"), int)
        or not 1 <= query["max_pages"] <= 10000
        or not isinstance(query.get("max_records"), int)
        or not 1 <= query["max_records"] <= 100000
        or not isinstance(query.get("timeout_seconds"), int)
        or not 1 <= query["timeout_seconds"] <= 120
        or not isinstance(query.get("snapshot_ttl_seconds"), int)
        or not 10800 <= query["snapshot_ttl_seconds"] <= 28800
        or query.get("consistency_passes") != 2
        or not _is_https_endpoint(execution.get("api_endpoint"))
        or not _is_https_endpoint(execution.get("provider_issuer"))
        or execution.get("api_endpoint_sha256")
        != hashlib.sha256(str(execution.get("api_endpoint", "")).encode()).hexdigest()
        or execution.get("provider_issuer_sha256")
        != hashlib.sha256(str(execution.get("provider_issuer", "")).encode()).hexdigest()
        or execution.get("principal_type") != "service-account"
        or not re.fullmatch(r"serviceaccount-[A-Za-z0-9-]{8,128}", str(execution.get("principal_id", "")))
        or not isinstance(execution.get("directory_reader_role"), dict)
        or set(execution["directory_reader_role"]) != {"id", "permissions", "permissions_sha256"}
        or not re.fullmatch(
            r"[A-Za-z0-9._:-]{8,253}", str(execution["directory_reader_role"].get("id", ""))
        )
        or not isinstance(execution["directory_reader_role"].get("permissions"), list)
        or not execution["directory_reader_role"]["permissions"]
        or len(execution["directory_reader_role"]["permissions"])
        != len(set(execution["directory_reader_role"]["permissions"]))
        or any(
            not isinstance(permission, str)
            or not re.fullmatch(r"[a-z0-9._/-]+\.(?:get|list)", permission)
            for permission in execution["directory_reader_role"]["permissions"]
        )
        or execution["directory_reader_role"].get("permissions_sha256")
        != hashlib.sha256(
            canonical(sorted(execution["directory_reader_role"]["permissions"])).encode()
        ).hexdigest()
        or not _is_https_endpoint(authentication.get("oidc_issuer"))
        or authentication.get("oidc_issuer_sha256")
        != hashlib.sha256(str(authentication.get("oidc_issuer", "")).encode()).hexdigest()
        or authentication.get("oidc_issuer") != execution.get("provider_issuer")
        or not isinstance(authentication.get("audiences"), list)
        or not authentication["audiences"]
        or len(authentication["audiences"]) != len(set(authentication["audiences"]))
        or any(not re.fullmatch(r"[A-Za-z0-9._:/-]{3,253}", str(value)) for value in authentication["audiences"])
        or authentication.get("audiences_sha256")
        != hashlib.sha256(canonical(sorted(authentication["audiences"])).encode()).hexdigest()
        or authentication.get("username_claim") not in {"sub", "email"}
        or not isinstance(authentication.get("username_prefix"), str)
        or not re.fullmatch(r"[A-Za-z0-9:@._+/-]{0,128}", authentication["username_prefix"])
        or authentication.get("groups_claim") != "groups"
        or not isinstance(authentication.get("groups_prefix"), str)
        or not re.fullmatch(r"[A-Za-z0-9:@._+/-]{0,128}", authentication["groups_prefix"])
        or authentication.get("provider_subject_field") != "tenant_user_account.metadata.id"
        or authentication.get("provider_email_field") != "attributes.email"
        or authentication.get("provider_group_field") != "metadata.name"
        or authentication.get("probe_token_path")
        != "/etc/fs2/security/network-policy-provider-oidc-probe.jwt"
        or not re.fullmatch(r"[0-9a-f]{64}", str(authentication.get("probe_token_sha256", "")))
        or trust.get("execution_sha256") != hashlib.sha256(canonical(execution).encode()).hexdigest()
        or trust.get("kubernetes_authentication_sha256")
        != hashlib.sha256(canonical(authentication).encode()).hexdigest()
        or trust.get("tenant_sha256") != hashlib.sha256(query["tenant_id"].encode()).hexdigest()
        or trust.get("query_sha256")
        != hashlib.sha256(
            canonical({"execution": execution, "query": query, "authentication": authentication}).encode()
        ).hexdigest()
        or trust.get("adapter") != {
            "id": ADAPTER_ID,
            "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
        or not re.fullmatch(
            r"[0-9a-f]{64}", str(trust.get("provider_collection_authority_sha256", ""))
        )
    ):
        raise AdapterError("provider trust anchor is not source-pinned exact")
    now = dt.datetime.now(dt.UTC)
    try:
        valid_from = dt.datetime.fromisoformat(str(trust["valid_from"]).replace("Z", "+00:00"))
        expires_at = dt.datetime.fromisoformat(str(trust["expires_at"]).replace("Z", "+00:00"))
    except ValueError as error:
        raise AdapterError("provider trust validity is invalid") from error
    if (
        valid_from.tzinfo is None
        or expires_at.tzinfo is None
        or valid_from.astimezone(dt.UTC) > now
        or expires_at.astimezone(dt.UTC) <= now + dt.timedelta(seconds=query["snapshot_ttl_seconds"])
    ):
        raise AdapterError("provider trust anchor is not currently rollback-valid")
    cli_bytes = _root_owned_file(
        NEBIUS_CLI_PATH,
        modes={0o500, 0o550, 0o555, 0o700, 0o750, 0o755},
        maximum=268435456,
        label="provider CLI executable",
        required_gid=0,
    )
    config_bytes = _root_owned_file(
        NEBIUS_CONFIG_PATH,
        modes={0o400, 0o440},
        maximum=1048576,
        label="read-only provider CLI configuration",
        required_gid=0,
    )
    credential_bytes = _root_owned_file(
        NEBIUS_CREDENTIAL_PATH,
        modes={0o400, 0o440},
        maximum=1048576,
        label="read-only provider credential",
        required_gid=0,
    )
    try:
        config_document = yaml.safe_load(config_bytes)
        credential_document = json.loads(credential_bytes)
        profiles = _nested(config_document, execution.get("config_profiles_path"), label="profile root")
        profile = profiles.get(query["profile"]) if isinstance(profiles, dict) else None
        if not isinstance(profile, dict):
            raise AdapterError("configured provider profile is absent")
        configured_endpoint = _nested(
            profile, execution.get("config_endpoint_path"), label="profile endpoint"
        )
        configured_credential = _nested(
            profile, execution.get("config_credential_path"), label="profile credential"
        )
        configured_principal = _nested(
            profile, execution.get("config_principal_path"), label="profile principal"
        )
        credential_principal = _nested(
            credential_document,
            execution.get("credential_principal_path"),
            label="credential principal",
        )
    except (UnicodeDecodeError, json.JSONDecodeError, yaml.YAMLError) as error:
        raise AdapterError("provider configuration or credential is not parseable") from error
    if (
        execution.get("cli_sha256") != hashlib.sha256(cli_bytes).hexdigest()
        or execution.get("config_sha256") != hashlib.sha256(config_bytes).hexdigest()
        or execution.get("credential_sha256") != hashlib.sha256(credential_bytes).hexdigest()
        or configured_endpoint != execution["api_endpoint"]
        or configured_credential != str(NEBIUS_CREDENTIAL_PATH)
        or configured_principal != execution["principal_id"]
        or credential_principal != execution["principal_id"]
    ):
        raise AdapterError("provider executable or parsed profile-to-credential binding is not exact")
    return trust, hashlib.sha256(canonical(trust).encode()).hexdigest()


def _provider_page(
    execution: dict[str, Any],
    query: dict[str, Any],
    command: list[str],
    token: str,
) -> dict[str, Any]:
    arguments = [
        str(NEBIUS_CLI_PATH), *command,
        "--page-size", str(query["page_size"]), "--page-token", token,
        "--format", "json", "--config", str(NEBIUS_CONFIG_PATH), "--profile", query["profile"],
        "--endpoint", execution["api_endpoint"],
        "--no-check-update", "--no-browser", "--color=false", "--retries", "1",
        "--timeout", f"{query['timeout_seconds']}s",
        "--auth-timeout", f"{query['timeout_seconds']}s",
    ]
    try:
        result = subprocess.run(  # noqa: S603 -- executable and arguments are source/root anchored
            arguments,
            capture_output=True,
            check=False,
            close_fds=True,
            cwd="/",
            env={
                "HOME": "/var/empty",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "NEBIUS_CONFIG": str(NEBIUS_CONFIG_PATH),
                "PATH": "/usr/bin:/bin",
            },
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
            timeout=query["timeout_seconds"] + 5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AdapterError("provider query did not complete within its bound") from error
    if result.returncode != 0 or result.stderr.strip():
        raise AdapterError("provider query failed closed")
    try:
        page = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise AdapterError("provider query returned invalid JSON") from error
    if (
        not isinstance(page, dict)
        or not isinstance(page.get("items"), list)
        or not isinstance(page.get("next_page_token", ""), str)
        or set(page) - {"items", "next_page_token"}
    ):
        raise AdapterError("provider page schema is not exact")
    return page


def _provider_document(
    execution: dict[str, Any],
    query: dict[str, Any],
    command: list[str],
    *,
    label: str,
) -> dict[str, Any]:
    arguments = [
        str(NEBIUS_CLI_PATH),
        *command,
        "--format",
        "json",
        "--config",
        str(NEBIUS_CONFIG_PATH),
        "--profile",
        query["profile"],
        "--endpoint",
        execution["api_endpoint"],
        "--no-check-update",
        "--no-browser",
        "--color=false",
        "--retries",
        "1",
        "--timeout",
        f"{query['timeout_seconds']}s",
        "--auth-timeout",
        f"{query['timeout_seconds']}s",
    ]
    try:
        result = subprocess.run(  # noqa: S603 -- executable and arguments are source/root anchored
            arguments,
            capture_output=True,
            check=False,
            close_fds=True,
            cwd="/",
            env={
                "HOME": "/var/empty",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "NEBIUS_CONFIG": str(NEBIUS_CONFIG_PATH),
                "PATH": "/usr/bin:/bin",
            },
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
            timeout=query["timeout_seconds"] + 5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AdapterError(f"provider {label} query did not complete within its bound") from error
    if result.returncode != 0 or result.stderr.strip():
        raise AdapterError(f"provider {label} query failed closed")
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise AdapterError(f"provider {label} query returned invalid JSON") from error
    if not isinstance(document, dict):
        raise AdapterError(f"provider {label} query did not return an object")
    return document


def _capture_provider_authorization_once(trust: dict[str, Any]) -> dict[str, Any]:
    execution = trust["directory_execution"]
    query = trust["directory_query"]
    whoami = _provider_document(execution, query, ["iam", "whoami"], label="whoami")
    subject = whoami.get("subject", {}) if isinstance(whoami, dict) else {}
    if (
        not isinstance(subject, dict)
        or subject.get("type") != execution["principal_type"]
        or subject.get("id") != execution["principal_id"]
        or whoami.get("tenant_id") != query["tenant_id"]
        or set(whoami) != {"subject", "tenant_id"}
        or set(subject) != {"type", "id"}
    ):
        raise AdapterError("provider whoami does not match the parsed credential principal")
    budgets = {"pages": query["max_pages"], "records": query["max_records"]}
    membership_operation = (
        "group-membership.list-member-of:"
        f"{hashlib.sha256(execution['principal_id'].encode()).hexdigest()}"
    )
    memberships, membership_pages = _list_pages(
        execution,
        query,
        [
            "iam",
            "group-membership",
            "list-member-of",
            "--subject-id",
            execution["principal_id"],
        ],
        membership_operation,
        budgets,
    )
    principal_group_ids: list[str] = []
    for membership in memberships:
        metadata = membership.get("metadata", {}) if isinstance(membership, dict) else {}
        spec = membership.get("spec", {}) if isinstance(membership, dict) else {}
        group_id = metadata.get("parent_id") if isinstance(metadata, dict) else None
        if (
            not isinstance(spec, dict)
            or set(spec) != {"member_id"}
            or spec.get("member_id") != execution["principal_id"]
            or not isinstance(group_id, str)
            or not re.fullmatch(r"[A-Za-z0-9._:-]{8,253}", group_id)
        ):
            raise AdapterError("provider directory-reader group membership is malformed")
        principal_group_ids.append(group_id)
    if len(principal_group_ids) != len(set(principal_group_ids)):
        raise AdapterError("provider directory-reader group membership is duplicated")
    principal_group_ids.sort()
    effective_subject_ids = {execution["principal_id"], *principal_group_ids}
    bindings, binding_pages = _list_pages(
        execution,
        query,
        ["iam", "access-binding", "list", "--parent-id", query["tenant_id"]],
        "access-binding.list",
        budgets,
    )
    effective_bindings: list[dict[str, str]] = []
    for binding in bindings:
        spec = binding.get("spec", {}) if isinstance(binding, dict) else {}
        if (
            not isinstance(spec, dict)
            or set(spec) != {"subject_id", "role_id"}
            or not isinstance(spec.get("subject_id"), str)
            or not isinstance(spec.get("role_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9._:-]{8,253}", spec["subject_id"])
            or not re.fullmatch(r"[A-Za-z0-9._:-]{8,253}", spec["role_id"])
        ):
            raise AdapterError("provider access binding inventory is malformed")
        if spec["subject_id"] in effective_subject_ids:
            effective_bindings.append({
                "subject_id": spec["subject_id"],
                "subject_kind": (
                    "service-account"
                    if spec["subject_id"] == execution["principal_id"]
                    else "group"
                ),
                "role_id": spec["role_id"],
            })
    effective_bindings.sort(key=canonical)
    if not effective_bindings or len({canonical(value) for value in effective_bindings}) != len(
        effective_bindings
    ):
        raise AdapterError("provider effective access binding closure is empty or duplicated")
    role_contract = execution["directory_reader_role"]
    role_ids = sorted({binding["role_id"] for binding in effective_bindings})
    if role_contract["id"] not in role_ids or len(role_ids) > query["max_records"]:
        raise AdapterError("provider effective role closure omits the approved directory-reader role")
    approved_permissions = set(role_contract["permissions"])
    effective_permissions: set[str] = set()
    effective_roles: list[dict[str, Any]] = []
    for role_id in role_ids:
        role = _provider_document(
            execution,
            query,
            ["iam", "role", "get", "--id", role_id],
            label="effective directory-reader role",
        )
        role_metadata = role.get("metadata", {}) if isinstance(role, dict) else {}
        role_spec = role.get("spec", {}) if isinstance(role, dict) else {}
        permissions = role_spec.get("permissions") if isinstance(role_spec, dict) else None
        if (
            not isinstance(role_metadata, dict)
            or role_metadata.get("id") != role_id
            or not isinstance(permissions, list)
            or not permissions
            or any(
                not isinstance(permission, str)
                or not re.fullmatch(r"[a-z0-9._/-]+\.(?:get|list)", permission)
                for permission in permissions
            )
            or len(permissions) != len(set(permissions))
            or not set(permissions) <= approved_permissions
            or (
                role_id == role_contract["id"]
                and sorted(permissions) != sorted(role_contract["permissions"])
            )
        ):
            raise AdapterError("provider effective role permission closure is not read-only exact")
        effective_permissions.update(permissions)
        effective_roles.append({
            "role_id": role_id,
            "document": role,
            "document_sha256": hashlib.sha256(canonical(role).encode()).hexdigest(),
            "permissions_sha256": hashlib.sha256(
                canonical(sorted(permissions)).encode()
            ).hexdigest(),
        })
    if (
        effective_permissions != approved_permissions
        or hashlib.sha256(canonical(sorted(effective_permissions)).encode()).hexdigest()
        != role_contract["permissions_sha256"]
    ):
        raise AdapterError("provider effective permissions differ from the approved read-only set")
    record_count = query["max_records"] - budgets["records"]
    page_count = query["max_pages"] - budgets["pages"]
    return {
        "whoami": whoami,
        "membership_pages": membership_pages,
        "principal_group_ids": principal_group_ids,
        "access_binding_pages": binding_pages,
        "effective_bindings": effective_bindings,
        "effective_roles": effective_roles,
        "effective_permissions": sorted(effective_permissions),
        "effective_permissions_sha256": role_contract["permissions_sha256"],
        "page_count": page_count,
        "record_count": record_count,
    }


def _capture_provider_authorization(trust: dict[str, Any]) -> dict[str, Any]:
    passes = trust["directory_query"]["consistency_passes"]
    captures = [_capture_provider_authorization_once(trust) for _ in range(passes)]
    baseline = captures[0]
    if any(capture != baseline for capture in captures[1:]):
        raise AdapterError("provider authorization changed across the required repeat-stability fence")
    collection_sha256 = hashlib.sha256(canonical(baseline).encode()).hexdigest()
    return {
        "consistency": {
            "mode": "double-collect-byte-identical",
            "passes": passes,
            "collection_sha256": collection_sha256,
        },
        "collections": [
            {
                "index": index,
                "evidence": capture,
                "sha256": hashlib.sha256(canonical(capture).encode()).hexdigest(),
            }
            for index, capture in enumerate(captures)
        ],
    }


def _list_pages(
    execution: dict[str, Any],
    query: dict[str, Any],
    command: list[str],
    operation: str,
    budgets: dict[str, int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    token = ""
    items: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    while True:
        budgets["pages"] -= 1
        if budgets["pages"] < 0:
            raise AdapterError("provider enumeration exceeded its page bound")
        page = _provider_page(execution, query, command, token)
        budgets["records"] -= len(page["items"])
        if budgets["records"] < 0 or len(page["items"]) > query["page_size"]:
            raise AdapterError("provider enumeration exceeded its record bound")
        pages.append({
            "operation": operation,
            "request_token": token,
            "response": page,
            "next_token": page.get("next_page_token", ""),
        })
        items.extend(page["items"])
        next_token = page.get("next_page_token", "")
        if not next_token:
            return items, pages
        if next_token == token:
            raise AdapterError("provider pagination did not advance")
        token = next_token


def _metadata_identity(value: Any, *, label: str) -> tuple[str, str]:
    metadata = value.get("metadata", {}) if isinstance(value, dict) else {}
    identifier = metadata.get("id") if isinstance(metadata, dict) else None
    name = metadata.get("name") if isinstance(metadata, dict) else None
    if (
        not isinstance(identifier, str)
        or not re.fullmatch(r"[A-Za-z0-9._:-]{8,253}", identifier)
        or not isinstance(name, str)
        or not re.fullmatch(r"[A-Za-z0-9:@._/-]{1,253}", name)
    ):
        raise AdapterError(f"provider {label} identity is invalid")
    return identifier, name


def _capture_directory(trust: dict[str, Any]) -> dict[str, Any]:
    execution = trust["directory_execution"]
    query = trust["directory_query"]
    authentication = trust["kubernetes_authentication"]
    budgets = {"pages": query["max_pages"], "records": query["max_records"]}
    raw_pages: list[dict[str, Any]] = []
    user_items, pages = _list_pages(
        execution,
        query,
        ["iam", "tenant-user-account-with-attributes", "list", "--parent-id", query["tenant_id"]],
        "tenant-user-account-with-attributes.list",
        budgets,
    )
    raw_pages.extend(pages)
    group_items, pages = _list_pages(
        execution,
        query,
        ["iam", "group", "list", "--parent-id", query["tenant_id"]],
        "group.list",
        budgets,
    )
    raw_pages.extend(pages)

    users_by_id: dict[str, dict[str, Any]] = {}
    for item in user_items:
        account = item.get("tenant_user_account", {}) if isinstance(item, dict) else {}
        identifier, _ = _metadata_identity(account, label="tenant user")
        attributes = item.get("attributes", {}) if isinstance(item, dict) else {}
        email = attributes.get("email") if isinstance(attributes, dict) else None
        claim = identifier if authentication["username_claim"] == "sub" else email
        username = f"{authentication['username_prefix']}{claim}" if isinstance(claim, str) else None
        if (
            not isinstance(username, str)
            or not re.fullmatch(r"[A-Za-z0-9:@._+/-]{3,253}", username)
            or identifier in users_by_id
            or any(value["username"] == username for value in users_by_id.values())
        ):
            raise AdapterError("provider human account inventory is invalid or duplicated")
        users_by_id[identifier] = {
            "provider_subject_id": identifier,
            "username": username,
            "groups": [],
        }

    groups_by_id: dict[str, str] = {}
    for item in group_items:
        identifier, name = _metadata_identity(item, label="group")
        mapped_name = f"{authentication['groups_prefix']}{name}"
        if (
            identifier in groups_by_id
            or mapped_name in groups_by_id.values()
            or not re.fullmatch(r"[A-Za-z0-9:@._+/-]{1,253}", mapped_name)
        ):
            raise AdapterError("provider group inventory is duplicated")
        groups_by_id[identifier] = mapped_name

    for user_id, user in sorted(users_by_id.items()):
        membership_items, pages = _list_pages(
            execution,
            query,
            ["iam", "group-membership", "list-member-of", "--subject-id", user_id],
            f"group-membership.list-member-of:{hashlib.sha256(user_id.encode()).hexdigest()}",
            budgets,
        )
        raw_pages.extend(pages)
        memberships: list[str] = []
        for membership in membership_items:
            metadata = membership.get("metadata", {}) if isinstance(membership, dict) else {}
            group_id = metadata.get("parent_id") if isinstance(metadata, dict) else None
            spec = membership.get("spec", {}) if isinstance(membership, dict) else {}
            if spec.get("member_id") != user_id or group_id not in groups_by_id:
                raise AdapterError("provider group membership is outside the complete directory inventory")
            memberships.append(groups_by_id[group_id])
        if len(memberships) != len(set(memberships)):
            raise AdapterError("provider group membership is duplicated")
        user["groups"] = sorted(memberships)

    if not users_by_id or not groups_by_id or not raw_pages:
        raise AdapterError("provider human directory is empty")
    logical_cursor = ""
    receipts: list[dict[str, Any]] = []
    for index, page in enumerate(raw_pages):
        next_logical_cursor = (
            "" if index + 1 == len(raw_pages) else canonical({
                "operation": raw_pages[index + 1]["operation"],
                "request_token": raw_pages[index + 1]["request_token"],
            })
        )
        receipts.append({
            "index": index,
            "request_cursor_sha256": hashlib.sha256(logical_cursor.encode()).hexdigest(),
            "response_sha256": hashlib.sha256(
                canonical({"operation": page["operation"], "response": page["response"]}).encode()
            ).hexdigest(),
            "next_cursor_sha256": (
                hashlib.sha256(next_logical_cursor.encode()).hexdigest() if next_logical_cursor else ""
            ),
        })
        logical_cursor = next_logical_cursor
    transcript_sha256 = hashlib.sha256(canonical(raw_pages).encode()).hexdigest()
    users = sorted(users_by_id.values(), key=lambda item: item["username"])
    groups = sorted(groups_by_id.values())
    record_count = query["max_records"] - budgets["records"]
    if record_count < len(users) + len(groups):
        raise AdapterError("provider enumeration record count is incomplete")
    return {
        "users": users,
        "groups": groups,
        "raw_pages": raw_pages,
        "receipts": receipts,
        "record_count": record_count,
        "collection_sha256": transcript_sha256,
    }


def capture() -> dict[str, Any]:
    trust, trust_sha256 = _trust_anchor()
    authority, authority_sha256 = _provider_authority(trust)
    query = trust["directory_query"]
    provider_authorization = _capture_provider_authorization(trust)
    collections = [_capture_directory(trust) for _ in range(query["consistency_passes"])]
    baseline = collections[0]
    if any(
        collection["users"] != baseline["users"]
        or collection["groups"] != baseline["groups"]
        or collection["raw_pages"] != baseline["raw_pages"]
        or collection["collection_sha256"] != baseline["collection_sha256"]
        for collection in collections[1:]
    ):
        raise AdapterError("provider directory changed across the required repeat-stability fence")
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    transcript_sha256 = baseline["collection_sha256"]
    users = baseline["users"]
    groups = baseline["groups"]
    return {
        "schema": SNAPSHOT_SCHEMA,
        "snapshot_id": f"nebius-iam-{now.strftime('%Y%m%dT%H%M%SZ')}-{transcript_sha256[:16]}",
        "provider": "nebius-iam",
        "adapter": {"id": ADAPTER_ID, "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
        "trust_anchor_sha256": trust_sha256,
        "provider_collection_authority_sha256": authority_sha256,
        "execution_sha256": trust["execution_sha256"],
        "kubernetes_authentication_sha256": trust["kubernetes_authentication_sha256"],
        "provider_principal": {
            "type": trust["directory_execution"]["principal_type"],
            "id": trust["directory_execution"]["principal_id"],
            "credential_sha256": trust["directory_execution"]["credential_sha256"],
        },
        "api_endpoint_sha256": trust["directory_execution"]["api_endpoint_sha256"],
        "provider_issuer_sha256": trust["directory_execution"]["provider_issuer_sha256"],
        "tenant_sha256": trust["tenant_sha256"],
        "query_sha256": trust["query_sha256"],
        "provider_authorization": provider_authorization,
        "complete": True,
        "pagination": {
            "subject_count": len(users) + len(groups),
            "page_size": query["page_size"],
            "consistency": {
                "mode": "double-collect-byte-identical",
                "passes": len(collections),
                "collection_sha256": transcript_sha256,
            },
            "collections": [
                {
                    "index": index,
                    "page_count": len(collection["receipts"]),
                    "record_count": collection["record_count"],
                    "terminal_cursor": "",
                    "pages": collection["receipts"],
                    "sha256": collection["collection_sha256"],
                }
                for index, collection in enumerate(collections)
            ],
        },
        "raw_collections": [collection["raw_pages"] for collection in collections],
        "human_users": users,
        "human_groups": groups,
        "captured_at": now.isoformat(),
        "expires_at": (now + dt.timedelta(seconds=query["snapshot_ttl_seconds"])).isoformat(),
        "signer_key_id": authority["snapshot_signer_key_id"],
    }


def main() -> int:
    if len(sys.argv) != 1:
        print("provider adapter failed closed: caller arguments are forbidden", file=sys.stderr)
        return 1
    try:
        print(canonical(capture()))
        return 0
    except (AdapterError, OSError, TypeError, ValueError) as error:
        print(f"provider adapter failed closed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
