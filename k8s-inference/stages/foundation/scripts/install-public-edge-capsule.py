#!/usr/bin/env python3
"""Install and atomically activate one signed public-edge capsule.

This is the privileged package-owner boundary.  Integration installs these
exact reviewed bytes at the fixed path below, root:root mode 0555, and binds
their digest in the root-owned acceptance policy.  The installer stable-opens
every caller input before verification, retains those descriptors through the
copy, writes only absent versioned destinations, and never removes an earlier
release or activation unit.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import ctypes
import fcntl
import grp
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


MANIFEST_SCHEMA = "fs2-serve.nebius.ai/public-edge-execution-capsule/v2"
TRUST_SCHEMA = "fs2-serve.nebius.ai/trusted-public-edge-capsule-issuers/v1"
POLICY_SCHEMA = "fs2-serve.nebius.ai/public-edge-capsule-acceptance/v1"
ISSUER_ROLE = "platform-security-public-edge-capsule"
FIXED_INSTALLER = Path("/usr/local/sbin/fs2-install-public-edge-capsule")
FIXED_INSTALLER_SOURCE = Path("/usr/local/libexec/fs2-public-edge-installer.py")
FIXED_TRUST_STORE = Path("/etc/fs2/public-edge-capsule-issuers.json")
FIXED_ACCEPTANCE_POLICY = Path("/etc/fs2/public-edge-capsule-acceptance.json")
FIXED_STATIC_OPENSSL = Path("/usr/local/libexec/fs2-public-edge-installer-openssl-static")
RELEASE_ROOT = Path("/opt/fs2/releases")
RELEASE_ATTEMPT_ROOT = Path("/opt/fs2/release-attempts")
ACTIVATION_ROOT = Path("/usr/local/libexec/fs2-public-edge-activations")
ACTIVATION_ATTEMPT_ROOT = Path("/usr/local/libexec/fs2-public-edge-activation-attempts")
CURRENT_LINK = Path("/usr/local/libexec/fs2-public-edge-current")
INSTALL_LOCK = Path("/run/fs2-public-edge-capsule-install.lock")
CAPSULE_GROUP = "fs2-public-edge-capsule"
HEX_40 = re.compile(r"^[a-f0-9]{40}$")
HEX_64 = re.compile(r"^[a-f0-9]{64}$")
KEY_ID = re.compile(r"^sha256:[a-f0-9]{64}$")
MAX_FILE_BYTES = 1024 * 1024 * 1024
RENAME_NOREPLACE = 1
RENAME_EXCHANGE = 2


class InstallError(RuntimeError):
    """The proposed capsule cannot be safely installed."""


def fail(message: str) -> None:
    raise InstallError(message)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def exact(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        fail(f"{label} must contain exactly {sorted(keys)}")
    return value


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
        raise InstallError(f"{label} is not canonical base64url") from exc
    if (
        len(decoded) != size
        or base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != value
    ):
        fail(f"{label} has the wrong size or encoding")
    return decoded


def require_static_elf(raw: bytes, label: str) -> None:
    if (
        len(raw) < 64
        or raw[:4] != b"\x7fELF"
        or raw[4] != 2
        or raw[5] != 1
        or raw[6] != 1
    ):
        fail(f"{label} is not one supported ELF64 little-endian executable")
    program_offset = int.from_bytes(raw[32:40], "little")
    entry_size = int.from_bytes(raw[54:56], "little")
    entry_count = int.from_bytes(raw[56:58], "little")
    if entry_size < 56 or entry_count < 1 or program_offset + entry_size * entry_count > len(raw):
        fail(f"{label} has a malformed ELF program-header table")
    program_types = {
        int.from_bytes(
            raw[program_offset + index * entry_size : program_offset + index * entry_size + 4],
            "little",
        )
        for index in range(entry_count)
    }
    if 2 in program_types or 3 in program_types:
        fail(f"{label} must be fully static with no PT_DYNAMIC or PT_INTERP")


def protected_directory(path: Path, *, allowed_gids: set[int] | None = None) -> None:
    details = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != 0
        or (allowed_gids is not None and details.st_gid not in allowed_gids)
        or stat.S_IMODE(details.st_mode) & 0o022
    ):
        fail(f"protected directory authority is unsafe: {path}")


def reject_symlink_argument(path: Path, label: str, *, directory: bool = False) -> int:
    if not path.is_absolute():
        fail(f"{label} must be an absolute path")
    try:
        details = os.lstat(path)
    except OSError as exc:
        raise InstallError(f"cannot inspect {label}") from exc
    expected = stat.S_ISDIR(details.st_mode) if directory else stat.S_ISREG(details.st_mode)
    if stat.S_ISLNK(details.st_mode) or not expected:
        fail(f"{label} argument must be a real {'directory' if directory else 'regular file'}, not a symlink")
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise InstallError(f"cannot stable-open {label}") from exc
    after = os.fstat(descriptor)
    if (details.st_dev, details.st_ino, details.st_mode) != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
    ):
        os.close(descriptor)
        fail(f"{label} changed while it was opened")
    return descriptor


def read_stable(descriptor: int, label: str, maximum: int = MAX_FILE_BYTES) -> bytes:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
        fail(f"{label} is not one bounded regular file")
    chunks: list[bytes] = []
    offset = 0
    while offset < before.st_size:
        chunk = os.pread(descriptor, min(1024 * 1024, before.st_size - offset), offset)
        if not chunk:
            fail(f"{label} ended before its recorded size")
        chunks.append(chunk)
        offset += len(chunk)
    after = os.fstat(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
        before.st_mode,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
        after.st_mode,
    ):
        fail(f"{label} changed while read")
    return b"".join(chunks)


def canonical_json(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"{label} is not UTF-8 JSON") from exc
    if canonical_bytes(value) + b"\n" != raw or not isinstance(value, dict):
        fail(f"{label} must be canonical JSON plus one newline")
    return value


def open_root_authority(path: Path, label: str) -> tuple[int, bytes]:
    if path not in {FIXED_TRUST_STORE, FIXED_ACCEPTANCE_POLICY}:
        fail(f"{label} path is not fixed")
    protected_directory(Path("/etc"))
    protected_directory(Path("/etc/fs2"))
    descriptor = reject_symlink_argument(path, label)
    details = os.fstat(descriptor)
    if details.st_uid != 0 or stat.S_IMODE(details.st_mode) & 0o022:
        os.close(descriptor)
        fail(f"{label} is not root-owned and protected")
    return descriptor, read_stable(descriptor, label, 1024 * 1024)


def sealed_memfd(name: str, content: bytes) -> int:
    descriptor = os.memfd_create(
        name, os.MFD_CLOEXEC | getattr(os, "MFD_ALLOW_SEALING", 0)
    )
    os.write(descriptor, content)
    seals = (
        fcntl.F_SEAL_SEAL
        | fcntl.F_SEAL_SHRINK
        | fcntl.F_SEAL_GROW
        | fcntl.F_SEAL_WRITE
    )
    fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, seals)
    if fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) & seals != seals:
        os.close(descriptor)
        fail(f"cannot seal {name}")
    return descriptor


def pinned_openssl(expected_sha256: str) -> tuple[int, str]:
    protected_directory(Path("/usr"))
    protected_directory(Path("/usr/bin"))
    protected_directory(Path("/usr/local"))
    protected_directory(Path("/usr/local/libexec"))
    descriptor = reject_symlink_argument(FIXED_STATIC_OPENSSL, "fixed static OpenSSL")
    details = os.fstat(descriptor)
    if details.st_uid != 0 or stat.S_IMODE(details.st_mode) & 0o022:
        os.close(descriptor)
        fail("fixed OpenSSL is not root-owned and protected")
    openssl_raw = read_stable(descriptor, "fixed static OpenSSL")
    require_static_elf(openssl_raw, "fixed installer OpenSSL")
    if hashlib.sha256(openssl_raw).hexdigest() != expected_sha256:
        os.close(descriptor)
        fail("fixed OpenSSL differs from the root acceptance policy")
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
    key_fd = sealed_memfd("capsule-install-key", pem)
    message_fd = sealed_memfd("capsule-install-message", message)
    signature_fd = sealed_memfd("capsule-install-signature", signature)
    try:
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
            env={"HOME": "/nonexistent", "PATH": "/nonexistent", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            close_fds=True,
            pass_fds=(openssl_fd, key_fd, message_fd, signature_fd),
        )
    finally:
        os.close(key_fd)
        os.close(message_fd)
        os.close(signature_fd)
    if result.returncode != 0:
        fail("capsule installation receipt signature is invalid")


def trusted_key(trust: object, issuer: str, key_id: str) -> bytes:
    store = exact(trust, {"schema", "issuers"}, "capsule issuer registry")
    if store["schema"] != TRUST_SCHEMA or not isinstance(store["issuers"], list):
        fail("capsule issuer registry schema is unsupported")
    matches: list[bytes] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(store["issuers"]):
        record = exact(raw, {"id", "role", "key_id", "public_key"}, f"issuer {index}")
        identity = (record["id"], record["key_id"])
        if identity in seen:
            fail("capsule issuer registry contains a duplicate authority")
        seen.add(identity)
        key = b64url(record["public_key"], 32, "capsule issuer public key")
        if (
            not isinstance(record["id"], str)
            or record["role"] != ISSUER_ROLE
            or not isinstance(record["key_id"], str)
            or KEY_ID.fullmatch(record["key_id"]) is None
            or record["key_id"] != "sha256:" + hashlib.sha256(key).hexdigest()
        ):
            fail("capsule issuer registry contains a malformed authority")
        if identity == (issuer, key_id):
            matches.append(key)
    if len(matches) != 1:
        fail("capsule receipt issuer is not one enrolled authority")
    return matches[0]


def enumerate_bundle(root_fd: int) -> dict[str, int]:
    opened: dict[str, int] = {}

    def visit(directory_fd: int, prefix: PurePosixPath) -> None:
        names = sorted(os.listdir(directory_fd))
        for name in names:
            if name in {".", ".."} or "/" in name or "\x00" in name:
                fail("capsule bundle contains an unsafe name")
            details = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            relative = (prefix / name).as_posix()
            if stat.S_ISDIR(details.st_mode):
                child = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
                try:
                    visit(child, prefix / name)
                finally:
                    os.close(child)
            elif stat.S_ISREG(details.st_mode):
                descriptor = os.open(
                    name,
                    os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
                after = os.fstat(descriptor)
                if (details.st_dev, details.st_ino, details.st_mode) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_mode,
                ):
                    os.close(descriptor)
                    fail("capsule bundle entry changed while opened")
                opened[relative] = descriptor
            else:
                fail("capsule bundle may contain only directories and regular files")

    visit(root_fd, PurePosixPath())
    return opened


def verify_inputs(
    *,
    manifest_fd: int,
    launcher_fd: int,
    bootstrap_fd: int,
    python_fd: int,
    bundle_files: Mapping[str, int],
    trust_raw: bytes,
    policy_raw: bytes,
) -> tuple[dict[str, Any], str, str]:
    manifest_raw = read_stable(manifest_fd, "capsule manifest", 8 * 1024 * 1024)
    manifest = exact(
        canonical_json(manifest_raw, "capsule manifest"),
        {
            "schema", "accepted_commit", "accepted_tree", "installed_source_root",
            "capsule_group", "installer_sha256", "launcher_sha256", "bootstrap_sha256",
            "sources", "tools", "terraform", "nebius_auth", "release_files",
            "installation_receipt",
        },
        "capsule manifest",
    )
    if (
        manifest["schema"] != MANIFEST_SCHEMA
        or manifest["capsule_group"] != CAPSULE_GROUP
        or not isinstance(manifest["accepted_commit"], str)
        or HEX_40.fullmatch(manifest["accepted_commit"]) is None
        or not isinstance(manifest["accepted_tree"], str)
        or HEX_40.fullmatch(manifest["accepted_tree"]) is None
    ):
        fail("capsule manifest identity is unsupported")
    manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
    trust = canonical_json(trust_raw, "capsule issuer registry")
    policy = exact(
        canonical_json(policy_raw, "capsule acceptance policy"),
        {
            "schema", "accepted_commit", "accepted_tree", "manifest_sha256",
            "trust_store_sha256", "installer_sha256", "launcher_sha256",
            "bootstrap_sha256", "openssl_sha256", "approved_by", "approved_at",
        },
        "capsule acceptance policy",
    )
    if policy["schema"] != POLICY_SCHEMA:
        fail("capsule acceptance policy schema is unsupported")
    self_fd = reject_symlink_argument(FIXED_INSTALLER, "fixed capsule installer gate")
    try:
        self_details = os.fstat(self_fd)
        self_sha256 = hashlib.sha256(read_stable(self_fd, "fixed capsule installer", 4 * 1024 * 1024)).hexdigest()
    finally:
        os.close(self_fd)
    if (
        self_details.st_uid != 0
        or stat.S_IMODE(self_details.st_mode) != 0o555
        or Path(__file__) != FIXED_INSTALLER_SOURCE
        or sys.flags.isolated != 1
        or sys.flags.no_site != 1
        or not sys.dont_write_bytecode
        or manifest["installer_sha256"] != self_sha256
        or policy["installer_sha256"] != self_sha256
    ):
        fail("running installer is not the isolated root-owned policy-pinned executable")
    if (
        hashlib.sha256(trust_raw).hexdigest() != digest(policy["trust_store_sha256"], "policy trust digest")
        or manifest["accepted_commit"] != policy["accepted_commit"]
        or manifest["accepted_tree"] != policy["accepted_tree"]
        or manifest_sha256 != digest(policy["manifest_sha256"], "policy manifest digest")
        or manifest["launcher_sha256"] != digest(policy["launcher_sha256"], "policy launcher digest")
        or manifest["bootstrap_sha256"] != digest(policy["bootstrap_sha256"], "policy bootstrap digest")
        or not isinstance(policy["approved_by"], str)
        or not policy["approved_by"]
        or not isinstance(policy["approved_at"], str)
        or not policy["approved_at"]
    ):
        fail("capsule manifest differs from the fixed root acceptance policy")
    receipt = exact(
        manifest["installation_receipt"],
        {"issuer", "key_id", "payload_sha256", "signature", "reviewed_at"},
        "installation receipt",
    )
    payload = {key: manifest[key] for key in manifest if key != "installation_receipt"}
    payload_sha256 = digest(receipt["payload_sha256"], "receipt payload digest")
    if hashlib.sha256(canonical_bytes(payload)).hexdigest() != payload_sha256:
        fail("installation receipt does not bind the manifest payload")
    public_key = trusted_key(trust, receipt["issuer"], receipt["key_id"])
    openssl_fd, openssl_path = pinned_openssl(digest(policy["openssl_sha256"], "policy OpenSSL digest"))
    try:
        verify_signature(
            public_key,
            b64url(receipt["signature"], 64, "receipt signature"),
            canonical_bytes(
                {
                    "schema": manifest["schema"],
                    "payload": payload,
                    "payload_sha256": payload_sha256,
                    "reviewed_at": receipt["reviewed_at"],
                }
            ),
            openssl_fd=openssl_fd,
            openssl_path=openssl_path,
        )
    finally:
        os.close(openssl_fd)
    records = manifest["release_files"]
    if not isinstance(records, list) or not records:
        fail("release inventory must be non-empty")
    expected: list[str] = []
    for index, raw in enumerate(records):
        record = exact(raw, {"relative_path", "sha256"}, f"release file {index}")
        relative = record["relative_path"]
        if (
            not isinstance(relative, str)
            or not relative
            or relative.startswith("/")
            or ".." in PurePosixPath(relative).parts
        ):
            fail("release inventory contains an unsafe path")
        expected.append(relative)
        descriptor = bundle_files.get(relative)
        if descriptor is None or hashlib.sha256(read_stable(descriptor, f"release file {relative}")).hexdigest() != digest(record["sha256"], "release digest"):
            fail(f"release file {relative} differs from the signed inventory")
    if expected != sorted(set(expected)) or sorted(bundle_files) != expected:
        fail("bundle file set differs from the signed exhaustive inventory")
    launcher_sha256 = hashlib.sha256(read_stable(launcher_fd, "launcher binary", 16 * 1024 * 1024)).hexdigest()
    bootstrap_sha256 = hashlib.sha256(read_stable(bootstrap_fd, "bootstrap", 8 * 1024 * 1024)).hexdigest()
    python_sha256 = hashlib.sha256(read_stable(python_fd, "capsule Python", 256 * 1024 * 1024)).hexdigest()
    python_record = exact(manifest["tools"]["python3"], {"relative_path", "sha256"}, "Python tool")
    if (
        launcher_sha256 != manifest["launcher_sha256"]
        or bootstrap_sha256 != manifest["bootstrap_sha256"]
        or python_sha256 != digest(python_record["sha256"], "Python tool digest")
        or python_record["relative_path"] not in bundle_files
    ):
        fail("launcher, bootstrap, or Python input differs from the signed capsule")
    source_root = Path(manifest["installed_source_root"])
    expected_leaf = f"{manifest['accepted_commit']}-{manifest['accepted_tree']}"
    if source_root.parent != RELEASE_ROOT or source_root.name != expected_leaf:
        fail("installed_source_root is not the exact absent versioned release destination")
    return manifest, manifest_sha256, hashlib.sha256(policy_raw).hexdigest()


def capsule_gid() -> int:
    try:
        group = grp.getgrnam(CAPSULE_GROUP)
    except KeyError as exc:
        raise InstallError("dedicated capsule group does not exist") from exc
    if group.gr_mem or any(account.pw_gid == group.gr_gid for account in pwd.getpwall()):
        fail("dedicated capsule group must have no members or primary-group users")
    return group.gr_gid


def ensure_directory(path: Path, mode: int, gid: int) -> None:
    try:
        details = os.lstat(path)
    except FileNotFoundError:
        os.mkdir(path, mode)
        os.chown(path, 0, gid, follow_symlinks=False)
        os.chmod(path, mode, follow_symlinks=False)
        details = os.lstat(path)
    if (
        not stat.S_ISDIR(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
        or details.st_uid != 0
        or details.st_gid != gid
        or stat.S_IMODE(details.st_mode) != mode
    ):
        fail(f"installation directory has a conflicting identity: {path}")


def copy_descriptor(
    descriptor: int,
    destination: Path,
    mode: int,
    gid: int,
    *,
    expected_sha256: str | None = None,
) -> str:
    if destination.exists() or destination.is_symlink():
        fail(f"refusing to replace existing destination: {destination}")
    output = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    hasher = hashlib.sha256()
    try:
        size = os.fstat(descriptor).st_size
        offset = 0
        while offset < size:
            chunk = os.pread(descriptor, min(1024 * 1024, size - offset), offset)
            if not chunk:
                fail("verified source descriptor ended during installation")
            hasher.update(chunk)
            written = 0
            while written < len(chunk):
                count = os.write(output, chunk[written:])
                if count <= 0:
                    fail("installed destination stopped accepting bytes")
                written += count
            offset += len(chunk)
        os.fchown(output, 0, gid)
        os.fchmod(output, mode)
        os.fsync(output)
        details = os.fstat(output)
        if details.st_uid != 0 or details.st_gid != gid or stat.S_IMODE(details.st_mode) != mode:
            fail("installed destination ownership or mode differs")
    finally:
        os.close(output)
    verify_fd = reject_symlink_argument(destination, f"installed {destination.name}")
    try:
        installed_sha256 = hashlib.sha256(read_stable(verify_fd, f"installed {destination.name}")).hexdigest()
    finally:
        os.close(verify_fd)
    if installed_sha256 != hasher.hexdigest():
        fail("installed destination differs from its retained source descriptor")
    if expected_sha256 is not None and installed_sha256 != expected_sha256:
        fail("retained source descriptor changed after signed verification")
    return installed_sha256


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_tree_directories(root: Path) -> None:
    directories = [Path(directory) for directory, _children, _files in os.walk(root)]
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        fsync_directory(directory)


def attempt_identity() -> str:
    return f"{time_ns()}-{os.getpid()}-{os.urandom(16).hex()}"


def time_ns() -> int:
    return int.from_bytes(os.urandom(8), "big")


def verify_release_tree(
    destination: Path,
    manifest: Mapping[str, Any],
    gid: int,
) -> str:
    protected_directory(destination, allowed_gids={gid})
    expected_digests = {
        record["relative_path"]: digest(record["sha256"], "release digest")
        for record in manifest["release_files"]
    }
    executable_paths = {
        record["relative_path"] for record in manifest["tools"].values()
    } | {
        record["relative_path"] for record in manifest["terraform"]["provider_files"]
    }
    observed: list[str] = []
    projection: list[dict[str, object]] = []
    for directory, directories, files in os.walk(destination, followlinks=False):
        directory_path = Path(directory)
        protected_directory(directory_path, allowed_gids={gid})
        for name in directories:
            child = directory_path / name
            if child.is_symlink():
                fail("installed release contains a directory symlink")
        for name in files:
            path = directory_path / name
            relative = path.relative_to(destination).as_posix()
            descriptor = reject_symlink_argument(path, f"installed release file {relative}")
            try:
                details = os.fstat(descriptor)
                expected_mode = 0o550 if relative in executable_paths else 0o440
                if details.st_uid != 0 or details.st_gid != gid or stat.S_IMODE(details.st_mode) != expected_mode:
                    fail("installed release ownership or mode differs from its manifest role")
                observed_sha256 = hashlib.sha256(
                    read_stable(descriptor, f"installed release file {relative}")
                ).hexdigest()
            finally:
                os.close(descriptor)
            if expected_digests.get(relative) != observed_sha256:
                fail("installed release digest differs from the signed inventory")
            observed.append(relative)
            projection.append(
                {"mode": expected_mode, "relative_path": relative, "sha256": observed_sha256}
            )
    if sorted(observed) != sorted(expected_digests):
        fail("installed release file set differs from the signed inventory")
    return hashlib.sha256(canonical_bytes(sorted(projection, key=lambda item: item["relative_path"]))).hexdigest()


def write_attempt_journal(path: Path, payload: Mapping[str, Any], gid: int) -> str:
    raw = canonical_bytes(dict(payload)) + b"\n"
    descriptor = sealed_memfd("capsule-completion-journal", raw)
    try:
        result = copy_descriptor(
            descriptor,
            path,
            0o440,
            gid,
            expected_sha256=hashlib.sha256(raw).hexdigest(),
        )
        fsync_directory(path.parent)
        return result
    finally:
        os.close(descriptor)


def install_release(
    manifest: Mapping[str, Any],
    bundle_files: Mapping[str, int],
    gid: int,
    *,
    manifest_sha256: str,
) -> Path:
    ensure_directory(Path("/opt/fs2"), 0o755, 0)
    ensure_directory(RELEASE_ROOT, 0o550, gid)
    ensure_directory(RELEASE_ATTEMPT_ROOT, 0o550, gid)
    destination = Path(manifest["installed_source_root"])
    if destination.is_symlink():
        fail("versioned release destination conflicts with a symlink")
    if destination.exists():
        verify_release_tree(destination, manifest, gid)
        return destination
    attempt = RELEASE_ATTEMPT_ROOT / attempt_identity()
    os.mkdir(attempt, 0o550)
    os.chown(attempt, 0, gid, follow_symlinks=False)
    fsync_directory(RELEASE_ATTEMPT_ROOT)
    payload = attempt / "payload"
    os.mkdir(payload, 0o550)
    os.chown(payload, 0, gid, follow_symlinks=False)
    executable_paths = {
        record["relative_path"] for record in manifest["tools"].values()
    } | {
        record["relative_path"] for record in manifest["terraform"]["provider_files"]
    }
    directories = sorted(
        {
            parent.as_posix()
            for relative in bundle_files
            for parent in PurePosixPath(relative).parents
            if parent.as_posix() != "."
        },
        key=lambda value: (value.count("/"), value),
    )
    for relative in directories:
        target = payload / relative
        os.mkdir(target, 0o550)
        os.chown(target, 0, gid, follow_symlinks=False)
    expected_digests = {
        record["relative_path"]: digest(record["sha256"], "release digest")
        for record in manifest["release_files"]
    }
    for relative in sorted(bundle_files):
        mode = 0o550 if relative in executable_paths else 0o440
        copy_descriptor(
            bundle_files[relative],
            payload / relative,
            mode,
            gid,
            expected_sha256=expected_digests[relative],
        )
    release_projection_sha256 = verify_release_tree(payload, manifest, gid)
    write_attempt_journal(
        attempt / "release-complete.json",
        {
            "accepted_commit": manifest["accepted_commit"],
            "accepted_tree": manifest["accepted_tree"],
            "destination": str(destination),
            "manifest_sha256": manifest_sha256,
            "release_projection_sha256": release_projection_sha256,
            "schema": "fs2-serve.nebius.ai/public-edge-release-completion/v1",
        },
        gid,
    )
    verify_release_tree(payload, manifest, gid)
    fsync_tree_directories(payload)
    fsync_directory(attempt)
    renameat2(payload, destination, RENAME_NOREPLACE)
    # Persist both sides of the cross-directory rename before the release can
    # be named by an activation unit.
    fsync_directory(attempt)
    fsync_directory(RELEASE_ROOT)
    verify_release_tree(destination, manifest, gid)
    destination_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(destination_fd)
    finally:
        os.close(destination_fd)
    return destination


def renameat2(old: Path, new: Path, flags: int) -> None:
    function = ctypes.CDLL(None, use_errno=True).renameat2
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(-100, os.fsencode(old), -100, os.fsencode(new), flags) != 0:
        error = ctypes.get_errno()
        raise InstallError(f"atomic activation rename failed: {os.strerror(error)}")


def recover_activation_transitions(gid: int) -> None:
    """Finish every journaled pointer transition without deleting evidence."""

    if not ACTIVATION_ATTEMPT_ROOT.exists():
        return
    for attempt in sorted(ACTIVATION_ATTEMPT_ROOT.iterdir()):
        if attempt.is_symlink() or not attempt.is_dir():
            fail("activation attempt registry contains an unsafe entry")
        prepared_path = attempt / "activation-exchange-prepared.json"
        preserved_path = attempt / "activation-pointer-preserved.json"
        if not prepared_path.exists() or preserved_path.exists():
            continue
        prepared_fd = reject_symlink_argument(prepared_path, "prepared activation journal")
        try:
            prepared_raw = read_stable(
                prepared_fd, "prepared activation journal", 64 * 1024
            )
        finally:
            os.close(prepared_fd)
        prepared = canonical_json(prepared_raw, "prepared activation journal")
        if not isinstance(prepared, dict) or set(prepared) != {
            "activation_identity", "attempt_id", "candidate", "desired_target",
            "previous_target", "schema",
        } or prepared["schema"] != "fs2-serve.nebius.ai/public-edge-activation-exchange-prepared/v1":
            fail("prepared activation journal is malformed")
        attempt_id = prepared["attempt_id"]
        if attempt.name != attempt_id or not isinstance(attempt_id, str):
            fail("prepared activation journal is not bound to its attempt")
        candidate = CURRENT_LINK.parent / f".fs2-public-edge-candidate-{attempt_id}"
        if str(candidate) != prepared["candidate"]:
            fail("prepared activation candidate path is not canonical")
        current_target = os.readlink(CURRENT_LINK) if CURRENT_LINK.is_symlink() else None
        candidate_target = os.readlink(candidate) if candidate.is_symlink() else None
        previous_target = prepared["previous_target"]
        desired_target = prepared["desired_target"]
        disposition: str
        preserved: Path | None = None
        if current_target == desired_target and candidate_target == previous_target:
            preserved = CURRENT_LINK.parent / f"fs2-public-edge-previous-{attempt_id}"
            renameat2(candidate, preserved, RENAME_NOREPLACE)
            disposition = "completed-prior-pointer-preservation"
        elif current_target == previous_target and candidate_target == desired_target:
            preserved = CURRENT_LINK.parent / f"fs2-public-edge-uncommitted-{attempt_id}"
            renameat2(candidate, preserved, RENAME_NOREPLACE)
            disposition = "preserved-uncommitted-candidate"
        elif previous_target is None and current_target is None and candidate_target == desired_target:
            renameat2(candidate, CURRENT_LINK, RENAME_NOREPLACE)
            disposition = "completed-initial-activation"
        elif current_target == previous_target and candidate_target is None:
            # This is safe even for filesystems that violated the intended
            # ordering after a power loss: nothing was exchanged, no prior
            # pointer is lost, and a new append-only attempt may retry.
            disposition = "candidate-absent-before-exchange"
        elif current_target == desired_target and candidate_target is None:
            disposition = "pointer-already-durable"
        else:
            fail("journaled activation transition has an ambiguous retained state")
        fsync_directory(CURRENT_LINK.parent)
        write_attempt_journal(
            preserved_path,
            {
                "activation_identity": prepared["activation_identity"],
                "current_target": (
                    os.readlink(CURRENT_LINK) if CURRENT_LINK.is_symlink() else None
                ),
                "disposition": disposition,
                "preserved_pointer": str(preserved) if preserved is not None else None,
                "schema": "fs2-serve.nebius.ai/public-edge-activation-pointer-preserved/v1",
            },
            gid,
        )


def verify_activation_unit(
    unit: Path,
    *,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    policy_sha256: str,
    gid: int,
) -> str:
    protected_directory(unit, allowed_gids={gid})
    expected = {
        "bootstrap.py": (0o440, manifest["bootstrap_sha256"]),
        "launcher": (0o2755, manifest["launcher_sha256"]),
        "manifest.json": (0o440, manifest_sha256),
        "python3": (0o550, manifest["tools"]["python3"]["sha256"]),
    }
    observed = sorted(path.name for path in unit.iterdir())
    if observed != sorted([*expected, "install-receipt.json"]):
        fail("activation unit file set is incomplete or contains extras")
    projection: dict[str, str] = {}
    for name, (mode, expected_sha256) in expected.items():
        descriptor = reject_symlink_argument(unit / name, f"activation {name}")
        try:
            details = os.fstat(descriptor)
            if details.st_uid != 0 or details.st_gid != gid or stat.S_IMODE(details.st_mode) != mode:
                fail("activation file ownership or mode differs")
            observed_sha256 = hashlib.sha256(
                read_stable(descriptor, f"activation {name}")
            ).hexdigest()
        finally:
            os.close(descriptor)
        if observed_sha256 != expected_sha256:
            fail("activation file digest differs from the accepted manifest")
        projection[name] = observed_sha256
    receipt_fd = reject_symlink_argument(unit / "install-receipt.json", "activation receipt")
    try:
        receipt_details = os.fstat(receipt_fd)
        if receipt_details.st_uid != 0 or receipt_details.st_gid != gid or stat.S_IMODE(receipt_details.st_mode) != 0o440:
            fail("activation receipt ownership or mode differs")
        receipt_raw = read_stable(receipt_fd, "activation receipt", 1024 * 1024)
    finally:
        os.close(receipt_fd)
    receipt = canonical_json(receipt_raw, "activation receipt")
    if (
        receipt.get("schema") != "fs2-serve.nebius.ai/public-edge-capsule-install/v2"
        or receipt.get("accepted_commit") != manifest["accepted_commit"]
        or receipt.get("accepted_tree") != manifest["accepted_tree"]
        or receipt.get("manifest_sha256") != manifest_sha256
        or receipt.get("policy_sha256") != policy_sha256
        or receipt.get("installed_source_root") != manifest["installed_source_root"]
        or receipt.get("file_sha256") != projection
    ):
        fail("activation receipt does not bind the completed unit")
    projection["install-receipt.json"] = hashlib.sha256(receipt_raw).hexdigest()
    return hashlib.sha256(canonical_bytes(projection)).hexdigest()


def install_activation(
    *,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    policy_sha256: str,
    launcher_fd: int,
    bootstrap_fd: int,
    python_fd: int,
    manifest_fd: int,
    gid: int,
) -> Path:
    ensure_directory(ACTIVATION_ROOT, 0o550, gid)
    ensure_directory(ACTIVATION_ATTEMPT_ROOT, 0o550, gid)
    recover_activation_transitions(gid)
    identity = f"{manifest['accepted_commit']}-{manifest['accepted_tree']}-{manifest_sha256[:16]}"
    unit = ACTIVATION_ROOT / identity
    if unit.is_symlink():
        fail("activation-unit destination conflicts with a symlink")
    activation_attempt = attempt_identity()
    attempt = ACTIVATION_ATTEMPT_ROOT / activation_attempt
    if attempt.exists() or attempt.is_symlink():
        fail("unique activation attempt unexpectedly conflicts")
    os.mkdir(attempt, 0o550)
    os.chown(attempt, 0, gid, follow_symlinks=False)
    fsync_directory(ACTIVATION_ATTEMPT_ROOT)
    payload = attempt / "unit"
    if unit.exists():
        verify_activation_unit(
            unit,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            policy_sha256=policy_sha256,
            gid=gid,
        )
    else:
        os.mkdir(payload, 0o550)
        os.chown(payload, 0, gid, follow_symlinks=False)
    installed = {} if unit.exists() else {
        "launcher_sha256": copy_descriptor(
            launcher_fd, payload / "launcher", 0o2755, gid,
            expected_sha256=manifest["launcher_sha256"],
        ),
        "bootstrap_sha256": copy_descriptor(
            bootstrap_fd, payload / "bootstrap.py", 0o440, gid,
            expected_sha256=manifest["bootstrap_sha256"],
        ),
        "python_sha256": copy_descriptor(
            python_fd, payload / "python3", 0o550, gid,
            expected_sha256=manifest["tools"]["python3"]["sha256"],
        ),
        "manifest_sha256": copy_descriptor(
            manifest_fd, payload / "manifest.json", 0o440, gid,
            expected_sha256=manifest_sha256,
        ),
    }
    if installed and (installed["launcher_sha256"] != manifest["launcher_sha256"] or installed["bootstrap_sha256"] != manifest["bootstrap_sha256"] or installed["manifest_sha256"] != manifest_sha256):
        fail("activation unit differs from the accepted identities")
    previous_target: str | None = None
    try:
        current_details = os.lstat(CURRENT_LINK)
    except FileNotFoundError:
        current_details = None
    if current_details is not None:
        if not stat.S_ISLNK(current_details.st_mode):
            fail("fixed activation path exists but is not a symlink")
        previous_target = os.readlink(CURRENT_LINK)
        previous_resolved = (CURRENT_LINK.parent / previous_target).resolve(strict=True)
        try:
            previous_resolved.relative_to(ACTIVATION_ROOT)
        except ValueError as exc:
            raise InstallError("current activation escapes the protected activation root") from exc
        protected_directory(previous_resolved, allowed_gids={gid})
    if not unit.exists():
        receipt = {
            "accepted_commit": manifest["accepted_commit"],
            "accepted_tree": manifest["accepted_tree"],
            "activation_identity": identity,
            "file_sha256": {
                "bootstrap.py": installed["bootstrap_sha256"],
                "launcher": installed["launcher_sha256"],
                "manifest.json": installed["manifest_sha256"],
                "python3": installed["python_sha256"],
            },
            "installed_source_root": manifest["installed_source_root"],
            "manifest_sha256": manifest_sha256,
            "policy_sha256": policy_sha256,
            "schema": "fs2-serve.nebius.ai/public-edge-capsule-install/v2",
        }
        receipt_fd = sealed_memfd("capsule-install-receipt", canonical_bytes(receipt) + b"\n")
        try:
            copy_descriptor(receipt_fd, payload / "install-receipt.json", 0o440, gid)
        finally:
            os.close(receipt_fd)
        projection_sha256 = verify_activation_unit(
            payload,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            policy_sha256=policy_sha256,
            gid=gid,
        )
        write_attempt_journal(
            attempt / "unit-complete.json",
            {
                "activation_identity": identity,
                "manifest_sha256": manifest_sha256,
                "schema": "fs2-serve.nebius.ai/public-edge-activation-completion/v1",
                "unit_projection_sha256": projection_sha256,
            },
            gid,
        )
        verify_activation_unit(
            payload,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            policy_sha256=policy_sha256,
            gid=gid,
        )
        fsync_tree_directories(payload)
        fsync_directory(attempt)
        renameat2(payload, unit, RENAME_NOREPLACE)
        fsync_directory(attempt)
        fsync_directory(ACTIVATION_ROOT)
    verify_activation_unit(
        unit,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        policy_sha256=policy_sha256,
        gid=gid,
    )
    if previous_target is not None and previous_resolved == unit:
        write_attempt_journal(
            attempt / "activation-complete.json",
            {
                "activation_identity": identity,
                "current_target": previous_target,
                "schema": "fs2-serve.nebius.ai/public-edge-activation-event/v1",
                "status": "already-active",
            },
            gid,
        )
        return unit
    candidate = CURRENT_LINK.parent / f".fs2-public-edge-candidate-{activation_attempt}"
    if candidate.exists() or candidate.is_symlink():
        fail("activation candidate path conflicts with preserved prior work")
    os.symlink(os.path.relpath(unit, CURRENT_LINK.parent), candidate)
    desired_target = os.readlink(candidate)
    # The candidate directory entry must be durable before a durable journal
    # can claim it exists. Recovery also handles the conservative absent case.
    fsync_directory(CURRENT_LINK.parent)
    write_attempt_journal(
        attempt / "activation-exchange-prepared.json",
        {
            "activation_identity": identity,
            "attempt_id": activation_attempt,
            "candidate": str(candidate),
            "desired_target": desired_target,
            "previous_target": previous_target,
            "schema": "fs2-serve.nebius.ai/public-edge-activation-exchange-prepared/v1",
        },
        gid,
    )
    fsync_directory(CURRENT_LINK.parent)
    if current_details is None:
        renameat2(candidate, CURRENT_LINK, RENAME_NOREPLACE)
        fsync_directory(CURRENT_LINK.parent)
    else:
        previous = CURRENT_LINK.parent / f"fs2-public-edge-previous-{activation_attempt}"
        if previous.exists() or previous.is_symlink():
            fail("previous-activation receipt path already exists")
        renameat2(candidate, CURRENT_LINK, RENAME_EXCHANGE)
        fsync_directory(CURRENT_LINK.parent)
        write_attempt_journal(
            attempt / "activation-exchanged.json",
            {
                "activation_identity": identity,
                "candidate_now_holds": os.readlink(candidate),
                "current_target": os.readlink(CURRENT_LINK),
                "schema": "fs2-serve.nebius.ai/public-edge-activation-exchanged/v1",
            },
            gid,
        )
        renameat2(candidate, previous, RENAME_NOREPLACE)
        fsync_directory(CURRENT_LINK.parent)
    write_attempt_journal(
        attempt / "activation-pointer-preserved.json",
        {
            "activation_identity": identity,
            "current_target": os.readlink(CURRENT_LINK),
            "disposition": "activated-and-preserved",
            "preserved_pointer": str(previous) if current_details is not None else None,
            "schema": "fs2-serve.nebius.ai/public-edge-activation-pointer-preserved/v1",
        },
        gid,
    )
    write_attempt_journal(
        attempt / "activation-complete.json",
        {
            "activation_identity": identity,
            "current_target": os.readlink(CURRENT_LINK),
            "previous_activation_target": previous_target,
            "schema": "fs2-serve.nebius.ai/public-edge-activation-event/v1",
            "status": "activated",
        },
        gid,
    )
    return unit


def main() -> int:
    if os.geteuid() != 0 or Path(__file__) != FIXED_INSTALLER_SOURCE:
        fail("installer source must run as root behind its fixed static gate")
    for parent in (
        Path("/usr"),
        Path("/usr/local"),
        Path("/usr/local/sbin"),
        Path("/usr/local/libexec"),
        Path("/opt"),
    ):
        protected_directory(parent)
    if any(
        name.startswith(("LD_", "DYLD_", "PYTHON"))
        for name in os.environ
    ):
        fail("installer refuses ambient loader or Python controls")
    os.umask(0o077)
    protected_directory(Path("/run"))
    lock_fd = os.open(
        INSTALL_LOCK,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    lock_details = os.fstat(lock_fd)
    if (
        not stat.S_ISREG(lock_details.st_mode)
        or lock_details.st_uid != 0
        or stat.S_IMODE(lock_details.st_mode) != 0o600
    ):
        fail("capsule installation lock is not root-owned mode 0600")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise InstallError("another capsule installation owns the fixed lock") from exc
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--launcher-binary", required=True, type=Path)
    parser.add_argument("--bootstrap", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    arguments = parser.parse_args()
    bundle_fd = reject_symlink_argument(arguments.bundle, "capsule bundle", directory=True)
    manifest_fd = reject_symlink_argument(arguments.manifest, "capsule manifest")
    launcher_fd = reject_symlink_argument(arguments.launcher_binary, "launcher binary")
    bootstrap_fd = reject_symlink_argument(arguments.bootstrap, "capsule bootstrap")
    python_fd = reject_symlink_argument(arguments.python, "capsule Python")
    trust_fd, trust_raw = open_root_authority(FIXED_TRUST_STORE, "capsule trust store")
    policy_fd, policy_raw = open_root_authority(FIXED_ACCEPTANCE_POLICY, "capsule acceptance policy")
    bundle_files = enumerate_bundle(bundle_fd)
    manifest, manifest_sha256, policy_sha256 = verify_inputs(
        manifest_fd=manifest_fd,
        launcher_fd=launcher_fd,
        bootstrap_fd=bootstrap_fd,
        python_fd=python_fd,
        bundle_files=bundle_files,
        trust_raw=trust_raw,
        policy_raw=policy_raw,
    )
    gid = capsule_gid()
    install_release(
        manifest,
        bundle_files,
        gid,
        manifest_sha256=manifest_sha256,
    )
    unit = install_activation(
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        policy_sha256=policy_sha256,
        launcher_fd=launcher_fd,
        bootstrap_fd=bootstrap_fd,
        python_fd=python_fd,
        manifest_fd=manifest_fd,
        gid=gid,
    )
    print(json.dumps({"status": "INSTALLED_AND_ACTIVATED", "activation_unit": str(unit), "manifest_sha256": manifest_sha256}, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InstallError as exc:
        print(f"capsule installation failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
