#!/usr/bin/env python3
"""Enter one source-enrolled public-edge execution capsule.

The static setgid launcher opens this file and the accepted release manifest
before Python starts.  This bootstrap accepts neither path nor digest from the
caller: it verifies the inherited, root-owned manifest; pins every accepted
source/tool/provider file by descriptor; then executes only the named source
snapshot.  The capsule group deliberately has no members, so an ordinary
caller cannot manufacture the effective-GID plus unreadable-manifest proof by
setting environment variables and invoking Python directly.
"""

from __future__ import annotations

import fcntl
import array
import base64
import binascii
import hashlib
import json
import os
import re
import socket
import stat
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping


MANIFEST_SCHEMA = "fs2-serve.nebius.ai/public-edge-execution-capsule/v2"
AUTH_TRUST_SCHEMA = "fs2-serve.nebius.ai/trusted-nebius-auth-brokers/v2"
AUTH_ENVELOPE_SCHEMA = "fs2-serve.nebius.ai/short-lived-nebius-auth/v3"
AUTH_BROKER_ROLE = "nebius-short-lived-token-broker"
AUTH_BROKER_SOCKET = Path("/run/fs2/public-edge-nebius-auth.sock")
AUTH_BROKER_CONFIG = Path("/etc/fs2/public-edge-nebius-auth-broker.json")
AUTH_BROKER_EXECUTABLE = Path("/usr/local/libexec/fs2-public-edge-nebius-auth-broker")
AUTH_TRUST_PATH = "stages/foundation/trusted-public-edge-auth-broker-authorities.json"
MUTATION_SETTLEMENT_TRUST_PATH = (
    "stages/foundation/trusted-public-edge-mutation-settlement-authorities.json"
)
INTERNAL_DEBUG_ACTIVATION_TRUST_PATH = (
    "stages/foundation/trusted-internal-debug-activation-issuers.json"
)
AUTH_AUDIENCE = "fs2-public-edge-operator"
SOURCE_IDS = {
    "edge-client-identity-verifier",
    "inference-stack",
    "internal-edge-acceptance",
    "internal-edge-proxy",
    "jobset-api-gate",
    "jobset-chart-materializer",
    "jobset-crd-upgrade",
    "jobset-release-verifier",
    "public-edge-verifier",
    "kueue-materializer",
    "kueue-admission-gate",
    "kueue-destroy-cleanup",
}
SOURCE_MODES = {
    "edge-client-identity-verifier": {"external"},
    "inference-stack": {"operator"},
    "internal-edge-acceptance": {"library"},
    "internal-edge-proxy": {"library"},
    "jobset-api-gate": {"local-exec"},
    "jobset-chart-materializer": {"external"},
    "jobset-crd-upgrade": {"local-exec"},
    "jobset-release-verifier": {"local-exec"},
    "public-edge-verifier": {"external", "local-exec", "receipt-contract"},
    "kueue-materializer": {"local-exec"},
    "kueue-admission-gate": {"local-exec"},
    "kueue-destroy-cleanup": {"local-exec"},
}
SOURCE_PATHS = {
    "edge-client-identity-verifier": "stages/workloads/scripts/verify-edge-client-identity-receipt.py",
    "inference-stack": "inference-stack",
    "internal-edge-acceptance": "stages/workloads/scripts/internal_edge_acceptance.py",
    "internal-edge-proxy": "stages/workloads/scripts/internal_edge_proxy.py",
    "jobset-api-gate": "modules/jobset-controller/scripts/wait-for-jobset-api.sh",
    "jobset-chart-materializer": "modules/jobset-controller/scripts/materialize-chart.sh",
    "jobset-crd-upgrade": "modules/jobset-controller/scripts/apply-jobset-crd.sh",
    "jobset-release-verifier": "modules/jobset-controller/scripts/verify-jobset-release.sh",
    "public-edge-verifier": "stages/foundation/scripts/verify-public-edge-node-eligibility.py",
    "kueue-materializer": "stages/foundation/scripts/materialize-kueue-release.sh",
    "kueue-admission-gate": "stages/foundation/scripts/wait-for-kueue-deployment-admission.sh",
    "kueue-destroy-cleanup": "stages/foundation/scripts/cleanup-kueue-aggregate-roles.sh",
}
REQUIRED_TOOLS = {
    "awk",
    "bash",
    "cat",
    "crane",
    "date",
    "find",
    "git",
    "grep",
    "helm",
    "id",
    "install",
    "jq",
    "kubectl",
    "mktemp",
    "nebius",
    "openssl",
    "python3",
    "realpath",
    "rm",
    "sed",
    "sha256sum",
    "sleep",
    "stat",
    "tar",
    "terraform",
    "timeout",
    "tr",
    "wc",
}
REQUIRED_PROVIDER_ADDRESSES = {
    "registry.terraform.io/hashicorp/external",
    "registry.terraform.io/hashicorp/helm",
    "registry.terraform.io/hashicorp/kubernetes",
    "registry.terraform.io/hashicorp/random",
    "terraform-provider.storage.eu-north1.nebius.cloud/nebius/nebius",
}
HEX_40 = re.compile(r"^[a-f0-9]{40}$")
HEX_64 = re.compile(r"^[a-f0-9]{64}$")
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_PINNED_FILE_BYTES = 1024 * 1024 * 1024
MAX_AUTH_ENVELOPE_BYTES = 128 * 1024
MAX_AUTH_TOKEN_BYTES = 64 * 1024
MIN_AUTH_REMAINING_SECONDS = 2 * 60 * 60


class CapsuleError(RuntimeError):
    """The installed capsule is not the accepted immutable release."""


def fail(message: str) -> None:
    raise CapsuleError(message)


def exact_object(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        fail(f"{label} must contain exactly {sorted(keys)}")
    return value


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def lower_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or HEX_64.fullmatch(value) is None:
        fail(f"{label} must be a lowercase SHA-256 digest")
    if value == "0" * 64:
        fail(f"{label} cannot be the zero digest")
    return value


def require_static_elf(raw: bytes, label: str) -> None:
    """Reject PT_INTERP/PT_DYNAMIC so no unpinned runtime code can execute."""

    if (
        len(raw) < 64
        or raw[:4] != b"\x7fELF"
        or raw[4] != 2
        or raw[5] != 1
        or raw[6] != 1
    ):
        fail(f"{label} is not one supported ELF64 little-endian executable")
    program_offset = struct.unpack_from("<Q", raw, 32)[0]
    entry_size = struct.unpack_from("<H", raw, 54)[0]
    entry_count = struct.unpack_from("<H", raw, 56)[0]
    if entry_size < 56 or entry_count < 1 or program_offset + entry_size * entry_count > len(raw):
        fail(f"{label} has a malformed ELF program-header table")
    program_types = {
        struct.unpack_from("<I", raw, program_offset + index * entry_size)[0]
        for index in range(entry_count)
    }
    if 2 in program_types or 3 in program_types:
        fail(f"{label} must be fully static with no PT_DYNAMIC or PT_INTERP")


def inherited_fd(variable: str) -> int:
    value = os.environ.get(variable, "")
    if not value.isdecimal():
        fail(f"{variable} is not an inherited descriptor")
    descriptor = int(value)
    if descriptor < 3:
        fail(f"{variable} cannot name a standard descriptor")
    return descriptor


def require_capsule_process() -> tuple[int, int]:
    real_gid = os.getgid()
    effective_gid = os.getegid()
    if effective_gid == real_gid or effective_gid in os.getgroups():
        fail("capsule process lacks the no-member setgid launcher proof")
    if os.environ.get("FS2_CAPSULE_LAUNCHER") != "fs2-public-edge-capsule-v1":
        fail("capsule launcher marker is absent")
    if (
        sys.flags.isolated != 1
        or sys.flags.no_site != 1
        or not sys.dont_write_bytecode
        or sys.path
    ):
        fail("capsule Python must use isolated/no-site/no-bytecode mode with an empty import path")
    return real_gid, effective_gid


def read_inherited_file(
    descriptor: int,
    *,
    label: str,
    owner_uid: int,
    owner_gid: int,
    exact_mode: int,
    maximum_bytes: int,
) -> bytes:
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != owner_uid
        or before.st_gid != owner_gid
        or stat.S_IMODE(before.st_mode) != exact_mode
    ):
        fail(f"{label} is not the exact protected regular file")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum_bytes:
            fail(f"{label} exceeds its accepted size bound")
    after = os.fstat(descriptor)
    stable = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
        before.st_mode,
        before.st_uid,
        before.st_gid,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
        after.st_mode,
        after.st_uid,
        after.st_gid,
    )
    if not stable or after.st_size != total:
        fail(f"{label} changed while it was read")
    os.lseek(descriptor, 0, os.SEEK_SET)
    return b"".join(chunks)


def decode_canonical_manifest(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapsuleError("accepted capsule manifest is not UTF-8 JSON") from exc
    if canonical_bytes(value) + b"\n" != raw:
        fail("accepted capsule manifest must be canonical JSON plus one newline")
    manifest = exact_object(
        value,
        {
            "schema",
            "accepted_commit",
            "accepted_tree",
            "installed_source_root",
            "capsule_group",
            "installer_sha256",
            "launcher_sha256",
            "bootstrap_sha256",
            "sources",
            "tools",
            "terraform",
            "nebius_auth",
            "release_files",
            "installation_receipt",
        },
        "accepted capsule manifest",
    )
    if manifest["schema"] != MANIFEST_SCHEMA:
        fail("accepted capsule manifest has an unsupported schema")
    if not isinstance(manifest["accepted_commit"], str) or HEX_40.fullmatch(
        manifest["accepted_commit"]
    ) is None:
        fail("accepted capsule commit is malformed")
    if not isinstance(manifest["accepted_tree"], str) or HEX_40.fullmatch(
        manifest["accepted_tree"]
    ) is None:
        fail("accepted capsule tree is malformed")
    lower_digest(manifest["launcher_sha256"], "accepted launcher digest")
    lower_digest(manifest["bootstrap_sha256"], "accepted bootstrap digest")
    lower_digest(manifest["installer_sha256"], "accepted installer digest")
    receipt = exact_object(
        manifest["installation_receipt"],
        {"issuer", "key_id", "payload_sha256", "signature", "reviewed_at"},
        "capsule installation receipt",
    )
    if not all(isinstance(receipt[key], str) and receipt[key] for key in receipt):
        fail("capsule installation receipt fields must be non-empty strings")
    payload_digest = lower_digest(
        receipt["payload_sha256"], "installation receipt payload digest"
    )
    signed_payload = {
        key: manifest[key] for key in manifest if key != "installation_receipt"
    }
    if hashlib.sha256(canonical_bytes(signed_payload)).hexdigest() != payload_digest:
        fail("installation receipt does not bind the accepted capsule payload")
    return manifest


def protected_parent_chain(path: Path, root: Path, capsule_gid: int, label: str) -> None:
    if not path.is_absolute() or not root.is_absolute():
        fail(f"{label} paths must be absolute")
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise CapsuleError(f"{label} escapes the accepted capsule root") from exc
    candidates = [root]
    ancestor = root.parent
    while True:
        candidates.append(ancestor)
        if ancestor == ancestor.parent:
            break
        ancestor = ancestor.parent
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        candidates.append(current)
    for current in dict.fromkeys(candidates):
        details = os.stat(current, follow_symlinks=False)
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != 0
            or details.st_gid not in {0, capsule_gid}
            or stat.S_IMODE(details.st_mode) & 0o022
        ):
            fail(f"{label} parent chain is not root-owned and protected")


def pin_manifest_file(
    root: Path,
    record: object,
    capsule_gid: int,
    label: str,
) -> tuple[int, str]:
    entry = exact_object(record, {"relative_path", "sha256"}, label)
    relative = entry["relative_path"]
    if (
        not isinstance(relative, str)
        or not relative
        or relative.startswith("/")
        or ".." in Path(relative).parts
    ):
        fail(f"{label} has an unsafe relative path")
    expected = lower_digest(entry["sha256"], f"{label} digest")
    path = root / relative
    protected_parent_chain(path, root, capsule_gid, label)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CapsuleError(f"cannot open accepted {label}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or before.st_gid not in {0, capsule_gid}
            or stat.S_IMODE(before.st_mode) & 0o022
            or before.st_size > MAX_PINNED_FILE_BYTES
        ):
            fail(f"{label} is not a protected regular file")
        hasher = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            fail(f"{label} changed while it was pinned")
        if hasher.hexdigest() != expected:
            fail(f"{label} differs from the accepted capsule digest")
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.set_inheritable(descriptor, True)
        return descriptor, f"/proc/self/fd/{descriptor}"
    except BaseException:
        os.close(descriptor)
        raise


def sealed_memfd(name: str, content: bytes) -> int:
    if not hasattr(os, "memfd_create"):
        fail("sealed anonymous file descriptors are unavailable")
    descriptor = os.memfd_create(
        name, os.MFD_CLOEXEC | getattr(os, "MFD_ALLOW_SEALING", 0)
    )
    try:
        os.write(descriptor, content)
        os.lseek(descriptor, 0, os.SEEK_SET)
        seals = (
            fcntl.F_SEAL_SEAL
            | fcntl.F_SEAL_SHRINK
            | fcntl.F_SEAL_GROW
            | fcntl.F_SEAL_WRITE
        )
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, seals)
        if fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) & seals != seals:
            fail(f"{name} memfd is not fully sealed")
        os.set_inheritable(descriptor, True)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def b64url(value: object, size: int, label: str) -> bytes:
    if not isinstance(value, str):
        fail(f"{label} must be base64url text")
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, binascii.Error) as exc:
        raise CapsuleError(f"{label} is not canonical base64url") from exc
    if (
        len(decoded) != size
        or base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != value
    ):
        fail(f"{label} has the wrong size or encoding")
    return decoded


def read_sealed_secret(descriptor: int, label: str, maximum_bytes: int) -> bytes:
    details = os.fstat(descriptor)
    required_seals = (
        fcntl.F_SEAL_SEAL
        | fcntl.F_SEAL_SHRINK
        | fcntl.F_SEAL_GROW
        | fcntl.F_SEAL_WRITE
    )
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_size < 1
        or details.st_size > maximum_bytes
        or fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) & required_seals
        != required_seals
    ):
        fail(f"{label} is not one bounded fully sealed descriptor")
    content = os.pread(descriptor, details.st_size, 0)
    after = os.fstat(descriptor)
    if len(content) != details.st_size or (
        details.st_dev,
        details.st_ino,
        details.st_size,
        details.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_ctime_ns,
    ):
        fail(f"{label} changed while read")
    return content


def verify_ed25519(
    public_key: bytes,
    signature: bytes,
    message: bytes,
    *,
    openssl_path: str,
    inherited_fds: tuple[int, ...],
) -> None:
    der = bytes.fromhex("302a300506032b6570032100") + public_key
    encoded = base64.b64encode(der).decode("ascii")
    pem = (
        "-----BEGIN PUBLIC KEY-----\n"
        + "\n".join(
            encoded[index : index + 64] for index in range(0, len(encoded), 64)
        )
        + "\n-----END PUBLIC KEY-----\n"
    ).encode("ascii")
    key_fd = sealed_memfd("fs2-auth-public-key", pem)
    message_fd = sealed_memfd("fs2-auth-message", message)
    signature_fd = sealed_memfd("fs2-auth-signature", signature)
    try:
        descriptors = tuple(
            sorted({*inherited_fds, key_fd, message_fd, signature_fd})
        )
        result = subprocess.run(
            [
                openssl_path,
                "pkeyutl",
                "-verify",
                "-pubin",
                "-inkey",
                f"/proc/self/fd/{key_fd}",
                "-rawin",
                "-in",
                f"/proc/self/fd/{message_fd}",
                "-sigfile",
                f"/proc/self/fd/{signature_fd}",
            ],
            env={
                "HOME": "/nonexistent",
                "PATH": "/nonexistent",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            close_fds=True,
            pass_fds=descriptors,
        )
    finally:
        os.close(key_fd)
        os.close(message_fd)
        os.close(signature_fd)
    if result.returncode != 0:
        fail("short-lived Nebius authentication envelope signature is invalid")


def requested_nebius_profile(arguments: list[str]) -> str:
    values: list[str] = []
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == "--nebius-profile":
            if index + 1 >= len(arguments):
                fail("--nebius-profile is missing its value")
            values.append(arguments[index + 1])
            index += 2
            continue
        if value.startswith("--nebius-profile="):
            values.append(value.split("=", 1)[1])
        index += 1
    if len(values) > 1 or (
        values and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", values[0]) is None
    ):
        fail("operator arguments contain an ambiguous or malformed Nebius profile")
    return values[0] if values else "sandbox"


def auth_contract(manifest: Mapping[str, Any]) -> dict[str, Any]:
    contract = exact_object(
        manifest["nebius_auth"],
        {
            "audience",
            "broker_socket_path",
            "maximum_token_ttl_seconds",
            "minimum_remaining_seconds",
            "trust_store",
        },
        "capsule Nebius authentication contract",
    )
    if (
        contract["audience"] != AUTH_AUDIENCE
        or contract["broker_socket_path"] != str(AUTH_BROKER_SOCKET)
        or not isinstance(contract["maximum_token_ttl_seconds"], int)
        or contract["maximum_token_ttl_seconds"] < MIN_AUTH_REMAINING_SECONDS
        or contract["maximum_token_ttl_seconds"] > 4 * 60 * 60
        or contract["minimum_remaining_seconds"] != MIN_AUTH_REMAINING_SECONDS
    ):
        fail("capsule Nebius authentication bounds are unsupported")
    trust_record = exact_object(
        contract["trust_store"], {"relative_path", "sha256"}, "auth trust store"
    )
    if trust_record["relative_path"] != AUTH_TRUST_PATH:
        fail("capsule Nebius auth trust store is not at its canonical release path")
    lower_digest(trust_record["sha256"], "auth trust-store digest")
    return contract


def auth_authority(
    trust_raw: bytes,
    *,
    profile: str,
    authority_id: str,
    key_id: str,
    caller_uid: int,
    caller_real_gid: int,
    operator_identity: str,
) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    try:
        decoded = json.loads(trust_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapsuleError("Nebius auth authority registry is not UTF-8 JSON") from exc
    if canonical_bytes(decoded) + b"\n" != trust_raw:
        fail("Nebius auth authority registry must be canonical JSON plus one newline")
    registry = exact_object(
        decoded, {"schema", "authorities"}, "Nebius auth authority registry"
    )
    if registry["schema"] != AUTH_TRUST_SCHEMA or not isinstance(
        registry["authorities"], list
    ):
        fail("Nebius auth authority registry schema is unsupported")
    matches: list[tuple[bytes, dict[str, Any], dict[str, Any]]] = []
    identities: set[tuple[str, str]] = set()
    for index, raw in enumerate(registry["authorities"]):
        record = exact_object(
            raw,
            {
                "audience",
                "broker_config_sha256",
                "broker_executable_sha256",
                "endpoint",
                "id",
                "key_id",
                "peer_credential_mode",
                "peer_gid",
                "peer_runtime_review_sha256",
                "peer_uid",
                "profiles",
                "public_key",
                "role",
                "socket_path",
            },
            f"Nebius auth authority {index}",
        )
        identity = (record["id"], record["key_id"])
        if identity in identities:
            fail("Nebius auth authority registry contains a duplicate identity")
        identities.add(identity)
        public_key = b64url(record["public_key"], 32, "auth authority public key")
        if (
            record["role"] != AUTH_BROKER_ROLE
            or record["audience"] != AUTH_AUDIENCE
            or record["endpoint"] != "api.nebius.cloud"
            or record["socket_path"] != str(AUTH_BROKER_SOCKET)
            or record["peer_credential_mode"] != "linux-so-peercred-pid-uid-gid/v1"
            or HEX_64.fullmatch(str(record["peer_runtime_review_sha256"])) is None
            or record["peer_runtime_review_sha256"] == "0" * 64
            or isinstance(record["peer_uid"], bool)
            or not isinstance(record["peer_uid"], int)
            or record["peer_uid"] != 0
            or isinstance(record["peer_gid"], bool)
            or not isinstance(record["peer_gid"], int)
            or record["peer_gid"] < 0
            or HEX_64.fullmatch(str(record["broker_executable_sha256"])) is None
            or record["broker_executable_sha256"] == "0" * 64
            or HEX_64.fullmatch(str(record["broker_config_sha256"])) is None
            or record["broker_config_sha256"] == "0" * 64
            or not isinstance(record["profiles"], list)
            or record["key_id"]
            != "sha256:" + hashlib.sha256(public_key).hexdigest()
        ):
            fail("Nebius auth authority registry contains a malformed authority")
        profile_names: set[str] = set()
        for profile_index, raw_profile in enumerate(record["profiles"]):
            profile_record = exact_object(
                raw_profile,
                {"name", "operators", "project_id", "subject_id", "tenant_id"},
                f"Nebius auth authority {index} profile {profile_index}",
            )
            profile_name = profile_record["name"]
            if (
                not isinstance(profile_name, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", profile_name) is None
                or profile_name in profile_names
                or not isinstance(profile_record["project_id"], str)
                or re.fullmatch(r"project-[a-z0-9]+", profile_record["project_id"]) is None
                or not isinstance(profile_record["tenant_id"], str)
                or re.fullmatch(r"tenant-[a-z0-9]+", profile_record["tenant_id"]) is None
                or not isinstance(profile_record["subject_id"], str)
                or re.fullmatch(r"serviceaccount-[a-z0-9]+", profile_record["subject_id"]) is None
                or not isinstance(profile_record["operators"], list)
            ):
                fail("Nebius auth authority contains a malformed profile binding")
            profile_names.add(profile_name)
            operator_keys: set[tuple[int, int, str]] = set()
            for operator_index, raw_operator in enumerate(profile_record["operators"]):
                operator = exact_object(
                    raw_operator,
                    {"gid", "identity", "uid"},
                    f"Nebius auth authority {index} profile {profile_index} operator {operator_index}",
                )
                operator_key = (operator["uid"], operator["gid"], operator["identity"])
                if (
                    isinstance(operator["uid"], bool)
                    or not isinstance(operator["uid"], int)
                    or operator["uid"] < 1
                    or isinstance(operator["gid"], bool)
                    or not isinstance(operator["gid"], int)
                    or operator["gid"] < 1
                    or not isinstance(operator["identity"], str)
                    or re.fullmatch(r"[a-z][a-z0-9._-]{2,127}", operator["identity"]) is None
                    or operator_key in operator_keys
                ):
                    fail("Nebius auth profile contains a malformed or duplicate operator")
                operator_keys.add(operator_key)
            if (
                identity == (authority_id, key_id)
                and profile_name == profile
                and (caller_uid, caller_real_gid, operator_identity) in operator_keys
            ):
                matches.append((public_key, record, profile_record))
    if len(matches) != 1:
        fail("Nebius auth envelope is not bound to one enrolled profile authority")
    return matches[0]


def validate_brokered_auth(
    *,
    envelope_raw: bytes,
    token_fd: int,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    trust_raw: bytes,
    profile: str,
    request_nonce: str,
    peer_runtime: tuple[int, int, int, str, str] | None,
    paths: Mapping[str, str],
    inherited_fds: tuple[int, ...],
) -> dict[str, Any]:
    try:
        decoded = json.loads(envelope_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapsuleError("Nebius auth envelope is not UTF-8 JSON") from exc
    if canonical_bytes(decoded) + b"\n" != envelope_raw:
        fail("Nebius auth envelope must be canonical JSON plus one newline")
    envelope = exact_object(decoded, {"payload", "signature"}, "Nebius auth envelope")
    payload = exact_object(
        envelope["payload"],
        {
            "accepted_commit",
            "accepted_tree",
            "audience",
            "authority_id",
            "broker_config_sha256",
            "broker_executable_sha256",
            "caller_effective_gid",
            "caller_real_gid",
            "caller_uid",
            "endpoint",
            "expires_at",
            "issued_at",
            "key_id",
            "manifest_sha256",
            "operator_identity",
            "peer_credential_mode",
            "peer_observed_caller_gid",
            "peer_observed_caller_uid",
            "profile",
            "project_id",
            "request_nonce",
            "schema",
            "subject_id",
            "tenant_id",
            "token_sha256",
        },
        "Nebius auth envelope payload",
    )
    token = read_sealed_secret(token_fd, "brokered Nebius IAM token", MAX_AUTH_TOKEN_BYTES)
    try:
        token_text = token.decode("ascii")
    except UnicodeDecodeError as exc:
        raise CapsuleError("brokered Nebius IAM token is not ASCII") from exc
    if not token_text or any(character.isspace() for character in token_text):
        fail("brokered Nebius IAM token contains whitespace or is empty")
    contract = auth_contract(manifest)
    now = int(time.time())
    if (
        payload["schema"] != AUTH_ENVELOPE_SCHEMA
        or payload["accepted_commit"] != manifest["accepted_commit"]
        or payload["accepted_tree"] != manifest["accepted_tree"]
        or payload["manifest_sha256"] != manifest_sha256
        or payload["audience"] != contract["audience"]
        or payload["endpoint"] != "api.nebius.cloud"
        or payload["profile"] != profile
        or payload["request_nonce"] != request_nonce
        or payload["caller_uid"] != os.getuid()
        or payload["caller_real_gid"] != os.getgid()
        or payload["caller_effective_gid"] != os.getegid()
        or payload["peer_observed_caller_uid"] != os.getuid()
        or payload["peer_observed_caller_gid"] != os.getegid()
        or not isinstance(payload["operator_identity"], str)
        or re.fullmatch(r"[a-z][a-z0-9._-]{2,127}", payload["operator_identity"])
        is None
        or not isinstance(payload["issued_at"], int)
        or not isinstance(payload["expires_at"], int)
        or payload["issued_at"] > now + 30
        or payload["expires_at"] - payload["issued_at"]
        > contract["maximum_token_ttl_seconds"]
        or payload["expires_at"] - now < contract["minimum_remaining_seconds"]
        or payload["token_sha256"] != hashlib.sha256(token).hexdigest()
        or not isinstance(payload["authority_id"], str)
        or not isinstance(payload["key_id"], str)
    ):
        fail("Nebius auth envelope does not bind the requested accepted capsule")
    public_key, authority, profile_authority = auth_authority(
        trust_raw,
        profile=profile,
        authority_id=payload["authority_id"],
        key_id=payload["key_id"],
        caller_uid=payload["caller_uid"],
        caller_real_gid=payload["caller_real_gid"],
        operator_identity=payload["operator_identity"],
    )
    if peer_runtime is not None:
        peer_pid, peer_uid, peer_gid, peer_executable_sha256, peer_config_sha256 = peer_runtime
        if (
            peer_pid <= 1
            or peer_uid != authority["peer_uid"]
            or peer_gid != authority["peer_gid"]
            or peer_executable_sha256 != authority["broker_executable_sha256"]
            or peer_config_sha256 != authority["broker_config_sha256"]
        ):
            fail("Nebius auth broker runtime differs from its enrolled authority")
    if (
        payload["endpoint"] != authority["endpoint"]
        or payload["broker_executable_sha256"] != authority["broker_executable_sha256"]
        or payload["broker_config_sha256"] != authority["broker_config_sha256"]
        or payload["peer_credential_mode"] != authority["peer_credential_mode"]
        or payload["subject_id"] != profile_authority["subject_id"]
        or payload["project_id"] != profile_authority["project_id"]
        or payload["tenant_id"] != profile_authority["tenant_id"]
    ):
        fail("Nebius auth envelope differs from its exact broker/profile authority")
    verify_ed25519(
        public_key,
        b64url(envelope["signature"], 64, "Nebius auth envelope signature"),
        canonical_bytes(payload),
        openssl_path=paths["openssl"],
        inherited_fds=inherited_fds,
    )
    os.set_inheritable(token_fd, True)
    return payload


def receive_token_descriptor(sock: socket.socket) -> tuple[bytes, int]:
    ancillary_size = socket.CMSG_SPACE(array.array("i", [0]).itemsize)
    response, ancillary, flags, _address = sock.recvmsg(
        MAX_AUTH_ENVELOPE_BYTES + 1, ancillary_size
    )
    if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or len(response) > MAX_AUTH_ENVELOPE_BYTES:
        fail("Nebius auth broker response is truncated or oversized")
    descriptors: list[int] = []
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            values = array.array("i")
            values.frombytes(data[: len(data) - (len(data) % values.itemsize)])
            descriptors.extend(values.tolist())
    if len(descriptors) != 1:
        for descriptor in descriptors:
            os.close(descriptor)
        fail("Nebius auth broker must return exactly one token descriptor")
    return response, descriptors[0]


def request_brokered_auth(
    *,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    trust_raw: bytes,
    profile: str,
    capsule_gid: int,
    paths: Mapping[str, str],
    inherited_fds: tuple[int, ...],
) -> tuple[int, bytes, dict[str, Any]]:
    for parent in (Path("/run"), AUTH_BROKER_SOCKET.parent):
        details = os.stat(parent, follow_symlinks=False)
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != 0
            or stat.S_IMODE(details.st_mode) & 0o022
        ):
            fail("Nebius auth broker parent authority is not root-owned and protected")
    details = os.stat(AUTH_BROKER_SOCKET, follow_symlinks=False)
    if (
        not stat.S_ISSOCK(details.st_mode)
        or details.st_uid != 0
        or details.st_gid != capsule_gid
        or stat.S_IMODE(details.st_mode) != 0o660
    ):
        fail("Nebius auth broker socket is not the protected fixed authority")
    nonce = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode("ascii")
    request = {
        "accepted_commit": manifest["accepted_commit"],
        "accepted_tree": manifest["accepted_tree"],
        "audience": AUTH_AUDIENCE,
        "caller_effective_gid": os.getegid(),
        "caller_real_gid": os.getgid(),
        "caller_uid": os.getuid(),
        "manifest_sha256": manifest_sha256,
        "profile": profile,
        "request_nonce": nonce,
        "schema": "fs2-serve.nebius.ai/short-lived-nebius-auth-request/v2",
    }
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as broker:
        broker.connect(str(AUTH_BROKER_SOCKET))
        peer_pid, peer_uid, peer_gid = struct.unpack(
            "3i", broker.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        )
        if peer_pid <= 1 or peer_uid != 0:
            fail("Nebius auth broker peer is not root-owned")
        # An ordinary setgid caller cannot portably dereference /proc/PID/exe
        # for a root broker. Hash the fixed root-owned executable instead; the
        # enrolled nonzero peer-runtime review binds the unit's exact ExecStart
        # path to SO_PEERCRED pid/uid/gid and is an integration prerequisite.
        executable_fd = os.open(
            AUTH_BROKER_EXECUTABLE,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            executable_details = os.fstat(executable_fd)
            if not stat.S_ISREG(executable_details.st_mode) or executable_details.st_uid != 0:
                fail("Nebius auth broker executable is not root-owned")
            peer_executable_sha256 = hashlib.sha256(
                read_inherited_file(
                    executable_fd,
                    label="Nebius auth broker executable",
                    owner_uid=0,
                    owner_gid=executable_details.st_gid,
                    exact_mode=stat.S_IMODE(executable_details.st_mode),
                    maximum_bytes=MAX_PINNED_FILE_BYTES,
                )
            ).hexdigest()
        finally:
            os.close(executable_fd)
        config_fd = os.open(
            AUTH_BROKER_CONFIG,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            config_details = os.fstat(config_fd)
            if (
                not stat.S_ISREG(config_details.st_mode)
                or config_details.st_uid != 0
                or stat.S_IMODE(config_details.st_mode) != 0o440
            ):
                fail("Nebius auth broker config is not root-owned mode 0440")
            peer_config_sha256 = hashlib.sha256(
                read_inherited_file(
                    config_fd,
                    label="Nebius auth broker config",
                    owner_uid=0,
                    owner_gid=config_details.st_gid,
                    exact_mode=0o440,
                    maximum_bytes=MAX_MANIFEST_BYTES,
                )
            ).hexdigest()
        finally:
            os.close(config_fd)
        broker.sendall(canonical_bytes(request) + b"\n")
        envelope_raw, token_fd = receive_token_descriptor(broker)
    try:
        payload = validate_brokered_auth(
            envelope_raw=envelope_raw,
            token_fd=token_fd,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            trust_raw=trust_raw,
            profile=profile,
            request_nonce=nonce,
            peer_runtime=(
                peer_pid,
                peer_uid,
                peer_gid,
                peer_executable_sha256,
                peer_config_sha256,
            ),
            paths=paths,
            inherited_fds=inherited_fds,
        )
        return token_fd, envelope_raw, payload
    except BaseException:
        os.close(token_fd)
        raise


def send_brokered_auth(sock: socket.socket, envelope_raw: bytes, token_fd: int) -> None:
    descriptor = array.array("i", [token_fd])
    sent = sock.sendmsg(
        [envelope_raw],
        [(socket.SOL_SOCKET, socket.SCM_RIGHTS, descriptor.tobytes())],
    )
    if sent != len(envelope_raw):
        fail("Nebius auth refresh agent could not return one complete lease")


def serve_auth_refresh(
    sock: socket.socket,
    *,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    trust_raw: bytes,
    profile: str,
    capsule_gid: int,
    paths: Mapping[str, str],
    inherited_fds: tuple[int, ...],
) -> None:
    """Issue a fresh verified lease only to the accepted operator child."""

    while True:
        request = sock.recv(1)
        if request == b"":
            return
        if request != b"\x01":
            fail("Nebius auth refresh request is malformed")
        token_fd, envelope_raw, _payload = request_brokered_auth(
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            trust_raw=trust_raw,
            profile=profile,
            capsule_gid=capsule_gid,
            paths=paths,
            inherited_fds=inherited_fds,
        )
        try:
            send_brokered_auth(sock, envelope_raw, token_fd)
        finally:
            os.close(token_fd)


def delegated_brokered_auth(
    *,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    trust_raw: bytes,
    paths: Mapping[str, str],
    inherited_fds: tuple[int, ...],
) -> tuple[int, bytes, dict[str, Any]]:
    token_fd = inherited_fd("FS2_CAPSULE_DELEGATED_AUTH_FD")
    raw = os.environ.get("FS2_CAPSULE_DELEGATED_AUTH_ENVELOPE", "")
    try:
        envelope_raw = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (ValueError, binascii.Error) as exc:
        raise CapsuleError("delegated Nebius auth envelope is malformed") from exc
    try:
        decoded = json.loads(envelope_raw.decode("utf-8"))
        profile = decoded["payload"]["profile"]
        nonce = decoded["payload"]["request_nonce"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise CapsuleError("delegated Nebius auth envelope cannot be decoded") from exc
    payload = validate_brokered_auth(
        envelope_raw=envelope_raw,
        token_fd=token_fd,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        trust_raw=trust_raw,
        profile=profile,
        request_nonce=nonce,
        # The accepted parent already performed SO_PEERCRED plus executable and
        # config hashing before delegating this signed, exact lease.
        peer_runtime=None,
        paths=paths,
        inherited_fds=inherited_fds,
    )
    return token_fd, envelope_raw, payload


def pin_capsule(
    manifest: Mapping[str, Any],
    capsule_gid: int,
    *,
    verified_frozen_python_sha256: str,
) -> tuple[dict[str, str], tuple[int, ...]]:
    source_root_text = manifest["installed_source_root"]
    if not isinstance(source_root_text, str) or not source_root_text.startswith("/opt/fs2/"):
        fail("accepted source root must be under /opt/fs2")
    source_root = Path(source_root_text)
    root_details = os.stat(source_root, follow_symlinks=False)
    if (
        not stat.S_ISDIR(root_details.st_mode)
        or root_details.st_uid != 0
        or root_details.st_gid not in {0, capsule_gid}
        or stat.S_IMODE(root_details.st_mode) & 0o022
    ):
        fail("accepted source root is not root-owned and protected")

    release_files = manifest["release_files"]
    if not isinstance(release_files, list) or not release_files:
        fail("accepted capsule release file inventory must be non-empty")
    expected_files: list[str] = []
    release_records: dict[str, Mapping[str, Any]] = {}
    for index, record in enumerate(release_files):
        entry = exact_object(
            record, {"relative_path", "sha256"}, f"release file {index}"
        )
        relative = entry["relative_path"]
        if not isinstance(relative, str):
            fail("release file path must be a string")
        expected_files.append(relative)
        release_records[relative] = entry
        descriptor, _path = pin_manifest_file(
            source_root, entry, capsule_gid, f"release file {index}"
        )
        os.close(descriptor)
    if expected_files != sorted(set(expected_files)):
        fail("accepted release file inventory must be sorted and unique")
    observed_files: list[str] = []
    for directory, directories, files in os.walk(source_root, followlinks=False):
        directory_path = Path(directory)
        for name in directories:
            candidate = directory_path / name
            if candidate.is_symlink():
                fail("accepted release tree cannot contain a directory symlink")
        for name in files:
            candidate = directory_path / name
            if candidate.is_symlink() or not candidate.is_file():
                fail("accepted release tree may contain only regular files")
            observed_files.append(candidate.relative_to(source_root).as_posix())
    if sorted(observed_files) != expected_files:
        fail("installed release file set differs from the accepted manifest")

    source_records = exact_object(manifest["sources"], SOURCE_IDS, "capsule sources")
    tool_records = exact_object(manifest["tools"], REQUIRED_TOOLS, "capsule tools")
    descriptors: list[int] = []
    paths: dict[str, str] = {}
    for name in sorted(SOURCE_IDS):
        record = source_records[name]
        if (
            not isinstance(record, Mapping)
            or record.get("relative_path") != SOURCE_PATHS[name]
        ):
            fail(f"source {name} is not mapped to its canonical release path")
        descriptor, path = pin_manifest_file(
            source_root, record, capsule_gid, f"source {name}"
        )
        descriptors.append(descriptor)
        paths[f"source:{name}"] = path
    for name in sorted(REQUIRED_TOOLS):
        descriptor, path = pin_manifest_file(
            source_root, tool_records[name], capsule_gid, f"tool {name}"
        )
        tool_bytes = os.pread(descriptor, os.fstat(descriptor).st_size, 0)
        if name == "python3":
            # The fixed C launcher has already applied the complete
            # fs2-frozen-runtime.h proof before this Python instruction could
            # execute. Rejoin the manifest tool record to those exact verified
            # bytes instead of incorrectly applying the ET_EXEC-only helper to
            # the required ET_DYN static PIE.
            if hashlib.sha256(tool_bytes).hexdigest() != verified_frozen_python_sha256:
                fail("capsule Python tool differs from the launcher-verified frozen runtime")
        else:
            # Whole-file hashes do not close an ELF runtime whose PT_INTERP or
            # DT_NEEDED entries can load mutable host /lib bytes. Every other
            # accepted operator tool is therefore a genuine static ELF.
            require_static_elf(tool_bytes, f"capsule tool {name}")
        descriptors.append(descriptor)
        paths[name] = path

    authentication = auth_contract(manifest)
    auth_trust_fd, auth_trust_path = pin_manifest_file(
        source_root,
        authentication["trust_store"],
        capsule_gid,
        "Nebius auth authority registry",
    )
    descriptors.append(auth_trust_fd)
    paths["nebius_auth_trust"] = auth_trust_path
    settlement_trust_record = release_records.get(MUTATION_SETTLEMENT_TRUST_PATH)
    if settlement_trust_record is None:
        fail("accepted release omits the mutation-settlement trust registry")
    settlement_trust_fd, settlement_trust_path = pin_manifest_file(
        source_root,
        settlement_trust_record,
        capsule_gid,
        "mutation-settlement authority registry",
    )
    descriptors.append(settlement_trust_fd)
    paths["mutation_settlement_trust"] = settlement_trust_path
    debug_trust_record = release_records.get(INTERNAL_DEBUG_ACTIVATION_TRUST_PATH)
    if debug_trust_record is None:
        fail("accepted release omits the internal-debug activation issuer registry")
    debug_trust_fd, debug_trust_path = pin_manifest_file(
        source_root,
        debug_trust_record,
        capsule_gid,
        "internal-debug activation issuer registry",
    )
    descriptors.append(debug_trust_fd)
    paths["internal_debug_activation_trust"] = debug_trust_path

    terraform = exact_object(
        manifest["terraform"],
        {
            "provider_files",
            "provider_mirror_relative_path",
            "provider_overrides",
            "tool_bin_relative_path",
        },
        "capsule Terraform contract",
    )
    provider_files = terraform["provider_files"]
    if not isinstance(provider_files, list) or not provider_files:
        fail("capsule Terraform provider set must be non-empty")
    for index, record in enumerate(provider_files):
        descriptor, path = pin_manifest_file(
            source_root,
            record,
            capsule_gid,
            f"Terraform provider file {index}",
        )
        require_static_elf(
            os.pread(descriptor, os.fstat(descriptor).st_size, 0),
            f"Terraform provider file {index}",
        )
        descriptors.append(descriptor)
        paths[f"provider:{index}"] = path
    mirror_relative = terraform["provider_mirror_relative_path"]
    if (
        not isinstance(mirror_relative, str)
        or not mirror_relative
        or mirror_relative.startswith("/")
        or ".." in Path(mirror_relative).parts
    ):
        fail("Terraform provider mirror path is unsafe")
    mirror_path = source_root / mirror_relative
    protected_parent_chain(
        mirror_path / "sentinel", source_root, capsule_gid, "provider mirror"
    )
    mirror_details = os.stat(mirror_path, follow_symlinks=False)
    if (
        not stat.S_ISDIR(mirror_details.st_mode)
        or mirror_details.st_uid != 0
        or stat.S_IMODE(mirror_details.st_mode) & 0o022
    ):
        fail("Terraform provider mirror is not root-owned and protected")
    overrides = exact_object(
        terraform["provider_overrides"],
        REQUIRED_PROVIDER_ADDRESSES,
        "Terraform provider overrides",
    )
    override_lines: list[str] = []
    for address in sorted(REQUIRED_PROVIDER_ADDRESSES):
        relative = overrides[address]
        if (
            not isinstance(relative, str)
            or not relative
            or relative.startswith("/")
            or ".." in Path(relative).parts
        ):
            fail("Terraform provider override path is unsafe")
        override_path = source_root / relative
        protected_parent_chain(
            override_path / "sentinel",
            source_root,
            capsule_gid,
            f"provider override {address}",
        )
        details = os.stat(override_path, follow_symlinks=False)
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != 0
            or stat.S_IMODE(details.st_mode) & 0o022
        ):
            fail("Terraform provider override is not root-owned and protected")
        override_lines.append(
            f"    {json.dumps(address)} = {json.dumps(str(override_path))}"
        )
    cli_config_text = "\n".join(
        [
            "provider_installation {",
            "  dev_overrides {",
            *override_lines,
            "  }",
            "  filesystem_mirror {",
            f"    path = {json.dumps(str(mirror_path))}",
            '    include = ["*/*", "*/*/*"]',
            "  }",
            "}",
            "",
        ]
    )
    if "direct" in cli_config_text or "network_mirror" in cli_config_text:
        fail("generated Terraform CLI configuration enabled network discovery")
    cli_config = cli_config_text.encode("utf-8")
    cli_config_fd = sealed_memfd("fs2-terraform-cli-config", cli_config)
    descriptors.append(cli_config_fd)
    paths["terraform_cli_config"] = f"/proc/self/fd/{cli_config_fd}"
    tool_bin_relative = terraform["tool_bin_relative_path"]
    if (
        not isinstance(tool_bin_relative, str)
        or not tool_bin_relative
        or tool_bin_relative.startswith("/")
        or ".." in Path(tool_bin_relative).parts
    ):
        fail("capsule tool-bin path is unsafe")
    tool_bin = source_root / tool_bin_relative
    protected_parent_chain(tool_bin / "sentinel", source_root, capsule_gid, "tool bin")
    details = os.stat(tool_bin, follow_symlinks=False)
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != 0 or stat.S_IMODE(details.st_mode) & 0o022:
        fail("capsule tool bin is not root-owned and protected")
    for name in REQUIRED_TOOLS:
        record = tool_records[name]
        if Path(record["relative_path"]).parent != Path(tool_bin_relative):
            fail(f"tool {name} is outside the accepted tool bin")
    paths["tool_bin"] = str(tool_bin)
    return paths, tuple(sorted(descriptors))


def main() -> int:
    _real_gid, capsule_gid = require_capsule_process()
    manifest_fd = inherited_fd("FS2_CAPSULE_MANIFEST_FD")
    python_fd = inherited_fd("FS2_CAPSULE_PYTHON_FD")
    launcher_fd = inherited_fd("FS2_CAPSULE_LAUNCHER_FD")
    bootstrap_fd = inherited_fd("FS2_CAPSULE_BOOTSTRAP_FD")
    manifest_raw = read_inherited_file(
        manifest_fd,
        label="accepted capsule manifest",
        owner_uid=0,
        owner_gid=capsule_gid,
        exact_mode=0o440,
        maximum_bytes=MAX_MANIFEST_BYTES,
    )
    manifest = decode_canonical_manifest(manifest_raw)
    if manifest["capsule_group"] != os.environ.get("FS2_CAPSULE_GROUP"):
        fail("accepted capsule group differs from the launcher identity")
    launcher_bytes = read_inherited_file(
        launcher_fd,
        label="capsule launcher",
        owner_uid=0,
        owner_gid=capsule_gid,
        exact_mode=0o2755,
        maximum_bytes=MAX_PINNED_FILE_BYTES,
    )
    bootstrap_bytes = read_inherited_file(
        bootstrap_fd,
        label="capsule bootstrap",
        owner_uid=0,
        owner_gid=capsule_gid,
        exact_mode=0o440,
        maximum_bytes=MAX_PINNED_FILE_BYTES,
    )
    if hashlib.sha256(launcher_bytes).hexdigest() != manifest["launcher_sha256"]:
        fail("running launcher differs from the accepted manifest")
    if hashlib.sha256(bootstrap_bytes).hexdigest() != manifest["bootstrap_sha256"]:
        fail("running bootstrap differs from the accepted manifest")
    python_bytes = read_inherited_file(
        python_fd,
        label="capsule Python interpreter",
        owner_uid=0,
        owner_gid=capsule_gid,
        exact_mode=0o550,
        maximum_bytes=MAX_PINNED_FILE_BYTES,
    )
    tool_records = manifest["tools"]
    if not isinstance(tool_records, Mapping):
        fail("capsule tool records are malformed")
    python_record = exact_object(
        tool_records.get("python3"), {"relative_path", "sha256"}, "Python tool"
    )
    if hashlib.sha256(python_bytes).hexdigest() != lower_digest(
        python_record["sha256"], "accepted Python digest"
    ):
        fail("running Python interpreter differs from the accepted tool digest")
    if len(sys.argv) < 3:
        fail("capsule bootstrap requires a logical source and mode")
    source_id, mode, *arguments = sys.argv[1:]
    if source_id not in SOURCE_IDS or mode not in SOURCE_MODES[source_id]:
        fail("capsule bootstrap source/mode pair is unsupported")
    manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
    paths, pinned_fds = pin_capsule(
        manifest,
        capsule_gid,
        verified_frozen_python_sha256=hashlib.sha256(python_bytes).hexdigest(),
    )
    initial_pass_fds = tuple(
        sorted({manifest_fd, python_fd, launcher_fd, bootstrap_fd, *pinned_fds})
    )
    auth_trust_fd = int(paths["nebius_auth_trust"].rsplit("/", 1)[1])
    auth_trust_raw = os.pread(
        auth_trust_fd, os.fstat(auth_trust_fd).st_size, 0
    )
    read_only_operator = (
        source_id == "inference-stack"
        and bool(arguments)
        and arguments[0]
        in {
            "status",
            "output",
            "proxy",
            "debug-proxy",
            "debug-view",
            "debug-export",
            "activate-debug",
            "disable-debug",
        }
    )
    if source_id == "inference-stack":
        secret_broker_fd = inherited_fd("FS2_CAPSULE_SECRET_BROKER_FD")
        if read_only_operator:
            token_fd = -1
            refresh_fd = -1
            auth_envelope_raw = b""
            auth_payload = {}
            extra_fds = {secret_broker_fd}
        else:
            operator_profile = requested_nebius_profile(arguments)
            token_fd, auth_envelope_raw, auth_payload = request_brokered_auth(
                manifest=manifest,
                manifest_sha256=manifest_sha256,
                trust_raw=auth_trust_raw,
                profile=operator_profile,
                capsule_gid=capsule_gid,
                paths=paths,
                inherited_fds=initial_pass_fds,
            )
            refresh_parent, refresh_child = socket.socketpair(
                socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC
            )
            refresh_pid = os.fork()
            if refresh_pid < 0:
                fail("cannot start the accepted Nebius auth refresh agent")
            if refresh_pid > 0:
                refresh_child.close()
                try:
                    serve_auth_refresh(
                        refresh_parent,
                        manifest=manifest,
                        manifest_sha256=manifest_sha256,
                        trust_raw=auth_trust_raw,
                        profile=operator_profile,
                        capsule_gid=capsule_gid,
                        paths=paths,
                        inherited_fds=initial_pass_fds,
                    )
                finally:
                    refresh_parent.close()
                _waited, status = os.waitpid(refresh_pid, 0)
                os._exit(os.waitstatus_to_exitcode(status))
            refresh_parent.close()
            refresh_fd = refresh_child.detach()
            os.set_inheritable(refresh_fd, True)
            extra_fds = {secret_broker_fd, refresh_fd}
    else:
        helper_profile = os.environ.get("FS2_CAPSULE_NEBIUS_PROFILE", "")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", helper_profile) is None:
            fail("nested capsule source lacks an exact signed Nebius profile selector")
        # Terraform intentionally does not forward arbitrary inherited file
        # descriptors to providers/local-exec children. Every nested capsule
        # therefore obtains a new short-lived token directly from the same
        # enrolled broker instead of trusting a replayed ambient credential.
        token_fd, auth_envelope_raw, auth_payload = request_brokered_auth(
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            trust_raw=auth_trust_raw,
            profile=helper_profile,
            capsule_gid=capsule_gid,
            paths=paths,
            inherited_fds=initial_pass_fds,
        )
        extra_fds = set()
    if token_fd >= 0:
        paths["nebius_token"] = f"/proc/self/fd/{token_fd}"
    pass_fds = tuple(
        sorted({*initial_pass_fds, *({token_fd} if token_fd >= 0 else set()), *extra_fds})
    )
    source_fd_path = paths[f"source:{source_id}"]
    source_fd = int(source_fd_path.rsplit("/", 1)[1])
    source_bytes = read_inherited_file(
        source_fd,
        label=f"accepted source {source_id}",
        owner_uid=0,
        owner_gid=os.fstat(source_fd).st_gid,
        exact_mode=stat.S_IMODE(os.fstat(source_fd).st_mode),
        maximum_bytes=MAX_PINNED_FILE_BYTES,
    )

    os.environ["FS2_CAPSULE_MANIFEST_SHA256"] = manifest_sha256
    os.environ["FS2_CAPSULE_ACCEPTED_COMMIT"] = manifest["accepted_commit"]
    os.environ["FS2_CAPSULE_ACCEPTED_TREE"] = manifest["accepted_tree"]
    os.environ["FS2_CAPSULE_SOURCE_ROOT"] = manifest["installed_source_root"]
    os.environ["FS2_CAPSULE_SOURCE_ID"] = source_id
    os.environ["FS2_CAPSULE_SOURCE_SHA256"] = hashlib.sha256(source_bytes).hexdigest()
    os.environ["FS2_CAPSULE_TOOL_PATHS_JSON"] = json.dumps(paths, sort_keys=True)
    os.environ["FS2_CAPSULE_PASS_FDS"] = ",".join(str(item) for item in pass_fds)
    os.environ["FS2_CAPSULE_ACCESS_MODE"] = (
        "local-read-only" if read_only_operator else "brokered-cloud"
    )
    if token_fd >= 0:
        os.environ["FS2_CAPSULE_NEBIUS_AUTH_JSON"] = json.dumps(
            auth_payload, sort_keys=True, separators=(",", ":")
        )
        os.environ["FS2_CAPSULE_DELEGATED_AUTH_FD"] = str(token_fd)
        os.environ["FS2_CAPSULE_DELEGATED_AUTH_ENVELOPE"] = (
            base64.urlsafe_b64encode(auth_envelope_raw).rstrip(b"=").decode("ascii")
        )
    if source_id == "inference-stack":
        if refresh_fd >= 0:
            os.environ["FS2_CAPSULE_AUTH_REFRESH_FD"] = str(refresh_fd)
    os.environ["TF_CLI_CONFIG_FILE"] = paths["terraform_cli_config"]
    os.environ["FS2_CAPSULE_TOOL_BIN"] = paths["tool_bin"]
    os.environ["PATH"] = paths["tool_bin"]

    if source_id == "inference-stack":
        sys.argv = [str(Path(manifest["installed_source_root"]) / "inference-stack"), *arguments]
    elif source_id == "public-edge-verifier" and mode == "local-exec":
        sys.argv = [source_id]
    elif source_id == "public-edge-verifier":
        sys.argv = [source_id, f"--{mode}"]
    elif source_id == "edge-client-identity-verifier":
        sys.argv = [source_id]
    else:
        os.environ["NEBIUS_IAM_TOKEN"] = read_sealed_secret(
            token_fd, "brokered Nebius IAM token", MAX_AUTH_TOKEN_BYTES
        ).decode("ascii")
        os.execve(
            paths["bash"],
            [paths["bash"], source_fd_path],
            dict(os.environ),
        )
        fail("accepted shell source could not be executed")
    scope = {
        "__name__": "__main__",
        "__file__": str(Path(manifest["installed_source_root"]) / source_records_path(manifest, source_id)),
        "__package__": None,
    }
    exec(
        compile(
            source_bytes,
            f"capsule:{manifest['accepted_commit']}:{source_id}",
            "exec",
        ),
        scope,
        scope,
    )
    return 0


def source_records_path(manifest: Mapping[str, Any], source_id: str) -> str:
    sources = manifest["sources"]
    if not isinstance(sources, Mapping):
        fail("capsule sources are malformed")
    record = sources[source_id]
    if not isinstance(record, Mapping) or not isinstance(record.get("relative_path"), str):
        fail("capsule source record is malformed")
    return record["relative_path"]


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CapsuleError as exc:
        print(f"public-edge capsule: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
