#!/usr/bin/env python3
"""Fail closed unless the public edge still has provider-owned HA placement.

This creation-time Terraform gate deliberately performs fresh read-only cloud
and Kubernetes observations during *apply*.  A saved plan therefore cannot
authorize edge Pods from its older data-source snapshot.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


MEMBERSHIP_RECEIPT_SCHEMA = (
    "fs2-serve.nebius.ai/public-edge-membership-receipt/v3"
)
MEMBERSHIP_PAYLOAD_SCHEMA = (
    "fs2-serve.nebius.ai/public-edge-membership-evidence/v3"
)
MEMBERSHIP_SUBJECT_SCHEMA = (
    "fs2-serve.nebius.ai/public-edge-membership-terraform-subject/v3"
)
MEMBERSHIP_TRUST_SCHEMA = (
    "fs2-serve.nebius.ai/trusted-public-edge-membership-issuers/v3"
)
PROVIDER_ADAPTER_TRUST_SCHEMA = (
    "fs2-serve.nebius.ai/trusted-public-edge-provider-adapters/v1"
)
PREVENTIVE_BOUNDARY_TRUST_SCHEMA = (
    "fs2-serve.nebius.ai/trusted-public-edge-preventive-boundary-issuers/v2"
)
PREVENTIVE_BOUNDARY_RECEIPT_SCHEMA = (
    "fs2-serve.nebius.ai/public-edge-preventive-boundary-receipt/v1"
)
PREVENTIVE_BOUNDARY_PAYLOAD_SCHEMA = (
    "fs2-serve.nebius.ai/public-edge-preventive-boundary-evidence/v3"
)
MEMBERSHIP_ISSUER_ROLE = "platform-security-public-edge-membership"
PREVENTIVE_BOUNDARY_ISSUER_ROLE = "platform-security-public-edge-preventive-boundary"
MEMBERSHIP_RECEIPT_FILENAME = "public-edge-node-group-membership-receipt.json"
MEMBERSHIP_EVIDENCE_FILENAME = "public-edge-provider-membership.json"
MEMBERSHIP_TRUST_STORE = (
    Path(__file__).resolve().parents[1]
    / "trusted-public-edge-membership-issuers.json"
)
PROVIDER_ADAPTER_TRUST_STORE = (
    Path(__file__).resolve().parents[1]
    / "trusted-public-edge-provider-adapters.json"
)
PREVENTIVE_BOUNDARY_TRUST_STORE = (
    Path(__file__).resolve().parents[1]
    / "trusted-public-edge-preventive-boundary-issuers.json"
)
PREVENTIVE_BOUNDARY_RECEIPT_FILENAME = (
    "public-edge-preventive-boundary-receipt.json"
)
PREVENTIVE_BOUNDARY_EVIDENCE_FILENAME = (
    "public-edge-preventive-boundary-evidence.json"
)
PREVENTIVE_PROVIDER_IAM_EXPORT_FILENAME = (
    "public-edge-preventive-provider-iam-export.json"
)
PREVENTIVE_APISERVER_EXPORT_FILENAME = (
    "public-edge-preventive-apiserver-enforcement-export.json"
)
PREVENTIVE_IDENTITY_REVIEW_FILENAME = (
    "public-edge-preventive-identity-path-review.json"
)
PREVENTIVE_CA_HISTORY_FILENAME = (
    "public-edge-preventive-certificate-authority-history.json"
)
MAX_RECEIPT_BYTES = 256 * 1024
# Signed receipts and projections remain deliberately small.  Native exports
# are separately bounded because the identity inventory contains complete,
# paginated cluster-wide objects and cannot fit the receipt ceiling on a real
# cluster.  These caps still bound memory/JSON work before signature and
# semantic validation; collectors must split or reject inventories above them.
MAX_PROVIDER_IAM_EXPORT_BYTES = 16 * 1024 * 1024
MAX_APISERVER_EXPORT_BYTES = 8 * 1024 * 1024
MAX_IDENTITY_EXPORT_BYTES = 128 * 1024 * 1024
MAX_CA_EXPORT_BYTES = 32 * 1024 * 1024
MAX_MEMBERSHIP_VALIDITY = timedelta(hours=24)
MAX_CLOCK_SKEW = timedelta(minutes=5)
MAX_PREVENTIVE_SNAPSHOT_AGE = timedelta(minutes=5)
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
KEY_ID_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
EXTERNAL_QUERY: Mapping[str, Any] | None = None
PINNED_COMMAND_FDS: tuple[int, ...] = ()
CAPSULE_COMMAND_FDS: tuple[int, ...] = ()
CAPSULE_TOOL_PATHS: dict[str, str] = {}
CAPSULE_TOOL_BIN = ""
CAPSULE_NEBIUS_TOKEN = ""


class GateError(RuntimeError):
    """A public-edge placement invariant is not currently proven."""


def fail(message: str) -> None:
    raise GateError(message)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def terraform_json_sha256(value: object) -> str:
    """Hash the exact canonical escaping used by Terraform ``jsonencode``."""

    encoded = canonical_bytes(value).decode("ascii")
    encoded = (
        encoded.replace("<", r"\u003c")
        .replace(">", r"\u003e")
        .replace("&", r"\u0026")
    )
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def exact_object(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        fail(f"{label} must contain exactly {sorted(keys)}")
    return value


def no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail(f"duplicate JSON key in signed input: {key}")
        result[key] = value
    return result


def read_descriptor(
    descriptor: int,
    name: str,
    *,
    private: bool,
    maximum_bytes: int = MAX_RECEIPT_BYTES,
) -> bytes:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        fail(f"{name} is not a regular file")
    mode = stat.S_IMODE(before.st_mode)
    if private and mode != 0o600:
        fail(f"{name} must have mode 0600")
    if not private and mode & 0o022:
        fail(f"{name} must not be group/world writable")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(65536, maximum_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum_bytes:
            fail(f"{name} exceeds {maximum_bytes} bytes")
    after = os.fstat(descriptor)
    stable = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
        stat.S_IMODE(before.st_mode),
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
        stat.S_IMODE(after.st_mode),
    )
    if not stable or after.st_size != total:
        fail(f"{name} changed while it was being read")
    return b"".join(chunks)


def open_regular_file(
    path: Path,
    *,
    private: bool,
    maximum_bytes: int = MAX_RECEIPT_BYTES,
) -> bytes:
    if not path.is_absolute():
        fail("membership input paths must be absolute")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise GateError(f"cannot open required membership input {path.name}") from exc
    try:
        return read_descriptor(
            descriptor,
            path.name,
            private=private,
            maximum_bytes=maximum_bytes,
        )
    finally:
        os.close(descriptor)


def decode_canonical_json(raw: bytes, label: str) -> Any:
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=no_duplicate_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateError(f"{label} is not canonical UTF-8 JSON") from exc
    if canonical_bytes(value) + b"\n" != raw:
        fail(f"{label} must use canonical JSON plus one newline")
    return value


def timestamp(value: object, label: str) -> datetime:
    text = string_value(value, label)
    if not text.endswith("Z"):
        fail(f"{label} must use UTC with a Z suffix")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise GateError(f"{label} is not RFC3339 UTC") from exc
    if parsed.tzinfo != timezone.utc or parsed.microsecond:
        fail(f"{label} must use whole UTC seconds")
    return parsed


def digest(value: object, label: str) -> str:
    text = string_value(value, label, SHA256_RE.pattern)
    if text == "0" * 64:
        fail(f"{label} cannot be the zero digest")
    return text


def b64url(value: object, label: str, expected_size: int) -> bytes:
    text = string_value(value, label)
    try:
        decoded = base64.b64decode(
            text + "=" * (-len(text) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, binascii.Error) as exc:
        raise GateError(f"{label} is not canonical base64url") from exc
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if len(decoded) != expected_size or canonical != text:
        fail(f"{label} has the wrong size or encoding")
    return decoded


def canonical_base64(
    value: object,
    label: str,
    *,
    minimum_size: int = 1,
    maximum_size: int = 1024 * 1024,
) -> bytes:
    text = string_value(value, label)
    try:
        decoded = base64.b64decode(text, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise GateError(f"{label} is not canonical base64") from exc
    if (
        not minimum_size <= len(decoded) <= maximum_size
        or base64.b64encode(decoded).decode("ascii") != text
    ):
        fail(f"{label} has the wrong size or encoding")
    return decoded


def canonical_pem_blocks(
    raw: bytes,
    *,
    pem_label: str,
    label: str,
    maximum_blocks: int,
) -> list[bytes]:
    """Decode an exact newline-terminated PEM sequence without ignored bytes."""

    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise GateError(f"{label} is not ASCII PEM") from exc
    if not text.endswith("\n") or "\r" in text:
        fail(f"{label} is not canonical newline-terminated PEM")
    begin = f"-----BEGIN {pem_label}-----"
    end = f"-----END {pem_label}-----"
    lines = text.splitlines()
    blocks: list[bytes] = []
    cursor = 0
    while cursor < len(lines):
        if lines[cursor] != begin:
            fail(f"{label} contains text outside its PEM blocks")
        cursor += 1
        encoded_lines: list[str] = []
        while cursor < len(lines) and lines[cursor] != end:
            line = lines[cursor]
            if not line or len(line) > 64 or re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", line) is None:
                fail(f"{label} has a malformed PEM body")
            encoded_lines.append(line)
            cursor += 1
        if cursor >= len(lines) or not encoded_lines:
            fail(f"{label} has an unterminated PEM block")
        cursor += 1
        encoded = "".join(encoded_lines)
        try:
            der = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise GateError(f"{label} PEM body is not canonical base64") from exc
        canonical_lines = [
            base64.b64encode(der).decode("ascii")[offset : offset + 64]
            for offset in range(0, len(base64.b64encode(der)), 64)
        ]
        if encoded_lines != canonical_lines or not der:
            fail(f"{label} PEM body is not canonical")
        blocks.append(der)
        if len(blocks) > maximum_blocks:
            fail(f"{label} contains too many PEM blocks")
    if not blocks:
        fail(f"{label} is empty")
    return blocks


def pem_encode_der(der: bytes, pem_label: str) -> bytes:
    encoded = base64.b64encode(der).decode("ascii")
    body = "\n".join(
        encoded[offset : offset + 64] for offset in range(0, len(encoded), 64)
    )
    return (
        f"-----BEGIN {pem_label}-----\n{body}\n"
        f"-----END {pem_label}-----\n"
    ).encode("ascii")


def openssl_verify_certificate_chain(
    *,
    leaf_der: bytes,
    intermediate_der: Sequence[bytes],
    trust_bundle_pem: bytes,
    purpose: str,
    verification_time: datetime,
) -> bytes:
    leaf_pem = pem_encode_der(leaf_der, "CERTIFICATE")
    intermediate_pem = b"".join(
        pem_encode_der(item, "CERTIFICATE") for item in intermediate_der
    )
    descriptors = [
        sealed_memfd("public-edge-leaf-certificate", leaf_pem),
        sealed_memfd("public-edge-ca-trust-bundle", trust_bundle_pem),
    ]
    if intermediate_pem:
        descriptors.append(
            sealed_memfd("public-edge-intermediate-certificates", intermediate_pem)
        )
    command = [
        openssl_binary(),
        "verify",
        "-x509_strict",
        "-purpose",
        purpose,
        "-attime",
        str(int(verification_time.timestamp())),
        "-CAfile",
        f"/proc/self/fd/{descriptors[1]}",
    ]
    if intermediate_pem:
        command.extend(("-untrusted", f"/proc/self/fd/{descriptors[2]}"))
    command.append(f"/proc/self/fd/{descriptors[0]}")
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            close_fds=True,
            pass_fds=tuple(sorted({*descriptors, *CAPSULE_COMMAND_FDS})),
            env={
                "HOME": "/nonexistent",
                "LANG": "C",
                "LC_ALL": "C",
                "OPENSSL_CONF": "/dev/null",
                "PATH": CAPSULE_TOOL_BIN or "/usr/bin:/bin",
            },
            cwd="/",
            timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        raise GateError("pinned OpenSSL chain verification timed out") from exc
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    if result.returncode or result.stdout != b"/proc/self/fd/0: OK\n":
        # OpenSSL names the descriptor path, not the certificate subject. Keep
        # the check exact without persisting stderr or customer-controlled data.
        expected = f"/proc/self/fd/{descriptors[0]}: OK\n".encode("ascii")
        if result.returncode or result.stdout != expected:
            fail("certificate chain does not verify to the enrolled trust bundle")
    return result.stdout


def openssl_binary() -> str:
    accepted = CAPSULE_TOOL_PATHS.get("openssl")
    if accepted is not None:
        return accepted
    for candidate in ("/usr/bin/openssl", "/bin/openssl"):
        try:
            details = os.stat(candidate, follow_symlinks=True)
        except OSError:
            continue
        if (
            stat.S_ISREG(details.st_mode)
            and details.st_uid == 0
            and not stat.S_IMODE(details.st_mode) & 0o022
        ):
            return candidate
    fail("a root-owned non-writable OpenSSL binary is required")


def memfd(name: str, content: bytes) -> int:
    if not hasattr(os, "memfd_create"):
        fail("anonymous in-memory file descriptors are unavailable")
    descriptor = os.memfd_create(
        name,
        os.MFD_CLOEXEC | getattr(os, "MFD_ALLOW_SEALING", 0),
    )
    os.write(descriptor, content)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return descriptor


def sealed_memfd(name: str, content: bytes) -> int:
    descriptor = memfd(name, content)
    required = (
        getattr(fcntl, "F_SEAL_SEAL", 0)
        | getattr(fcntl, "F_SEAL_SHRINK", 0)
        | getattr(fcntl, "F_SEAL_GROW", 0)
        | getattr(fcntl, "F_SEAL_WRITE", 0)
    )
    if not required or not hasattr(fcntl, "F_ADD_SEALS"):
        os.close(descriptor)
        fail("sealed anonymous file descriptors are unavailable")
    os.fchmod(descriptor, 0o400)
    try:
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, required)
    except OSError as exc:
        os.close(descriptor)
        raise GateError("cannot seal the private file snapshot") from exc
    if fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) & required != required:
        os.close(descriptor)
        fail("private file snapshot is not fully sealed")
    return descriptor


def verify_ed25519(public_key: bytes, signature: bytes, message: bytes) -> None:
    der = bytes.fromhex("302a300506032b6570032100") + public_key
    encoded = base64.b64encode(der).decode("ascii")
    pem = (
        "-----BEGIN PUBLIC KEY-----\n"
        + "\n".join(
            encoded[index : index + 64]
            for index in range(0, len(encoded), 64)
        )
        + "\n-----END PUBLIC KEY-----\n"
    ).encode("ascii")
    descriptors = [
        sealed_memfd("public-edge-membership-key", pem),
        sealed_memfd("public-edge-membership-message", message),
        sealed_memfd("public-edge-membership-signature", signature),
    ]
    result: subprocess.CompletedProcess[bytes] | None = None
    try:
        result = subprocess.run(
            [
                openssl_binary(),
                "pkeyutl",
                "-verify",
                "-pubin",
                "-inkey",
                f"/proc/self/fd/{descriptors[0]}",
                "-rawin",
                "-in",
                f"/proc/self/fd/{descriptors[1]}",
                "-sigfile",
                f"/proc/self/fd/{descriptors[2]}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
            close_fds=True,
            pass_fds=tuple(sorted({*descriptors, *CAPSULE_COMMAND_FDS})),
            env={
                "HOME": "/nonexistent",
                "OPENSSL_CONF": "/dev/null",
                "PATH": CAPSULE_TOOL_BIN or "/usr/bin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
            },
        )
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    if result is None or result.returncode != 0:
        fail("public-edge membership signature verification failed")


def openssl_der_output(
    data: bytes,
    *,
    command: str,
    arguments: Sequence[str],
    input_format: str = "DER",
) -> bytes:
    """Run the policy-pinned OpenSSL over one sealed certificate/CSR snapshot."""

    descriptor = sealed_memfd(f"public-edge-{command}", data)
    try:
        result = subprocess.run(
            [
                openssl_binary(),
                command,
                "-inform",
                input_format,
                "-in",
                f"/proc/self/fd/{descriptor}",
                *arguments,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            close_fds=True,
            pass_fds=tuple(sorted({descriptor, *CAPSULE_COMMAND_FDS})),
            env={
                "HOME": "/nonexistent",
                "LANG": "C",
                "LC_ALL": "C",
                "OPENSSL_CONF": "/dev/null",
                "PATH": CAPSULE_TOOL_BIN or "/usr/bin:/bin",
            },
            cwd="/",
            timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        raise GateError("pinned OpenSSL certificate parsing timed out") from exc
    finally:
        os.close(descriptor)
    if result.returncode or len(result.stdout) > 4 * 1024 * 1024:
        fail("pinned OpenSSL rejected the certificate-authority evidence")
    return result.stdout


def openssl_public_key_sha256(data: bytes, *, command: str, input_format: str) -> str:
    public_key_pem = openssl_der_output(
        data,
        command=command,
        input_format=input_format,
        arguments=["-pubkey", "-noout"],
    )
    public_key_der = openssl_der_output(
        public_key_pem,
        command="pkey",
        input_format="PEM",
        arguments=["-pubin", "-outform", "DER"],
    )
    return hashlib.sha256(public_key_der).hexdigest()


def openssl_name_fields(data: bytes, *, command: str, input_format: str) -> dict[str, str]:
    arguments = ["-noout", "-subject", "-nameopt", "RFC2253,utf8"]
    if command == "x509":
        arguments.extend(
            ["-issuer", "-serial", "-dates", "-dateopt", "iso_8601"]
        )
    lines = openssl_der_output(
        data,
        command=command,
        input_format=input_format,
        arguments=arguments,
    ).decode("utf-8", errors="strict").splitlines()
    fields: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if not separator or key in fields:
            fail("pinned OpenSSL returned ambiguous certificate identity output")
        fields[key] = value
    required = {"subject"}
    if command == "x509":
        required.update({"issuer", "serial", "notBefore", "notAfter"})
    if set(fields) != required:
        fail("pinned OpenSSL omitted exact certificate identity fields")
    return fields


def openssl_time(value: str, label: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise GateError(f"{label} is not pinned OpenSSL ISO-8601 output") from exc
    return parsed


def openssl_sans(data: bytes, *, command: str, input_format: str) -> dict[str, list[str]]:
    if command == "x509":
        output = openssl_der_output(
            data,
            command="x509",
            input_format=input_format,
            arguments=["-noout", "-ext", "subjectAltName"],
        ).decode("utf-8", errors="strict")
    else:
        output = openssl_der_output(
            data,
            command="req",
            input_format=input_format,
            arguments=["-noout", "-text"],
        ).decode("utf-8", errors="strict")
    marker = "X509v3 Subject Alternative Name:"
    if marker not in output:
        return {"dns_names": [], "ip_addresses": [], "uris": []}
    suffix = output.split(marker, 1)[1]
    value_lines: list[str] = []
    for line in suffix.splitlines()[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("X509v3 ") or stripped.startswith("Signature Algorithm:"):
            break
        value_lines.append(stripped)
    values = ", ".join(value_lines).split(", ") if value_lines else []
    result = {"dns_names": [], "ip_addresses": [], "uris": []}
    prefixes = {
        "DNS:": "dns_names",
        "IP Address:": "ip_addresses",
        "URI:": "uris",
    }
    for value in values:
        matches = [prefix for prefix in prefixes if value.startswith(prefix)]
        if len(matches) != 1:
            fail("certificate contains an unsupported or ambiguous SAN authority")
        prefix = matches[0]
        result[prefixes[prefix]].append(value.removeprefix(prefix))
    for key, entries in result.items():
        if not entries or entries == sorted(set(entries)):
            result[key] = sorted(entries)
        else:
            fail("certificate SAN extension contains duplicate identities")
    return result


def trusted_membership_key(
    trust_store: object, issuer: object
) -> bytes:
    store = exact_object(
        trust_store, {"schema", "issuers"}, "membership issuer trust store"
    )
    if (
        store["schema"] != MEMBERSHIP_TRUST_SCHEMA
        or not isinstance(store["issuers"], list)
    ):
        fail("membership issuer trust store has an unsupported schema")
    expected = exact_object(
        issuer, {"id", "role", "key_id"}, "membership receipt issuer"
    )
    issuer_id = string_value(expected["id"], "membership issuer ID")
    if expected["role"] != MEMBERSHIP_ISSUER_ROLE:
        fail("membership receipt issuer has the wrong authority role")
    key_id = string_value(
        expected["key_id"], "membership issuer key ID", KEY_ID_RE.pattern
    )
    matches: list[bytes] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(store["issuers"]):
        item = exact_object(
            raw,
            {"id", "role", "key_id", "public_key"},
            f"trusted membership issuer {index}",
        )
        item_id = string_value(item["id"], f"trusted membership issuer {index} ID")
        item_key_id = string_value(
            item["key_id"],
            f"trusted membership issuer {index} key ID",
            KEY_ID_RE.pattern,
        )
        if (item_id, item_key_id) in seen:
            fail("membership issuer trust store contains a duplicate authority")
        seen.add((item_id, item_key_id))
        key = b64url(
            item["public_key"], f"trusted membership issuer {index} public key", 32
        )
        if item_key_id != "sha256:" + hashlib.sha256(key).hexdigest():
            fail("trusted membership issuer key ID does not bind its public key")
        if item["role"] != MEMBERSHIP_ISSUER_ROLE:
            fail("trusted membership issuer has an unsupported role")
        if item_id == issuer_id and item_key_id == key_id:
            matches.append(key)
    if len(matches) != 1:
        fail("membership receipt issuer is not a unique source-trusted authority")
    return matches[0]


def trusted_preventive_boundary_key(
    trust_store: object, issuer: object
) -> tuple[bytes, Sequence[object]]:
    store = exact_object(
        trust_store,
        {"schema", "issuers"},
        "preventive-boundary issuer trust store",
    )
    if (
        store["schema"] != PREVENTIVE_BOUNDARY_TRUST_SCHEMA
        or not isinstance(store["issuers"], list)
    ):
        fail("preventive-boundary issuer trust store has an unsupported schema")
    expected = exact_object(
        issuer,
        {"id", "role", "key_id"},
        "preventive-boundary receipt issuer",
    )
    issuer_id = string_value(expected["id"], "preventive-boundary issuer ID")
    key_id = string_value(
        expected["key_id"], "preventive-boundary issuer key ID", KEY_ID_RE.pattern
    )
    if expected["role"] != PREVENTIVE_BOUNDARY_ISSUER_ROLE:
        fail("preventive-boundary receipt issuer has the wrong authority role")
    matches: list[tuple[bytes, Sequence[object]]] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(store["issuers"]):
        record = exact_object(
            raw,
            {"id", "role", "key_id", "public_key", "response_authorities"},
            f"trusted preventive-boundary issuer {index}",
        )
        record_id = string_value(
            record["id"], f"trusted preventive-boundary issuer {index} ID"
        )
        record_key_id = string_value(
            record["key_id"],
            f"trusted preventive-boundary issuer {index} key ID",
            KEY_ID_RE.pattern,
        )
        if (record_id, record_key_id) in seen:
            fail("preventive-boundary trust store contains a duplicate authority")
        seen.add((record_id, record_key_id))
        public_key = b64url(
            record["public_key"],
            f"trusted preventive-boundary issuer {index} public key",
            32,
        )
        if record_key_id != "sha256:" + hashlib.sha256(public_key).hexdigest():
            fail("preventive-boundary issuer key ID does not bind its public key")
        response_authorities = list_value(
            record["response_authorities"],
            f"trusted preventive-boundary issuer {index} response authorities",
        )
        if not response_authorities:
            fail("preventive-boundary issuer lacks independent response authorities")
        if record["role"] != PREVENTIVE_BOUNDARY_ISSUER_ROLE:
            fail("preventive-boundary trust store contains an unsupported role")
        if record_id == issuer_id and record_key_id == key_id:
            matches.append((public_key, response_authorities))
    if len(matches) != 1:
        fail("preventive-boundary issuer is not one source-trusted authority")
    return matches[0]


def trusted_native_response_key(
    authorities: Sequence[object],
    authority: object,
    *,
    role: str,
    endpoint: str,
) -> bytes:
    expected = exact_object(
        authority,
        {"id", "key_id", "role"},
        "native response authority",
    )
    matches: list[bytes] = []
    seen: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(authorities):
        record = exact_object(
            raw,
            {
                "id",
                "key_id",
                "role",
                "endpoint",
                "public_key",
                "collector_executable_sha256",
                "collector_config_sha256",
                "runtime_review_sha256",
            },
            f"native response authority {index}",
        )
        identity = (
            string_value(record["id"], f"native response authority {index} ID"),
            string_value(
                record["key_id"],
                f"native response authority {index} key ID",
                KEY_ID_RE.pattern,
            ),
            string_value(record["role"], f"native response authority {index} role"),
        )
        if identity in seen:
            fail("native response authority registry contains a duplicate")
        seen.add(identity)
        key = b64url(
            record["public_key"], f"native response authority {index} key", 32
        )
        if identity[1] != "sha256:" + hashlib.sha256(key).hexdigest():
            fail("native response authority key ID does not bind its public key")
        for digest_key in (
            "collector_executable_sha256",
            "collector_config_sha256",
            "runtime_review_sha256",
        ):
            digest(record[digest_key], f"native response authority {digest_key}")
        if (
            identity
            == (expected["id"], expected["key_id"], role)
            and record["endpoint"] == endpoint
        ):
            matches.append(key)
    if len(matches) != 1 or expected["role"] != role:
        fail("native response is not bound to one source-enrolled authority")
    return matches[0]


def trusted_provider_observer(
    trust_store: object,
    observer: object,
    executable: object,
) -> dict[str, str]:
    store = exact_object(
        trust_store, {"schema", "adapters"}, "provider observer trust store"
    )
    if (
        store["schema"] != PROVIDER_ADAPTER_TRUST_SCHEMA
        or not isinstance(store["adapters"], list)
    ):
        fail("provider observer trust store has an unsupported schema")
    expected = exact_object(
        observer,
        {
            "id",
            "provider",
            "endpoint",
            "credential_authority",
            "credential_subject",
            "audience",
            "configuration_sha256",
            "adapter_sha256",
        },
        "signed provider observer",
    )
    normalized = {
        "id": string_value(expected["id"], "provider observer ID", r"[a-z][a-z0-9-]{7,127}"),
        "provider": string_value(expected["provider"], "provider observer provider"),
        "endpoint": string_value(
            expected["endpoint"],
            "provider observer endpoint",
            r"https://[a-z0-9.-]+(?::[1-9][0-9]{0,4})?",
        ),
        "credential_authority": string_value(
            expected["credential_authority"], "provider observer credential authority"
        ),
        "credential_subject": string_value(
            expected["credential_subject"], "provider observer credential subject"
        ),
        "audience": string_value(expected["audience"], "provider observer audience"),
        "configuration_sha256": digest(
            expected["configuration_sha256"], "provider observer configuration digest"
        ),
        "adapter_sha256": digest(
            expected["adapter_sha256"], "provider observer adapter digest"
        ),
    }
    if normalized["provider"] != "nebius":
        fail("provider observer must be enrolled for Nebius")
    executable_item = exact_object(
        executable,
        {"path", "resolved_path", "sha256"},
        "provider observer executable",
    )
    executable_sha256 = digest(
        executable_item["sha256"], "provider observer executable digest"
    )
    matches = []
    seen: set[str] = set()
    for index, raw in enumerate(store["adapters"]):
        item = exact_object(
            raw,
            {
                "id",
                "provider",
                "endpoint",
                "credential_authority",
                "credential_subject",
                "audience",
                "configuration_sha256",
                "adapter_sha256",
                "executable_sha256",
            },
            f"trusted provider observer {index}",
        )
        item_id = string_value(item["id"], f"trusted provider observer {index} ID")
        if item_id in seen:
            fail("provider observer trust store contains a duplicate authority")
        seen.add(item_id)
        candidate = {
            key: item[key]
            for key in normalized
        }
        for key in ("configuration_sha256", "adapter_sha256"):
            digest(candidate[key], f"trusted provider observer {index} {key}")
        enrolled_executable = digest(
            item["executable_sha256"],
            f"trusted provider observer {index} executable digest",
        )
        if (
            candidate == normalized
            and enrolled_executable == executable_sha256
        ):
            matches.append(normalized)
    if len(matches) != 1:
        fail(
            "provider observer is not one unique source-enrolled adapter authority"
        )
    return matches[0]


def validate_parent_chain(path: Path, label: str) -> None:
    allowed_owners = {0, os.getuid()}
    current = path.parent
    while True:
        try:
            details = current.stat()
        except OSError as exc:
            raise GateError(f"{label} parent directory cannot be inspected") from exc
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid not in allowed_owners
            or stat.S_IMODE(details.st_mode) & 0o022
        ):
            fail(f"{label} parent directory chain is not owner-bound and non-writable")
        if current == current.parent:
            break
        current = current.parent


def pin_executable(record: object, label: str) -> tuple[str, str, int]:
    item = exact_object(
        record, {"path", "resolved_path", "sha256"}, f"{label} executable"
    )
    path = Path(string_value(item["path"], f"{label} executable path"))
    expected_resolved = string_value(
        item["resolved_path"], f"{label} resolved executable path"
    )
    expected_digest = digest(item["sha256"], f"{label} executable digest")
    if not path.is_absolute() or ".." in path.parts:
        fail(f"{label} path must be canonical and absolute")
    try:
        link_details = path.lstat()
        resolved = path.resolve(strict=True)
        details = resolved.stat()
    except OSError as exc:
        raise GateError(f"{label} executable cannot be resolved") from exc
    validate_parent_chain(path, f"{label} launcher")
    validate_parent_chain(resolved, f"{label} resolved executable")
    if link_details.st_uid != 0:
        fail(f"{label} launcher must be root-owned")
    if stat.S_ISREG(link_details.st_mode) and stat.S_IMODE(link_details.st_mode) & 0o022:
        fail(f"{label} launcher must be non-writable")
    if not (stat.S_ISREG(link_details.st_mode) or stat.S_ISLNK(link_details.st_mode)):
        fail(f"{label} launcher must be a regular file or symbolic link")
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != 0
        or stat.S_IMODE(details.st_mode) & 0o022
    ):
        fail(f"{label} executable must be a root-owned non-writable regular file")
    hasher = hashlib.sha256()
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(resolved, flags)
    except OSError as exc:
        raise GateError(f"{label} executable cannot be opened safely") from exc
    before = os.fstat(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_uid,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    ):
        os.close(descriptor)
        fail(f"{label} executable changed between resolution and open")
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
        os.close(descriptor)
        fail(f"{label} executable changed while it was hashed")
    if hasher.hexdigest() != expected_digest or str(resolved) != expected_resolved:
        os.close(descriptor)
        fail(f"{label} executable identity differs from the signed toolchain")
    os.lseek(descriptor, 0, os.SEEK_SET)
    return str(path), str(resolved), descriptor


def hash_executable(path: Path, label: str) -> tuple[str, str]:
    """Compatibility helper for focused tests; production execution uses an open FD."""

    resolved = path.resolve(strict=True)
    validate_parent_chain(path, f"{label} launcher")
    validate_parent_chain(resolved, f"{label} resolved executable")
    hasher = hashlib.sha256()
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(resolved, flags)
    try:
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    finally:
        os.close(descriptor)
    return str(resolved), hasher.hexdigest()


def checked_executable(record: object, label: str) -> str:
    path, _resolved, descriptor = pin_executable(record, label)
    os.close(descriptor)
    return path


def validate_membership_subject(value: object) -> dict[str, Any]:
    subject = exact_object(
        value,
        {
            "schema",
            "project_id",
            "cluster_id",
            "node_group_id",
            "run_id",
            "expected_node_count",
            "minimum_hostname_domains",
            "maximum_surge_members",
            "node_selector_sha256",
            "kubeconfig_sha256",
        },
        "membership receipt subject",
    )
    if subject["schema"] != MEMBERSHIP_SUBJECT_SCHEMA:
        fail("membership subject has an unsupported schema")
    string_value(subject["project_id"], "membership project ID", r"project-[a-z0-9]+")
    string_value(subject["cluster_id"], "membership cluster ID", r"mk8scluster-[a-z0-9]+")
    string_value(
        subject["node_group_id"],
        "membership NodeGroup ID",
        r"mk8snodegroup-[a-z0-9]+",
    )
    string_value(subject["run_id"], "membership run ID", r"[a-z][a-z0-9]{5,11}")
    if (
        integer_value(subject["expected_node_count"], "membership expected count") < 3
        or integer_value(
            subject["minimum_hostname_domains"], "membership minimum domains"
        )
        < 3
    ):
        fail("membership subject does not require public HA capacity")
    maximum_surge = integer_value(
        subject["maximum_surge_members"], "membership maximum surge"
    )
    if maximum_surge < 1:
        fail("membership subject must retain positive rollout surge")
    digest(subject["node_selector_sha256"], "membership selector digest")
    digest(subject["kubeconfig_sha256"], "membership kubeconfig digest")
    return subject


def member_ids(value: object, label: str) -> list[str]:
    values = [
        string_value(item, label, r"computeinstance-[a-z0-9]+")
        for item in list_value(value, label)
    ]
    if values != sorted(set(values)):
        fail(f"{label} must be sorted and unique")
    return values


def validate_membership_epoch(
    value: object,
    *,
    expected_count: int,
    maximum_surge: int,
    provider_member_ids: Sequence[str],
) -> dict[str, object]:
    epoch = exact_object(
        value,
        {
            "sequence",
            "phase",
            "predecessor_payload_sha256",
            "serving_member_instance_ids",
            "joining_member_instance_ids",
            "retiring_member_instance_ids",
            "admitted_member_instance_ids",
        },
        "signed membership epoch",
    )
    sequence = integer_value(epoch["sequence"], "membership epoch sequence")
    if sequence < 1:
        fail("membership epoch sequence must be positive")
    phase = string_value(epoch["phase"], "membership epoch phase")
    if phase not in {"stable", "prepare", "cutover"}:
        fail("membership epoch phase is unsupported")
    predecessor = string_value(
        epoch["predecessor_payload_sha256"],
        "membership predecessor payload digest",
        SHA256_RE.pattern,
    )
    if phase != "stable" and predecessor == "0" * 64:
        fail("a transition epoch must bind a non-genesis predecessor")
    serving = member_ids(
        epoch["serving_member_instance_ids"], "serving member ID"
    )
    joining = member_ids(
        epoch["joining_member_instance_ids"], "joining member ID"
    )
    retiring = member_ids(
        epoch["retiring_member_instance_ids"], "retiring member ID"
    )
    admitted = member_ids(
        epoch["admitted_member_instance_ids"], "admitted member ID"
    )
    if len(serving) != expected_count:
        fail("membership epoch must retain the full serving-node count")
    if set(serving) & set(joining) or set(serving) & set(retiring) or set(joining) & set(retiring):
        fail("membership epoch sets must be disjoint")
    union = sorted({*serving, *joining, *retiring})
    if admitted != union or list(provider_member_ids) != admitted:
        fail("membership epoch admission union must equal exact provider membership")
    if phase == "stable" and (joining or retiring):
        fail("stable membership epoch cannot contain transition members")
    if phase == "prepare" and (
        not joining or retiring or len(joining) > maximum_surge
    ):
        fail("prepare membership epoch requires bounded joining surge only")
    if phase == "cutover" and (
        joining or not retiring or len(retiring) > maximum_surge
    ):
        fail("cutover membership epoch requires bounded retiring members only")
    normalized = {
        "sequence": sequence,
        "phase": phase,
        "predecessor_payload_sha256": predecessor,
        "serving_member_instance_ids": serving,
        "joining_member_instance_ids": joining,
        "retiring_member_instance_ids": retiring,
        "admitted_member_instance_ids": admitted,
    }
    return {**normalized, "epoch_id": canonical_sha256(normalized)}


def validate_membership_receipt(
    receipt: object,
    trust_store: object,
    provider_adapter_trust_store: object,
    expected_subject: object,
    evidence: object,
    evidence_raw_sha256: str,
    *,
    validation_time: datetime | None = None,
) -> dict[str, object]:
    envelope = exact_object(
        receipt,
        {"schema", "algorithm", "payload", "payload_sha256", "signature"},
        "public-edge membership receipt",
    )
    if envelope["schema"] != MEMBERSHIP_RECEIPT_SCHEMA or envelope["algorithm"] != "ed25519":
        fail("public-edge membership receipt has an unsupported signature contract")
    payload = exact_object(
        envelope["payload"],
        {
            "schema",
            "issuer",
            "nonce",
            "issued_at",
            "expires_at",
            "subject",
            "provider_membership",
            "toolchain",
            "evidence",
        },
        "public-edge membership payload",
    )
    if payload["schema"] != MEMBERSHIP_PAYLOAD_SCHEMA:
        fail("public-edge membership payload has an unsupported schema")
    payload_digest = digest(
        envelope["payload_sha256"], "membership payload digest"
    )
    if hashlib.sha256(canonical_bytes(payload)).hexdigest() != payload_digest:
        fail("membership payload digest does not match the reopened payload")
    digest(payload["nonce"], "membership nonce")
    issued = timestamp(payload["issued_at"], "membership issued_at")
    expires = timestamp(payload["expires_at"], "membership expires_at")
    if expires <= issued or expires - issued > MAX_MEMBERSHIP_VALIDITY:
        fail("membership validity interval is outside the 24-hour bound")
    now = validation_time or datetime.now(timezone.utc).replace(microsecond=0)
    if now.tzinfo is None:
        fail("membership validation time must be timezone-aware")
    now = now.astimezone(timezone.utc).replace(microsecond=0)
    if issued > now + MAX_CLOCK_SKEW or expires <= now:
        fail("public-edge membership receipt is not fresh")
    public_key = trusted_membership_key(trust_store, payload["issuer"])
    signature = b64url(envelope["signature"], "membership signature", 64)
    verify_ed25519(
        public_key,
        signature,
        canonical_bytes(
            {
                "schema": envelope["schema"],
                "algorithm": envelope["algorithm"],
                "payload": payload,
                "payload_sha256": payload_digest,
            }
        ),
    )
    subject = validate_membership_subject(expected_subject)
    if payload["subject"] != subject:
        fail("signed membership subject does not match the exact Terraform run")
    authority = exact_object(
        payload["provider_membership"],
        {
            "relation_api",
            "node_group_resource_version",
            "member_instance_ids",
            "membership_epoch",
            "provider_observer",
        },
        "signed provider membership",
    )
    if authority["relation_api"] != "nebius-managed-kubernetes-node-group-membership/v1":
        fail("signed provider membership uses an unsupported relation API")
    group_revision = string_value(
        authority["node_group_resource_version"],
        "signed NodeGroup resource version",
        r"[1-9][0-9]*",
    )
    member_ids_value = member_ids(
        authority["member_instance_ids"], "signed provider member ID"
    )
    epoch = validate_membership_epoch(
        authority["membership_epoch"],
        expected_count=integer_value(
            subject["expected_node_count"], "membership expected count"
        ),
        maximum_surge=integer_value(
            subject["maximum_surge_members"], "membership maximum surge"
        ),
        provider_member_ids=member_ids_value,
    )
    toolchain = exact_object(
        payload["toolchain"],
        {"python3", "provider_observer", "kubectl"},
        "signed toolchain",
    )
    provider_observer = trusted_provider_observer(
        provider_adapter_trust_store,
        authority["provider_observer"],
        toolchain["provider_observer"],
    )
    tools = {
        name: checked_executable(toolchain[name], name)
        for name in ("python3", "provider_observer", "kubectl")
    }
    if Path(sys.executable).resolve(strict=True) != Path(tools["python3"]).resolve(strict=True):
        fail("running Python interpreter differs from the signed absolute executable")
    evidence_reference = exact_object(
        payload["evidence"],
        {"provider_membership_export_sha256"},
        "signed membership evidence reference",
    )
    evidence_digest = digest(
        evidence_reference["provider_membership_export_sha256"],
        "provider membership export digest",
    )
    evidence_object = exact_object(
        evidence,
        {
            "schema",
            "provider",
            "project_id",
            "cluster_id",
            "node_group_id",
            "node_group_resource_version",
            "relation_api",
            "member_instance_ids",
            "membership_epoch",
            "provider_observer",
            "collected_at",
            "adapter_sha256",
        },
        "provider membership export",
    )
    if evidence_raw_sha256 != evidence_digest:
        fail("reopened provider membership export bytes do not match the signed digest")
    if evidence_object != {
        "schema": "fs2-serve.nebius.ai/provider-node-group-membership-export/v2",
        "provider": "nebius",
        "project_id": subject["project_id"],
        "cluster_id": subject["cluster_id"],
        "node_group_id": subject["node_group_id"],
        "node_group_resource_version": group_revision,
        "relation_api": authority["relation_api"],
        "member_instance_ids": member_ids_value,
        "membership_epoch": {
            key: epoch[key]
            for key in (
                "sequence",
                "phase",
                "predecessor_payload_sha256",
                "serving_member_instance_ids",
                "joining_member_instance_ids",
                "retiring_member_instance_ids",
                "admitted_member_instance_ids",
            )
        },
        "provider_observer": provider_observer,
        "collected_at": evidence_object["collected_at"],
        "adapter_sha256": provider_observer["adapter_sha256"],
    }:
        fail("provider membership export does not bind the signed exact relation")
    collected_at = timestamp(
        evidence_object["collected_at"], "provider membership collected_at"
    )
    if collected_at < issued - MAX_CLOCK_SKEW or collected_at > issued + MAX_CLOCK_SKEW:
        fail("provider membership export was not collected with the signed receipt")
    if evidence_object["adapter_sha256"] != provider_observer["adapter_sha256"]:
        fail("provider membership export adapter is not source-enrolled")
    return {
        "payload_sha256": payload_digest,
        "issuer_key_id": payload["issuer"]["key_id"],
        "node_group_resource_version": group_revision,
        "provider_member_instance_ids": member_ids_value,
        "epoch_id": epoch["epoch_id"],
        "epoch_sequence": epoch["sequence"],
        "phase": epoch["phase"],
        "predecessor_payload_sha256": epoch["predecessor_payload_sha256"],
        "serving_member_instance_ids": epoch["serving_member_instance_ids"],
        "joining_member_instance_ids": epoch["joining_member_instance_ids"],
        "retiring_member_instance_ids": epoch["retiring_member_instance_ids"],
        "admitted_member_instance_ids": epoch["admitted_member_instance_ids"],
        "provider_observer": provider_observer,
        "tools": tools,
        "toolchain": toolchain,
        "evidence_sha256": evidence_digest,
    }


def load_membership_contract(
    run_root: Path,
    expected_subject: object,
    *,
    membership_trust_sha256: str,
    provider_adapter_trust_sha256: str,
    validation_time: datetime | None = None,
) -> dict[str, object]:
    receipt_path = run_root / MEMBERSHIP_RECEIPT_FILENAME
    evidence_path = run_root / MEMBERSHIP_EVIDENCE_FILENAME
    receipt_raw = open_regular_file(receipt_path, private=True)
    evidence_raw = open_regular_file(evidence_path, private=True)
    trust_raw = open_regular_file(MEMBERSHIP_TRUST_STORE, private=False)
    provider_adapter_trust_raw = open_regular_file(
        PROVIDER_ADAPTER_TRUST_STORE, private=False
    )
    if hashlib.sha256(trust_raw).hexdigest() != digest(
        membership_trust_sha256, "planned membership trust-store digest"
    ):
        fail("membership trust store differs from the planned source bytes")
    if hashlib.sha256(provider_adapter_trust_raw).hexdigest() != digest(
        provider_adapter_trust_sha256,
        "planned provider-adapter trust-store digest",
    ):
        fail("provider-adapter trust store differs from the planned source bytes")
    result = validate_membership_receipt(
        decode_canonical_json(receipt_raw, MEMBERSHIP_RECEIPT_FILENAME),
        decode_canonical_json(trust_raw, MEMBERSHIP_TRUST_STORE.name),
        decode_canonical_json(
            provider_adapter_trust_raw, PROVIDER_ADAPTER_TRUST_STORE.name
        ),
        expected_subject,
        decode_canonical_json(evidence_raw, MEMBERSHIP_EVIDENCE_FILENAME),
        hashlib.sha256(evidence_raw).hexdigest(),
        validation_time=validation_time,
    )
    result["receipt_sha256"] = hashlib.sha256(receipt_raw).hexdigest()
    return result


def validate_preventive_export_provenance(
    value: object, *, label: str, expected_endpoint: str
) -> None:
    provenance = exact_object(
        value,
        {
            "collector_id",
            "collector_executable_sha256",
            "collector_config_sha256",
            "api_endpoint",
            "request_ids",
            "response_attestation_sha256",
        },
        f"{label} provenance",
    )
    if (
        re.fullmatch(r"[a-z][a-z0-9._-]{2,127}", str(provenance["collector_id"]))
        is None
        or provenance["api_endpoint"] != expected_endpoint
    ):
        fail(f"{label} provenance does not name its exact observer/endpoint")
    for key in (
        "collector_executable_sha256",
        "collector_config_sha256",
        "response_attestation_sha256",
    ):
        digest(provenance[key], f"{label} provenance {key}")
    request_ids = list_value(provenance["request_ids"], f"{label} request IDs")
    if (
        not request_ids
        or request_ids != sorted(set(request_ids))
        or not all(
            isinstance(item, str)
            and re.fullmatch(r"[A-Za-z0-9._:/-]{8,256}", item) is not None
            for item in request_ids
        )
    ):
        fail(f"{label} provenance lacks exact authoritative request IDs")


def verify_native_authority_export(
    raw: bytes,
    filename: str,
    *,
    response_authorities: Sequence[object],
    role: str,
    endpoint: str,
) -> Mapping[str, Any]:
    envelope = exact_object(
        decode_canonical_json(raw, filename),
        {"schema", "authority", "payload", "payload_sha256", "signature"},
        f"native authority export {filename}",
    )
    if envelope["schema"] != "fs2-serve.nebius.ai/native-authority-export/v1":
        fail(f"{filename} has an unsupported native-export schema")
    payload = object_value(envelope["payload"], f"{filename} payload")
    payload_sha256 = digest(envelope["payload_sha256"], f"{filename} payload digest")
    if canonical_sha256(payload) != payload_sha256:
        fail(f"{filename} payload digest differs from reopened native content")
    key = trusted_native_response_key(
        response_authorities,
        envelope["authority"],
        role=role,
        endpoint=endpoint,
    )
    verify_ed25519(
        key,
        b64url(envelope["signature"], f"{filename} response signature", 64),
        canonical_bytes(
            {
                "schema": envelope["schema"],
                "authority": envelope["authority"],
                "payload": payload,
                "payload_sha256": payload_sha256,
            }
        ),
    )
    return payload


def native_list_items(
    value: object,
    *,
    label: str,
    api_group: str,
    resource: str,
    representation: str = "full-object",
) -> tuple[list[Mapping[str, Any]], str]:
    export = exact_object(
        value,
        {"pages", "scope", "resource", "api_group", "item_count", "page_count"},
        f"{label} complete list export",
    )
    pages = list_value(export["pages"], f"{label} pages")
    if (
        export["scope"] != "all"
        or export["api_group"] != api_group
        or export["resource"] != resource
        or export["page_count"] != len(pages)
        or not pages
    ):
        fail(f"{label} does not declare one complete all-scope native list")
    expected_continue = ""
    previous_remaining: int | None = None
    resource_version: str | None = None
    items: list[Mapping[str, Any]] = []
    seen_uids: set[str] = set()
    for index, raw_page in enumerate(pages):
        page = exact_object(
            raw_page,
            {"request", "response", "request_id"},
            f"{label} page {index}",
        )
        request_fields = {"api_group", "resource", "scope", "limit", "continue"}
        if representation == "partial-object-metadata":
            request_fields.add("accept")
        elif representation != "full-object":
            fail(f"{label} requests an unsupported native representation")
        request = exact_object(
            page["request"], request_fields, f"{label} page {index} request"
        )
        response = exact_object(
            page["response"],
            {"apiVersion", "kind", "metadata", "items"},
            f"{label} page {index} native response",
        )
        metadata = object_value(response["metadata"], f"{label} list metadata")
        current_continue = metadata.get("continue", "")
        remaining = metadata.get("remainingItemCount")
        current_resource_version = string_value(
            metadata.get("resourceVersion"), f"{label} resourceVersion"
        )
        page_items = list_value(response["items"], f"{label} page items")
        if (
            request
            != {
                "api_group": api_group,
                "resource": resource,
                "scope": "all",
                "limit": 500,
                "continue": expected_continue,
                **(
                    {
                        "accept": (
                            "application/json;as=PartialObjectMetadataList;"
                            "g=meta.k8s.io;v=v1"
                        )
                    }
                    if representation == "partial-object-metadata"
                    else {}
                ),
            }
            or response["apiVersion"]
            != (
                "meta.k8s.io/v1"
                if representation == "partial-object-metadata"
                else ("v1" if api_group == "" else f"{api_group}/v1")
            )
            or not isinstance(response["kind"], str)
            or (
                representation == "partial-object-metadata"
                and response["kind"] != "PartialObjectMetadataList"
            )
            or (
                representation == "full-object"
                and not response["kind"].endswith("List")
            )
            or not isinstance(current_continue, str)
            or not (
                remaining is None
                or (
                    isinstance(remaining, int)
                    and not isinstance(remaining, bool)
                    and remaining >= 0
                )
            )
            or (index + 1 == len(pages) and remaining not in {None, 0})
            or (
                previous_remaining is not None
                and remaining is not None
                and remaining >= previous_remaining
            )
            or (resource_version is not None and current_resource_version != resource_version)
            or re.fullmatch(r"[A-Za-z0-9._:/-]{8,256}", str(page["request_id"])) is None
        ):
            fail(f"{label} pagination or native response is incomplete")
        if remaining is not None:
            previous_remaining = remaining
        resource_version = current_resource_version
        for item in page_items:
            native = object_value(item, f"{label} native object")
            if representation == "partial-object-metadata" and set(native) != {
                "apiVersion",
                "kind",
                "metadata",
            }:
                fail(f"{label} partial metadata response exposes unsupported fields")
            metadata_value = object_value(
                native.get("metadata"), f"{label} native metadata"
            )
            uid = string_value(metadata_value.get("uid"), f"{label} native UID")
            if uid in seen_uids:
                fail(f"{label} native list repeats an object UID")
            seen_uids.add(uid)
            items.append(native)
        expected_continue = current_continue
        if index + 1 < len(pages) and not expected_continue:
            fail(f"{label} pagination terminated before its declared last page")
    if expected_continue or export["item_count"] != len(items):
        fail(f"{label} native list is not terminal and complete")
    assert resource_version is not None
    return items, resource_version


def native_ca_history_records(
    value: object,
    *,
    label: str,
    cluster_id: str,
    record_key: str,
) -> tuple[list[Mapping[str, Any]], datetime, datetime]:
    """Reconstruct one complete, terminal native CA history pagination."""

    export = exact_object(
        value,
        {
            "history_start",
            "observed_through",
            "page_count",
            "pages",
            "record_count",
        },
        label,
    )
    history_start = timestamp(export["history_start"], f"{label} history_start")
    observed_through = timestamp(
        export["observed_through"], f"{label} observed_through"
    )
    pages = list_value(export["pages"], f"{label} pages")
    if (
        history_start >= observed_through
        or export["page_count"] != len(pages)
        or not pages
    ):
        fail(f"{label} is not a bounded complete native history")
    expected_cursor = ""
    previous_remaining: int | None = None
    records: list[Mapping[str, Any]] = []
    seen_keys: set[str] = set()
    for index, raw_page in enumerate(pages):
        page = exact_object(
            raw_page,
            {"request", "request_id", "response", "response_attestation_sha256"},
            f"{label} page {index}",
        )
        request = exact_object(
            page["request"],
            {
                "cluster_id",
                "cursor",
                "history_start",
                "limit",
                "observed_through",
            },
            f"{label} page {index} request",
        )
        response = exact_object(
            page["response"],
            {"next_cursor", "records", "remaining_count"},
            f"{label} page {index} response",
        )
        remaining = response["remaining_count"]
        next_cursor = response["next_cursor"]
        if (
            request
            != {
                "cluster_id": cluster_id,
                "cursor": expected_cursor,
                "history_start": export["history_start"],
                "limit": 500,
                "observed_through": export["observed_through"],
            }
            or not isinstance(next_cursor, str)
            or not isinstance(remaining, int)
            or isinstance(remaining, bool)
            or remaining < 0
            or (index + 1 == len(pages) and (next_cursor or remaining != 0))
            or (
                previous_remaining is not None
                and remaining >= previous_remaining
            )
            or re.fullmatch(
                r"[A-Za-z0-9._:/-]{8,256}", str(page["request_id"])
            )
            is None
        ):
            fail(f"{label} pagination is incomplete or inconsistent")
        digest(
            page["response_attestation_sha256"],
            f"{label} page {index} native response attestation",
        )
        page_records = list_value(
            response["records"], f"{label} page {index} records"
        )
        for raw_record in page_records:
            record = object_value(raw_record, f"{label} native record")
            key = string_value(record.get(record_key), f"{label} {record_key}")
            if key in seen_keys:
                fail(f"{label} repeats native record {key}")
            seen_keys.add(key)
            records.append(record)
        expected_cursor = next_cursor
        previous_remaining = remaining
        if index + 1 < len(pages) and not expected_cursor:
            fail(f"{label} terminated before its declared last page")
    if expected_cursor or export["record_count"] != len(records):
        fail(f"{label} is not a terminal complete history")
    return records, history_start, observed_through


def native_rule_matches(
    rule: Mapping[str, Any],
    *,
    api_groups: set[str],
    resources: set[str],
    verbs: set[str],
    resource_names: set[str] | None = None,
) -> bool:
    groups = set(list_value(rule.get("apiGroups"), "native RBAC apiGroups"))
    rule_resources = set(list_value(rule.get("resources"), "native RBAC resources"))
    rule_verbs = set(list_value(rule.get("verbs"), "native RBAC verbs"))
    names = list_value(rule.get("resourceNames", []), "native RBAC resourceNames")
    non_resource_urls = list_value(
        rule.get("nonResourceURLs", []), "native RBAC nonResourceURLs"
    )
    if non_resource_urls and rule_resources:
        fail("native RBAC rule mixes resource and non-resource authority")
    names_match = (
        resource_names is None
        or not names
        or "*" in names
        or bool(set(names) & resource_names)
    )
    resource_matches = any(
        candidate == "*"
        or candidate in resources
        or any(
            requested.endswith("/*")
            and candidate.startswith(requested.removesuffix("*"))
            for requested in resources
        )
        for candidate in rule_resources
    )
    return (
        bool(groups & api_groups or "*" in groups)
        and resource_matches
        and bool(rule_verbs & verbs or "*" in rule_verbs)
        and names_match
    )


def native_object_identity(value: Mapping[str, Any], label: str) -> tuple[str, str]:
    metadata = object_value(value.get("metadata"), f"{label} metadata")
    name = string_value(metadata.get("name"), f"{label} name")
    namespace = metadata.get("namespace", "")
    if not isinstance(namespace, str):
        fail(f"{label} namespace is malformed")
    string_value(metadata.get("uid"), f"{label} UID")
    string_value(metadata.get("resourceVersion"), f"{label} resourceVersion")
    return namespace, name


def native_rbac_subject(value: object, label: str) -> dict[str, str]:
    subject = object_value(value, label)
    kind = subject.get("kind")
    if kind == "ServiceAccount":
        exact = exact_object(subject, {"kind", "name", "namespace"}, label)
        if not exact["namespace"]:
            fail(f"{label} ServiceAccount namespace is empty")
        return {
            "kind": "ServiceAccount",
            "name": string_value(exact["name"], f"{label} name"),
            "namespace": string_value(exact["namespace"], f"{label} namespace"),
        }
    if kind in {"User", "Group"}:
        exact = exact_object(subject, {"apiGroup", "kind", "name"}, label)
        if exact["apiGroup"] != "rbac.authorization.k8s.io":
            fail(f"{label} has an unsupported RBAC API group")
        return {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": str(kind),
            "name": string_value(exact["name"], f"{label} name"),
        }
    fail(f"{label} has an unsupported subject kind")


def validate_preventive_raw_exports(
    *,
    provider_raw: bytes,
    apiserver_raw: bytes,
    identity_raw: bytes,
    ca_history_raw: bytes,
    provider_summary: Mapping[str, Any],
    apiserver_summary: Mapping[str, Any],
    identity_summary: Mapping[str, Any],
    ca_history_summary: Mapping[str, Any],
    boundary: Mapping[str, Any],
    approval_projection: Mapping[str, Any],
    project_id: str,
    cluster_id: str,
    collected_at: datetime,
    response_authorities: Sequence[object],
) -> None:
    controller_username_parts = str(boundary["controller_username"]).split(":")
    controller_namespace_contract = (
        controller_username_parts[2]
        if len(controller_username_parts) == 4
        and controller_username_parts[:2] == ["system", "serviceaccount"]
        else ""
    )
    credential_secret_names: dict[str, set[str]] = {}
    for index, raw_secret in enumerate(boundary["enrolled_credential_secrets"]):
        secret = object_value(raw_secret, f"enrolled credential Secret {index}")
        credential_secret_names.setdefault(
            string_value(secret.get("namespace"), "enrolled credential Secret namespace"),
            set(),
        ).add(string_value(secret.get("name"), "enrolled credential Secret name"))

    credential_workload_names: dict[tuple[str, str, str], set[str]] = {}
    credential_pod_names: dict[str, set[str]] = {}
    credential_service_account_names: dict[str, set[str]] = {}
    for index, raw_workload in enumerate(boundary["enrolled_credential_workloads"]):
        workload = object_value(raw_workload, f"enrolled credential workload {index}")
        namespace = string_value(
            workload.get("namespace"), "enrolled credential workload namespace"
        )
        kind = string_value(workload.get("kind"), "enrolled credential workload kind")
        name = string_value(workload.get("name"), "enrolled credential workload name")
        api_version = string_value(
            workload.get("api_version"), "enrolled credential workload apiVersion"
        )
        api_group, _, version = api_version.partition("/")
        if not version:
            api_group, version = "", api_group
        credential_workload_names.setdefault(
            (api_group, version, kind), set()
        ).add(f"{namespace}/{name}")
        if kind == "Pod":
            credential_pod_names.setdefault(namespace, set()).add(name)
        service_account_name = workload.get("service_account_name")
        if isinstance(service_account_name, str) and service_account_name:
            credential_service_account_names.setdefault(namespace, set()).add(
                service_account_name
            )

    protected_resource_contract: list[dict[str, Any]] = [
        {
            "actions": ["create", "delete", "deletecollection", "patch", "update"],
            "api_group": "admissionregistration.k8s.io",
            "api_version": "v1",
            "resources": ["validatingadmissionpolicies"],
            "operations": ["CREATE", "DELETE", "UPDATE"],
            "namespaces": [],
            "semantic_guard": "exact-name-immutable",
            "names": [
                "fs2-public-edge-cas-bootstrap",
                "fs2-public-edge-node-authority",
                "fs2-public-edge-node-authority-cas",
            ],
        },
        {
            "actions": ["create", "delete", "deletecollection", "patch", "update"],
            "api_group": "admissionregistration.k8s.io",
            "api_version": "v1",
            "resources": ["validatingadmissionpolicybindings"],
            "operations": ["CREATE", "DELETE", "UPDATE"],
            "namespaces": [],
            "semantic_guard": "exact-name-immutable",
            "names": [
                "fs2-public-edge-cas-bootstrap-binding",
                "fs2-public-edge-node-authority-binding",
                "fs2-public-edge-node-authority-cas-binding",
            ],
        },
        {
            "actions": ["create", "delete", "deletecollection", "patch", "update"],
            "api_group": "apiextensions.k8s.io",
            "api_version": "v1",
            "resources": ["customresourcedefinitions"],
            "operations": ["CREATE", "DELETE", "UPDATE"],
            "namespaces": [],
            "semantic_guard": "exact-name-immutable",
            "names": [
                "publicedgenodeauthorityapprovals.security.fs2.nebius.ai"
            ],
        },
        {
            "actions": ["create", "delete", "deletecollection", "patch", "update"],
            "api_group": "security.fs2.nebius.ai",
            "api_version": "v1",
            "resources": ["publicedgenodeauthorityapprovals"],
            "operations": ["CREATE", "DELETE", "UPDATE"],
            "namespaces": [],
            "semantic_guard": "exact-name-immutable",
            "names": ["fs2-public-edge-node-authority-approval"],
        },
        {
            "actions": ["bind", "create", "delete", "deletecollection", "escalate", "patch", "update"],
            "api_group": "rbac.authorization.k8s.io",
            "api_version": "v1",
            "resources": ["clusterrolebindings", "clusterroles", "rolebindings", "roles"],
            "operations": ["CREATE", "DELETE", "UPDATE"],
            "namespaces": list(boundary["credential_namespaces"]),
            "semantic_guard": "deny-non-enrolled-authority-path",
            "names": ["*"],
        },
        {
            "actions": ["approve", "create", "delete", "deletecollection", "get", "list", "patch", "sign", "update", "watch"],
            "api_group": "certificates.k8s.io",
            "api_version": "v1",
            "resources": ["certificatesigningrequests", "certificatesigningrequests/approval"],
            "operations": ["CREATE", "DELETE", "UPDATE"],
            "namespaces": [],
            "semantic_guard": "deny-non-enrolled-certificate-path",
            "names": ["*"],
        },
        {
            "actions": ["create", "delete", "deletecollection", "patch", "update"],
            "api_group": "admissionregistration.k8s.io",
            "api_version": "v1",
            "resources": ["mutatingwebhookconfigurations", "validatingwebhookconfigurations"],
            "operations": ["CREATE", "DELETE", "UPDATE"],
            "namespaces": [],
            "semantic_guard": "deny-authority-intersecting-webhook-change",
            "names": ["*"],
        },
        {
            "actions": ["connect", "get", "list", "patch", "update", "watch"],
            "api_group": "",
            "api_version": "v1",
            "resources": ["nodes", "nodes/proxy"],
            "operations": ["CONNECT", "CREATE", "DELETE", "UPDATE"],
            "namespaces": [],
            "semantic_guard": "deny-controller-credential-path",
            "names": ["*"],
        },
    ]

    def add_scoped_contract(
        *,
        actions: Sequence[str],
        api_group: str,
        api_version: str,
        resources: Sequence[str],
        operations: Sequence[str],
        namespace: str,
        names: Sequence[str],
        semantic_guard: str,
    ) -> None:
        if not names:
            return
        protected_resource_contract.append(
            {
                "actions": sorted(set(actions)),
                "api_group": api_group,
                "api_version": api_version,
                "resources": sorted(set(resources)),
                "operations": sorted(set(operations)),
                "namespaces": [namespace],
                "semantic_guard": semantic_guard,
                "names": sorted(set(names)),
            }
        )

    for namespace, names in sorted(credential_secret_names.items()):
        add_scoped_contract(
            actions=[
                "delete",
                "deletecollection",
                "get",
                "list",
                "patch",
                "update",
                "watch",
            ],
            api_group="",
            api_version="v1",
            resources=["secrets"],
            operations=["DELETE", "UPDATE"],
            namespace=namespace,
            names=sorted(names),
            semantic_guard="deny-controller-credential-secret-path",
        )
    for namespace, names in sorted(credential_pod_names.items()):
        add_scoped_contract(
            actions=[
                "delete",
                "deletecollection",
                "get",
                "list",
                "patch",
                "update",
                "watch",
            ],
            api_group="",
            api_version="v1",
            resources=["pods"],
            operations=["DELETE", "UPDATE"],
            namespace=namespace,
            names=sorted(names),
            semantic_guard="deny-controller-credential-pod-path",
        )
        add_scoped_contract(
            actions=["connect", "create", "get"],
            api_group="",
            api_version="v1",
            resources=[
                "pods/attach",
                "pods/ephemeralcontainers",
                "pods/exec",
                "pods/portforward",
            ],
            operations=["CONNECT", "CREATE", "UPDATE"],
            namespace=namespace,
            names=sorted(names),
            semantic_guard="deny-controller-credential-pod-subresource-path",
        )
    for namespace, names in sorted(credential_service_account_names.items()):
        add_scoped_contract(
            actions=["delete", "deletecollection", "get", "list", "patch", "update", "watch"],
            api_group="",
            api_version="v1",
            resources=["serviceaccounts"],
            operations=["DELETE", "UPDATE"],
            namespace=namespace,
            names=sorted(names),
            semantic_guard="deny-controller-service-account-path",
        )
        add_scoped_contract(
            actions=["create", "get"],
            api_group="",
            api_version="v1",
            resources=["serviceaccounts/token"],
            operations=["CREATE"],
            namespace=namespace,
            names=sorted(names),
            semantic_guard="deny-controller-tokenrequest-path",
        )

    for namespace in sorted(set(boundary["credential_namespaces"])):
        add_scoped_contract(
            actions=["create", "patch", "update"],
            api_group="",
            api_version="v1",
            resources=["pods", "replicationcontrollers", "serviceaccounts"],
            operations=["CREATE", "UPDATE"],
            namespace=namespace,
            names=["*"],
            semantic_guard="inspect-new-controller-credential-reachability",
        )
        add_scoped_contract(
            actions=["create", "patch", "update"],
            api_group="",
            api_version="v1",
            resources=["secrets"],
            operations=["CREATE", "UPDATE"],
            namespace=namespace,
            names=["*"],
            semantic_guard="classify-secret-content-before-admission",
        )
        add_scoped_contract(
            actions=["create", "patch", "update"],
            api_group="apps",
            api_version="v1",
            resources=["daemonsets", "deployments", "replicasets", "statefulsets"],
            operations=["CREATE", "UPDATE"],
            namespace=namespace,
            names=["*"],
            semantic_guard="inspect-new-controller-credential-reachability",
        )
        add_scoped_contract(
            actions=["create", "patch", "update"],
            api_group="batch",
            api_version="v1",
            resources=["cronjobs", "jobs"],
            operations=["CREATE", "UPDATE"],
            namespace=namespace,
            names=["*"],
            semantic_guard="inspect-new-controller-credential-reachability",
        )

    workload_resource = {
        "CronJob": "cronjobs",
        "DaemonSet": "daemonsets",
        "Deployment": "deployments",
        "Job": "jobs",
        "ReplicaSet": "replicasets",
        "ReplicationController": "replicationcontrollers",
        "StatefulSet": "statefulsets",
    }
    for (api_group, api_version, kind), qualified_names in sorted(
        credential_workload_names.items()
    ):
        if kind == "Pod":
            continue
        resource = workload_resource.get(kind)
        if resource is None:
            fail("signed credential workload has an unsupported native kind")
        names_by_namespace: dict[str, set[str]] = {}
        for qualified_name in qualified_names:
            namespace, name = qualified_name.split("/", 1)
            names_by_namespace.setdefault(namespace, set()).add(name)
        for namespace, names in sorted(names_by_namespace.items()):
            add_scoped_contract(
                actions=["delete", "deletecollection", "patch", "update"],
                api_group=api_group,
                api_version=api_version,
                resources=[resource],
                operations=["DELETE", "UPDATE"],
                namespace=namespace,
                names=sorted(names),
                semantic_guard="deny-controller-credential-workload-path",
            )

    protected_actions = sorted(
        {
            action
            for contract in protected_resource_contract
            for action in contract["actions"]
        }
    )
    protected_names = sorted(
        {
            name
            for contract in protected_resource_contract
            for name in contract["names"]
        }
    )
    expected_controller = {
        "username": boundary["controller_username"],
        "uid": boundary["controller_uid"],
        "groups": boundary["controller_groups"],
        "image_digest": boundary["controller_image_digest"],
        "provider_principal_id": boundary["controller_provider_principal_id"],
    }

    ca_history = exact_object(
        verify_native_authority_export(
            ca_history_raw,
            PREVENTIVE_CA_HISTORY_FILENAME,
            response_authorities=response_authorities,
            role="kubernetes-ca-native-response-attestor",
            endpoint=f"kubernetes://{cluster_id}/certificate-authority-history",
        ),
        {
            "schema",
            "authority_snapshot_id",
            "cluster_id",
            "cluster_created_at",
            "collected_at",
            "issuer_authorities",
            "issuance_history",
            "revocation_history",
        },
        "authoritative certificate-authority history export",
    )
    ca_collected = timestamp(
        ca_history["collected_at"], "certificate-authority history collected_at"
    )
    raw_ca_authorities = list_value(
        ca_history["issuer_authorities"], "certificate-authority issuer inventory"
    )
    ca_authorities: list[dict[str, Any]] = []
    for index, raw_authority in enumerate(raw_ca_authorities):
        authority = exact_object(
            raw_authority,
            {
                "ca_key_id",
                "issuance_log_id",
                "response_attestation_sha256",
                "revocation_mode",
                "signer_name",
                "trust_anchor_key_ids",
                "trust_bundle_pem_base64",
                "trust_bundle_sha256",
            },
            f"certificate authority {index}",
        )
        if (
            re.fullmatch(r"sha256:[a-f0-9]{64}", str(authority["ca_key_id"]))
            is None
            or re.fullmatch(
                r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?/[A-Za-z0-9._:-]{1,253}",
                str(authority["signer_name"]),
            )
            is None
            or authority["revocation_mode"]
            not in {"certificate-revocation-list", "ocsp+certificate-revocation-list"}
            or re.fullmatch(
                r"[A-Za-z0-9._:/-]{8,256}", str(authority["issuance_log_id"])
            )
            is None
        ):
            fail("certificate-authority inventory contains an unsafe issuer")
        trust_bundle_pem = canonical_base64(
            authority["trust_bundle_pem_base64"],
            f"certificate authority {index} trust bundle",
            maximum_size=4 * 1024 * 1024,
        )
        trust_anchor_der = canonical_pem_blocks(
            trust_bundle_pem,
            pem_label="CERTIFICATE",
            label=f"certificate authority {index} trust bundle",
            maximum_blocks=32,
        )
        trust_anchor_key_ids = sorted(
            {
                "sha256:"
                + openssl_public_key_sha256(
                    item, command="x509", input_format="DER"
                )
                for item in trust_anchor_der
            }
        )
        if (
            hashlib.sha256(trust_bundle_pem).hexdigest()
            != digest(authority["trust_bundle_sha256"], "CA trust bundle")
            or authority["trust_anchor_key_ids"] != trust_anchor_key_ids
            or not trust_anchor_key_ids
        ):
            fail("certificate authority trust bundle does not bind its enrolled roots")
        digest(
            authority["response_attestation_sha256"],
            "CA response attestation",
        )
        ca_authorities.append(authority)
    ca_authorities.sort(key=canonical_sha256)
    if ca_authorities != boundary["enrolled_certificate_authorities"]:
        fail("native certificate-authority inventory differs from signed enrollment")
    ca_runtime = {
        (str(authority["signer_name"]), str(authority["ca_key_id"])): {
            "trust_bundle_pem": canonical_base64(
                authority["trust_bundle_pem_base64"],
                "enrolled CA trust bundle",
                maximum_size=4 * 1024 * 1024,
            ),
            "trust_anchor_der": canonical_pem_blocks(
                canonical_base64(
                    authority["trust_bundle_pem_base64"],
                    "enrolled CA trust bundle",
                    maximum_size=4 * 1024 * 1024,
                ),
                pem_label="CERTIFICATE",
                label="enrolled CA trust bundle",
                maximum_blocks=32,
            ),
            "trust_bundle_sha256": authority["trust_bundle_sha256"],
        }
        for authority in ca_authorities
    }
    issued_certificates, issuance_history_start, issuance_observed_through = (
        native_ca_history_records(
            ca_history["issuance_history"],
            label="native certificate issuance history",
            cluster_id=cluster_id,
            record_key="serial_hex",
        )
    )
    revocations, revocation_history_start, revocation_observed_through = (
        native_ca_history_records(
            ca_history["revocation_history"],
            label="native certificate revocation history",
            cluster_id=cluster_id,
            record_key="serial_hex",
        )
    )
    required_history_start = timestamp(
        boundary["certificate_history_start"],
        "signed certificate history start",
    )
    cluster_created_at = timestamp(
        ca_history["cluster_created_at"],
        "native certificate-authority cluster creation",
    )
    if (
        ca_history["schema"]
        != "fs2-serve.nebius.ai/public-edge-certificate-authority-history/v1"
        or ca_history["cluster_id"] != cluster_id
        or ca_history["authority_snapshot_id"] != boundary["authority_snapshot_id"]
        or ca_history["cluster_created_at"] != boundary["cluster_created_at"]
        or required_history_start > cluster_created_at
        or issuance_history_start != required_history_start
        or revocation_history_start != required_history_start
        or issuance_observed_through != ca_collected
        or revocation_observed_through != ca_collected
        or ca_history_summary
        != {
            "cluster_id": cluster_id,
            "authority_snapshot_id": boundary["authority_snapshot_id"],
            "cluster_created_at": boundary["cluster_created_at"],
            "collected_at": ca_history["collected_at"],
            "history_start": boundary["certificate_history_start"],
            "issuance_count": len(issued_certificates),
            "revocation_count": len(revocations),
            "raw_export_sha256": hashlib.sha256(ca_history_raw).hexdigest(),
        }
    ):
        fail("certificate-authority history is incomplete or not receipt-bound")

    provider = exact_object(
        verify_native_authority_export(
            provider_raw,
            PREVENTIVE_PROVIDER_IAM_EXPORT_FILENAME,
            response_authorities=response_authorities,
            role="provider-iam-native-response-attestor",
            endpoint="api.nebius.cloud",
        ),
        {
            "schema",
            "authority_snapshot_id",
            "collected_at",
            "provider_api",
            "project_id",
            "cluster_id",
            "policy_id",
            "policy_get",
            "access_binding_list",
        },
        "authoritative provider-IAM export",
    )
    provider_collected = timestamp(provider["collected_at"], "provider-IAM collected_at")
    policy_get = exact_object(
        provider["policy_get"],
        {"request", "response", "request_id"},
        "native provider-IAM policy get",
    )
    policy_request = exact_object(
        policy_get["request"],
        {"operation", "policy_id", "project_id"},
        "native provider-IAM policy request",
    )
    policy_response = exact_object(
        policy_get["response"],
        {"metadata", "spec"},
        "native provider-IAM policy response",
    )
    policy_metadata = exact_object(
        policy_response["metadata"],
        {"id", "parent_id", "resource_version"},
        "native provider-IAM policy metadata",
    )
    policy_spec = exact_object(
        policy_response["spec"],
        {
            "default_effect",
            "protected_actions",
            "protected_resource_names",
            "protected_resources",
        },
        "native provider-IAM policy spec",
    )
    bindings, binding_revision = native_list_items(
        provider["access_binding_list"],
        label="provider IAM access bindings",
        api_group="iam.nebius.ai",
        resource="accessbindings",
    )

    def selector_values(value: object, label: str) -> set[str]:
        return {
            string_value(item, f"{label} item")
            for item in list_value(value, label)
        }

    def selectors_overlap(actual: set[str], expected: Sequence[str]) -> bool:
        expected_values = set(expected)
        return (
            not actual
            or "*" in actual
            or "*" in expected_values
            or bool(actual & expected_values)
        )

    def binding_overlaps_contract(
        *,
        actions: set[str],
        resource_names: set[str],
        resource_contracts: Sequence[object],
        expected: Mapping[str, Any],
    ) -> bool:
        expected_actions = set(expected["actions"])
        if not selectors_overlap(actions, sorted(expected_actions)):
            return False
        if not selectors_overlap(resource_names, expected["names"]):
            return False
        if not resource_contracts or "*" in resource_contracts:
            return True
        for index, raw_selector in enumerate(resource_contracts):
            if raw_selector == "*":
                return True
            selector = object_value(
                raw_selector, f"provider protected-resource selector {index}"
            )
            selector_group = selector.get("api_group", "*")
            selector_version = selector.get("api_version", "*")
            if selector_group not in {"*", expected["api_group"]} or selector_version not in {
                "*",
                expected["api_version"],
            }:
                continue
            selector_resources = selector_values(
                selector.get("resources", []),
                "provider protected-resource resources",
            )
            selector_namespaces = selector_values(
                selector.get("namespaces", []),
                "provider protected-resource namespaces",
            )
            selector_names = selector_values(
                selector.get("names", []),
                "provider protected-resource names",
            )
            selector_actions = selector_values(
                selector.get("actions", []),
                "provider protected-resource actions",
            )
            if (
                selectors_overlap(selector_resources, expected["resources"])
                and selectors_overlap(selector_namespaces, expected["namespaces"])
                and selectors_overlap(selector_names, expected["names"])
                and selectors_overlap(selector_actions, expected["actions"])
            ):
                return True
        return False

    protected_bindings: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for raw_binding in bindings:
        binding = exact_object(
            raw_binding,
            {"apiVersion", "kind", "metadata", "spec"},
            "native provider access binding",
        )
        if binding["apiVersion"] != "iam.nebius.ai/v1" or binding["kind"] != "AccessBinding":
            fail("provider IAM export contains a non-native access binding")
        native_object_identity(binding, "provider access binding")
        spec = exact_object(
            binding["spec"],
            {
                "effect",
                "subject",
                "actions",
                "resourceNames",
                "protectedResources",
                "condition",
            },
            "native provider access-binding spec",
        )
        actions = selector_values(
            spec["actions"], "provider access-binding actions"
        )
        resource_names = selector_values(
            spec["resourceNames"], "provider access-binding resource names"
        )
        resource_contracts = list_value(
            spec["protectedResources"],
            "provider access-binding protected resources",
        )
        if any(
            binding_overlaps_contract(
                actions=actions,
                resource_names=resource_names,
                resource_contracts=resource_contracts,
                expected=expected,
            )
            for expected in protected_resource_contract
        ):
            protected_bindings.append((binding, spec))
    if len(protected_bindings) != 1:
        fail("native provider IAM closure must contain one protected-resource binding")
    _binding, protected_spec = protected_bindings[0]
    provider_subject = exact_object(
        protected_spec["subject"], {"type", "id"}, "provider controller subject"
    )
    provider_condition = exact_object(
        protected_spec["condition"],
        {"project_id", "cluster_id", "configuration_sha256", "authority_snapshot_id"},
        "provider controller condition",
    )
    if (
        provider["schema"]
        != "fs2-serve.nebius.ai/public-edge-provider-iam-native-export/v3"
        or provider["authority_snapshot_id"] != boundary["authority_snapshot_id"]
        or provider["provider_api"] != provider_summary["provider_api"]
        or provider["project_id"] != project_id
        or provider["cluster_id"] != cluster_id
        or provider["policy_id"] != boundary["provider_iam_policy_id"]
        or policy_request
        != {
            "operation": "get",
            "policy_id": boundary["provider_iam_policy_id"],
            "project_id": project_id,
        }
        or policy_metadata
        != {
            "id": boundary["provider_iam_policy_id"],
            "parent_id": project_id,
            "resource_version": provider_summary["resource_version"],
        }
        or binding_revision != provider_summary["binding_resource_version"]
        or policy_spec["default_effect"] != "DENY"
        or policy_spec["protected_resource_names"] != protected_names
        or policy_spec["protected_resources"] != protected_resource_contract
        or policy_spec["protected_actions"] != protected_actions
        or protected_spec["effect"] != "ALLOW"
        or provider_subject
        != {
            "type": "serviceAccount",
            "id": boundary["controller_provider_principal_id"],
        }
        or protected_spec["resourceNames"] != protected_names
        or protected_spec["protectedResources"] != protected_resource_contract
        or protected_spec["actions"] != protected_actions
        or provider_condition
        != {
            "project_id": project_id,
            "cluster_id": cluster_id,
            "configuration_sha256": boundary["configuration_sha256"],
            "authority_snapshot_id": boundary["authority_snapshot_id"],
        }
    ):
        fail("provider-IAM raw policy does not enforce exact-controller default deny")

    apiserver = exact_object(
        verify_native_authority_export(
            apiserver_raw,
            PREVENTIVE_APISERVER_EXPORT_FILENAME,
            response_authorities=response_authorities,
            role="kubernetes-apiserver-native-response-attestor",
            endpoint=f"kubernetes://{cluster_id}/configuration",
        ),
        {
            "schema",
            "authority_snapshot_id",
            "collected_at",
            "cluster_id",
            "enforcement_id",
            "resource_version",
            "configuration_sha256",
            "authentication_configuration",
            "authorization_configuration",
            "admission_configuration",
        },
        "authoritative API-server enforcement export",
    )
    apiserver_collected = timestamp(
        apiserver["collected_at"], "API-server enforcement collected_at"
    )
    authentication = exact_object(
        apiserver["authentication_configuration"],
        {
            "anonymous",
            "authentication_webhooks",
            "bootstrap_tokens",
            "client_certificate",
            "oidc_issuers",
            "provider_control_plane",
            "requestheader",
            "service_accounts",
            "static_tokens",
        },
        "native API-server authentication configuration",
    )
    authorization = exact_object(
        apiserver["authorization_configuration"],
        {"modes", "webhooks"},
        "native API-server authorization configuration",
    )
    admission = exact_object(
        apiserver["admission_configuration"],
        {
            "failure_policy",
            "match_policy",
            "protected_resources",
            "default_decision",
            "allowed_controller",
            "plugin",
            "snapshot_fence",
        },
        "native API-server admission configuration",
    )
    # The exact field set is the enabled API server's complete native
    # authenticator inventory.  Derive the reviewed paths from that inventory
    # rather than accepting a signer-supplied list of conclusions.
    derived_identity_paths = {
        "direct-user",
        "csr-approval",
        "csr-signing",
        "impersonated-group",
        "impersonated-uid",
        "impersonated-user",
        "impersonated-userextra",
    }
    authenticator_path = {
        "anonymous": "anonymous",
        "authentication_webhooks": "authentication-webhook",
        "bootstrap_tokens": "bootstrap-token",
        "client_certificate": "client-certificate",
        "oidc_issuers": "oidc",
        "provider_control_plane": "provider-control-plane",
        "requestheader": "requestheader-front-proxy",
        "service_accounts": "service-account-token",
        "static_tokens": "static-token",
    }
    derived_identity_paths.update(authenticator_path.values())
    if object_value(authentication["client_certificate"], "client-certificate authenticator").get("enabled"):
        derived_identity_paths.update({"kubelet-client-certificate", "node-credential"})
    for key in (
        "authentication_webhooks",
        "bootstrap_tokens",
        "oidc_issuers",
        "static_tokens",
    ):
        list_value(authentication[key], f"API-server {key}")
    for key in (
        "provider_control_plane",
        "requestheader",
        "service_accounts",
    ):
        configuration = object_value(authentication[key], f"API-server {key}")
        if set(configuration) != {"enabled", "configuration_sha256"}:
            fail(f"API-server {key} configuration is not exact")
        if not isinstance(configuration["enabled"], bool):
            fail(f"API-server {key} enabled state is not boolean")
        digest(configuration["configuration_sha256"], f"API-server {key} configuration")
    client_certificate_authenticator = exact_object(
        authentication["client_certificate"],
        {
            "configuration_sha256",
            "enabled",
            "issuer_inventory_sha256",
            "maximum_status_age_seconds",
            "revocation_fail_closed",
            "revocation_inventory_sha256",
            "revocation_mode",
        },
        "API-server client-certificate authenticator",
    )
    status_age = client_certificate_authenticator["maximum_status_age_seconds"]
    if (
        not isinstance(client_certificate_authenticator["enabled"], bool)
        or not isinstance(status_age, int)
        or isinstance(status_age, bool)
        or not 1 <= status_age <= 300
        or client_certificate_authenticator["revocation_fail_closed"] is not True
        or client_certificate_authenticator["revocation_mode"]
        not in {"certificate-revocation-list", "ocsp+certificate-revocation-list"}
        or client_certificate_authenticator["issuer_inventory_sha256"]
        != canonical_sha256(ca_authorities)
        or client_certificate_authenticator["revocation_inventory_sha256"]
        != canonical_sha256(revocations)
    ):
        fail("API-server client-certificate revocation enforcement is not exact")
    digest(
        client_certificate_authenticator["configuration_sha256"],
        "API-server client-certificate configuration",
    )
    snapshot_fence = exact_object(
        admission["snapshot_fence"],
        {
            "failure_policy",
            "maximum_age_seconds",
            "protected_resources_sha256",
            "snapshot_id",
        },
        "API-server authority snapshot fence",
    )
    if (
        apiserver["schema"]
        != "fs2-serve.nebius.ai/public-edge-apiserver-native-export/v4"
        or apiserver["authority_snapshot_id"] != boundary["authority_snapshot_id"]
        or apiserver["cluster_id"] != cluster_id
        or apiserver["enforcement_id"] != boundary["apiserver_enforcement_id"]
        or apiserver["resource_version"] != apiserver_summary["resource_version"]
        or apiserver["configuration_sha256"] != boundary["configuration_sha256"]
        or authentication["anonymous"] is not False
        or authentication["bootstrap_tokens"] != []
        or authentication["static_tokens"] != []
        or admission["plugin"] != "ExternalPreventiveBoundary"
        or admission["failure_policy"] != "Fail"
        or admission["match_policy"] != "Equivalent"
        or admission["protected_resources"] != protected_resource_contract
        or admission["default_decision"] != "Deny"
        or admission["allowed_controller"] != expected_controller
        or snapshot_fence
        != {
            "failure_policy": "Fail",
            "maximum_age_seconds": int(MAX_PREVENTIVE_SNAPSHOT_AGE.total_seconds()),
            "protected_resources_sha256": canonical_sha256(protected_resource_contract),
            "snapshot_id": boundary["authority_snapshot_id"],
        }
        or authorization["modes"] != ["Node", "RBAC"]
        or authorization["webhooks"] != []
        or sorted(derived_identity_paths) != boundary["identity_paths"]
    ):
        fail("API-server raw export does not enforce the protected exact-controller boundary")

    identity = exact_object(
        verify_native_authority_export(
            identity_raw,
            PREVENTIVE_IDENTITY_REVIEW_FILENAME,
            response_authorities=response_authorities,
            role="kubernetes-rbac-native-response-attestor",
            endpoint=f"kubernetes://{cluster_id}/rbac-csr",
        ),
        {
            "schema",
            "authority_snapshot_id",
            "collected_at",
            "project_id",
            "cluster_id",
            "cluster_roles",
            "cluster_role_bindings",
            "roles",
            "role_bindings",
            "certificate_signing_requests",
            "service_accounts",
            "secret_metadata",
            "secret_authority_classifications",
            "pods",
            "replica_sets",
            "replication_controllers",
            "deployments",
            "stateful_sets",
            "daemon_sets",
            "jobs",
            "cron_jobs",
            "validating_webhook_configurations",
            "mutating_webhook_configurations",
            "custom_resource_definitions",
            "public_edge_node_authority_approvals",
        },
        "authoritative RBAC/impersonation export",
    )
    identity_collected = timestamp(
        identity["collected_at"], "RBAC/impersonation collected_at"
    )
    cluster_roles, cluster_roles_rv = native_list_items(
        identity["cluster_roles"],
        label="Kubernetes ClusterRoles",
        api_group="rbac.authorization.k8s.io",
        resource="clusterroles",
    )
    cluster_bindings, cluster_bindings_rv = native_list_items(
        identity["cluster_role_bindings"],
        label="Kubernetes ClusterRoleBindings",
        api_group="rbac.authorization.k8s.io",
        resource="clusterrolebindings",
    )
    roles, roles_rv = native_list_items(
        identity["roles"],
        label="Kubernetes Roles",
        api_group="rbac.authorization.k8s.io",
        resource="roles",
    )
    role_bindings, role_bindings_rv = native_list_items(
        identity["role_bindings"],
        label="Kubernetes RoleBindings",
        api_group="rbac.authorization.k8s.io",
        resource="rolebindings",
    )
    csrs, csrs_rv = native_list_items(
        identity["certificate_signing_requests"],
        label="Kubernetes CertificateSigningRequests",
        api_group="certificates.k8s.io",
        resource="certificatesigningrequests",
    )
    service_accounts, service_accounts_rv = native_list_items(
        identity["service_accounts"],
        label="Kubernetes ServiceAccounts",
        api_group="",
        resource="serviceaccounts",
    )
    secret_metadata, secrets_rv = native_list_items(
        identity["secret_metadata"],
        label="Kubernetes Secret metadata",
        api_group="",
        resource="secrets",
        representation="partial-object-metadata",
    )
    raw_secret_classifications = list_value(
        identity["secret_authority_classifications"],
        "content-attested Secret classifications",
    )
    secret_metadata_by_uid: dict[str, Mapping[str, Any]] = {
        string_value(
            object_value(secret.get("metadata"), "native Secret metadata").get("uid"),
            "native Secret UID",
        ): secret
        for secret in secret_metadata
    }
    secret_classifications: list[dict[str, Any]] = []
    authority_secret_records: list[dict[str, Any]] = []
    authority_secret_names_by_namespace: dict[str, set[str]] = {}
    allowed_credential_classes = {
        "controller-kubeconfig",
        "controller-service-account-token",
        "provider-credential",
        "public-edge-broker-credential",
    }
    seen_classified_secret_uids: set[str] = set()
    for index, raw_classification in enumerate(raw_secret_classifications):
        classification = exact_object(
            raw_classification,
            {
                "content_attestation_sha256",
                "credential_classes",
                "data_key_names",
                "kms_key_id",
                "name",
                "namespace",
                "resource_version",
                "secret_type",
                "uid",
            },
            f"Secret authority classification {index}",
        )
        uid = string_value(classification["uid"], "classified Secret UID")
        metadata_secret = secret_metadata_by_uid.get(uid)
        if metadata_secret is None or uid in seen_classified_secret_uids:
            fail("Secret authority classification is absent from the complete inventory")
        seen_classified_secret_uids.add(uid)
        namespace, name = native_object_identity(
            metadata_secret, "classified native Secret metadata"
        )
        metadata_value = object_value(
            metadata_secret.get("metadata"), "classified native Secret metadata"
        )
        credential_classes = list_value(
            classification["credential_classes"], "Secret credential classes"
        )
        data_key_names = list_value(
            classification["data_key_names"], "Secret data-key names"
        )
        if (
            namespace != classification["namespace"]
            or name != classification["name"]
            or metadata_value.get("resourceVersion")
            != classification["resource_version"]
            or credential_classes != sorted(set(credential_classes))
            or not set(credential_classes) <= allowed_credential_classes
            or data_key_names != sorted(set(data_key_names))
            or not all(isinstance(item, str) and item for item in data_key_names)
            or re.fullmatch(
                r"sha256:[a-f0-9]{64}", str(classification["kms_key_id"])
            )
            is None
        ):
            fail("Secret authority classification does not bind exact native metadata")
        digest(
            classification["content_attestation_sha256"],
            "Secret content attestation",
        )
        normalized_classification = dict(classification)
        secret_classifications.append(normalized_classification)
        if credential_classes:
            authority_secret_records.append(normalized_classification)
            authority_secret_names_by_namespace.setdefault(namespace, set()).add(name)
    if seen_classified_secret_uids != set(secret_metadata_by_uid):
        fail("one or more Secrets lacks a content-bound authority classification")
    secret_classifications.sort(key=canonical_sha256)
    authority_secret_records.sort(key=canonical_sha256)
    if authority_secret_records != boundary["enrolled_credential_secrets"]:
        fail("credential-bearing Secret inventory differs from signed enrollment")
    pods, pods_rv = native_list_items(
        identity["pods"], label="Kubernetes Pods", api_group="", resource="pods"
    )
    replica_sets, replica_sets_rv = native_list_items(
        identity["replica_sets"],
        label="Kubernetes ReplicaSets",
        api_group="apps",
        resource="replicasets",
    )
    replication_controllers, replication_controllers_rv = native_list_items(
        identity["replication_controllers"],
        label="Kubernetes ReplicationControllers",
        api_group="",
        resource="replicationcontrollers",
    )
    deployments, deployments_rv = native_list_items(
        identity["deployments"],
        label="Kubernetes Deployments",
        api_group="apps",
        resource="deployments",
    )
    stateful_sets, stateful_sets_rv = native_list_items(
        identity["stateful_sets"],
        label="Kubernetes StatefulSets",
        api_group="apps",
        resource="statefulsets",
    )
    daemon_sets, daemon_sets_rv = native_list_items(
        identity["daemon_sets"],
        label="Kubernetes DaemonSets",
        api_group="apps",
        resource="daemonsets",
    )
    jobs, jobs_rv = native_list_items(
        identity["jobs"], label="Kubernetes Jobs", api_group="batch", resource="jobs"
    )
    cron_jobs, cron_jobs_rv = native_list_items(
        identity["cron_jobs"],
        label="Kubernetes CronJobs",
        api_group="batch",
        resource="cronjobs",
    )
    validating_webhooks, validating_webhooks_rv = native_list_items(
        identity["validating_webhook_configurations"],
        label="Kubernetes ValidatingWebhookConfigurations",
        api_group="admissionregistration.k8s.io",
        resource="validatingwebhookconfigurations",
    )
    mutating_webhooks, mutating_webhooks_rv = native_list_items(
        identity["mutating_webhook_configurations"],
        label="Kubernetes MutatingWebhookConfigurations",
        api_group="admissionregistration.k8s.io",
        resource="mutatingwebhookconfigurations",
    )
    custom_resource_definitions, crds_rv = native_list_items(
        identity["custom_resource_definitions"],
        label="Kubernetes CustomResourceDefinitions",
        api_group="apiextensions.k8s.io",
        resource="customresourcedefinitions",
    )
    approval_objects, approval_objects_rv = native_list_items(
        identity["public_edge_node_authority_approvals"],
        label="PublicEdgeNodeAuthorityApproval objects",
        api_group="security.fs2.nebius.ai",
        resource="publicedgenodeauthorityapprovals",
    )

    protected_admission_resources = {
        "": {
            "nodes",
            "nodes/proxy",
            "pods",
            "pods/attach",
            "pods/ephemeralcontainers",
            "pods/exec",
            "pods/portforward",
            "replicationcontrollers",
            "secrets",
            "serviceaccounts",
            "serviceaccounts/token",
        },
        "admissionregistration.k8s.io": {
            "mutatingwebhookconfigurations",
            "validatingadmissionpolicies",
            "validatingadmissionpolicybindings",
            "validatingwebhookconfigurations",
        },
        "apiextensions.k8s.io": {"customresourcedefinitions"},
        "apps": {"daemonsets", "deployments", "replicasets", "statefulsets"},
        "batch": {"cronjobs", "jobs"},
        "certificates.k8s.io": {
            "certificatesigningrequests",
            "certificatesigningrequests/approval",
        },
        "rbac.authorization.k8s.io": {
            "clusterrolebindings",
            "clusterroles",
            "rolebindings",
            "roles",
        },
        "security.fs2.nebius.ai": {"publicedgenodeauthorityapprovals"},
    }

    def admission_rule_intersects_authority(raw_rule: object, label: str) -> bool:
        rule = exact_object(
            raw_rule,
            {"apiGroups", "apiVersions", "operations", "resources", "scope"},
            label,
        )
        groups = set(list_value(rule["apiGroups"], f"{label} apiGroups"))
        resources = set(list_value(rule["resources"], f"{label} resources"))
        operations = set(list_value(rule["operations"], f"{label} operations"))
        versions = list_value(rule["apiVersions"], f"{label} apiVersions")
        if (
            not groups
            or not resources
            or not versions
            or operations.isdisjoint({"*", "CONNECT", "CREATE", "DELETE", "UPDATE"})
            or rule["scope"] not in {"*", "Cluster", "Namespaced"}
        ):
            return False
        for protected_group, protected_resources in protected_admission_resources.items():
            if protected_group not in groups and "*" not in groups:
                continue
            if any(
                candidate == "*"
                or candidate in protected_resources
                or any(
                    candidate.endswith("/*")
                    and protected.startswith(candidate.removesuffix("*"))
                    for protected in protected_resources
                )
                for candidate in resources
            ):
                return True
        return False

    dangerous_validating_webhooks: list[dict[str, Any]] = []
    dangerous_mutating_webhooks: list[dict[str, Any]] = []
    for configurations, webhook_kind, output in (
        (
            validating_webhooks,
            "ValidatingWebhookConfiguration",
            dangerous_validating_webhooks,
        ),
        (
            mutating_webhooks,
            "MutatingWebhookConfiguration",
            dangerous_mutating_webhooks,
        ),
    ):
        for configuration in configurations:
            _namespace, configuration_name = native_object_identity(
                configuration, f"native {webhook_kind}"
            )
            configuration_metadata = object_value(
                configuration.get("metadata"), f"native {webhook_kind} metadata"
            )
            for webhook_index, raw_webhook in enumerate(
                list_value(
                    configuration.get("webhooks"), f"native {webhook_kind} webhooks"
                )
            ):
                webhook = object_value(
                    raw_webhook, f"native {webhook_kind} webhook {webhook_index}"
                )
                rules = list_value(
                    webhook.get("rules", []),
                    f"native {webhook_kind} webhook {webhook_index} rules",
                )
                if not any(
                    admission_rule_intersects_authority(
                        rule,
                        f"native {webhook_kind} webhook {webhook_index} rule",
                    )
                    for rule in rules
                ):
                    continue
                client_config = exact_object(
                    webhook.get("clientConfig"),
                    {"caBundle", "service"},
                    f"native {webhook_kind} clientConfig",
                )
                service = exact_object(
                    client_config["service"],
                    {"name", "namespace", "path", "port"},
                    f"native {webhook_kind} service reference",
                )
                canonical_base64(
                    client_config["caBundle"],
                    f"native {webhook_kind} CA bundle",
                    maximum_size=1024 * 1024,
                )
                review_versions = list_value(
                    webhook.get("admissionReviewVersions"),
                    f"native {webhook_kind} admissionReviewVersions",
                )
                timeout_seconds = webhook.get("timeoutSeconds")
                if (
                    review_versions != sorted(set(review_versions))
                    or "v1" not in review_versions
                    or not isinstance(timeout_seconds, int)
                    or isinstance(timeout_seconds, bool)
                    or not 1 <= timeout_seconds <= 10
                    or webhook.get("sideEffects") not in {"None", "NoneOnDryRun"}
                    or not all(
                        isinstance(service.get(key), str) and service.get(key)
                        for key in ("name", "namespace", "path")
                    )
                    or not isinstance(service.get("port"), int)
                ):
                    fail("authority-intersecting admission webhook is not bounded")
                output.append(
                    {
                        "client_config_sha256": canonical_sha256(client_config),
                        "configuration_name": configuration_name,
                        "configuration_resource_version": configuration_metadata.get(
                            "resourceVersion"
                        ),
                        "configuration_uid": configuration_metadata.get("uid"),
                        "failure_policy": webhook.get("failurePolicy"),
                        "kind": webhook_kind,
                        "match_conditions_sha256": canonical_sha256(
                            webhook.get("matchConditions", [])
                        ),
                        "match_policy": webhook.get("matchPolicy"),
                        "namespace_selector_sha256": canonical_sha256(
                            webhook.get("namespaceSelector", {})
                        ),
                        "object_selector_sha256": canonical_sha256(
                            webhook.get("objectSelector", {})
                        ),
                        "reinvocation_policy": webhook.get("reinvocationPolicy"),
                        "rules_sha256": canonical_sha256(rules),
                        "side_effects": webhook.get("sideEffects"),
                        "timeout_seconds": timeout_seconds,
                        "webhook_name": string_value(
                            webhook.get("name"), f"native {webhook_kind} name"
                        ),
                    }
                )
    if dangerous_mutating_webhooks:
        fail("a mutating webhook can alter protected identity or authority resources")
    dangerous_validating_webhooks.sort(key=canonical_sha256)
    if (
        any(
            item["failure_policy"] != "Fail"
            or item["match_policy"] != "Equivalent"
            for item in dangerous_validating_webhooks
        )
        or dangerous_validating_webhooks
        != boundary["enrolled_admission_webhooks"]
    ):
        fail("validating webhook authority closure differs from signed enrollment")

    def native_pod_template(value: Mapping[str, Any], kind: str) -> Mapping[str, Any]:
        spec = object_value(value.get("spec"), f"native {kind} spec")
        if kind == "Pod":
            return spec
        if kind == "CronJob":
            job_template = object_value(spec.get("jobTemplate"), "CronJob jobTemplate")
            job_spec = object_value(job_template.get("spec"), "CronJob jobTemplate spec")
            template = object_value(job_spec.get("template"), "CronJob Pod template")
        else:
            template = object_value(spec.get("template"), f"native {kind} Pod template")
        return object_value(template.get("spec"), f"native {kind} Pod template spec")

    def pod_secret_references(template: Mapping[str, Any], label: str) -> set[str]:
        references: set[str] = set()
        for raw_pull_secret in list_value(
            template.get("imagePullSecrets", []), f"{label} imagePullSecrets"
        ):
            pull_secret = object_value(raw_pull_secret, f"{label} imagePullSecret")
            references.add(string_value(pull_secret.get("name"), f"{label} pull Secret"))
        for raw_volume in list_value(template.get("volumes", []), f"{label} volumes"):
            volume = object_value(raw_volume, f"{label} volume")
            if "secret" in volume:
                secret = object_value(volume["secret"], f"{label} Secret volume")
                references.add(
                    string_value(secret.get("secretName"), f"{label} Secret volume name")
                )
            if "projected" in volume:
                projected = object_value(volume["projected"], f"{label} projected volume")
                for raw_source in list_value(
                    projected.get("sources", []), f"{label} projected sources"
                ):
                    source = object_value(raw_source, f"{label} projected source")
                    if "secret" in source:
                        secret = object_value(source["secret"], f"{label} projected Secret")
                        references.add(
                            string_value(secret.get("name"), f"{label} projected Secret name")
                        )
        containers = [
            *list_value(template.get("initContainers", []), f"{label} initContainers"),
            *list_value(template.get("containers", []), f"{label} containers"),
            *list_value(template.get("ephemeralContainers", []), f"{label} ephemeralContainers"),
        ]
        for raw_container in containers:
            container = object_value(raw_container, f"{label} container")
            for raw_env in list_value(container.get("env", []), f"{label} container env"):
                env = object_value(raw_env, f"{label} environment entry")
                value_from = object_value(
                    env.get("valueFrom", {}), f"{label} environment valueFrom"
                )
                if "secretKeyRef" in value_from:
                    secret_ref = object_value(
                        value_from["secretKeyRef"], f"{label} secretKeyRef"
                    )
                    references.add(
                        string_value(secret_ref.get("name"), f"{label} secretKeyRef name")
                    )
            for raw_env_from in list_value(
                container.get("envFrom", []), f"{label} container envFrom"
            ):
                env_from = object_value(raw_env_from, f"{label} envFrom entry")
                if "secretRef" in env_from:
                    secret_ref = object_value(env_from["secretRef"], f"{label} envFrom Secret")
                    references.add(
                        string_value(secret_ref.get("name"), f"{label} envFrom Secret name")
                    )
        return references

    credential_namespaces = sorted(
        {
            *authority_secret_names_by_namespace,
            *([controller_namespace_contract] if controller_namespace_contract else []),
        }
    )
    if credential_namespaces != boundary["credential_namespaces"]:
        fail("credential-bearing namespace closure differs from signed enrollment")
    authority_reachable_workloads: list[dict[str, Any]] = []
    authority_pod_names_by_namespace: dict[str, set[str]] = {}
    for items, kind in (
        (pods, "Pod"),
        (replica_sets, "ReplicaSet"),
        (replication_controllers, "ReplicationController"),
        (deployments, "Deployment"),
        (stateful_sets, "StatefulSet"),
        (daemon_sets, "DaemonSet"),
        (jobs, "Job"),
        (cron_jobs, "CronJob"),
    ):
        for item in items:
            namespace, name = native_object_identity(item, f"native {kind}")
            template = native_pod_template(item, kind)
            references = sorted(pod_secret_references(template, f"native {kind}"))
            authority_references = sorted(
                set(references) & authority_secret_names_by_namespace.get(namespace, set())
            )
            uses_controller_sa = (
                namespace == controller_namespace_contract
                and template.get("serviceAccountName")
                == (
                    controller_username_parts[3]
                    if len(controller_username_parts) == 4
                    else None
                )
            )
            if not authority_references and not uses_controller_sa:
                continue
            metadata_value = object_value(item.get("metadata"), f"native {kind} metadata")
            record = {
                "api_version": item.get("apiVersion"),
                "authority_secret_names": authority_references,
                "kind": kind,
                "name": name,
                "namespace": namespace,
                "pod_template_sha256": canonical_sha256(template),
                "service_account_name": template.get("serviceAccountName"),
                "uid": metadata_value.get("uid"),
            }
            authority_reachable_workloads.append(record)
            if kind == "Pod":
                authority_pod_names_by_namespace.setdefault(namespace, set()).add(name)
    authority_reachable_workloads.sort(key=canonical_sha256)
    if authority_reachable_workloads != boundary["enrolled_credential_workloads"]:
        fail("credential-reachable workload closure differs from signed enrollment")
    authority_service_accounts_by_namespace: dict[str, set[str]] = {}
    for workload in authority_reachable_workloads:
        service_account_name = workload.get("service_account_name")
        if isinstance(service_account_name, str) and service_account_name:
            authority_service_accounts_by_namespace.setdefault(
                str(workload["namespace"]), set()
            ).add(service_account_name)

    role_rules: dict[tuple[str, str], Sequence[object]] = {}
    for native in [*cluster_roles, *roles]:
        namespace, name = native_object_identity(native, "native RBAC role")
        expected_kind = "ClusterRole" if not namespace else "Role"
        if native.get("apiVersion") != "rbac.authorization.k8s.io/v1" or native.get("kind") != expected_kind:
            fail("RBAC export contains a non-native role object")
        role_rules[(namespace, name)] = list_value(native.get("rules", []), "native RBAC role rules")
    controller_username_parts = str(boundary["controller_username"]).split(":")
    expected_controller_subject = (
        {
            "kind": "ServiceAccount",
            "name": controller_username_parts[3],
            "namespace": controller_username_parts[2],
        }
        if len(controller_username_parts) == 4
        and controller_username_parts[:2] == ["system", "serviceaccount"]
        else {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "User",
            "name": boundary["controller_username"],
        }
    )
    controller_namespace = (
        expected_controller_subject.get("namespace", "")
        if expected_controller_subject["kind"] == "ServiceAccount"
        else ""
    )
    allowed_capabilities = {
        "admission-authority-mutation",
        "controller-secret-read",
        "controller-serviceaccount-mutation",
        "controller-workload-mutation",
        "csr-authority",
        "impersonation",
        "node-or-kubelet-proxy",
        "pod-subresource-access",
        "protected-policy-mutation",
        "rbac-delegation",
        "serviceaccount-token-mint",
    }
    enrolled_identities: list[dict[str, Any]] = []
    enrolled_capabilities: set[tuple[str, str]] = set()
    for index, raw_enrollment in enumerate(
        list_value(boundary["enrolled_identities"], "signed enrolled identity paths")
    ):
        enrollment = exact_object(
            raw_enrollment,
            {
                "authority_id",
                "capabilities",
                "expires_at",
                "provider_principal_id",
                "subject",
            },
            f"enrolled identity {index}",
        )
        subject = native_rbac_subject(
            enrollment["subject"], f"enrolled identity {index} subject"
        )
        capabilities = list_value(
            enrollment["capabilities"], f"enrolled identity {index} capabilities"
        )
        expires = timestamp(
            enrollment["expires_at"], f"enrolled identity {index} expiry"
        )
        if (
            capabilities != sorted(set(capabilities))
            or not capabilities
            or not set(capabilities) <= allowed_capabilities
            or expires <= identity_collected
            or expires - identity_collected > MAX_MEMBERSHIP_VALIDITY
            or re.fullmatch(
                r"[a-z][a-z0-9._-]{7,127}",
                str(enrollment["authority_id"]),
            )
            is None
            or re.fullmatch(
                r"serviceaccount-[a-z0-9]+",
                str(enrollment["provider_principal_id"]),
            )
            is None
            or (
                subject.get("kind") == "Group"
                and (
                    subject["name"]
                    in {
                        "system:authenticated",
                        "system:unauthenticated",
                        "system:serviceaccounts",
                    }
                    or subject["name"].startswith("system:serviceaccounts:")
                )
            )
        ):
            fail("enrolled identity grants an unsafe or non-expiring capability")
        normalized_enrollment = {
            "authority_id": enrollment["authority_id"],
            "capabilities": capabilities,
            "expires_at": enrollment["expires_at"],
            "provider_principal_id": enrollment["provider_principal_id"],
            "subject": subject,
        }
        subject_sha256 = canonical_sha256(subject)
        for capability in capabilities:
            key = (subject_sha256, str(capability))
            if key in enrolled_capabilities:
                fail("enrolled identity repeats a subject capability")
            enrolled_capabilities.add(key)
        enrolled_identities.append(normalized_enrollment)
    protected_subjects: list[Mapping[str, Any]] = []
    impersonating_subjects: list[Mapping[str, Any]] = []
    csr_authorities: list[Mapping[str, Any]] = []
    credential_path_subjects: dict[str, list[Mapping[str, Any]]] = {
        "admission-authority-mutation": [],
        "controller-secret-read": [],
        "controller-serviceaccount-mutation": [],
        "controller-workload-mutation": [],
        "csr-authority": [],
        "impersonation": [],
        "node-or-kubelet-proxy": [],
        "pod-subresource-access": [],
        "rbac-delegation": [],
        "serviceaccount-token-mint": [],
    }
    for native in [*cluster_bindings, *role_bindings]:
        namespace, _name = native_object_identity(native, "native RBAC binding")
        expected_kind = "ClusterRoleBinding" if not namespace else "RoleBinding"
        if native.get("apiVersion") != "rbac.authorization.k8s.io/v1" or native.get("kind") != expected_kind:
            fail("RBAC export contains a non-native binding object")
        role_ref = exact_object(
            native.get("roleRef"), {"apiGroup", "kind", "name"}, "native RBAC roleRef"
        )
        if role_ref["apiGroup"] != "rbac.authorization.k8s.io" or role_ref["kind"] not in {"Role", "ClusterRole"}:
            fail("RBAC binding has an unsupported native roleRef")
        role_namespace = namespace if role_ref["kind"] == "Role" else ""
        rules = role_rules.get((role_namespace, role_ref["name"]))
        if rules is None:
            fail("RBAC binding references a role absent from the complete native lists")
        subjects = list_value(native.get("subjects", []), "native RBAC binding subjects")
        normalized_subjects = [native_rbac_subject(item, "RBAC subject") for item in subjects]
        scoped_namespaces = (
            set(credential_namespaces) if not namespace else {namespace}
        )
        reachable_secret_names = set().union(
            *(
                authority_secret_names_by_namespace.get(item, set())
                for item in scoped_namespaces
            )
        )
        reachable_pod_names = set().union(
            *(
                authority_pod_names_by_namespace.get(item, set())
                for item in scoped_namespaces
            )
        )
        reachable_service_account_names = set().union(
            *(
                authority_service_accounts_by_namespace.get(item, set())
                for item in scoped_namespaces
            )
        )
        reaches_credential_namespace = bool(
            scoped_namespaces & set(credential_namespaces)
        )
        for raw_rule in rules:
            rule = object_value(raw_rule, "native RBAC rule")
            if not namespace and native_rule_matches(
                rule,
                api_groups={"admissionregistration.k8s.io"},
                resources={"validatingadmissionpolicies", "validatingadmissionpolicybindings"},
                verbs={"create", "delete", "deletecollection", "patch", "update"},
                resource_names=set(protected_names),
            ):
                protected_subjects.extend(normalized_subjects)
            impersonates = not namespace and (
                native_rule_matches(
                    rule,
                    api_groups={""},
                    resources={"users", "groups", "serviceaccounts"},
                    verbs={"impersonate"},
                )
                or native_rule_matches(
                    rule,
                    api_groups={"authentication.k8s.io"},
                    resources={"uids", "userextras/*"},
                    verbs={"impersonate"},
                )
            )
            if impersonates:
                impersonating_subjects.extend(normalized_subjects)
                credential_path_subjects["impersonation"].extend(normalized_subjects)
            approves_csr = not namespace and (
                native_rule_matches(
                    rule,
                    api_groups={"certificates.k8s.io"},
                    resources={"certificatesigningrequests/approval"},
                    verbs={"update", "patch"},
                )
                or native_rule_matches(
                    rule,
                    api_groups={"certificates.k8s.io"},
                    resources={"signers"},
                    verbs={"approve", "sign"},
                )
            )
            if approves_csr:
                csr_authorities.extend(normalized_subjects)
                credential_path_subjects["csr-authority"].extend(normalized_subjects)
            dangerous_rules = {
                "serviceaccount-token-mint": bool(reachable_service_account_names)
                and native_rule_matches(
                    rule,
                    api_groups={""},
                    resources={"serviceaccounts/token"},
                    verbs={"create"},
                    resource_names=reachable_service_account_names,
                ),
                "controller-secret-read": bool(reachable_secret_names)
                and native_rule_matches(
                    rule,
                    api_groups={""},
                    resources={"secrets"},
                    verbs={"get", "list", "watch"},
                    resource_names=reachable_secret_names,
                ),
                "rbac-delegation": reaches_credential_namespace
                and native_rule_matches(
                    rule,
                    api_groups={"rbac.authorization.k8s.io"},
                    resources={
                        "roles",
                        "clusterroles",
                        "rolebindings",
                        "clusterrolebindings",
                    },
                    verbs={
                        "bind",
                        "escalate",
                        "create",
                        "delete",
                        "deletecollection",
                        "update",
                        "patch",
                    },
                ),
                "pod-subresource-access": bool(reachable_pod_names)
                and native_rule_matches(
                    rule,
                    api_groups={""},
                    resources={
                        "pods/exec",
                        "pods/attach",
                        "pods/portforward",
                        "pods/ephemeralcontainers",
                    },
                    verbs={"create", "get", "patch", "update"},
                    resource_names=reachable_pod_names,
                ),
                "node-or-kubelet-proxy": not namespace
                and native_rule_matches(
                    rule,
                    api_groups={""},
                    resources={"nodes/proxy", "nodes"},
                    verbs={"create", "get", "patch", "update"},
                ),
                "admission-authority-mutation": not namespace
                and (
                    native_rule_matches(
                        rule,
                        api_groups={"admissionregistration.k8s.io"},
                        resources={
                            "validatingwebhookconfigurations",
                            "mutatingwebhookconfigurations",
                            "validatingadmissionpolicies",
                            "validatingadmissionpolicybindings",
                        },
                        verbs={"create", "delete", "deletecollection", "patch", "update"},
                    )
                    or native_rule_matches(
                        rule,
                        api_groups={"apiextensions.k8s.io"},
                        resources={"customresourcedefinitions"},
                        verbs={"create", "delete", "deletecollection", "patch", "update"},
                    )
                    or native_rule_matches(
                        rule,
                        api_groups={"security.fs2.nebius.ai"},
                        resources={"publicedgenodeauthorityapprovals"},
                        verbs={"create", "delete", "deletecollection", "patch", "update"},
                        resource_names={"fs2-public-edge-node-authority-approval"},
                    )
                ),
                "controller-serviceaccount-mutation": bool(
                    reachable_service_account_names
                )
                and native_rule_matches(
                    rule,
                    api_groups={""},
                    resources={"serviceaccounts"},
                    verbs={"create", "delete", "deletecollection", "patch", "update"},
                    resource_names=reachable_service_account_names,
                ),
                "controller-workload-mutation": reaches_credential_namespace
                and (
                    native_rule_matches(
                        rule,
                        api_groups={""},
                        resources={"pods", "replicationcontrollers"},
                        verbs={"create", "delete", "deletecollection", "patch", "update"},
                    )
                    or native_rule_matches(
                        rule,
                        api_groups={"apps"},
                        resources={
                            "deployments",
                            "statefulsets",
                            "daemonsets",
                            "replicasets",
                        },
                        verbs={"create", "delete", "deletecollection", "patch", "update"},
                    )
                    or native_rule_matches(
                        rule,
                        api_groups={"batch"},
                        resources={"jobs", "cronjobs"},
                        verbs={"create", "delete", "deletecollection", "patch", "update"},
                    )
                ),
            }
            for capability, granted in dangerous_rules.items():
                if granted:
                    credential_path_subjects[capability].extend(normalized_subjects)
    controller_default_groups = (
        {
            "system:authenticated",
            "system:serviceaccounts",
            f"system:serviceaccounts:{controller_namespace}",
        }
        if expected_controller_subject["kind"] == "ServiceAccount"
        else {"system:authenticated"}
    )
    if not controller_default_groups <= set(boundary["controller_groups"]):
        fail("signed controller groups omit its effective Kubernetes groups")
    unauthorized_credential_paths = {
        capability: [
            subject
            for subject in subjects
            if (canonical_sha256(subject), capability) not in enrolled_capabilities
        ]
        for capability, subjects in credential_path_subjects.items()
    }
    controller_enrollments = [
        enrollment
        for enrollment in enrolled_identities
        if enrollment["subject"] == expected_controller_subject
        and "protected-policy-mutation" in enrollment["capabilities"]
        and enrollment["provider_principal_id"]
        == boundary["controller_provider_principal_id"]
    ]

    inventory_contracts = (
        (service_accounts, "v1", "ServiceAccount", "ServiceAccount"),
        (secret_metadata, "meta.k8s.io/v1", "PartialObjectMetadata", "Secret metadata"),
        (pods, "v1", "Pod", "Pod"),
        (replica_sets, "apps/v1", "ReplicaSet", "ReplicaSet"),
        (
            replication_controllers,
            "v1",
            "ReplicationController",
            "ReplicationController",
        ),
        (deployments, "apps/v1", "Deployment", "Deployment"),
        (stateful_sets, "apps/v1", "StatefulSet", "StatefulSet"),
        (daemon_sets, "apps/v1", "DaemonSet", "DaemonSet"),
        (jobs, "batch/v1", "Job", "Job"),
        (cron_jobs, "batch/v1", "CronJob", "CronJob"),
        (
            validating_webhooks,
            "admissionregistration.k8s.io/v1",
            "ValidatingWebhookConfiguration",
            "ValidatingWebhookConfiguration",
        ),
        (
            mutating_webhooks,
            "admissionregistration.k8s.io/v1",
            "MutatingWebhookConfiguration",
            "MutatingWebhookConfiguration",
        ),
        (
            custom_resource_definitions,
            "apiextensions.k8s.io/v1",
            "CustomResourceDefinition",
            "CustomResourceDefinition",
        ),
        (
            approval_objects,
            "security.fs2.nebius.ai/v1",
            "PublicEdgeNodeAuthorityApproval",
            "PublicEdgeNodeAuthorityApproval",
        ),
    )
    for items, api_version, kind, label in inventory_contracts:
        for item in items:
            if item.get("apiVersion") != api_version or item.get("kind") != kind:
                fail(f"native {label} inventory contains an unsupported object")
            native_object_identity(item, f"native {label}")

    approval_identities = [
        native_object_identity(item, "native PublicEdgeNodeAuthorityApproval")
        for item in approval_objects
    ]
    if approval_identities != [("", "fs2-public-edge-node-authority-approval")]:
        fail("approval inventory does not contain the one protected parameter root")
    approval_spec = object_value(
        approval_objects[0].get("spec"), "native approval parameter spec"
    )
    if approval_spec.get("preventiveBoundary") != boundary:
        fail("native approval parameter does not bind the signed preventive boundary")
    native_approval_projection = {
        "apiVersion": approval_objects[0].get("apiVersion"),
        "kind": approval_objects[0].get("kind"),
        "metadata": {
            "name": approval_identities[0][1],
            "resourceVersion": object_value(
                approval_objects[0].get("metadata"),
                "native approval parameter metadata",
            ).get("resourceVersion"),
            "uid": object_value(
                approval_objects[0].get("metadata"),
                "native approval parameter metadata",
            ).get("uid"),
        },
        "spec": approval_spec,
        "status": approval_objects[0].get("status"),
    }
    if terraform_json_sha256(native_approval_projection) != terraform_json_sha256(
        approval_projection
    ):
        fail("native approval object differs from the separately signed live projection")
    approval_crds = [
        item
        for item in custom_resource_definitions
        if native_object_identity(item, "native approval CustomResourceDefinition")
        == ("", "publicedgenodeauthorityapprovals.security.fs2.nebius.ai")
    ]
    if len(approval_crds) != 1:
        fail("CRD inventory does not contain the protected approval API root")
    approval_crd_spec = object_value(
        approval_crds[0].get("spec"), "native approval CRD spec"
    )
    approval_crd_names = object_value(
        approval_crd_spec.get("names"), "native approval CRD names"
    )
    approval_crd_versions = list_value(
        approval_crd_spec.get("versions"), "native approval CRD versions"
    )
    storage_versions = [
        version
        for version in approval_crd_versions
        if object_value(version, "native approval CRD version").get("storage") is True
    ]
    served_v1 = [
        version
        for version in approval_crd_versions
        if object_value(version, "native approval CRD version").get("name") == "v1"
        and version.get("served") is True
    ]
    if (
        approval_crd_spec.get("group") != "security.fs2.nebius.ai"
        or approval_crd_spec.get("scope") != "Cluster"
        or approval_crd_names.get("kind") != "PublicEdgeNodeAuthorityApproval"
        or approval_crd_names.get("plural")
        != "publicedgenodeauthorityapprovals"
        or len(storage_versions) != 1
        or len(served_v1) != 1
        or object_value(
            approval_crd_spec.get("conversion", {"strategy": "None"}),
            "native approval CRD conversion",
        ).get("strategy")
        != "None"
    ):
        fail("approval CRD does not expose the exact cluster-scoped v1 parameter API")

    controller_service_accounts = [
        account
        for account in service_accounts
        if native_object_identity(account, "native controller ServiceAccount")
        == (controller_namespace, expected_controller_subject["name"])
    ]
    if expected_controller_subject["kind"] == "ServiceAccount" and len(
        controller_service_accounts
    ) != 1:
        fail("complete ServiceAccount inventory does not contain one controller identity")
    controller_secret_metadata = []
    for secret in secret_metadata:
        namespace, _name = native_object_identity(secret, "native Secret metadata")
        metadata = object_value(secret.get("metadata"), "native Secret metadata")
        annotations = object_value(
            metadata.get("annotations", {}), "native Secret annotations"
        )
        if (
            namespace == controller_namespace
            and annotations.get("kubernetes.io/service-account.name")
            == expected_controller_subject["name"]
        ):
            controller_secret_metadata.append(secret)

    controller_workloads: list[dict[str, Any]] = []
    for items, kind in (
        (pods, "Pod"),
        (replica_sets, "ReplicaSet"),
        (replication_controllers, "ReplicationController"),
        (deployments, "Deployment"),
        (stateful_sets, "StatefulSet"),
        (daemon_sets, "DaemonSet"),
        (jobs, "Job"),
        (cron_jobs, "CronJob"),
    ):
        for item in items:
            namespace, name = native_object_identity(item, f"native {kind}")
            template = native_pod_template(item, kind)
            if (
                namespace == controller_namespace
                and template.get("serviceAccountName")
                == expected_controller_subject["name"]
            ):
                containers = [
                    *list_value(template.get("initContainers", []), f"{kind} initContainers"),
                    *list_value(template.get("containers", []), f"{kind} containers"),
                    *list_value(
                        template.get("ephemeralContainers", []),
                        f"{kind} ephemeralContainers",
                    ),
                ]
                images = sorted(
                    string_value(
                        object_value(container, f"{kind} container").get("image"),
                        f"{kind} container image",
                    )
                    for container in containers
                )
                allowed_image_digests = set(
                    list_value(
                        boundary["controller_allowed_image_digests"],
                        "signed controller image digest closure",
                    )
                )
                observed_image_digests = {
                    image.rsplit("@", 1)[1]
                    for image in images
                    if "@" in image
                }
                if (
                    not images
                    or any(
                        re.fullmatch(r"[^@\s]+@sha256:[a-f0-9]{64}", image) is None
                        for image in images
                    )
                    or observed_image_digests - allowed_image_digests
                    or str(boundary["controller_image_digest"])
                    not in observed_image_digests
                ):
                    fail("controller workload image closure is not immutable and signed")
                metadata = object_value(item.get("metadata"), f"native {kind} metadata")
                controller_workloads.append(
                    {
                        "api_version": item.get("apiVersion"),
                        "images": images,
                        "kind": kind,
                        "name": name,
                        "namespace": namespace,
                        "pod_template_sha256": canonical_sha256(template),
                        "service_account_name": template.get("serviceAccountName"),
                        "uid": metadata.get("uid"),
                    }
                )
    if expected_controller_subject["kind"] == "ServiceAccount" and not controller_workloads:
        fail("complete workload inventory does not contain the signed controller")
    controller_workloads.sort(
        key=lambda item: (
            str(item["namespace"]),
            str(item["kind"]),
            str(item["name"]),
            str(item["uid"]),
        )
    )
    if controller_workloads != boundary["enrolled_controller_workloads"]:
        fail("controller workload runtime closure differs from signed enrollment")

    issuer_keys = {
        (str(authority["signer_name"]), str(authority["ca_key_id"]))
        for authority in ca_authorities
    }
    revocations_by_serial: dict[str, dict[str, Any]] = {}
    for index, raw_revocation in enumerate(revocations):
        revocation = exact_object(
            raw_revocation,
            {
                "ca_key_id",
                "certificate_sha256",
                "reason",
                "revoked_at",
                "serial_hex",
                "signer_name",
                "status_response_sha256",
            },
            f"certificate revocation {index}",
        )
        serial_hex = string_value(
            revocation["serial_hex"], f"certificate revocation {index} serial"
        )
        revoked_at = timestamp(
            revocation["revoked_at"], f"certificate revocation {index} time"
        )
        if (
            re.fullmatch(r"[0-9a-f]{1,128}", serial_hex) is None
            or set(serial_hex) == {"0"}
            or (
                str(revocation["signer_name"]),
                str(revocation["ca_key_id"]),
            )
            not in issuer_keys
            or revoked_at < required_history_start
            or revoked_at > ca_collected + MAX_CLOCK_SKEW
            or serial_hex in revocations_by_serial
        ):
            fail("certificate revocation history is malformed or not issuer-bound")
        digest(revocation["certificate_sha256"], "revoked certificate digest")
        digest(revocation["status_response_sha256"], "revocation status response")
        string_value(revocation["reason"], "certificate revocation reason")
        revocations_by_serial[serial_hex] = revocation

    issued_by_serial: dict[str, dict[str, Any]] = {}
    issued_by_csr_uid: dict[str, dict[str, Any]] = {}
    active_certificate_identities: list[dict[str, Any]] = []
    for index, raw_issuance in enumerate(issued_certificates):
        issuance = exact_object(
            raw_issuance,
            {
                "ca_key_id",
                "certificate_chain_der_base64",
                "certificate_der_base64",
                "certificate_sha256",
                "chain_verification_sha256",
                "csr_public_key_sha256",
                "csr_request_der_sha256",
                "csr_requested_sans",
                "csr_subject_rfc2253",
                "csr_uid",
                "issued_at",
                "issuer_rfc2253",
                "not_after",
                "not_before",
                "public_key_sha256",
                "requester",
                "sans",
                "serial_hex",
                "signer_name",
                "subject_rfc2253",
                "usages",
            },
            f"certificate issuance {index}",
        )
        serial_hex = string_value(
            issuance["serial_hex"], f"certificate issuance {index} serial"
        )
        csr_uid = string_value(
            issuance["csr_uid"], f"certificate issuance {index} CSR UID"
        )
        certificate_der = canonical_base64(
            issuance["certificate_der_base64"],
            f"certificate issuance {index} certificate",
            maximum_size=4 * 1024 * 1024,
        )
        certificate_chain_der: list[bytes] = []
        for chain_index, chain_value in enumerate(
            list_value(
                issuance["certificate_chain_der_base64"],
                f"certificate issuance {index} certificate chain",
            )
        ):
            chain_der = canonical_base64(
                chain_value,
                f"certificate issuance {index} chain certificate {chain_index}",
                maximum_size=4 * 1024 * 1024,
            )
            openssl_name_fields(chain_der, command="x509", input_format="DER")
            certificate_chain_der.append(chain_der)
        sans = exact_object(
            issuance["sans"],
            {"dns_names", "ip_addresses", "uris"},
            f"certificate issuance {index} SANs",
        )
        requester = exact_object(
            issuance["requester"],
            {"extra", "groups", "uid", "username"},
            f"certificate issuance {index} requester",
        )
        issued_at = timestamp(
            issuance["issued_at"], f"certificate issuance {index} issued_at"
        )
        not_before = timestamp(
            issuance["not_before"], f"certificate issuance {index} not_before"
        )
        not_after = timestamp(
            issuance["not_after"], f"certificate issuance {index} not_after"
        )
        usages = list_value(
            issuance["usages"], f"certificate issuance {index} usages"
        )
        if not all(isinstance(item, str) and item for item in usages):
            fail("certificate usage inventory is malformed")
        normalized_sans: dict[str, list[Any]] = {}
        for key in ("dns_names", "ip_addresses", "uris"):
            values = list_value(sans[key], f"certificate issuance {index} {key}")
            if values != sorted(set(values)):
                fail("certificate SAN inventory is not canonical")
            normalized_sans[key] = values
        certificate_fields = openssl_name_fields(
            certificate_der, command="x509", input_format="DER"
        )
        derived_serial = certificate_fields["serial"].lower()
        derived_not_before = openssl_time(
            certificate_fields["notBefore"], "certificate notBefore"
        )
        derived_not_after = openssl_time(
            certificate_fields["notAfter"], "certificate notAfter"
        )
        derived_sans = openssl_sans(
            certificate_der, command="x509", input_format="DER"
        )
        derived_public_key_sha256 = openssl_public_key_sha256(
            certificate_der, command="x509", input_format="DER"
        )
        runtime_authority = ca_runtime.get(
            (str(issuance["signer_name"]), str(issuance["ca_key_id"]))
        )
        if runtime_authority is None:
            fail("certificate issuance has no enrolled trust runtime")
        if certificate_chain_der:
            signing_certificate_der = certificate_chain_der[0]
        else:
            direct_signers = [
                item
                for item in runtime_authority["trust_anchor_der"]
                if openssl_name_fields(
                    item, command="x509", input_format="DER"
                )["subject"]
                == certificate_fields["issuer"]
            ]
            if len(direct_signers) != 1:
                fail("directly issued certificate does not identify one enrolled root")
            signing_certificate_der = direct_signers[0]
        signing_key_id = "sha256:" + openssl_public_key_sha256(
            signing_certificate_der,
            command="x509",
            input_format="DER",
        )
        purpose = (
            "sslclient"
            if "client auth" in usages
            else ("sslserver" if "server auth" in usages else "any")
        )
        openssl_verify_certificate_chain(
            leaf_der=certificate_der,
            intermediate_der=certificate_chain_der,
            trust_bundle_pem=runtime_authority["trust_bundle_pem"],
            purpose=purpose,
            verification_time=issued_at,
        )
        chain_verification_sha256 = canonical_sha256(
            {
                "certificate_sha256": hashlib.sha256(certificate_der).hexdigest(),
                "intermediate_sha256": [
                    hashlib.sha256(item).hexdigest()
                    for item in certificate_chain_der
                ],
                "purpose": purpose,
                "signing_key_id": signing_key_id,
                "trust_bundle_sha256": runtime_authority["trust_bundle_sha256"],
                "verified_at": issuance["issued_at"],
                "verifier": "openssl-verify-x509-strict/v1",
            }
        )
        for field in (
            "csr_subject_rfc2253",
            "issuer_rfc2253",
            "subject_rfc2253",
        ):
            string_value(
                issuance[field], f"certificate issuance {index} {field}"
            )
        if (
            re.fullmatch(r"[0-9a-f]{1,128}", serial_hex) is None
            or set(serial_hex) == {"0"}
            or derived_serial != serial_hex
            or (
                str(issuance["signer_name"]),
                str(issuance["ca_key_id"]),
            )
            not in issuer_keys
            or hashlib.sha256(certificate_der).hexdigest()
            != digest(
                issuance["certificate_sha256"],
                f"certificate issuance {index} certificate digest",
            )
            or issued_at < required_history_start
            or not_before < required_history_start
            or not_before > issued_at + MAX_CLOCK_SKEW
            or not_after <= not_before
            or not_after - not_before > timedelta(days=397)
            or usages != sorted(set(usages))
            or certificate_fields["subject"] != issuance["subject_rfc2253"]
            or certificate_fields["issuer"] != issuance["issuer_rfc2253"]
            or derived_not_before != not_before
            or derived_not_after != not_after
            or derived_sans != normalized_sans
            or derived_public_key_sha256 != issuance["public_key_sha256"]
            or signing_key_id != issuance["ca_key_id"]
            or chain_verification_sha256
            != issuance["chain_verification_sha256"]
            or not string_value(
                requester["username"],
                f"certificate issuance {index} requester username",
            )
            or serial_hex in issued_by_serial
            or csr_uid in issued_by_csr_uid
        ):
            fail("certificate issuance history is malformed or not issuer-bound")
        digest(issuance["csr_request_der_sha256"], "CSR request DER digest")
        digest(issuance["csr_public_key_sha256"], "CSR public-key digest")
        digest(issuance["public_key_sha256"], "certificate public-key digest")
        if issuance["csr_public_key_sha256"] != issuance["public_key_sha256"]:
            fail("issued certificate public key differs from its CSR")
        csr_requested_sans = exact_object(
            issuance["csr_requested_sans"],
            {"dns_names", "ip_addresses", "uris"},
            "certificate issuance CSR SANs",
        )
        for key in ("dns_names", "ip_addresses", "uris"):
            values = list_value(csr_requested_sans[key], f"CSR requested {key}")
            if values != sorted(set(values)):
                fail("CSR requested SAN inventory is not canonical")
        requester_groups = list_value(
            requester["groups"], f"certificate issuance {index} requester groups"
        )
        if requester_groups != sorted(set(requester_groups)) or not isinstance(
            requester["extra"], Mapping
        ):
            fail("certificate requester identity is not canonical")
        issued_by_serial[serial_hex] = issuance
        issued_by_csr_uid[csr_uid] = issuance
        revocation = revocations_by_serial.get(serial_hex)
        if revocation is not None and (
            revocation["certificate_sha256"] != issuance["certificate_sha256"]
            or revocation["signer_name"] != issuance["signer_name"]
            or revocation["ca_key_id"] != issuance["ca_key_id"]
        ):
            fail("certificate revocation does not identify its exact issuance")
        revoked_at = (
            timestamp(revocation["revoked_at"], "certificate revoked_at")
            if revocation is not None
            else None
        )
        if not_after > ca_collected and (
            revoked_at is None or revoked_at > ca_collected
        ):
            active_certificate_identities.append(
                {
                    "ca_key_id": issuance["ca_key_id"],
                    "certificate_sha256": issuance["certificate_sha256"],
                    "certificate_chain_sha256": [
                        hashlib.sha256(item).hexdigest()
                        for item in certificate_chain_der
                    ],
                    "chain_verification_sha256": chain_verification_sha256,
                    "not_after": issuance["not_after"],
                    "public_key_sha256": issuance["public_key_sha256"],
                    "sans": normalized_sans,
                    "serial_hex": serial_hex,
                    "signer_name": issuance["signer_name"],
                    "subject_rfc2253": issuance["subject_rfc2253"],
                    "usages": usages,
                }
            )
    unknown_revocations = sorted(set(revocations_by_serial) - set(issued_by_serial))
    if unknown_revocations:
        fail("revocation history refers to issuance absent from complete CA history")
    active_certificate_identities.sort(key=canonical_sha256)
    if active_certificate_identities != boundary["enrolled_certificate_identities"]:
        fail("a still-valid certificate identity is not independently enrolled")

    for csr in csrs:
        if csr.get("apiVersion") != "certificates.k8s.io/v1" or csr.get("kind") != "CertificateSigningRequest":
            fail("CSR export contains a non-native object")
        metadata_value = object_value(csr.get("metadata"), "native CSR metadata")
        native_object_identity(csr, "native CSR")
        csr_uid = string_value(metadata_value.get("uid"), "native CSR UID")
        spec = object_value(csr.get("spec"), "native CSR spec")
        status = object_value(csr.get("status", {}), "native CSR status")
        required_spec = {"groups", "request", "signerName", "uid", "usages", "username"}
        allowed_spec = {*required_spec, "expirationSeconds", "extra"}
        if not required_spec <= set(spec) <= allowed_spec or not set(status) <= {
            "certificate",
            "conditions",
        }:
            fail("native CSR spec/status contains unsupported authority fields")
        request_pem = canonical_base64(
            spec["request"], "native CSR request", maximum_size=1024 * 1024
        )
        request_blocks = canonical_pem_blocks(
            request_pem,
            pem_label="CERTIFICATE REQUEST",
            label="native CSR request",
            maximum_blocks=1,
        )
        request_der = request_blocks[0]
        openssl_der_output(
            request_pem,
            command="req",
            input_format="PEM",
            arguments=["-verify", "-noout"],
        )
        csr_identity = openssl_name_fields(
            request_pem, command="req", input_format="PEM"
        )
        csr_public_key_sha256 = openssl_public_key_sha256(
            request_pem, command="req", input_format="PEM"
        )
        csr_requested_sans = openssl_sans(
            request_pem, command="req", input_format="PEM"
        )
        csr_usages = list_value(spec["usages"], "native CSR usages")
        csr_groups = list_value(spec["groups"], "native CSR groups")
        if csr_usages != sorted(set(csr_usages)) or csr_groups != sorted(
            set(csr_groups)
        ):
            fail("native CSR identity or usage set is not canonical")
        conditions = list_value(status.get("conditions", []), "native CSR conditions")
        condition_types: set[str] = set()
        for condition_index, raw_condition in enumerate(conditions):
            condition = object_value(raw_condition, "native CSR condition")
            if not {"status", "type"} <= set(condition) <= {
                "lastTransitionTime",
                "lastUpdateTime",
                "message",
                "reason",
                "status",
                "type",
            }:
                fail("native CSR condition contains unsupported authority fields")
            condition_type = string_value(
                condition["type"], f"native CSR condition {condition_index} type"
            )
            if condition_type in condition_types or condition["status"] != "True":
                fail("native CSR has duplicate or nonterminal authority conditions")
            condition_types.add(condition_type)
        certificate_text = status.get("certificate")
        issuance = issued_by_csr_uid.get(csr_uid)
        if certificate_text is None:
            if issuance is not None:
                fail("CA issuance history contains a certificate absent from live CSR status")
            continue
        certificate_pem_chain = canonical_base64(
            certificate_text,
            "native CSR issued certificate",
            maximum_size=4 * 1024 * 1024,
        )
        certificate_chain_der = canonical_pem_blocks(
            certificate_pem_chain,
            pem_label="CERTIFICATE",
            label="native CSR issued certificate chain",
            maximum_blocks=8,
        )
        certificate_der = certificate_chain_der[0]
        certificate_identity = openssl_name_fields(
            certificate_der, command="x509", input_format="DER"
        )
        certificate_sans = openssl_sans(
            certificate_der, command="x509", input_format="DER"
        )
        if (
            issuance is None
            or "Approved" not in condition_types
            or "Denied" in condition_types
            or issuance["certificate_sha256"]
            != hashlib.sha256(certificate_der).hexdigest()
            or issuance["csr_request_der_sha256"]
            != hashlib.sha256(request_der).hexdigest()
            or issuance["certificate_chain_der_base64"]
            != [
                base64.b64encode(item).decode("ascii")
                for item in certificate_chain_der[1:]
            ]
            or issuance["csr_subject_rfc2253"] != csr_identity["subject"]
            or issuance["csr_public_key_sha256"] != csr_public_key_sha256
            or issuance["csr_requested_sans"] != csr_requested_sans
            or issuance["signer_name"] != spec["signerName"]
            or issuance["usages"] != csr_usages
            or issuance["subject_rfc2253"] != certificate_identity["subject"]
            or issuance["issuer_rfc2253"] != certificate_identity["issuer"]
            or issuance["sans"] != certificate_sans
            or issuance["requester"]
            != {
                "extra": spec.get("extra", {}),
                "groups": csr_groups,
                "uid": spec["uid"],
                "username": spec["username"],
            }
        ):
            fail("live CSR certificate does not reconcile to authoritative CA history")
    if (
        identity["schema"]
        != "fs2-serve.nebius.ai/public-edge-kubernetes-authority-native-export/v4"
        or identity["authority_snapshot_id"] != boundary["authority_snapshot_id"]
        or identity["project_id"] != project_id
        or identity["cluster_id"] != cluster_id
        or protected_subjects.count(expected_controller_subject) != 1
        or len(protected_subjects) != 1
        or len(controller_enrollments) != 1
        or any(unauthorized_credential_paths.values())
    ):
        fail("raw RBAC/impersonation evidence does not deny every non-controller identity path")
    rbac_projection = {
        "approval_objects": approval_objects,
        "authority_reachable_workloads": authority_reachable_workloads,
        "authority_secret_records": authority_secret_records,
        "cluster_roles": cluster_roles,
        "cluster_role_bindings": cluster_bindings,
        "credential_path_subjects": credential_path_subjects,
        "controller_workloads": controller_workloads,
        "controller_secret_metadata": controller_secret_metadata,
        "custom_resource_definitions": custom_resource_definitions,
        "dangerous_mutating_webhooks": dangerous_mutating_webhooks,
        "dangerous_validating_webhooks": dangerous_validating_webhooks,
        "daemon_sets": daemon_sets,
        "deployments": deployments,
        "enrolled_identities": enrolled_identities,
        "jobs": jobs,
        "cron_jobs": cron_jobs,
        "mutating_webhook_configurations": mutating_webhooks,
        "pods": pods,
        "replica_sets": replica_sets,
        "replication_controllers": replication_controllers,
        "roles": roles,
        "role_bindings": role_bindings,
        "secret_metadata": secret_metadata,
        "secret_authority_classifications": secret_classifications,
        "service_accounts": service_accounts,
        "stateful_sets": stateful_sets,
        "validating_webhook_configurations": validating_webhooks,
        "resource_versions": {
            "cluster_roles": cluster_roles_rv,
            "cluster_role_bindings": cluster_bindings_rv,
            "approval_objects": approval_objects_rv,
            "cron_jobs": cron_jobs_rv,
            "custom_resource_definitions": crds_rv,
            "daemon_sets": daemon_sets_rv,
            "deployments": deployments_rv,
            "jobs": jobs_rv,
            "mutating_webhook_configurations": mutating_webhooks_rv,
            "pods": pods_rv,
            "replica_sets": replica_sets_rv,
            "replication_controllers": replication_controllers_rv,
            "roles": roles_rv,
            "role_bindings": role_bindings_rv,
            "secret_metadata": secrets_rv,
            "service_accounts": service_accounts_rv,
            "stateful_sets": stateful_sets_rv,
            "validating_webhook_configurations": validating_webhooks_rv,
        },
    }
    impersonation_projection = {
        "impersonating_subjects": impersonating_subjects,
        "csr_authorities": csr_authorities,
        "certificate_signing_requests": csrs,
        "csr_resource_version": csrs_rv,
        "authentication_configuration": authentication,
        "authorization_configuration": authorization,
    }
    if (
        identity_summary["rbac_review_sha256"]
        != canonical_sha256(rbac_projection)
        or identity_summary["impersonation_review_sha256"]
        != canonical_sha256(impersonation_projection)
    ):
        fail("RBAC/impersonation review digests do not derive from reopened exports")
    if any(
        abs((observed - collected_at).total_seconds()) > MAX_CLOCK_SKEW.total_seconds()
        for observed in (
            provider_collected,
            apiserver_collected,
            identity_collected,
            ca_collected,
        )
    ):
        fail("preventive-boundary raw exports were not collected with the signed evidence")


def load_preventive_boundary_contract(
    run_root: Path,
    *,
    project_id: str,
    cluster_id: str,
    approval_projection: Mapping[str, Any],
    preventive_boundary_trust_sha256: str,
    validation_time: datetime | None = None,
) -> dict[str, str]:
    """Verify external provider-IAM/API-server evidence for the live approval.

    The source registry intentionally contains no boundary identities.  A
    separately custodied Platform Security signer authorizes the exact raw
    evidence, controller provenance and *live* approval projection.  An empty
    production trust registry therefore keeps public mode fail closed.
    """

    receipt_raw = open_regular_file(
        run_root / PREVENTIVE_BOUNDARY_RECEIPT_FILENAME, private=True
    )
    evidence_raw = open_regular_file(
        run_root / PREVENTIVE_BOUNDARY_EVIDENCE_FILENAME, private=True
    )
    provider_raw = open_regular_file(
        run_root / PREVENTIVE_PROVIDER_IAM_EXPORT_FILENAME,
        private=True,
        maximum_bytes=MAX_PROVIDER_IAM_EXPORT_BYTES,
    )
    apiserver_raw = open_regular_file(
        run_root / PREVENTIVE_APISERVER_EXPORT_FILENAME,
        private=True,
        maximum_bytes=MAX_APISERVER_EXPORT_BYTES,
    )
    identity_raw = open_regular_file(
        run_root / PREVENTIVE_IDENTITY_REVIEW_FILENAME,
        private=True,
        maximum_bytes=MAX_IDENTITY_EXPORT_BYTES,
    )
    ca_history_raw = open_regular_file(
        run_root / PREVENTIVE_CA_HISTORY_FILENAME,
        private=True,
        maximum_bytes=MAX_CA_EXPORT_BYTES,
    )
    trust_raw = open_regular_file(PREVENTIVE_BOUNDARY_TRUST_STORE, private=False)
    if hashlib.sha256(trust_raw).hexdigest() != digest(
        preventive_boundary_trust_sha256,
        "planned preventive-boundary trust-store digest",
    ):
        fail("preventive-boundary trust store differs from planned source bytes")
    receipt = exact_object(
        decode_canonical_json(receipt_raw, PREVENTIVE_BOUNDARY_RECEIPT_FILENAME),
        {"schema", "algorithm", "payload", "payload_sha256", "signature"},
        "preventive-boundary receipt",
    )
    if (
        receipt["schema"] != PREVENTIVE_BOUNDARY_RECEIPT_SCHEMA
        or receipt["algorithm"] != "ed25519"
    ):
        fail("preventive-boundary receipt has an unsupported signature contract")
    payload = exact_object(
        receipt["payload"],
        {
            "schema",
            "issuer",
            "nonce",
            "issued_at",
            "expires_at",
            "subject",
            "preventive_boundary",
            "evidence_sha256",
        },
        "preventive-boundary payload",
    )
    if payload["schema"] != PREVENTIVE_BOUNDARY_PAYLOAD_SCHEMA:
        fail("preventive-boundary payload has an unsupported schema")
    payload_sha256 = digest(
        receipt["payload_sha256"], "preventive-boundary payload digest"
    )
    if canonical_sha256(payload) != payload_sha256:
        fail("preventive-boundary payload digest does not match reopened bytes")
    digest(payload["nonce"], "preventive-boundary nonce")
    issued_at = timestamp(payload["issued_at"], "preventive-boundary issued_at")
    expires_at = timestamp(payload["expires_at"], "preventive-boundary expires_at")
    now = validation_time or datetime.now(timezone.utc).replace(microsecond=0)
    if (
        expires_at <= issued_at
        or expires_at - issued_at > MAX_MEMBERSHIP_VALIDITY
        or issued_at > now + MAX_CLOCK_SKEW
        or expires_at <= now
    ):
        fail("preventive-boundary receipt is outside its 24-hour validity window")
    public_key, response_authorities = trusted_preventive_boundary_key(
        decode_canonical_json(trust_raw, PREVENTIVE_BOUNDARY_TRUST_STORE.name),
        payload["issuer"],
    )
    verify_ed25519(
        public_key,
        b64url(receipt["signature"], "preventive-boundary signature", 64),
        canonical_bytes(
            {
                "schema": receipt["schema"],
                "algorithm": receipt["algorithm"],
                "payload": payload,
                "payload_sha256": payload_sha256,
            }
        ),
    )
    approval_sha256 = terraform_json_sha256(approval_projection)
    subject = exact_object(
        payload["subject"],
        {
            "project_id",
            "cluster_id",
            "approval_api_version",
            "approval_kind",
            "approval_name",
            "approval_projection_sha256",
        },
        "preventive-boundary subject",
    )
    if subject != {
        "project_id": project_id,
        "cluster_id": cluster_id,
        "approval_api_version": approval_projection.get("apiVersion"),
        "approval_kind": approval_projection.get("kind"),
        "approval_name": object_value(
            approval_projection.get("metadata"), "boundary approval metadata"
        ).get("name"),
        "approval_projection_sha256": approval_sha256,
    }:
        fail("preventive-boundary receipt does not bind the live approval subject")
    boundary = exact_object(
        payload["preventive_boundary"],
        {
            "kind",
            "provider_iam_policy_id",
            "apiserver_enforcement_id",
            "authority_snapshot_id",
            "controller_username",
            "controller_uid",
            "controller_groups",
            "controller_allowed_image_digests",
            "controller_image_digest",
            "controller_provider_principal_id",
            "certificate_history_start",
            "cluster_created_at",
            "credential_namespaces",
            "enrolled_certificate_authorities",
            "enrolled_certificate_identities",
            "enrolled_admission_webhooks",
            "enrolled_controller_workloads",
            "enrolled_credential_secrets",
            "enrolled_credential_workloads",
            "enrolled_identities",
            "identity_paths",
            "configuration_sha256",
            "provenance_attestation_sha256",
            "receipt_sha256",
            "source_repository",
            "source_commit",
            "source_tree",
        },
        "signed preventive boundary",
    )
    receipt_sha256 = hashlib.sha256(receipt_raw).hexdigest()
    approval_spec = object_value(
        approval_projection.get("spec"), "boundary approval spec"
    )
    observed_boundary = object_value(
        approval_spec.get("preventiveBoundary"),
        "boundary approval preventiveBoundary",
    )
    if boundary != observed_boundary or boundary["receipt_sha256"] != receipt_sha256:
        fail("live approval is not bound to the reopened signed boundary receipt")
    groups = list_value(boundary["controller_groups"], "boundary controller groups")
    allowed_image_digests = list_value(
        boundary["controller_allowed_image_digests"],
        "boundary controller image digest closure",
    )
    identity_paths = list_value(boundary["identity_paths"], "boundary identity paths")
    enrolled_identities = list_value(
        boundary["enrolled_identities"], "boundary enrolled identities"
    )
    enrolled_workloads = list_value(
        boundary["enrolled_controller_workloads"],
        "boundary enrolled controller workloads",
    )
    signed_credential_namespaces = list_value(
        boundary["credential_namespaces"], "boundary credential namespaces"
    )
    enrolled_credential_secrets = list_value(
        boundary["enrolled_credential_secrets"],
        "boundary credential-bearing Secrets",
    )
    enrolled_credential_workloads = list_value(
        boundary["enrolled_credential_workloads"],
        "boundary credential-reachable workloads",
    )
    enrolled_admission_webhooks = list_value(
        boundary["enrolled_admission_webhooks"],
        "boundary enrolled admission webhooks",
    )
    enrolled_ca_authorities = list_value(
        boundary["enrolled_certificate_authorities"],
        "boundary enrolled certificate authorities",
    )
    enrolled_certificate_identities = list_value(
        boundary["enrolled_certificate_identities"],
        "boundary enrolled certificate identities",
    )
    certificate_history_start = timestamp(
        boundary["certificate_history_start"],
        "boundary certificate history start",
    )
    cluster_created_at = timestamp(
        boundary["cluster_created_at"], "boundary cluster creation"
    )
    if (
        boundary["kind"] != "provider-iam+apiserver-admission"
        or re.fullmatch(r"sha256:[a-f0-9]{64}", str(boundary["authority_snapshot_id"]))
        is None
        or allowed_image_digests != sorted(set(allowed_image_digests))
        or boundary["controller_image_digest"] not in allowed_image_digests
        or not all(
            re.fullmatch(r"sha256:[a-f0-9]{64}", str(item)) is not None
            for item in allowed_image_digests
        )
        or groups != sorted(set(groups))
        or not signed_credential_namespaces
        or signed_credential_namespaces != sorted(set(signed_credential_namespaces))
        or enrolled_credential_secrets
        != sorted(enrolled_credential_secrets, key=canonical_sha256)
        or enrolled_credential_workloads
        != sorted(enrolled_credential_workloads, key=canonical_sha256)
        or not enrolled_identities
        or enrolled_identities
        != sorted(enrolled_identities, key=canonical_sha256)
        or not enrolled_workloads
        or enrolled_workloads
        != sorted(
            enrolled_workloads,
            key=lambda item: (
                str(object_value(item, "enrolled controller workload").get("namespace")),
                str(item.get("kind")),
                str(item.get("name")),
                str(item.get("uid")),
            ),
        )
        or enrolled_admission_webhooks
        != sorted(enrolled_admission_webhooks, key=canonical_sha256)
        or not enrolled_ca_authorities
        or enrolled_ca_authorities != sorted(enrolled_ca_authorities, key=canonical_sha256)
        or enrolled_certificate_identities
        != sorted(enrolled_certificate_identities, key=canonical_sha256)
        or certificate_history_start > cluster_created_at
        or cluster_created_at > issued_at + MAX_CLOCK_SKEW
        or identity_paths != sorted(set(identity_paths))
        or identity_paths
        != [
            "anonymous",
            "authentication-webhook",
            "bootstrap-token",
            "client-certificate",
            "csr-approval",
            "csr-signing",
            "direct-user",
            "impersonated-group",
            "impersonated-uid",
            "impersonated-user",
            "impersonated-userextra",
            "kubelet-client-certificate",
            "node-credential",
            "oidc",
            "provider-control-plane",
            "requestheader-front-proxy",
            "service-account-token",
            "static-token",
        ]
        or re.fullmatch(r"sha256:[a-f0-9]{64}", str(boundary["controller_image_digest"])) is None
        or re.fullmatch(
            r"serviceaccount-[a-z0-9]+",
            str(boundary["controller_provider_principal_id"]),
        )
        is None
        or re.fullmatch(r"[a-f0-9]{40}", str(boundary["source_commit"])) is None
        or re.fullmatch(r"[a-f0-9]{40}", str(boundary["source_tree"])) is None
    ):
        fail("signed preventive-boundary identity/provenance is malformed")
    for key in (
        "configuration_sha256",
        "provenance_attestation_sha256",
    ):
        digest(boundary[key], f"preventive-boundary {key}")
    evidence_sha256 = digest(
        payload["evidence_sha256"], "preventive-boundary evidence digest"
    )
    if hashlib.sha256(evidence_raw).hexdigest() != evidence_sha256:
        fail("preventive-boundary evidence bytes differ from the signed digest")
    evidence = exact_object(
        decode_canonical_json(
            evidence_raw, PREVENTIVE_BOUNDARY_EVIDENCE_FILENAME
        ),
        {
            "schema",
            "collected_at",
            "subject",
            "preventive_boundary",
            "provider_iam_export",
            "apiserver_enforcement_export",
            "certificate_authority_export",
            "controller_provenance",
            "identity_path_review",
        },
        "preventive-boundary evidence",
    )
    if (
        evidence["schema"] != PREVENTIVE_BOUNDARY_PAYLOAD_SCHEMA
        or evidence["subject"] != subject
        or evidence["preventive_boundary"] != boundary
    ):
        fail("preventive-boundary evidence does not bind the signed subject")
    collected_at = timestamp(
        evidence["collected_at"], "preventive-boundary evidence collected_at"
    )
    if collected_at < issued_at - MAX_CLOCK_SKEW or collected_at > issued_at + MAX_CLOCK_SKEW:
        fail("preventive-boundary evidence was not collected with the receipt")
    if collected_at > now + MAX_CLOCK_SKEW or now - collected_at > MAX_PREVENTIVE_SNAPSHOT_AGE:
        fail("preventive-boundary authority snapshot is stale at mutation time")
    provider_export = exact_object(
        evidence["provider_iam_export"],
        {
            "authority_snapshot_id",
            "policy_id",
            "project_id",
            "provider_api",
            "resource_version",
            "binding_resource_version",
            "raw_export_sha256",
        },
        "provider-IAM export",
    )
    apiserver_export = exact_object(
        evidence["apiserver_enforcement_export"],
        {
            "authority_snapshot_id",
            "enforcement_id",
            "cluster_id",
            "resource_version",
            "configuration_sha256",
            "raw_export_sha256",
        },
        "API-server enforcement export",
    )
    ca_history_export = exact_object(
        evidence["certificate_authority_export"],
        {
            "authority_snapshot_id",
            "cluster_created_at",
            "cluster_id",
            "collected_at",
            "history_start",
            "issuance_count",
            "raw_export_sha256",
            "revocation_count",
        },
        "certificate-authority export",
    )
    controller_provenance = exact_object(
        evidence["controller_provenance"],
        {"image_digest", "source_repository", "source_commit", "source_tree", "attestation_sha256"},
        "boundary controller provenance",
    )
    identity_review = exact_object(
        evidence["identity_path_review"],
        {"authority_snapshot_id", "identity_paths", "impersonation_review_sha256", "rbac_review_sha256", "raw_export_sha256"},
        "boundary identity-path review",
    )
    if (
        provider_export["policy_id"] != boundary["provider_iam_policy_id"]
        or provider_export["authority_snapshot_id"] != boundary["authority_snapshot_id"]
        or provider_export["project_id"] != project_id
        or apiserver_export["enforcement_id"] != boundary["apiserver_enforcement_id"]
        or apiserver_export["cluster_id"] != cluster_id
        or apiserver_export["authority_snapshot_id"] != boundary["authority_snapshot_id"]
        or apiserver_export["configuration_sha256"] != boundary["configuration_sha256"]
        or ca_history_export["cluster_id"] != cluster_id
        or ca_history_export["authority_snapshot_id"] != boundary["authority_snapshot_id"]
        or ca_history_export["cluster_created_at"] != boundary["cluster_created_at"]
        or ca_history_export["history_start"] != boundary["certificate_history_start"]
        or controller_provenance["image_digest"] != boundary["controller_image_digest"]
        or controller_provenance["source_repository"] != boundary["source_repository"]
        or controller_provenance["source_commit"] != boundary["source_commit"]
        or controller_provenance["source_tree"] != boundary["source_tree"]
        or controller_provenance["attestation_sha256"] != boundary["provenance_attestation_sha256"]
        or identity_review["identity_paths"] != boundary["identity_paths"]
        or identity_review["authority_snapshot_id"] != boundary["authority_snapshot_id"]
    ):
        fail("preventive-boundary raw evidence does not join to its signed authority")
    for record, key in (
        (provider_export, "raw_export_sha256"),
        (apiserver_export, "raw_export_sha256"),
        (ca_history_export, "raw_export_sha256"),
        (identity_review, "raw_export_sha256"),
        (identity_review, "impersonation_review_sha256"),
        (identity_review, "rbac_review_sha256"),
    ):
        digest(record[key], f"preventive-boundary {key}")
    if (
        provider_export["raw_export_sha256"]
        != hashlib.sha256(provider_raw).hexdigest()
        or apiserver_export["raw_export_sha256"]
        != hashlib.sha256(apiserver_raw).hexdigest()
        or identity_review["raw_export_sha256"]
        != hashlib.sha256(identity_raw).hexdigest()
        or ca_history_export["raw_export_sha256"]
        != hashlib.sha256(ca_history_raw).hexdigest()
    ):
        fail("preventive-boundary summaries do not bind the reopened raw exports")
    validate_preventive_raw_exports(
        provider_raw=provider_raw,
        apiserver_raw=apiserver_raw,
        identity_raw=identity_raw,
        ca_history_raw=ca_history_raw,
        provider_summary=provider_export,
        apiserver_summary=apiserver_export,
        identity_summary=identity_review,
        ca_history_summary=ca_history_export,
        boundary=boundary,
        approval_projection=approval_projection,
        project_id=project_id,
        cluster_id=cluster_id,
        collected_at=collected_at,
        response_authorities=response_authorities,
    )
    return {
        "payload_sha256": payload_sha256,
        "receipt_sha256": receipt_sha256,
        "evidence_sha256": evidence_sha256,
        "approval_sha256": approval_sha256,
    }


def object_value(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        fail(f"{label} must be an object")
    return value


def list_value(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, list):
        fail(f"{label} must be an array")
    return value


def string_value(value: object, label: str, pattern: str | None = None) -> str:
    if not isinstance(value, str) or not value:
        fail(f"{label} must be a non-empty string")
    if pattern is not None and re.fullmatch(pattern, value) is None:
        fail(f"{label} has an invalid format")
    return value


def integer_value(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        fail(f"{label} must be an integer")
    return value


def environment(name: str) -> str:
    value = EXTERNAL_QUERY.get(name) if EXTERNAL_QUERY is not None else os.environ.get(name)
    if value is None or not value.strip():
        fail(f"missing {name}")
    return value


def require_verified_source(expected: object) -> str:
    global CAPSULE_COMMAND_FDS, CAPSULE_NEBIUS_TOKEN, CAPSULE_TOOL_BIN
    expected_digest = digest(expected, "planned verifier digest")
    if (
        os.environ.get("FS2_CAPSULE_LAUNCHER") != "fs2-public-edge-capsule-v1"
        or os.environ.get("FS2_CAPSULE_SOURCE_ID") != "public-edge-verifier"
        or os.environ.get("FS2_CAPSULE_SOURCE_SHA256") != expected_digest
        or os.getegid() == os.getgid()
        or os.getegid() in os.getgroups()
        or not re.fullmatch(
            r"[a-f0-9]{40}", os.environ.get("FS2_CAPSULE_ACCEPTED_COMMIT", "")
        )
        or not re.fullmatch(
            r"[a-f0-9]{64}", os.environ.get("FS2_CAPSULE_MANIFEST_SHA256", "")
        )
    ):
        fail("public-edge verifier lacks the accepted no-member capsule proof")
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != expected_digest:
        fail("executing verifier bytes differ from the accepted manifest and plan")
    if sys.flags.isolated != 1 or not sys.dont_write_bytecode:
        fail("public-edge verifier Python is not isolated and no-bytecode")
    try:
        raw_paths = json.loads(os.environ["FS2_CAPSULE_TOOL_PATHS_JSON"])
        descriptors = tuple(
            int(value) for value in os.environ["FS2_CAPSULE_PASS_FDS"].split(",")
        )
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise GateError("capsule tool descriptor contract is absent") from exc
    required_tools = {"kubectl", "nebius", "openssl", "python3"}
    if not isinstance(raw_paths, dict) or not required_tools.issubset(raw_paths):
        fail("capsule tool descriptor contract is incomplete")
    for name in required_tools:
        path = raw_paths[name]
        if (
            not isinstance(path, str)
            or re.fullmatch(r"/proc/self/fd/[0-9]+", path) is None
            or int(path.rsplit("/", 1)[1]) not in descriptors
        ):
            fail(f"capsule tool {name} is not descriptor-pinned")
    tool_bin = raw_paths.get("tool_bin")
    if not isinstance(tool_bin, str) or not tool_bin.startswith("/opt/fs2/"):
        fail("capsule tool directory is absent")
    for descriptor in descriptors:
        if descriptor < 3:
            fail("capsule descriptor set is malformed")
        os.fstat(descriptor)
    token_path = raw_paths.get("nebius_token")
    if (
        not isinstance(token_path, str)
        or re.fullmatch(r"/proc/self/fd/[0-9]+", token_path) is None
        or int(token_path.rsplit("/", 1)[1]) not in descriptors
    ):
        fail("capsule Nebius token is not descriptor-pinned")
    token_descriptor = int(token_path.rsplit("/", 1)[1])
    token_details = os.fstat(token_descriptor)
    required_seals = (
        fcntl.F_SEAL_SEAL
        | fcntl.F_SEAL_SHRINK
        | fcntl.F_SEAL_GROW
        | fcntl.F_SEAL_WRITE
    )
    if (
        not stat.S_ISREG(token_details.st_mode)
        or token_details.st_size < 1
        or token_details.st_size > 64 * 1024
        or fcntl.fcntl(token_descriptor, fcntl.F_GET_SEALS) & required_seals
        != required_seals
    ):
        fail("capsule Nebius token is not one bounded sealed descriptor")
    token_bytes = os.pread(token_descriptor, token_details.st_size, 0)
    try:
        authentication = json.loads(os.environ["FS2_CAPSULE_NEBIUS_AUTH_JSON"])
        token = token_bytes.decode("ascii")
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateError("capsule Nebius authentication proof is absent") from exc
    if (
        not isinstance(authentication, dict)
        or authentication.get("schema")
        != "fs2-serve.nebius.ai/short-lived-nebius-auth/v3"
        or authentication.get("accepted_commit")
        != os.environ.get("FS2_CAPSULE_ACCEPTED_COMMIT")
        or authentication.get("manifest_sha256")
        != os.environ.get("FS2_CAPSULE_MANIFEST_SHA256")
        or authentication.get("endpoint") != "api.nebius.cloud"
        or re.fullmatch(r"serviceaccount-[a-z0-9]+", str(authentication.get("subject_id", ""))) is None
        or re.fullmatch(r"project-[a-z0-9]+", str(authentication.get("project_id", ""))) is None
        or re.fullmatch(r"tenant-[a-z0-9]+", str(authentication.get("tenant_id", ""))) is None
        or re.fullmatch(r"[a-f0-9]{64}", str(authentication.get("broker_executable_sha256", ""))) is None
        or re.fullmatch(r"[a-f0-9]{64}", str(authentication.get("broker_config_sha256", ""))) is None
        or authentication.get("peer_credential_mode") != "linux-so-peercred-pid-uid-gid/v1"
        or authentication.get("caller_uid") != os.getuid()
        or authentication.get("caller_real_gid") != os.getgid()
        or authentication.get("caller_effective_gid") != os.getegid()
        or authentication.get("peer_observed_caller_uid") != os.getuid()
        or authentication.get("peer_observed_caller_gid") != os.getgid()
        or not isinstance(authentication.get("operator_identity"), str)
        or re.fullmatch(
            r"[a-z][a-z0-9._-]{2,127}", authentication["operator_identity"]
        )
        is None
        or authentication.get("token_sha256")
        != hashlib.sha256(token_bytes).hexdigest()
        or not token
        or any(character.isspace() for character in token)
    ):
        fail("capsule Nebius authentication is not bound to this accepted source")
    CAPSULE_NEBIUS_TOKEN = token
    CAPSULE_TOOL_PATHS.clear()
    CAPSULE_TOOL_PATHS.update(
        {name: str(raw_paths[name]) for name in required_tools}
    )
    CAPSULE_COMMAND_FDS = tuple(sorted(set(descriptors)))
    CAPSULE_TOOL_BIN = tool_bin
    return expected_digest


def parse_plan_timestamp(value: str, *, now: datetime, maximum_age: int) -> int:
    try:
        planned_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GateError("planned_at is not RFC3339") from exc
    if planned_at.tzinfo is None:
        fail("planned_at must include a timezone")
    age = int((now - planned_at.astimezone(timezone.utc)).total_seconds())
    if age < -30:
        fail("planned_at is more than 30 seconds in the future")
    if age > maximum_age:
        fail(
            f"saved plan is {age}s old, exceeding the {maximum_age}s public-edge plan limit; replan"
        )
    return max(age, 0)


def checked_local_inputs(run_root: str, kubeconfig: str) -> tuple[Path, str, int, str]:
    root = Path(run_root)
    config = Path(kubeconfig)
    if not root.is_absolute() or not config.is_absolute():
        fail("run root and kubeconfig must be absolute")
    if root.is_symlink() or not root.is_dir():
        fail("run root must be an existing non-symlink directory")
    if config.is_symlink() or not config.is_file():
        fail("kubeconfig must be an existing non-symlink regular file")
    root_stat = root.stat()
    config_stat = config.stat()
    if stat.S_IMODE(root_stat.st_mode) != 0o700:
        fail("run root must be mode 0700")
    if stat.S_IMODE(config_stat.st_mode) != 0o600:
        fail("kubeconfig must be mode 0600")
    if root_stat.st_uid != os.getuid() or config_stat.st_uid != os.getuid():
        fail("run root and kubeconfig must be owned by the invoking user")
    resolved_root = root.resolve(strict=True)
    resolved_config = config.resolve(strict=True)
    if resolved_config != resolved_root / "kubeconfig":
        fail("kubeconfig must be the exact run-owned file")
    validate_parent_chain(resolved_config, "run-owned kubeconfig")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(resolved_config, flags)
    except OSError as exc:
        raise GateError("kubeconfig cannot be opened safely") from exc
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_uid != os.getuid()
        or stat.S_IMODE(opened.st_mode) != 0o600
        or (opened.st_dev, opened.st_ino) != (config_stat.st_dev, config_stat.st_ino)
    ):
        os.close(descriptor)
        fail("opened kubeconfig identity differs from the checked run-owned file")
    try:
        snapshot = read_descriptor(descriptor, "run-owned kubeconfig", private=True)
    finally:
        os.close(descriptor)
    snapshot_descriptor = sealed_memfd("public-edge-kubeconfig", snapshot)
    return (
        resolved_root,
        f"/proc/self/fd/{snapshot_descriptor}",
        snapshot_descriptor,
        hashlib.sha256(snapshot).hexdigest(),
    )


def run_json(command: Sequence[str], label: str) -> Mapping[str, Any]:
    try:
        pwd.getpwuid(os.getuid())
    except KeyError as exc:
        raise GateError("invoking user has no canonical passwd home") from exc
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
            stdin=subprocess.DEVNULL,
            cwd="/",
            close_fds=True,
            pass_fds=PINNED_COMMAND_FDS,
            env={
                "HOME": "/nonexistent",
                "PATH": CAPSULE_TOOL_BIN or "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "NEBIUS_IAM_TOKEN": CAPSULE_NEBIUS_TOKEN,
            },
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateError(f"{label} read failed") from exc
    if completed.returncode != 0:
        fail(f"{label} read failed")
    if len(completed.stdout.encode("utf-8")) > 16 * 1024 * 1024:
        fail(f"{label} response exceeds 16 MiB")
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise GateError(f"{label} response is not JSON") from exc
    return object_value(parsed, f"{label} response")


def paginated_list(
    command: Sequence[str],
    arguments: Sequence[str],
    label: str,
    *,
    maximum_pages: int = 256,
) -> Mapping[str, Any]:
    """Enumerate a provider collection and prove that its terminal page was read."""

    items: list[object] = []
    seen_ids: set[str] = set()
    seen_tokens: set[str] = set()
    page_token = ""
    for page_number in range(1, maximum_pages + 1):
        page_arguments = [*command, *arguments, "--page-size", "100"]
        if page_token:
            page_arguments.extend(("--page-token", page_token))
        page = run_json(page_arguments, f"{label} page {page_number}")
        page_items = list_value(page.get("items", []), f"{label}.items")
        for raw_item in page_items:
            item = object_value(raw_item, f"{label} item")
            item_id = string_value(
                metadata(item, f"{label} item").get("id"),
                f"{label} item.metadata.id",
            )
            if item_id in seen_ids:
                fail(f"{label} repeated resource {item_id!r} across pages")
            seen_ids.add(item_id)
            items.append(item)

        raw_next_token = page.get("next_page_token", "")
        if not isinstance(raw_next_token, str):
            fail(f"{label}.next_page_token must be a string")
        if not raw_next_token:
            return {
                "items": items,
                "pagination": {
                    "complete": True,
                    "page_count": page_number,
                    "item_count": len(items),
                    "terminal_next_page_token": "",
                },
            }
        if not page_items:
            fail(f"{label} returned a continuation token with an empty page")
        if raw_next_token == page_token or raw_next_token in seen_tokens:
            fail(f"{label} repeated a pagination token")
        seen_tokens.add(raw_next_token)
        page_token = raw_next_token
    fail(f"{label} exceeded the bounded {maximum_pages}-page enumeration")


def metadata(resource: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    return object_value(resource.get("metadata"), f"{label}.metadata")


def resource_revision(resource: Mapping[str, Any], label: str) -> str:
    value = metadata(resource, label).get("resource_version")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return str(value)
    return string_value(value, f"{label}.metadata.resource_version", r"[1-9][0-9]*")


def admission_contract_projection(
    resource: Mapping[str, Any], *, kind: str, name: str = "fs2-public-edge-node-authority"
) -> tuple[str, str, dict[str, object]]:
    label = kind
    if resource.get("apiVersion") != "admissionregistration.k8s.io/v1":
        fail(f"{label} has the wrong API version")
    if resource.get("kind") != kind:
        fail(f"{label} has the wrong kind")
    item_metadata = metadata(resource, label)
    if item_metadata.get("name") != name:
        fail(f"{label} has the wrong name")
    annotations = item_metadata.get("annotations", {})
    if not isinstance(annotations, Mapping):
        fail(f"{label} annotations must be an object")
    contract = {
        "apiVersion": resource["apiVersion"],
        "kind": kind,
        "metadata": {
            "name": item_metadata["name"],
            **({"annotations": dict(annotations)} if annotations else {}),
        },
        "spec": object_value(resource.get("spec"), f"{label}.spec"),
    }
    return (
        string_value(
            item_metadata.get("resourceVersion"),
            f"{label}.metadata.resourceVersion",
            r"[1-9][0-9]*",
        ),
        string_value(item_metadata.get("uid"), f"{label}.metadata.uid"),
        contract,
    )


def validate_admission_contract(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    kind: str,
    expected_sha256: str,
    name: str = "fs2-public-edge-node-authority",
) -> tuple[str, str, str]:
    before_revision, before_uid, before_contract = admission_contract_projection(
        before, kind=kind, name=name
    )
    after_revision, after_uid, after_contract = admission_contract_projection(
        after, kind=kind, name=name
    )
    if (
        before_revision != after_revision
        or before_uid != after_uid
        or before_contract != after_contract
    ):
        fail(f"{kind} changed during the final mutation observation")
    contract_sha256 = terraform_json_sha256(after_contract)
    if contract_sha256 != expected_sha256:
        fail(f"{kind} differs from the exact Terraform admission contract")
    return after_revision, after_uid, contract_sha256


def validate_boundary_approval(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    api_version: str,
    kind: str,
    name: str,
    expected_sha256: str,
) -> tuple[str, str, str]:
    projections: list[tuple[str, str, dict[str, Any]]] = []
    for label, resource in (("before", before), ("after", after)):
        item_metadata = metadata(resource, f"boundary approval {label}")
        if (
            resource.get("apiVersion") != api_version
            or resource.get("kind") != kind
            or item_metadata.get("name") != name
        ):
            fail("preventive-boundary approval identity changed or is not exact")
        projection = {
            "apiVersion": api_version,
            "kind": kind,
            "metadata": {
                "name": name,
                "resourceVersion": item_metadata.get("resourceVersion"),
                "uid": item_metadata.get("uid"),
            },
            "spec": object_value(
                resource.get("spec"), f"boundary approval {label}.spec"
            ),
            "status": resource.get("status"),
        }
        projections.append(
            (
                string_value(
                    item_metadata.get("resourceVersion"),
                    f"boundary approval {label}.metadata.resourceVersion",
                    r"[1-9][0-9]*",
                ),
                string_value(
                    item_metadata.get("uid"),
                    f"boundary approval {label}.metadata.uid",
                ),
                projection,
            )
        )
    if projections[0] != projections[1]:
        fail("preventive-boundary approval changed during the final observation")
    approval_sha256 = terraform_json_sha256(projections[1][2])
    if approval_sha256 != expected_sha256:
        fail("preventive-boundary approval differs from the exact enrolled receipt")
    return projections[1][0], projections[1][1], approval_sha256


def node_group_revision(group: Mapping[str, Any], label: str) -> str:
    return resource_revision(group, label)


def validate_cluster(
    cluster_before: Mapping[str, Any],
    cluster_after: Mapping[str, Any],
    *,
    cluster_id: str,
) -> tuple[str, str, str]:
    before_metadata = metadata(cluster_before, "cluster before")
    after_metadata = metadata(cluster_after, "cluster after")
    for label, current in (
        ("cluster before", before_metadata),
        ("cluster after", after_metadata),
    ):
        if current.get("id") != cluster_id:
            fail(f"{label} is not the exact Kubernetes cluster")
    project_id = string_value(
        before_metadata.get("parent_id"),
        "cluster.metadata.parent_id",
        r"project-[a-z0-9]+",
    )
    if after_metadata.get("parent_id") != project_id:
        fail("cluster changed parent project during the apply-time observation")
    revision = resource_revision(cluster_before, "cluster before")
    if (
        resource_revision(cluster_after, "cluster after") != revision
        or canonical_sha256(cluster_before) != canonical_sha256(cluster_after)
    ):
        fail("cluster changed during the apply-time eligibility observation")
    return project_id, revision, canonical_sha256(cluster_after)


def validate_node_group(
    group_list_before: Mapping[str, Any],
    group_list_after: Mapping[str, Any],
    group_before: Mapping[str, Any],
    group_after: Mapping[str, Any],
    *,
    cluster_id: str,
    group_id: str,
    expected_count: int,
    maximum_surge: int,
    membership_phase: str,
    selector: Mapping[str, str],
) -> tuple[str, str]:
    matches_by_observation: list[Mapping[str, Any]] = []
    for observation, group_list in (
        ("before", group_list_before),
        ("after", group_list_after),
    ):
        pagination = object_value(
            group_list.get("pagination"), f"node-group list {observation}.pagination"
        )
        if pagination.get("complete") is not True:
            fail("NodeGroup enumeration is not complete")
        items = [
            object_value(item, "node-group list item")
            for item in list_value(group_list.get("items"), "node-group list.items")
        ]
        matches = [
            item
            for item in items
            if metadata(item, "node-group list item").get("id") == group_id
        ]
        if len(matches) != 1:
            fail("exact system NodeGroup is not uniquely enumerated under the cluster")
        matches_by_observation.append(matches[0])

    before_metadata = metadata(group_before, "node-group before")
    after_metadata = metadata(group_after, "node-group after")
    listed_before_metadata = metadata(
        matches_by_observation[0], "listed node-group before"
    )
    listed_after_metadata = metadata(
        matches_by_observation[1], "listed node-group after"
    )
    for label, current in (
        ("listed node-group before", listed_before_metadata),
        ("listed node-group after", listed_after_metadata),
        ("node-group before", before_metadata),
        ("node-group after", after_metadata),
    ):
        if current.get("id") != group_id or current.get("parent_id") != cluster_id:
            fail(f"{label} is not the exact system group under the exact cluster")

    before_revision = node_group_revision(group_before, "node-group before")
    after_revision = node_group_revision(group_after, "node-group after")
    listed_before_revision = node_group_revision(
        matches_by_observation[0], "listed node-group before"
    )
    listed_after_revision = node_group_revision(
        matches_by_observation[1], "listed node-group after"
    )
    if listed_before_revision != before_revision or listed_after_revision != after_revision:
        fail("enumerated NodeGroup revision differs from the exact-ID provider read")
    if (
        before_revision != after_revision
        or canonical_sha256(group_before) != canonical_sha256(group_after)
        or canonical_sha256(matches_by_observation[0])
        != canonical_sha256(matches_by_observation[1])
    ):
        fail("NodeGroup changed during the apply-time eligibility observation")

    specification = object_value(group_after.get("spec"), "node-group.spec")
    status_value = object_value(group_after.get("status"), "node-group.status")
    if integer_value(specification.get("fixed_node_count"), "node-group.spec.fixed_node_count") != expected_count:
        fail("NodeGroup fixed count differs from the infrastructure contract")
    if status_value.get("state") != "RUNNING":
        fail("NodeGroup is not RUNNING")
    if membership_phase == "stable" and status_value.get("reconciling") is not False:
        fail("stable membership requires a fully reconciled NodeGroup")
    for field in ("target_node_count", "node_count", "ready_node_count"):
        observed = integer_value(status_value.get(field), f"node-group.status.{field}")
        if observed < expected_count or observed > expected_count + maximum_surge:
            fail(f"NodeGroup {field} is below the public-edge requirement")
    outdated = integer_value(
        status_value.get("outdated_node_count"),
        "node-group.status.outdated_node_count",
    )
    if outdated < 0 or outdated > maximum_surge or (
        membership_phase == "stable" and outdated != 0
    ):
        fail("NodeGroup outdated-node count exceeds the signed rollout epoch")

    template = object_value(specification.get("template"), "node-group.spec.template")
    template_metadata = object_value(template.get("metadata"), "node-group.spec.template.metadata")
    provider_labels = object_value(template_metadata.get("labels"), "node-group.spec.template.metadata.labels")
    for key, value in selector.items():
        if key != "nebius.com/node-group-id" and provider_labels.get(key) != value:
            fail("provider NodeGroup template does not own the exact scheduler selector")

    return before_revision, canonical_sha256(group_after)


def provider_member_ids(
    instance_list: Mapping[str, Any],
    *,
    project_id: str,
    signed_instance_ids: Sequence[str],
) -> list[str]:
    pagination = object_value(
        instance_list.get("pagination"), "Compute instance list.pagination"
    )
    if pagination.get("complete") is not True:
        fail("Compute instance enumeration is not complete")
    signed = list(signed_instance_ids)
    if signed != sorted(set(signed)) or not signed:
        fail("signed provider membership must be a non-empty sorted unique set")
    listed_ids: set[str] = set()
    for raw_instance in list_value(
        instance_list.get("items"), "Compute instance list.items"
    ):
        instance = object_value(raw_instance, "Compute instance list item")
        instance_metadata = metadata(instance, "Compute instance list item")
        if instance_metadata.get("parent_id") != project_id:
            fail("Compute instance list returned an instance outside the exact project")
        listed_ids.add(
            string_value(
                instance_metadata.get("id"), "Compute instance.metadata.id"
            )
        )
    missing = set(signed) - listed_ids
    if missing:
        fail("signed provider members are absent from the complete Compute enumeration")
    return signed


def project_provider_instance(
    instance: Mapping[str, Any],
    *,
    project_id: str,
    expected_id: str,
) -> dict[str, object]:
    instance_metadata = metadata(instance, "Compute instance")
    if (
        instance_metadata.get("id") != expected_id
        or instance_metadata.get("parent_id") != project_id
    ):
        fail("Compute instance does not match the signed exact provider member")
    instance_name = string_value(
        instance_metadata.get("name"), "Compute instance.metadata.name"
    )
    specification = object_value(instance.get("spec"), "Compute instance.spec")
    status_value = object_value(instance.get("status"), "Compute instance.status")
    if specification.get("stopped", False) is True:
        fail("provider-enumerated NodeGroup member is stopped")
    if (
        status_value.get("state") != "RUNNING"
        or status_value.get("reconciling", False) is not False
    ):
        fail("provider-enumerated NodeGroup member is not stable and RUNNING")
    return {
        "id": expected_id,
        # The name is retained as a non-authoritative change detector. Only the
        # signed provider relation supplies NodeGroup membership.
        "name": instance_name,
        "parent_id": project_id,
        "resource_version": resource_revision(instance, "Compute instance"),
        "created_at": string_value(
            instance_metadata.get("created_at"), "Compute instance.metadata.created_at"
        ),
        "state": status_value.get("state"),
        "reconciling": status_value.get("reconciling", False),
        "stopped": specification.get("stopped", False),
    }


def selected_provider_instances(
    instance_list: Mapping[str, Any], instance_ids: Sequence[str]
) -> dict[str, Mapping[str, Any]]:
    selected: dict[str, Mapping[str, Any]] = {}
    wanted = set(instance_ids)
    for raw_item in list_value(
        instance_list.get("items"), "Compute instance list.items"
    ):
        item = object_value(raw_item, "Compute instance list item")
        item_id = metadata(item, "Compute instance list item").get("id")
        if isinstance(item_id, str) and item_id in wanted:
            selected[item_id] = item
    return selected


def validate_provider_members(
    instance_list_before: Mapping[str, Any],
    instance_gets_before: Mapping[str, Mapping[str, Any]],
    instance_list_after: Mapping[str, Any],
    instance_gets_after: Mapping[str, Mapping[str, Any]],
    *,
    project_id: str,
    signed_instance_ids: Sequence[str],
    expected_count: int,
    maximum_surge: int,
) -> tuple[set[str], str]:
    before_ids = provider_member_ids(
        instance_list_before,
        project_id=project_id,
        signed_instance_ids=signed_instance_ids,
    )
    after_ids = provider_member_ids(
        instance_list_after,
        project_id=project_id,
        signed_instance_ids=signed_instance_ids,
    )
    if (
        before_ids != after_ids
        or len(after_ids) < expected_count
        or len(after_ids) > expected_count + maximum_surge
    ):
        fail("provider NodeGroup member instance set changed or has the wrong size")
    if set(instance_gets_before) != set(before_ids) or set(instance_gets_after) != set(after_ids):
        fail("exact provider member gets do not cover the complete enumerated set")
    listed_before_by_id = selected_provider_instances(instance_list_before, before_ids)
    listed_after_by_id = selected_provider_instances(instance_list_after, after_ids)
    if set(listed_before_by_id) != set(before_ids) or set(listed_after_by_id) != set(after_ids):
        fail("complete provider lists do not contain every selected member object")
    listed_before = [
        project_provider_instance(
            listed_before_by_id[instance_id],
            project_id=project_id,
            expected_id=instance_id,
        )
        for instance_id in before_ids
    ]
    listed_after = [
        project_provider_instance(
            listed_after_by_id[instance_id],
            project_id=project_id,
            expected_id=instance_id,
        )
        for instance_id in after_ids
    ]
    exact_before = [
        project_provider_instance(
            instance_gets_before[instance_id],
            project_id=project_id,
            expected_id=instance_id,
        )
        for instance_id in before_ids
    ]
    exact_after = [
        project_provider_instance(
            instance_gets_after[instance_id],
            project_id=project_id,
            expected_id=instance_id,
        )
        for instance_id in after_ids
    ]
    if listed_before != exact_before or listed_after != exact_after:
        fail("provider member list and exact-ID get projections differ")
    if exact_before != exact_after:
        fail("provider NodeGroup member instances changed during observation")
    return set(after_ids), canonical_sha256(exact_after)


def ready(node: Mapping[str, Any]) -> bool:
    status_value = object_value(node.get("status"), "Node.status")
    conditions = [
        object_value(item, "Node.status.conditions item")
        for item in list_value(status_value.get("conditions"), "Node.status.conditions")
    ]
    values = [item.get("status") for item in conditions if item.get("type") == "Ready"]
    return values == ["True"]


def project_owned_nodes(
    node_list: Mapping[str, Any],
    *,
    cluster_id: str,
    group_id: str,
    run_id: str,
    selector: Mapping[str, str],
    provider_member_ids: set[str],
    joining_member_ids: set[str],
) -> tuple[str, list[dict[str, object]]]:
    list_revision = string_value(
        metadata(node_list, "NodeList").get("resourceVersion"),
        "NodeList.metadata.resourceVersion",
        r"[0-9]+",
    )
    projected: list[dict[str, object]] = []
    for raw_node in list_value(node_list.get("items"), "NodeList.items"):
        node = object_value(raw_node, "NodeList item")
        node_metadata = metadata(node, "Node")
        labels = object_value(node_metadata.get("labels"), "Node.metadata.labels")
        annotations = object_value(
            node_metadata.get("annotations"), "Node.metadata.annotations"
        )
        claims_group = labels.get("nebius.com/node-group-id") == group_id
        matches_selector = all(labels.get(key) == value for key, value in selector.items())
        name_value = node_metadata.get("name")
        claims_provider_membership = (
            isinstance(name_value, str) and name_value in provider_member_ids
        )
        if not claims_group and not matches_selector and not claims_provider_membership:
            continue

        name = string_value(
            name_value, "Node.metadata.name", r"computeinstance-[a-z0-9]+"
        )
        specification = object_value(node.get("spec"), "Node.spec")
        raw_provider_id = specification.get("providerID")
        expected_provider_id = f"nebius://{name}"
        if name in joining_member_ids:
            if raw_provider_id not in {None, "", expected_provider_id}:
                fail("a joining provider member advertises a different providerID")
            if any(
                key in labels and labels.get(key) != value
                for key, value in selector.items()
            ):
                fail("a joining provider member advertises a different protected label")
            # Managed registration may exist before every protected field and
            # corroborating Cluster API annotation has arrived. The signed
            # prepare epoch retains the old serving set and never schedules
            # onto this incomplete Node. The admission policy permits only
            # monotonic initialization to the exact signed values.
            if raw_provider_id != expected_provider_id or not matches_selector:
                continue
        provider_id = string_value(
            raw_provider_id,
            "Node.spec.providerID",
            r"nebius://computeinstance-[a-z0-9]+",
        )
        owner_name = annotations.get("cluster.x-k8s.io/owner-name")
        machine_name = annotations.get("cluster.x-k8s.io/machine")
        owner_matches_group = isinstance(owner_name, str) and (
            owner_name == group_id
            or re.fullmatch(rf"{re.escape(group_id)}-[a-z0-9]{{5}}", owner_name)
            is not None
        )
        machine_matches_owner = (
            isinstance(machine_name, str)
            and isinstance(owner_name, str)
            and re.fullmatch(rf"{re.escape(owner_name)}-[a-z0-9]{{5}}", machine_name)
            is not None
        )
        # Membership authority is the freshly paginated Compute API result.
        # Cluster API annotations remain a corroborating controller signal;
        # no Kubernetes label or annotation can add a Node to this set.
        if name not in provider_member_ids or provider_id != f"nebius://{name}":
            fail("a Node claiming the system group is absent from provider membership")
        ownership_complete = (
            annotations.get("cluster.x-k8s.io/cluster-name") == cluster_id
            and annotations.get("cluster.x-k8s.io/owner-kind") == "MachineSet"
            and owner_matches_group
            and machine_matches_owner
        )
        if not ownership_complete and name in joining_member_ids:
            continue
        if not ownership_complete:
            fail("a provider member Node lacks corroborating Cluster API ownership")
        if not matches_selector or labels.get("lifecycle.fs2.nebius/run") != run_id:
            fail("a provider-owned system Node lacks the exact run scheduler labels")

        hard_taints = sorted(
            canonical_sha256(
                {
                    "key": item.get("key"),
                    "value": item.get("value", ""),
                    "effect": item.get("effect"),
                }
            )
            for item in (
                object_value(taint, "Node.spec.taints item")
                for taint in list_value(specification.get("taints", []), "Node.spec.taints")
            )
            if item.get("effect") in {"NoExecute", "NoSchedule"}
        )
        hostname = string_value(
            labels.get("kubernetes.io/hostname"), "Node hostname", r"computeinstance-[a-z0-9]+"
        )
        projected.append(
            {
                "provider_instance_id": name,
                "name_sha256": hashlib.sha256(name.encode("utf-8")).hexdigest(),
                "provider_id_sha256": hashlib.sha256(provider_id.encode("utf-8")).hexdigest(),
                "uid": string_value(node_metadata.get("uid"), "Node.metadata.uid"),
                "resource_version": string_value(
                    node_metadata.get("resourceVersion"),
                    "Node.metadata.resourceVersion",
                    r"[0-9]+",
                ),
                "hostname_sha256": hashlib.sha256(hostname.encode("utf-8")).hexdigest(),
                "ready": ready(node),
                "unschedulable": specification.get("unschedulable", False) is True,
                "hard_taints": hard_taints,
            }
        )
    projected.sort(key=lambda item: str(item["name_sha256"]))
    return list_revision, projected


def validate_nodes(
    node_list_before: Mapping[str, Any],
    node_list_after: Mapping[str, Any],
    *,
    cluster_id: str,
    group_id: str,
    run_id: str,
    selector: Mapping[str, str],
    provider_member_ids: set[str],
    serving_member_ids: set[str],
    expected_count: int,
    minimum_domains: int,
    joining_member_ids: set[str] | None = None,
) -> tuple[str, int, int, str]:
    joining = joining_member_ids or set()
    _before_revision, before = project_owned_nodes(
        node_list_before,
        cluster_id=cluster_id,
        group_id=group_id,
        run_id=run_id,
        selector=selector,
        provider_member_ids=provider_member_ids,
        joining_member_ids=joining,
    )
    after_revision, after = project_owned_nodes(
        node_list_after,
        cluster_id=cluster_id,
        group_id=group_id,
        run_id=run_id,
        selector=selector,
        provider_member_ids=provider_member_ids,
        joining_member_ids=joining,
    )
    if before != after:
        fail("system Node identities or eligibility facts changed during observation")
    observed_ids = {str(node["provider_instance_id"]) for node in after}
    if not observed_ids.issubset(provider_member_ids) or not serving_member_ids.issubset(
        observed_ids
    ):
        fail("Kubernetes Nodes do not contain the complete signed serving set")
    eligible = [
        node
        for node in after
        if str(node["provider_instance_id"]) in serving_member_ids
        and node["ready"]
        and not node["unschedulable"]
        and not node["hard_taints"]
    ]
    domains = {str(node["hostname_sha256"]) for node in eligible}
    if len(eligible) < expected_count:
        fail("fewer provider-owned Nodes are currently eligible than required")
    if len(domains) < minimum_domains:
        fail("provider-owned eligible Nodes do not span the required hostname domains")
    return after_revision, len(eligible), len(domains), canonical_sha256(after)


def membership_subject(
    *,
    project_id: str,
    cluster_id: str,
    group_id: str,
    run_id: str,
    expected_count: int,
    minimum_domains: int,
    maximum_surge: int,
    selector: Mapping[str, str],
    kubeconfig_sha256: str,
) -> dict[str, object]:
    return {
        "schema": MEMBERSHIP_SUBJECT_SCHEMA,
        "project_id": project_id,
        "cluster_id": cluster_id,
        "node_group_id": group_id,
        "run_id": run_id,
        "expected_node_count": expected_count,
        "minimum_hostname_domains": minimum_domains,
        "maximum_surge_members": maximum_surge,
        "node_selector_sha256": canonical_sha256(selector),
        "kubeconfig_sha256": digest(kubeconfig_sha256, "kubeconfig snapshot digest"),
    }


def receipt_contract_main() -> int:
    query = json.load(sys.stdin, object_pairs_hook=no_duplicate_object)
    query = exact_object(
        query,
        {
            "run_root",
            "expected_subject_json",
            "verifier_sha256",
            "membership_trust_sha256",
            "provider_adapter_trust_sha256",
        },
        "Terraform membership query",
    )
    require_verified_source(query["verifier_sha256"])
    run_root = Path(string_value(query["run_root"], "membership run root"))
    if not run_root.is_absolute() or run_root.is_symlink() or not run_root.is_dir():
        fail("membership run root must be an absolute existing non-symlink directory")
    root_details = run_root.stat()
    if stat.S_IMODE(root_details.st_mode) != 0o700 or root_details.st_uid != os.getuid():
        fail("membership run root must be mode 0700 and owned by the invoking user")
    expected_subject = json.loads(
        string_value(query["expected_subject_json"], "expected membership subject"),
        object_pairs_hook=no_duplicate_object,
    )
    result = load_membership_contract(
        run_root.resolve(strict=True),
        expected_subject,
        membership_trust_sha256=string_value(
            query["membership_trust_sha256"], "membership trust-store digest"
        ),
        provider_adapter_trust_sha256=string_value(
            query["provider_adapter_trust_sha256"],
            "provider-adapter trust-store digest",
        ),
    )
    sys.stdout.write(
        json.dumps(
            {
                "verified": "true",
                "payload_sha256": str(result["payload_sha256"]),
                "receipt_sha256": str(result["receipt_sha256"]),
                "evidence_sha256": str(result["evidence_sha256"]),
                "node_group_resource_version": str(
                    result["node_group_resource_version"]
                ),
                "provider_member_instance_ids_json": json.dumps(
                    result["provider_member_instance_ids"], separators=(",", ":")
                ),
                "epoch_id": str(result["epoch_id"]),
                "epoch_sequence": str(result["epoch_sequence"]),
                "phase": str(result["phase"]),
                "predecessor_payload_sha256": str(
                    result["predecessor_payload_sha256"]
                ),
                "serving_member_instance_ids_json": json.dumps(
                    result["serving_member_instance_ids"], separators=(",", ":")
                ),
                "joining_member_instance_ids_json": json.dumps(
                    result["joining_member_instance_ids"], separators=(",", ":")
                ),
                "retiring_member_instance_ids_json": json.dumps(
                    result["retiring_member_instance_ids"], separators=(",", ":")
                ),
                "admitted_member_instance_ids_json": json.dumps(
                    result["admitted_member_instance_ids"], separators=(",", ":")
                ),
                "provider_observer_json": json.dumps(
                    result["provider_observer"], separators=(",", ":")
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    return 0


def main() -> int:
    global PINNED_COMMAND_FDS
    require_verified_source(
        EXTERNAL_QUERY["verifier_sha256"]
        if EXTERNAL_QUERY is not None
        else environment("FS2_EDGE_GATE_VERIFIER_SHA256")
    )
    stage = string_value(
        environment("FS2_EDGE_GATE_STAGE"),
        "stage",
        r"(?:foundation|workloads)(?:-mutation)?",
    )
    cluster_id = string_value(
        environment("FS2_EDGE_GATE_CLUSTER_ID"), "cluster_id", r"mk8scluster-[a-z0-9]+"
    )
    group_id = string_value(
        environment("FS2_EDGE_GATE_NODE_GROUP_ID"),
        "node_group_id",
        r"mk8snodegroup-[a-z0-9]+",
    )
    run_id = string_value(environment("FS2_EDGE_GATE_RUN_ID"), "run_id", r"[a-z][a-z0-9]{5,11}")
    context = environment("FS2_EDGE_GATE_KUBE_CONTEXT")
    expected_project_id = string_value(
        environment("FS2_EDGE_GATE_PROJECT_ID"), "project_id", r"project-[a-z0-9]+"
    )
    expected_count = int(environment("FS2_EDGE_GATE_EXPECTED_NODE_COUNT"))
    minimum_domains = int(environment("FS2_EDGE_GATE_MINIMUM_DOMAINS"))
    maximum_surge = int(environment("FS2_EDGE_GATE_MAXIMUM_SURGE_MEMBERS"))
    maximum_age = int(environment("FS2_EDGE_GATE_MAX_PLAN_AGE_SECONDS"))
    if (
        expected_count < 3
        or minimum_domains < 3
        or maximum_surge < 1
        or maximum_age != 14400
    ):
        fail("public-edge count/domain/plan-window contract is invalid")
    selector_raw = json.loads(environment("FS2_EDGE_GATE_NODE_SELECTOR_JSON"))
    selector_object = object_value(selector_raw, "node selector")
    if not selector_object or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in selector_object.items()
    ):
        fail("node selector must be a non-empty string map")
    selector = {str(key): str(value) for key, value in selector_object.items()}
    expected_selector = {
        "workload.fs2.nebius/system": "true",
        "capacity.fs2.nebius/type": "regular",
        "capacity.fs2.nebius/pool": "system",
        "lifecycle.fs2.nebius/run": run_id,
        "nebius.com/node-group-id": group_id,
    }
    if selector != expected_selector:
        fail("node selector is not the exact infrastructure-owned public-edge selector")

    root, kubeconfig, kubeconfig_descriptor, kubeconfig_sha256 = checked_local_inputs(
        environment("FS2_EDGE_GATE_RUN_ROOT"), environment("FS2_EDGE_GATE_KUBECONFIG")
    )
    plan_age = parse_plan_timestamp(
        environment("FS2_EDGE_GATE_PLANNED_AT"),
        now=datetime.now(timezone.utc),
        maximum_age=maximum_age,
    )
    expected_namespace_uid = string_value(
        environment("FS2_EDGE_GATE_KUBE_SYSTEM_UID"), "kube_system_uid"
    )
    expected_policy_sha256 = digest(
        environment("FS2_EDGE_GATE_POLICY_SHA256"), "admission policy digest"
    )
    expected_binding_sha256 = digest(
        environment("FS2_EDGE_GATE_BINDING_SHA256"), "admission binding digest"
    )
    expected_cas_policy_sha256 = digest(
        environment("FS2_EDGE_GATE_CAS_POLICY_SHA256"),
        "admission CAS policy digest",
    )
    expected_cas_binding_sha256 = digest(
        environment("FS2_EDGE_GATE_CAS_BINDING_SHA256"),
        "admission CAS binding digest",
    )
    expected_bootstrap_policy_sha256 = digest(
        environment("FS2_EDGE_GATE_BOOTSTRAP_POLICY_SHA256"),
        "admission bootstrap policy digest",
    )
    expected_bootstrap_binding_sha256 = digest(
        environment("FS2_EDGE_GATE_BOOTSTRAP_BINDING_SHA256"),
        "admission bootstrap binding digest",
    )
    boundary_approval_api_version = string_value(
        environment("FS2_EDGE_GATE_BOUNDARY_APPROVAL_API_VERSION"),
        "preventive-boundary approval API version",
        r"[a-z0-9.-]+/v[0-9]+[a-z0-9]*",
    )
    boundary_approval_kind = string_value(
        environment("FS2_EDGE_GATE_BOUNDARY_APPROVAL_KIND"),
        "preventive-boundary approval kind",
        r"[A-Z][A-Za-z0-9]{2,127}",
    )
    boundary_approval_name = string_value(
        environment("FS2_EDGE_GATE_BOUNDARY_APPROVAL_NAME"),
        "preventive-boundary approval name",
        r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?",
    )
    boundary_approval_resource = string_value(
        environment("FS2_EDGE_GATE_BOUNDARY_APPROVAL_RESOURCE"),
        "preventive-boundary approval resource",
        r"[a-z][a-z0-9.-]{2,127}",
    )
    expected_boundary_approval_sha256 = digest(
        environment("FS2_EDGE_GATE_BOUNDARY_APPROVAL_SHA256"),
        "preventive-boundary approval digest",
    )
    preventive_boundary_trust_sha256 = digest(
        environment("FS2_EDGE_GATE_PREVENTIVE_BOUNDARY_TRUST_SHA256"),
        "preventive-boundary trust-store digest",
    )
    membership = load_membership_contract(
        root,
        membership_subject(
            project_id=expected_project_id,
            cluster_id=cluster_id,
            group_id=group_id,
            run_id=run_id,
            expected_count=expected_count,
            minimum_domains=minimum_domains,
            maximum_surge=maximum_surge,
            selector=selector,
            kubeconfig_sha256=kubeconfig_sha256,
        ),
        membership_trust_sha256=environment(
            "FS2_EDGE_GATE_MEMBERSHIP_TRUST_SHA256"
        ),
        provider_adapter_trust_sha256=environment(
            "FS2_EDGE_GATE_PROVIDER_ADAPTER_TRUST_SHA256"
        ),
    )
    signed_member_ids = [
        string_value(item, "signed provider member ID", r"computeinstance-[a-z0-9]+")
        for item in list_value(
            membership["provider_member_instance_ids"],
            "signed provider member IDs",
        )
    ]
    serving_member_ids = {
        string_value(item, "serving member ID", r"computeinstance-[a-z0-9]+")
        for item in list_value(
            membership["serving_member_instance_ids"], "serving member IDs"
        )
    }
    joining_member_ids = {
        string_value(item, "joining member ID", r"computeinstance-[a-z0-9]+")
        for item in list_value(
            membership["joining_member_instance_ids"], "joining member IDs"
        )
    }
    signed_toolchain = object_value(membership["toolchain"], "signed toolchain")
    pinned_tools = {
        name: pin_executable(signed_toolchain[name], name)
        for name in ("python3", "provider_observer", "kubectl")
    }
    if pinned_tools["python3"][1] != str(Path(sys.executable).resolve(strict=True)):
        fail("running Python interpreter differs from the signed toolchain")
    capsule_kubectl_fd = int(CAPSULE_TOOL_PATHS["kubectl"].rsplit("/", 1)[1])
    signed_kubectl_fd = pinned_tools["kubectl"][2]
    capsule_kubectl_stat = os.fstat(capsule_kubectl_fd)
    signed_kubectl_stat = os.fstat(signed_kubectl_fd)
    if (capsule_kubectl_stat.st_dev, capsule_kubectl_stat.st_ino) != (
        signed_kubectl_stat.st_dev,
        signed_kubectl_stat.st_ino,
    ):
        fail("signed kubectl differs from the accepted capsule executable")
    PINNED_COMMAND_FDS = tuple(
        sorted(
            {
                kubeconfig_descriptor,
                *(details[2] for details in pinned_tools.values()),
                *CAPSULE_COMMAND_FDS,
            }
        )
    )
    observer_authority = object_value(
        membership["provider_observer"], "provider observer authority"
    )
    provider_observer = [
        f"/proc/self/fd/{pinned_tools['provider_observer'][2]}",
        "--endpoint",
        str(observer_authority["endpoint"]),
        "--credential-authority",
        str(observer_authority["credential_authority"]),
        "--credential-subject",
        str(observer_authority["credential_subject"]),
        "--audience",
        str(observer_authority["audience"]),
        "--configuration-sha256",
        str(observer_authority["configuration_sha256"]),
        "--format",
        "json",
    ]
    cluster_cli = [*provider_observer, "mk8s", "cluster"]
    node_group_cli = [*provider_observer, "mk8s", "node-group"]
    compute_instance_cli = [*provider_observer, "compute", "instance"]
    kubectl = [
        f"/proc/self/fd/{pinned_tools['kubectl'][2]}",
        "--kubeconfig",
        str(kubeconfig),
        "--context",
        context,
        "--request-timeout=15s",
    ]

    policy_before = run_json(
        [
            *kubectl,
            "get",
            "validatingadmissionpolicy",
            "fs2-public-edge-node-authority",
            "-o",
            "json",
        ],
        "ValidatingAdmissionPolicy before",
    )
    binding_before = run_json(
        [
            *kubectl,
            "get",
            "validatingadmissionpolicybinding",
            "fs2-public-edge-node-authority",
            "-o",
            "json",
        ],
        "ValidatingAdmissionPolicyBinding before",
    )
    cas_policy_before = run_json(
        [
            *kubectl,
            "get",
            "validatingadmissionpolicy",
            "fs2-public-edge-node-authority-cas",
            "-o",
            "json",
        ],
        "CAS ValidatingAdmissionPolicy before",
    )
    cas_binding_before = run_json(
        [
            *kubectl,
            "get",
            "validatingadmissionpolicybinding",
            "fs2-public-edge-node-authority-cas-binding",
            "-o",
            "json",
        ],
        "CAS ValidatingAdmissionPolicyBinding before",
    )
    bootstrap_policy_before = run_json(
        [
            *kubectl,
            "get",
            "validatingadmissionpolicy",
            "fs2-public-edge-cas-bootstrap",
            "-o",
            "json",
        ],
        "bootstrap ValidatingAdmissionPolicy before",
    )
    bootstrap_binding_before = run_json(
        [
            *kubectl,
            "get",
            "validatingadmissionpolicybinding",
            "fs2-public-edge-cas-bootstrap-binding",
            "-o",
            "json",
        ],
        "bootstrap ValidatingAdmissionPolicyBinding before",
    )
    boundary_approval_path = (
        f"/apis/{boundary_approval_api_version}/"
        f"{boundary_approval_resource}/{boundary_approval_name}"
    )
    boundary_approval_before = run_json(
        [*kubectl, "get", "--raw", boundary_approval_path],
        "preventive-boundary approval before",
    )

    cluster_before = run_json(
        [*cluster_cli, "get", "--id", cluster_id], "cluster before"
    )
    project_id = string_value(
        metadata(cluster_before, "cluster before").get("parent_id"),
        "cluster.metadata.parent_id",
        r"project-[a-z0-9]+",
    )
    if project_id != expected_project_id:
        fail("cluster provider project differs from the signed Terraform subject")
    group_list_before = paginated_list(
        node_group_cli,
        ["list", "--parent-id", cluster_id],
        "NodeGroup list before",
    )
    group_before = run_json(
        [*node_group_cli, "get", "--id", group_id], "NodeGroup before"
    )
    instances_before = paginated_list(
        compute_instance_cli,
        ["list", "--parent-id", project_id],
        "Compute instance list before",
    )
    member_ids_before = provider_member_ids(
        instances_before,
        project_id=project_id,
        signed_instance_ids=signed_member_ids,
    )
    instance_gets_before = {
        instance_id: run_json(
            [*compute_instance_cli, "get", "--id", instance_id],
            f"Compute instance {instance_id} before",
        )
        for instance_id in member_ids_before
    }
    nodes_before = run_json([*kubectl, "get", "nodes", "-o", "json"], "NodeList before")
    namespace = run_json(
        [*kubectl, "get", "namespace", "kube-system", "-o", "json"],
        "kube-system Namespace",
    )
    # The second NodeList is read before the terminal provider sandwich. The
    # admission policy continuously protects member providerIDs and selector
    # labels after this read; the scheduler independently re-evaluates current
    # readiness, cordon, taints, and required spread at every Pod bind.
    nodes_after = run_json([*kubectl, "get", "nodes", "-o", "json"], "NodeList after")
    group_list_after = paginated_list(
        node_group_cli,
        ["list", "--parent-id", cluster_id],
        "NodeGroup list after",
    )
    instances_after = paginated_list(
        compute_instance_cli,
        ["list", "--parent-id", project_id],
        "Compute instance list after",
    )
    member_ids_after = provider_member_ids(
        instances_after,
        project_id=project_id,
        signed_instance_ids=signed_member_ids,
    )
    instance_gets_after = {
        instance_id: run_json(
            [*compute_instance_cli, "get", "--id", instance_id],
            f"Compute instance {instance_id} after",
        )
        for instance_id in member_ids_after
    }
    group_after = run_json(
        [*node_group_cli, "get", "--id", group_id], "NodeGroup after"
    )
    cluster_after = run_json(
        [*cluster_cli, "get", "--id", cluster_id], "cluster after"
    )
    policy_after = run_json(
        [
            *kubectl,
            "get",
            "validatingadmissionpolicy",
            "fs2-public-edge-node-authority",
            "-o",
            "json",
        ],
        "ValidatingAdmissionPolicy after",
    )
    binding_after = run_json(
        [
            *kubectl,
            "get",
            "validatingadmissionpolicybinding",
            "fs2-public-edge-node-authority",
            "-o",
            "json",
        ],
        "ValidatingAdmissionPolicyBinding after",
    )
    cas_policy_after = run_json(
        [
            *kubectl,
            "get",
            "validatingadmissionpolicy",
            "fs2-public-edge-node-authority-cas",
            "-o",
            "json",
        ],
        "CAS ValidatingAdmissionPolicy after",
    )
    cas_binding_after = run_json(
        [
            *kubectl,
            "get",
            "validatingadmissionpolicybinding",
            "fs2-public-edge-node-authority-cas-binding",
            "-o",
            "json",
        ],
        "CAS ValidatingAdmissionPolicyBinding after",
    )
    bootstrap_policy_after = run_json(
        [
            *kubectl,
            "get",
            "validatingadmissionpolicy",
            "fs2-public-edge-cas-bootstrap",
            "-o",
            "json",
        ],
        "bootstrap ValidatingAdmissionPolicy after",
    )
    bootstrap_binding_after = run_json(
        [
            *kubectl,
            "get",
            "validatingadmissionpolicybinding",
            "fs2-public-edge-cas-bootstrap-binding",
            "-o",
            "json",
        ],
        "bootstrap ValidatingAdmissionPolicyBinding after",
    )
    boundary_approval_after = run_json(
        [*kubectl, "get", "--raw", boundary_approval_path],
        "preventive-boundary approval after",
    )

    if metadata(namespace, "kube-system Namespace").get("uid") != expected_namespace_uid:
        fail("selected Kubernetes API has a different kube-system UID")
    validated_project_id, cluster_revision, cluster_sha = validate_cluster(
        cluster_before,
        cluster_after,
        cluster_id=cluster_id,
    )
    if validated_project_id != project_id:
        fail("cluster project changed during the apply-time eligibility observation")
    group_revision, group_sha = validate_node_group(
        group_list_before,
        group_list_after,
        group_before,
        group_after,
        cluster_id=cluster_id,
        group_id=group_id,
        expected_count=expected_count,
        maximum_surge=maximum_surge,
        membership_phase=str(membership["phase"]),
        selector=selector,
    )
    if group_revision != membership["node_group_resource_version"]:
        fail("current NodeGroup revision differs from the signed provider relation")
    provider_ids, provider_members_sha = validate_provider_members(
        instances_before,
        instance_gets_before,
        instances_after,
        instance_gets_after,
        project_id=project_id,
        signed_instance_ids=signed_member_ids,
        expected_count=expected_count,
        maximum_surge=maximum_surge,
    )
    node_revision, eligible_count, domain_count, nodes_sha = validate_nodes(
        nodes_before,
        nodes_after,
        cluster_id=cluster_id,
        group_id=group_id,
        run_id=run_id,
        selector=selector,
        provider_member_ids=provider_ids,
        serving_member_ids=serving_member_ids,
        expected_count=expected_count,
        minimum_domains=minimum_domains,
        joining_member_ids=joining_member_ids,
    )
    policy_revision, policy_uid, policy_sha256 = validate_admission_contract(
        policy_before,
        policy_after,
        kind="ValidatingAdmissionPolicy",
        expected_sha256=expected_policy_sha256,
    )
    binding_revision, binding_uid, binding_sha256 = validate_admission_contract(
        binding_before,
        binding_after,
        kind="ValidatingAdmissionPolicyBinding",
        expected_sha256=expected_binding_sha256,
    )
    cas_policy_revision, cas_policy_uid, cas_policy_sha256 = validate_admission_contract(
        cas_policy_before,
        cas_policy_after,
        kind="ValidatingAdmissionPolicy",
        expected_sha256=expected_cas_policy_sha256,
        name="fs2-public-edge-node-authority-cas",
    )
    cas_binding_revision, cas_binding_uid, cas_binding_sha256 = validate_admission_contract(
        cas_binding_before,
        cas_binding_after,
        kind="ValidatingAdmissionPolicyBinding",
        expected_sha256=expected_cas_binding_sha256,
        name="fs2-public-edge-node-authority-cas-binding",
    )
    bootstrap_policy_revision, bootstrap_policy_uid, bootstrap_policy_sha256 = validate_admission_contract(
        bootstrap_policy_before,
        bootstrap_policy_after,
        kind="ValidatingAdmissionPolicy",
        expected_sha256=expected_bootstrap_policy_sha256,
        name="fs2-public-edge-cas-bootstrap",
    )
    bootstrap_binding_revision, bootstrap_binding_uid, bootstrap_binding_sha256 = validate_admission_contract(
        bootstrap_binding_before,
        bootstrap_binding_after,
        kind="ValidatingAdmissionPolicyBinding",
        expected_sha256=expected_bootstrap_binding_sha256,
        name="fs2-public-edge-cas-bootstrap-binding",
    )
    boundary_approval_revision, boundary_approval_uid, boundary_approval_sha256 = validate_boundary_approval(
        boundary_approval_before,
        boundary_approval_after,
        api_version=boundary_approval_api_version,
        kind=boundary_approval_kind,
        name=boundary_approval_name,
        expected_sha256=expected_boundary_approval_sha256,
    )
    boundary_approval_projection = {
        "apiVersion": boundary_approval_api_version,
        "kind": boundary_approval_kind,
        "metadata": {
            "name": boundary_approval_name,
            "resourceVersion": object_value(
                boundary_approval_after.get("metadata"),
                "boundary approval after.metadata",
            ).get("resourceVersion"),
            "uid": object_value(
                boundary_approval_after.get("metadata"),
                "boundary approval after.metadata",
            ).get("uid"),
        },
        "spec": object_value(
            boundary_approval_after.get("spec"), "boundary approval after.spec"
        ),
        "status": boundary_approval_after.get("status"),
    }
    preventive_boundary = load_preventive_boundary_contract(
        root,
        project_id=expected_project_id,
        cluster_id=cluster_id,
        approval_projection=boundary_approval_projection,
        preventive_boundary_trust_sha256=preventive_boundary_trust_sha256,
    )
    if preventive_boundary["approval_sha256"] != boundary_approval_sha256:
        fail("signed preventive-boundary receipt changed after live approval validation")
    # Re-evaluate the bounded saved-plan window after every provider and
    # Kubernetes read. The short-lived mutation observation is timestamped
    # here, after the prerequisites and fresh reads, rather than at plan time.
    plan_age = parse_plan_timestamp(
        environment("FS2_EDGE_GATE_PLANNED_AT"),
        now=datetime.now(timezone.utc),
        maximum_age=maximum_age,
    )
    observed_at = datetime.now(timezone.utc).replace(microsecond=0)
    receipt_sha = canonical_sha256(
        {
            "schema": "fs2-serve.nebius.ai/public-edge-apply-eligibility/v1",
            "stage": stage,
            "cluster_id": cluster_id,
            "cluster_resource_version": cluster_revision,
            "cluster_sha256": cluster_sha,
            "node_group_id": group_id,
            "node_group_resource_version": group_revision,
            "node_group_sha256": group_sha,
            "membership_payload_sha256": membership["payload_sha256"],
            "membership_receipt_sha256": membership["receipt_sha256"],
            "membership_evidence_sha256": membership["evidence_sha256"],
            "membership_epoch_id": membership["epoch_id"],
            "membership_phase": membership["phase"],
            "membership_predecessor_payload_sha256": membership[
                "predecessor_payload_sha256"
            ],
            "provider_members_sha256": provider_members_sha,
            "provider_member_count": len(provider_ids),
            "node_group_list_pagination": group_list_after["pagination"],
            "compute_instance_list_pagination": instances_after["pagination"],
            "node_list_resource_version": node_revision,
            "eligible_nodes_sha256": nodes_sha,
            "kubeconfig_sha256": kubeconfig_sha256,
            "admission_policy_resource_version": policy_revision,
            "admission_policy_uid": policy_uid,
            "admission_policy_sha256": policy_sha256,
            "admission_binding_resource_version": binding_revision,
            "admission_binding_uid": binding_uid,
            "admission_binding_sha256": binding_sha256,
            "admission_cas_policy_resource_version": cas_policy_revision,
            "admission_cas_policy_uid": cas_policy_uid,
            "admission_cas_policy_sha256": cas_policy_sha256,
            "admission_cas_binding_resource_version": cas_binding_revision,
            "admission_cas_binding_uid": cas_binding_uid,
            "admission_cas_binding_sha256": cas_binding_sha256,
            "admission_bootstrap_policy_resource_version": bootstrap_policy_revision,
            "admission_bootstrap_policy_uid": bootstrap_policy_uid,
            "admission_bootstrap_policy_sha256": bootstrap_policy_sha256,
            "admission_bootstrap_binding_resource_version": bootstrap_binding_revision,
            "admission_bootstrap_binding_uid": bootstrap_binding_uid,
            "admission_bootstrap_binding_sha256": bootstrap_binding_sha256,
            "admission_boundary_approval_resource_version": boundary_approval_revision,
            "admission_boundary_approval_uid": boundary_approval_uid,
            "admission_boundary_approval_sha256": boundary_approval_sha256,
            "preventive_boundary_payload_sha256": preventive_boundary["payload_sha256"],
            "preventive_boundary_receipt_sha256": preventive_boundary["receipt_sha256"],
            "preventive_boundary_evidence_sha256": preventive_boundary["evidence_sha256"],
            "eligible_node_count": eligible_count,
            "distinct_hostname_count": domain_count,
            "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
        }
    )
    result = {
        "verdict": "PASS",
        "stage": stage,
        "plan_age_seconds": str(plan_age),
        "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
        "cluster_resource_version": cluster_revision,
        "node_group_resource_version": group_revision,
        "node_list_resource_version": node_revision,
        "kubeconfig_sha256": kubeconfig_sha256,
        "admission_policy_resource_version": policy_revision,
        "admission_policy_uid": policy_uid,
        "admission_policy_sha256": policy_sha256,
        "admission_binding_resource_version": binding_revision,
        "admission_binding_uid": binding_uid,
        "admission_binding_sha256": binding_sha256,
        "admission_cas_policy_resource_version": cas_policy_revision,
        "admission_cas_policy_uid": cas_policy_uid,
        "admission_cas_policy_sha256": cas_policy_sha256,
        "admission_cas_binding_resource_version": cas_binding_revision,
        "admission_cas_binding_uid": cas_binding_uid,
        "admission_cas_binding_sha256": cas_binding_sha256,
        "admission_bootstrap_policy_resource_version": bootstrap_policy_revision,
        "admission_bootstrap_policy_uid": bootstrap_policy_uid,
        "admission_bootstrap_policy_sha256": bootstrap_policy_sha256,
        "admission_bootstrap_binding_resource_version": bootstrap_binding_revision,
        "admission_bootstrap_binding_uid": bootstrap_binding_uid,
        "admission_bootstrap_binding_sha256": bootstrap_binding_sha256,
        "admission_boundary_approval_resource_version": boundary_approval_revision,
        "admission_boundary_approval_uid": boundary_approval_uid,
        "admission_boundary_approval_sha256": boundary_approval_sha256,
        "preventive_boundary_payload_sha256": preventive_boundary["payload_sha256"],
        "preventive_boundary_receipt_sha256": preventive_boundary["receipt_sha256"],
        "preventive_boundary_evidence_sha256": preventive_boundary["evidence_sha256"],
        "provider_member_count": str(len(provider_ids)),
        "eligible_node_count": str(eligible_count),
        "hostname_domain_count": str(domain_count),
        "membership_payload_sha256": str(membership["payload_sha256"]),
        "membership_receipt_sha256": str(membership["receipt_sha256"]),
        "membership_epoch_id": str(membership["epoch_id"]),
        "membership_phase": str(membership["phase"]),
        "receipt_sha256": receipt_sha,
    }
    if sys.argv[1:] == ["--external"]:
        # hashicorp/external requires a map of string values on stdout. The
        # query is an ordering nonce only; all authority is freshly reread.
        if EXTERNAL_QUERY is None or not isinstance(EXTERNAL_QUERY.get("gate_id"), str):
            fail("external mutation fence query is malformed")
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    elif sys.argv[1:]:
        fail("unsupported arguments")
    else:
        print(
            "PASS: public-edge apply eligibility "
            + " ".join(f"{key}={value}" for key, value in result.items() if key != "verdict")
        )
    return 0


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--receipt-contract"]:
            raise SystemExit(receipt_contract_main())
        if sys.argv[1:] == ["--external"]:
            external_query = json.load(
                sys.stdin, object_pairs_hook=no_duplicate_object
            )
            required_query_keys = {
                "gate_id",
                "verifier_sha256",
                "FS2_EDGE_GATE_STAGE",
                "FS2_EDGE_GATE_PLANNED_AT",
                "FS2_EDGE_GATE_MAX_PLAN_AGE_SECONDS",
                "FS2_EDGE_GATE_RUN_ROOT",
                "FS2_EDGE_GATE_KUBECONFIG",
                "FS2_EDGE_GATE_KUBE_CONTEXT",
                "FS2_EDGE_GATE_CLUSTER_ID",
                "FS2_EDGE_GATE_PROJECT_ID",
                "FS2_EDGE_GATE_KUBE_SYSTEM_UID",
                "FS2_EDGE_GATE_NODE_GROUP_ID",
                "FS2_EDGE_GATE_RUN_ID",
                "FS2_EDGE_GATE_EXPECTED_NODE_COUNT",
                "FS2_EDGE_GATE_MINIMUM_DOMAINS",
                "FS2_EDGE_GATE_MAXIMUM_SURGE_MEMBERS",
                "FS2_EDGE_GATE_NODE_SELECTOR_JSON",
                "FS2_EDGE_GATE_POLICY_SHA256",
                "FS2_EDGE_GATE_BINDING_SHA256",
                "FS2_EDGE_GATE_CAS_POLICY_SHA256",
                "FS2_EDGE_GATE_CAS_BINDING_SHA256",
                "FS2_EDGE_GATE_BOOTSTRAP_POLICY_SHA256",
                "FS2_EDGE_GATE_BOOTSTRAP_BINDING_SHA256",
                "FS2_EDGE_GATE_BOUNDARY_APPROVAL_API_VERSION",
                "FS2_EDGE_GATE_BOUNDARY_APPROVAL_KIND",
                "FS2_EDGE_GATE_BOUNDARY_APPROVAL_NAME",
                "FS2_EDGE_GATE_BOUNDARY_APPROVAL_RESOURCE",
                "FS2_EDGE_GATE_BOUNDARY_APPROVAL_SHA256",
                "FS2_EDGE_GATE_MEMBERSHIP_TRUST_SHA256",
                "FS2_EDGE_GATE_PROVIDER_ADAPTER_TRUST_SHA256",
            }
            EXTERNAL_QUERY = exact_object(
                external_query, required_query_keys, "external mutation fence query"
            )
            if not all(isinstance(value, str) for value in EXTERNAL_QUERY.values()):
                fail("external mutation fence query values must all be strings")
        raise SystemExit(main())
    except (GateError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"ERROR: public-edge apply eligibility: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
