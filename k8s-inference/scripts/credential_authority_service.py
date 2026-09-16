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
import hmac
import json
import os
import socket
import stat
import struct
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from append_only_evidence import append_event, load_stream

CONFIG_PATH = Path("/etc/fs2-credential-authority/config.json")
SOCKET_PATH = Path("/run/fs2-credential-authority/v1.sock")
AUDIT_ROOT = Path("/var/lib/fs2-credential-authority/audit")
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
READ_ONLY_OPERATIONS = frozenset(
    {
        "get",
        "inventory",
        "list-operation",
        "secret-bindings",
        "artifact-inventory",
        "consumer-readiness",
        "prove-consumers",
        "prove-zero-readers",
        "prove-absence",
        "prove-ciphertext-migration",
        "prove-customer-storage-cipher-migration",
        "prove-customer-storage-name-migration",
        "prove-auth-continuity",
        "verify-attestation",
    }
)
BUILTIN_OPERATIONS = frozenset({"verify-attestation"})


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


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    root_private_file(path, label="authority configuration")
    document = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(document, dict)
        or set(document)
        != {
            "schema",
            "allowed_client_uids",
            "operations",
            "configuration_id",
            "attestation_key_path",
            "artifact_inventory_scopes",
        }
        or document.get("schema")
        != "fs2-serve.nebius.ai/credential-authority-config/v1"
        or not isinstance(document.get("configuration_id"), str)
        or not document["configuration_id"]
        or not isinstance(document.get("allowed_client_uids"), list)
        or not document["allowed_client_uids"]
        or not all(
            isinstance(uid, int) and uid >= 0 for uid in document["allowed_client_uids"]
        )
        or len(document["allowed_client_uids"])
        != len(set(document["allowed_client_uids"]))
        or not isinstance(document.get("operations"), dict)
        or set(document["operations"]) != READ_ONLY_OPERATIONS - BUILTIN_OPERATIONS
        or not isinstance(document.get("attestation_key_path"), str)
        or not Path(document["attestation_key_path"]).is_absolute()
        or not isinstance(document.get("artifact_inventory_scopes"), list)
        or not document["artifact_inventory_scopes"]
    ):
        raise AuthorityServiceError("authority configuration is incomplete")
    scope_fields = {"id", "root", "owner", "purpose", "expires_at", "readers"}
    scope_ids: set[str] = set()
    scope_roots: set[str] = set()
    for scope in document["artifact_inventory_scopes"]:
        if (
            not isinstance(scope, dict)
            or set(scope) != scope_fields
            or not all(
                isinstance(scope.get(field), str) and scope[field]
                for field in ("id", "root", "owner", "purpose", "expires_at")
            )
            or not Path(scope["root"]).is_absolute()
            or len(Path(scope["root"]).parts) < 4
            or not isinstance(scope.get("readers"), list)
            or not scope["readers"]
            or not all(
                isinstance(reader, str) and reader for reader in scope["readers"]
            )
            or len(scope["readers"]) != len(set(scope["readers"]))
            or scope["id"] in scope_ids
            or scope["root"] in scope_roots
        ):
            raise AuthorityServiceError("artifact inventory scope is malformed")
        try:
            expiry = datetime.fromisoformat(scope["expires_at"].replace("Z", "+00:00"))
        except ValueError as error:
            raise AuthorityServiceError(
                "artifact inventory scope expiry is invalid"
            ) from error
        if expiry.tzinfo is None:
            raise AuthorityServiceError(
                "artifact inventory scope expiry needs timezone"
            )
        scope_ids.add(scope["id"])
        scope_roots.add(scope["root"])
    for left in scope_roots:
        for right in scope_roots - {left}:
            if Path(left) in Path(right).parents:
                raise AuthorityServiceError(
                    "artifact inventory scopes must not overlap"
                )
    for operation, adapter in document["operations"].items():
        if (
            not isinstance(adapter, dict)
            or set(adapter) != {"command", "file_sha256", "timeout_seconds"}
            or not isinstance(adapter.get("command"), list)
            or not adapter["command"]
            or not all(isinstance(value, str) and value for value in adapter["command"])
            or not Path(adapter["command"][0]).is_absolute()
            or not isinstance(adapter.get("file_sha256"), dict)
            or not isinstance(adapter.get("timeout_seconds"), int)
            or not 1 <= adapter["timeout_seconds"] <= 120
        ):
            raise AuthorityServiceError(f"authority adapter is malformed: {operation}")
        pinned = {str(Path(value).resolve()) for value in adapter["file_sha256"]}
        command_files = {
            str(Path(value).resolve())
            for value in adapter["command"]
            if Path(value).is_absolute() and Path(value).is_file()
        }
        if pinned != command_files:
            raise AuthorityServiceError(
                f"every authority command file must be digest-pinned: {operation}"
            )
        for resolved in sorted(pinned):
            command_file = Path(resolved)
            metadata = command_file.stat()
            expected = adapter["file_sha256"].get(resolved)
            if (
                command_file.is_symlink()
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != 0
                or stat.S_IMODE(metadata.st_mode) & 0o022
                or not isinstance(expected, str)
                or len(expected) != 64
                or file_sha256(command_file) != expected
            ):
                raise AuthorityServiceError(
                    f"authority adapter identity differs from root policy: {operation}"
                )
    root_private_file(
        Path(document["attestation_key_path"]), label="authority attestation key"
    )
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
    ):
        raise AuthorityServiceError("authority request binding is invalid")
    return uid, envelope


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


def attestation_key(config: dict[str, Any]) -> bytes:
    value = Path(config["attestation_key_path"]).read_bytes()
    if len(value) < 32:
        raise AuthorityServiceError("authority attestation key is too short")
    return value


def authority_attestation(
    *, config: dict[str, Any], request_sha256: str, payload: dict[str, Any]
) -> dict[str, Any]:
    claim = {
        "schema": "fs2-serve.nebius.ai/credential-authority-attestation/v1",
        "configuration_sha256": canonical_sha256(config),
        "request_sha256": request_sha256,
        "payload_sha256": canonical_sha256(payload),
        "issued_at": datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
    }
    claim["hmac_sha256"] = hmac.new(
        attestation_key(config),
        json.dumps(claim, sort_keys=True, separators=(",", ":")).encode(),
        hashlib.sha256,
    ).hexdigest()
    return claim


def verify_attestation(
    config: dict[str, Any], request: dict[str, Any]
) -> dict[str, Any]:
    payload = request.get("attested_payload")
    claim = request.get("authority_attestation")
    required = {
        "schema",
        "configuration_sha256",
        "request_sha256",
        "payload_sha256",
        "issued_at",
        "hmac_sha256",
    }
    if (
        not isinstance(payload, dict)
        or not isinstance(claim, dict)
        or set(claim) != required
        or claim.get("schema")
        != "fs2-serve.nebius.ai/credential-authority-attestation/v1"
        or claim.get("configuration_sha256") != canonical_sha256(config)
        or claim.get("payload_sha256") != canonical_sha256(payload)
    ):
        raise AuthorityServiceError("authority attestation claim is malformed")
    supplied = claim["hmac_sha256"]
    unsigned = {key: claim[key] for key in required - {"hmac_sha256"}}
    expected = hmac.new(
        attestation_key(config),
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode(),
        hashlib.sha256,
    ).hexdigest()
    if not isinstance(supplied, str) or not hmac.compare_digest(supplied, expected):
        raise AuthorityServiceError("authority attestation is invalid")
    return {
        "valid": True,
        "configuration_sha256": claim["configuration_sha256"],
        "payload_sha256": claim["payload_sha256"],
    }


def provider_call(config: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    operation = request["operation"]
    if operation == "verify-attestation":
        return verify_attestation(config, request)
    adapter = config["operations"][operation]
    provider_request = request
    if operation == "artifact-inventory":
        provider_request = {
            **request,
            "authority_scopes": config["artifact_inventory_scopes"],
            "scope_registry_sha256": canonical_sha256(
                config["artifact_inventory_scopes"]
            ),
        }
    completed = subprocess.run(
        adapter["command"],
        input=json.dumps(provider_request, sort_keys=True, separators=(",", ":")),
        text=True,
        capture_output=True,
        check=True,
        timeout=adapter["timeout_seconds"],
        env={
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
        },
    )
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        raise AuthorityServiceError("authority provider returned malformed JSON")
    if operation == "artifact-inventory":
        payload = {
            **payload,
            "configuration_sha256": canonical_sha256(config),
            "scope_registry_sha256": canonical_sha256(
                config["artifact_inventory_scopes"]
            ),
            "scope_count": len(config["artifact_inventory_scopes"]),
        }
    if contains_forbidden_value_field(payload):
        raise AuthorityServiceError("authority provider returned forbidden values")
    if operation == "secret-bindings":
        if (
            request.get("metadata_only") is not True
            or payload.get("data_fields_returned") not in (0, False)
            or payload.get("content_commitment_scheme")
            != request.get("content_commitment_scheme")
        ):
            raise AuthorityServiceError("Secret provider broke metadata-only policy")
    return payload


def audit_event(
    *,
    config: dict[str, Any],
    uid: int,
    request: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    if AUDIT_ROOT.is_symlink():
        raise AuthorityServiceError("authority audit root must not be a symlink")
    AUDIT_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = AUDIT_ROOT.stat()
    if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise AuthorityServiceError("authority audit root must be root-only")
    stream = AUDIT_ROOT / datetime.now(UTC).strftime("%Y%m%d")
    if not stream.exists():
        stream.mkdir(mode=0o700)
    event = {
        "schema": "fs2-serve.nebius.ai/credential-authority-audit/v1",
        "observed_at": datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "configuration_id": config["configuration_id"],
        "configuration_sha256": canonical_sha256(config),
        "client_uid": uid,
        "operation": request["operation"],
        "request_sha256": canonical_sha256(request),
        "payload_sha256": canonical_sha256(payload),
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
    if uid not in config["allowed_client_uids"]:
        raise AuthorityServiceError("client uid is not authorized by root policy")
    request = envelope["request"]
    payload = provider_call(config, request)
    audit_event(config=config, uid=uid, request=request, payload=payload)
    response = {
        "schema": "fs2-serve.nebius.ai/credential-authority-response/v1",
        "nonce": envelope["nonce"],
        "request_sha256": envelope["request_sha256"],
        "payload_sha256": canonical_sha256(payload),
        "payload": payload,
        "authority_attestation": authority_attestation(
            config=config,
            request_sha256=envelope["request_sha256"],
            payload=payload,
        ),
    }
    encoded = json.dumps(response, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise AuthorityServiceError("authority response is too large")
    connection.sendall(struct.pack("!I", len(encoded)) + encoded)


def serve() -> None:
    if os.geteuid() != 0:
        raise AuthorityServiceError("credential authority must run as root")
    config = load_config()
    if SOCKET_PATH.exists() or SOCKET_PATH.is_symlink():
        raise AuthorityServiceError(
            "authority socket already exists; reconcile it without deleting from this service"
        )
    SOCKET_PATH.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(SOCKET_PATH))
        os.chmod(SOCKET_PATH, 0o660)
        listener.listen(16)
        while True:
            connection, _address = listener.accept()
            with connection:
                handle(connection, config)


if __name__ == "__main__":
    try:
        serve()
    except (
        AuthorityServiceError,
        OSError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        print(f"error: {error}", file=os.sys.stderr)
        raise SystemExit(2) from error
