#!/usr/bin/env python3
"""Fail closed unless the public edge still has provider-owned HA placement.

This creation-time Terraform gate deliberately performs fresh read-only cloud
and Kubernetes observations during *apply*.  A saved plan therefore cannot
authorize edge Pods from its older data-source snapshot.
"""

from __future__ import annotations

import base64
import binascii
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
    "fs2-serve.nebius.ai/public-edge-membership-receipt/v1"
)
MEMBERSHIP_PAYLOAD_SCHEMA = (
    "fs2-serve.nebius.ai/public-edge-membership-evidence/v1"
)
MEMBERSHIP_SUBJECT_SCHEMA = (
    "fs2-serve.nebius.ai/public-edge-membership-terraform-subject/v1"
)
MEMBERSHIP_TRUST_SCHEMA = (
    "fs2-serve.nebius.ai/trusted-public-edge-membership-issuers/v1"
)
MEMBERSHIP_ISSUER_ROLE = "platform-security-public-edge-membership"
MEMBERSHIP_RECEIPT_FILENAME = "public-edge-node-group-membership-receipt.json"
MEMBERSHIP_EVIDENCE_FILENAME = "public-edge-provider-membership.json"
MEMBERSHIP_TRUST_STORE = (
    Path(__file__).resolve().parents[1]
    / "trusted-public-edge-membership-issuers.json"
)
MAX_RECEIPT_BYTES = 256 * 1024
MAX_MEMBERSHIP_VALIDITY = timedelta(hours=24)
MAX_CLOCK_SKEW = timedelta(minutes=5)
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
KEY_ID_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
EXTERNAL_QUERY: Mapping[str, Any] | None = None
PINNED_COMMAND_FDS: tuple[int, ...] = ()


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


def read_descriptor(descriptor: int, name: str, *, private: bool) -> bytes:
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
        chunk = os.read(descriptor, min(65536, MAX_RECEIPT_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_RECEIPT_BYTES:
            fail(f"{name} exceeds {MAX_RECEIPT_BYTES} bytes")
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


def open_regular_file(path: Path, *, private: bool) -> bytes:
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
        return read_descriptor(descriptor, path.name, private=private)
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


def openssl_binary() -> str:
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
    descriptor = os.memfd_create(name, os.MFD_CLOEXEC)
    os.write(descriptor, content)
    os.lseek(descriptor, 0, os.SEEK_SET)
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
        memfd("public-edge-membership-key", pem),
        memfd("public-edge-membership-message", message),
        memfd("public-edge-membership-signature", signature),
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
            pass_fds=tuple(descriptors),
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    if result is None or result.returncode != 0:
        fail("public-edge membership signature verification failed")


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
            "node_selector_sha256",
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
    digest(subject["node_selector_sha256"], "membership selector digest")
    return subject


def validate_membership_receipt(
    receipt: object,
    trust_store: object,
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
            "kubernetes_node_controller_username",
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
    raw_member_ids = list_value(
        authority["member_instance_ids"], "signed provider member IDs"
    )
    member_ids = [
        string_value(item, "signed provider member ID", r"computeinstance-[a-z0-9]+")
        for item in raw_member_ids
    ]
    if (
        member_ids != sorted(set(member_ids))
        or len(member_ids) != subject["expected_node_count"]
    ):
        fail("signed provider member IDs must be sorted, unique, and exact-count")
    controller_username = string_value(
        authority["kubernetes_node_controller_username"],
        "signed Kubernetes Node controller username",
        r"[A-Za-z0-9:@._/-]{3,253}",
    )
    toolchain = exact_object(
        payload["toolchain"], {"python3", "nebius", "kubectl"}, "signed toolchain"
    )
    tools = {
        name: checked_executable(toolchain[name], name)
        for name in ("python3", "nebius", "kubectl")
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
            "collected_at",
            "adapter_sha256",
        },
        "provider membership export",
    )
    if evidence_raw_sha256 != evidence_digest:
        fail("reopened provider membership export bytes do not match the signed digest")
    if evidence_object != {
        "schema": "fs2-serve.nebius.ai/provider-node-group-membership-export/v1",
        "provider": "nebius",
        "project_id": subject["project_id"],
        "cluster_id": subject["cluster_id"],
        "node_group_id": subject["node_group_id"],
        "node_group_resource_version": group_revision,
        "relation_api": authority["relation_api"],
        "member_instance_ids": member_ids,
        "collected_at": evidence_object["collected_at"],
        "adapter_sha256": evidence_object["adapter_sha256"],
    }:
        fail("provider membership export does not bind the signed exact relation")
    collected_at = timestamp(
        evidence_object["collected_at"], "provider membership collected_at"
    )
    if collected_at < issued - MAX_CLOCK_SKEW or collected_at > issued + MAX_CLOCK_SKEW:
        fail("provider membership export was not collected with the signed receipt")
    digest(evidence_object["adapter_sha256"], "provider membership adapter digest")
    return {
        "payload_sha256": payload_digest,
        "issuer_key_id": payload["issuer"]["key_id"],
        "node_group_resource_version": group_revision,
        "member_instance_ids": member_ids,
        "kubernetes_node_controller_username": controller_username,
        "tools": tools,
        "toolchain": toolchain,
        "evidence_sha256": evidence_digest,
    }


def load_membership_contract(
    run_root: Path, expected_subject: object, *, validation_time: datetime | None = None
) -> dict[str, object]:
    receipt_path = run_root / MEMBERSHIP_RECEIPT_FILENAME
    evidence_path = run_root / MEMBERSHIP_EVIDENCE_FILENAME
    receipt_raw = open_regular_file(receipt_path, private=True)
    evidence_raw = open_regular_file(evidence_path, private=True)
    trust_raw = open_regular_file(MEMBERSHIP_TRUST_STORE, private=False)
    result = validate_membership_receipt(
        decode_canonical_json(receipt_raw, MEMBERSHIP_RECEIPT_FILENAME),
        decode_canonical_json(trust_raw, MEMBERSHIP_TRUST_STORE.name),
        expected_subject,
        decode_canonical_json(evidence_raw, MEMBERSHIP_EVIDENCE_FILENAME),
        hashlib.sha256(evidence_raw).hexdigest(),
        validation_time=validation_time,
    )
    result["receipt_sha256"] = hashlib.sha256(receipt_raw).hexdigest()
    return result


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


def checked_local_inputs(run_root: str, kubeconfig: str) -> tuple[Path, str, int]:
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
    return resolved_root, f"/proc/self/fd/{descriptor}", descriptor


def run_json(command: Sequence[str], label: str) -> Mapping[str, Any]:
    try:
        canonical_home = pwd.getpwuid(os.getuid()).pw_dir
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
                "HOME": canonical_home,
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
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
    if status_value.get("state") != "RUNNING" or status_value.get("reconciling") is not False:
        fail("NodeGroup is not RUNNING and fully reconciled")
    for field in ("target_node_count", "node_count", "ready_node_count"):
        if integer_value(status_value.get(field), f"node-group.status.{field}") < expected_count:
            fail(f"NodeGroup {field} is below the public-edge requirement")
    if integer_value(status_value.get("outdated_node_count"), "node-group.status.outdated_node_count") != 0:
        fail("NodeGroup still contains outdated nodes")

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
    if before_ids != after_ids or len(after_ids) != expected_count:
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
        provider_id = string_value(
            object_value(node.get("spec"), "Node.spec").get("providerID"),
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
        if not (
            annotations.get("cluster.x-k8s.io/cluster-name") == cluster_id
            and annotations.get("cluster.x-k8s.io/owner-kind") == "MachineSet"
            and owner_matches_group
            and machine_matches_owner
        ):
            fail("a provider member Node lacks corroborating Cluster API ownership")
        if not matches_selector or labels.get("lifecycle.fs2.nebius/run") != run_id:
            fail("a provider-owned system Node lacks the exact run scheduler labels")

        specification = object_value(node.get("spec"), "Node.spec")
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
    expected_count: int,
    minimum_domains: int,
) -> tuple[str, int, int, str]:
    _before_revision, before = project_owned_nodes(
        node_list_before,
        cluster_id=cluster_id,
        group_id=group_id,
        run_id=run_id,
        selector=selector,
        provider_member_ids=provider_member_ids,
    )
    after_revision, after = project_owned_nodes(
        node_list_after,
        cluster_id=cluster_id,
        group_id=group_id,
        run_id=run_id,
        selector=selector,
        provider_member_ids=provider_member_ids,
    )
    if before != after:
        fail("system Node identities or eligibility facts changed during observation")
    if {str(node["provider_instance_id"]) for node in after} != provider_member_ids:
        fail("Kubernetes Node provider IDs do not equal the provider member set")
    eligible = [
        node
        for node in after
        if node["ready"] and not node["unschedulable"] and not node["hard_taints"]
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
    selector: Mapping[str, str],
) -> dict[str, object]:
    return {
        "schema": MEMBERSHIP_SUBJECT_SCHEMA,
        "project_id": project_id,
        "cluster_id": cluster_id,
        "node_group_id": group_id,
        "run_id": run_id,
        "expected_node_count": expected_count,
        "minimum_hostname_domains": minimum_domains,
        "node_selector_sha256": canonical_sha256(selector),
    }


def receipt_contract_main() -> int:
    query = json.load(sys.stdin, object_pairs_hook=no_duplicate_object)
    query = exact_object(
        query,
        {"run_root", "expected_subject_json"},
        "Terraform membership query",
    )
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
    result = load_membership_contract(run_root.resolve(strict=True), expected_subject)
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
                "member_instance_ids_json": json.dumps(
                    result["member_instance_ids"], separators=(",", ":")
                ),
                "kubernetes_node_controller_username": str(
                    result["kubernetes_node_controller_username"]
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
    profile = string_value(
        environment("FS2_EDGE_GATE_NEBIUS_PROFILE"), "nebius_profile", r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
    )
    expected_project_id = string_value(
        environment("FS2_EDGE_GATE_PROJECT_ID"), "project_id", r"project-[a-z0-9]+"
    )
    expected_count = int(environment("FS2_EDGE_GATE_EXPECTED_NODE_COUNT"))
    minimum_domains = int(environment("FS2_EDGE_GATE_MINIMUM_DOMAINS"))
    maximum_age = int(environment("FS2_EDGE_GATE_MAX_PLAN_AGE_SECONDS"))
    if expected_count < 3 or minimum_domains < 3 or maximum_age != 14400:
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

    root, kubeconfig, kubeconfig_descriptor = checked_local_inputs(
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
    membership = load_membership_contract(
        root,
        membership_subject(
            project_id=expected_project_id,
            cluster_id=cluster_id,
            group_id=group_id,
            run_id=run_id,
            expected_count=expected_count,
            minimum_domains=minimum_domains,
            selector=selector,
        ),
    )
    signed_member_ids = [
        string_value(item, "signed provider member ID", r"computeinstance-[a-z0-9]+")
        for item in list_value(
            membership["member_instance_ids"], "signed provider member IDs"
        )
    ]
    signed_toolchain = object_value(membership["toolchain"], "signed toolchain")
    pinned_tools = {
        name: pin_executable(signed_toolchain[name], name)
        for name in ("python3", "nebius", "kubectl")
    }
    if pinned_tools["python3"][1] != str(Path(sys.executable).resolve(strict=True)):
        fail("running Python interpreter differs from the signed toolchain")
    PINNED_COMMAND_FDS = (
        kubeconfig_descriptor,
        *(details[2] for details in pinned_tools.values()),
    )
    nebius = [
        f"/proc/self/fd/{pinned_tools['nebius'][2]}",
        "--profile",
        profile,
        "--no-browser",
        "--no-check-update",
        "--no-progress",
        "--format",
        "json",
    ]
    cluster_cli = [*nebius, "mk8s", "cluster"]
    node_group_cli = [*nebius, "mk8s", "node-group"]
    compute_instance_cli = [*nebius, "compute", "instance"]
    kubectl = [
        f"/proc/self/fd/{pinned_tools['kubectl'][2]}",
        "--kubeconfig",
        str(kubeconfig),
        "--context",
        context,
        "--request-timeout=15s",
    ]

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
    )
    node_revision, eligible_count, domain_count, nodes_sha = validate_nodes(
        nodes_before,
        nodes_after,
        cluster_id=cluster_id,
        group_id=group_id,
        run_id=run_id,
        selector=selector,
        provider_member_ids=provider_ids,
        expected_count=expected_count,
        minimum_domains=minimum_domains,
    )
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
            "provider_members_sha256": provider_members_sha,
            "provider_member_count": len(provider_ids),
            "node_group_list_pagination": group_list_after["pagination"],
            "compute_instance_list_pagination": instances_after["pagination"],
            "node_list_resource_version": node_revision,
            "eligible_nodes_sha256": nodes_sha,
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
        "provider_member_count": str(len(provider_ids)),
        "eligible_node_count": str(eligible_count),
        "hostname_domain_count": str(domain_count),
        "membership_payload_sha256": str(membership["payload_sha256"]),
        "membership_receipt_sha256": str(membership["receipt_sha256"]),
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
                "FS2_EDGE_GATE_NEBIUS_PROFILE",
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
                "FS2_EDGE_GATE_NODE_SELECTOR_JSON",
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
