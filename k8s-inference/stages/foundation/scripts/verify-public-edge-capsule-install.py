#!/usr/bin/env python3
"""Verify a public-edge capsule bundle before privileged installation.

This command is an offline package verifier, not an apply-time trust input. It
opens fixed root-owned issuer and acceptance authorities and checks the
Ed25519 installation receipt, exact accepted commit/tree,
complete release inventory, launcher/bootstrap bytes, tool bundle, and
Terraform provider mirror. The production registry is intentionally empty in
source; enrollment requires a separate reviewed commit.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


MANIFEST_SCHEMA = "fs2-serve.nebius.ai/public-edge-execution-capsule/v1"
TRUST_SCHEMA = "fs2-serve.nebius.ai/trusted-public-edge-capsule-issuers/v1"
POLICY_SCHEMA = "fs2-serve.nebius.ai/public-edge-capsule-acceptance/v1"
ISSUER_ROLE = "platform-security-public-edge-capsule"
FIXED_TRUST_STORE = Path("/etc/fs2/public-edge-capsule-issuers.json")
FIXED_ACCEPTANCE_POLICY = Path("/etc/fs2/public-edge-capsule-acceptance.json")
HEX_40 = re.compile(r"^[a-f0-9]{40}$")
HEX_64 = re.compile(r"^[a-f0-9]{64}$")
KEY_ID = re.compile(r"^sha256:[a-f0-9]{64}$")


class VerificationError(RuntimeError):
    """The proposed installation bundle is not accepted."""


def fail(message: str) -> None:
    raise VerificationError(message)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def exact(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        fail(f"{label} must contain exactly {sorted(keys)}")
    return value


def canonical_file(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        fail(f"{label} must be an absolute regular non-symlink file")
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError(f"{label} is not UTF-8 JSON") from exc
    if canonical_bytes(value) + b"\n" != raw:
        fail(f"{label} must be canonical JSON plus one newline")
    if not isinstance(value, dict):
        fail(f"{label} must be a JSON object")
    return value, raw


def root_policy_file(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if path not in {FIXED_TRUST_STORE, FIXED_ACCEPTANCE_POLICY}:
        fail(f"{label} is not at its fixed authority path")
    for parent in (Path("/etc"), Path("/etc/fs2")):
        details = os.stat(parent, follow_symlinks=False)
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != 0
            or stat.S_IMODE(details.st_mode) & 0o022
        ):
            fail(f"{label} parent authority is not root-owned and protected")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise VerificationError(f"cannot open fixed {label}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or stat.S_IMODE(before.st_mode) & 0o022
        ):
            fail(f"{label} is not a protected root-owned regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            before.st_mode,
            before.st_uid,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_mode,
            after.st_uid,
        ):
            fail(f"{label} changed while read")
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError(f"{label} is not UTF-8 JSON") from exc
    if canonical_bytes(value) + b"\n" != raw or not isinstance(value, dict):
        fail(f"{label} must be canonical JSON plus one newline")
    return value, raw


def digest(value: object, label: str) -> str:
    if not isinstance(value, str) or HEX_64.fullmatch(value) is None:
        fail(f"{label} must be a lowercase SHA-256 digest")
    return value


def b64url(value: object, size: int, label: str) -> bytes:
    if not isinstance(value, str):
        fail(f"{label} must be base64url text")
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, binascii.Error) as exc:
        raise VerificationError(f"{label} is not canonical base64url") from exc
    if (
        len(decoded) != size
        or base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != value
    ):
        fail(f"{label} has the wrong size or encoding")
    return decoded


def sha256_file(path: Path, label: str) -> str:
    if path.is_symlink() or not path.is_file():
        fail(f"{label} is not a regular non-symlink file")
    before = path.stat()
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    after = path.stat()
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
        fail(f"{label} changed while hashed")
    return hasher.hexdigest()


def pinned_openssl(expected_sha256: str) -> tuple[int, str]:
    path = Path("/usr/bin/openssl")
    for parent in (Path("/usr"), Path("/usr/bin")):
        details = os.stat(parent, follow_symlinks=False)
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != 0
            or stat.S_IMODE(details.st_mode) & 0o022
        ):
            fail("OpenSSL parent directory is not root-owned and protected")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise VerificationError("cannot open fixed OpenSSL executable") from exc
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != 0
        or stat.S_IMODE(before.st_mode) & 0o022
    ):
        os.close(descriptor)
        fail("fixed OpenSSL executable is not root-owned and protected")
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
    ) or hasher.hexdigest() != expected_sha256:
        os.close(descriptor)
        fail("fixed OpenSSL executable differs from the root acceptance policy")
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.set_inheritable(descriptor, True)
    return descriptor, f"/proc/self/fd/{descriptor}"


def verify_signature(
    public_key: bytes,
    signature: bytes,
    message: bytes,
    *,
    openssl_fd: int,
    openssl_path: str,
) -> None:
    der = bytes.fromhex("302a300506032b6570032100") + public_key
    encoded = base64.b64encode(der).decode("ascii")
    pem = (
        "-----BEGIN PUBLIC KEY-----\n"
        + "\n".join(encoded[index : index + 64] for index in range(0, len(encoded), 64))
        + "\n-----END PUBLIC KEY-----\n"
    ).encode("ascii")
    with tempfile.TemporaryDirectory(prefix="fs2-capsule-verify-") as temporary:
        root = Path(temporary)
        key_path, message_path, signature_path = (
            root / "key.pem",
            root / "message",
            root / "signature",
        )
        for path, content in (
            (key_path, pem),
            (message_path, message),
            (signature_path, signature),
        ):
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
        result = subprocess.run(
            [
                openssl_path,
                "pkeyutl",
                "-verify",
                "-pubin",
                "-inkey",
                str(key_path),
                "-rawin",
                "-in",
                str(message_path),
                "-sigfile",
                str(signature_path),
            ],
            env={
                "HOME": "/nonexistent",
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            close_fds=True,
            pass_fds=(openssl_fd,),
        )
    if result.returncode != 0:
        fail("capsule installation receipt signature is invalid")


def trusted_key(trust: object, issuer: str, key_id: str) -> bytes:
    store = exact(trust, {"schema", "issuers"}, "capsule issuer registry")
    if store["schema"] != TRUST_SCHEMA or not isinstance(store["issuers"], list):
        fail("capsule issuer registry schema is unsupported")
    matches: list[bytes] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(store["issuers"]):
        record = exact(
            raw,
            {"id", "role", "key_id", "public_key"},
            f"capsule issuer {index}",
        )
        identity = (record["id"], record["key_id"])
        if identity in seen:
            fail("capsule issuer registry contains a duplicate authority")
        seen.add(identity)
        if (
            not isinstance(record["id"], str)
            or record["role"] != ISSUER_ROLE
            or not isinstance(record["key_id"], str)
            or KEY_ID.fullmatch(record["key_id"]) is None
        ):
            fail("capsule issuer registry contains a malformed authority")
        key = b64url(record["public_key"], 32, "capsule issuer public key")
        if record["key_id"] != "sha256:" + hashlib.sha256(key).hexdigest():
            fail("capsule issuer key ID does not bind its public key")
        if identity == (issuer, key_id):
            matches.append(key)
    if len(matches) != 1:
        fail("capsule receipt issuer is not one source-enrolled authority")
    return matches[0]


def verify_bundle(bundle: Path, manifest: dict[str, Any]) -> None:
    if not bundle.is_absolute() or bundle.is_symlink() or not bundle.is_dir():
        fail("bundle root must be an absolute non-symlink directory")
    files = manifest.get("release_files")
    if not isinstance(files, list) or not files:
        fail("capsule release inventory must be non-empty")
    expected: list[str] = []
    for index, raw in enumerate(files):
        record = exact(raw, {"relative_path", "sha256"}, f"release file {index}")
        relative = record["relative_path"]
        if (
            not isinstance(relative, str)
            or not relative
            or relative.startswith("/")
            or ".." in Path(relative).parts
        ):
            fail("release inventory contains an unsafe path")
        expected.append(relative)
        if sha256_file(bundle / relative, f"release file {relative}") != digest(
            record["sha256"], f"release file {relative} digest"
        ):
            fail(f"release file {relative} differs from the accepted digest")
    observed: list[str] = []
    for directory, directories, filenames in os.walk(bundle, followlinks=False):
        root = Path(directory)
        if any((root / name).is_symlink() for name in directories):
            fail("capsule bundle contains a directory symlink")
        for name in filenames:
            path = root / name
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                fail("capsule bundle contains a non-regular file")
            observed.append(path.relative_to(bundle).as_posix())
    if expected != sorted(set(expected)) or sorted(observed) != expected:
        fail("capsule bundle file set differs from the accepted inventory")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--launcher-binary", required=True, type=Path)
    parser.add_argument("--bootstrap", required=True, type=Path)
    arguments = parser.parse_args()
    manifest, manifest_raw = canonical_file(
        arguments.manifest.resolve(), "capsule manifest"
    )
    trust, trust_raw = root_policy_file(FIXED_TRUST_STORE, "capsule trust store")
    policy_raw, policy_bytes = root_policy_file(
        FIXED_ACCEPTANCE_POLICY, "capsule acceptance policy"
    )
    policy = exact(
        policy_raw,
        {
            "schema",
            "accepted_commit",
            "accepted_tree",
            "manifest_sha256",
            "trust_store_sha256",
            "launcher_sha256",
            "bootstrap_sha256",
            "openssl_sha256",
            "approved_by",
            "approved_at",
        },
        "capsule acceptance policy",
    )
    if policy["schema"] != POLICY_SCHEMA:
        fail("capsule acceptance policy schema is unsupported")
    if hashlib.sha256(trust_raw).hexdigest() != digest(
        policy["trust_store_sha256"], "policy trust-store digest"
    ):
        fail("fixed capsule issuer registry differs from the root acceptance policy")
    accepted = exact(
        manifest,
        {
            "schema",
            "accepted_commit",
            "accepted_tree",
            "installed_source_root",
            "capsule_group",
            "launcher_sha256",
            "bootstrap_sha256",
            "sources",
            "tools",
            "terraform",
            "release_files",
            "installation_receipt",
        },
        "capsule manifest",
    )
    if accepted["schema"] != MANIFEST_SCHEMA:
        fail("capsule manifest schema is unsupported")
    if not isinstance(accepted["accepted_commit"], str) or HEX_40.fullmatch(
        accepted["accepted_commit"]
    ) is None:
        fail("accepted commit is malformed")
    if not isinstance(accepted["accepted_tree"], str) or HEX_40.fullmatch(
        accepted["accepted_tree"]
    ) is None:
        fail("accepted tree is malformed")
    manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
    if (
        accepted["accepted_commit"] != policy["accepted_commit"]
        or accepted["accepted_tree"] != policy["accepted_tree"]
        or manifest_sha256 != digest(policy["manifest_sha256"], "policy manifest digest")
        or accepted["launcher_sha256"]
        != digest(policy["launcher_sha256"], "policy launcher digest")
        or accepted["bootstrap_sha256"]
        != digest(policy["bootstrap_sha256"], "policy bootstrap digest")
        or not isinstance(policy["approved_by"], str)
        or not policy["approved_by"]
        or not isinstance(policy["approved_at"], str)
        or not policy["approved_at"]
    ):
        fail("manifest identity differs from the fixed root acceptance policy")
    openssl_sha256 = digest(policy["openssl_sha256"], "policy OpenSSL digest")
    openssl_fd, openssl_path = pinned_openssl(openssl_sha256)
    receipt = exact(
        accepted["installation_receipt"],
        {"issuer", "key_id", "payload_sha256", "signature", "reviewed_at"},
        "capsule installation receipt",
    )
    payload = {key: accepted[key] for key in accepted if key != "installation_receipt"}
    payload_sha256 = digest(receipt["payload_sha256"], "capsule payload digest")
    if hashlib.sha256(canonical_bytes(payload)).hexdigest() != payload_sha256:
        fail("capsule installation receipt does not bind the manifest payload")
    if not isinstance(receipt["issuer"], str) or not isinstance(receipt["key_id"], str):
        fail("capsule installation receipt issuer is malformed")
    public_key = trusted_key(trust, receipt["issuer"], receipt["key_id"])
    signature = b64url(receipt["signature"], 64, "capsule receipt signature")
    verify_signature(
        public_key,
        signature,
        canonical_bytes(
            {
                "schema": accepted["schema"],
                "payload": payload,
                "payload_sha256": payload_sha256,
                "reviewed_at": receipt["reviewed_at"],
            }
        ),
        openssl_fd=openssl_fd,
        openssl_path=openssl_path,
    )
    os.close(openssl_fd)
    verify_bundle(arguments.bundle.resolve(), accepted)
    if sha256_file(arguments.launcher_binary.resolve(), "launcher binary") != digest(
        accepted["launcher_sha256"], "accepted launcher digest"
    ):
        fail("launcher binary differs from the accepted digest")
    if sha256_file(arguments.bootstrap.resolve(), "bootstrap") != digest(
        accepted["bootstrap_sha256"], "accepted bootstrap digest"
    ):
        fail("bootstrap differs from the accepted digest")
    print(
        json.dumps(
            {
                "status": "VERIFIED_FOR_INSTALL",
                "accepted_commit": accepted["accepted_commit"],
                "accepted_tree": accepted["accepted_tree"],
                "manifest_sha256": manifest_sha256,
                "acceptance_policy_sha256": hashlib.sha256(policy_bytes).hexdigest(),
                "release_file_count": len(accepted["release_files"]),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except VerificationError as exc:
        print(f"capsule installation verification failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
