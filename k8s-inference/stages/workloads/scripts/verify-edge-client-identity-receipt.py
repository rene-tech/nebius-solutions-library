#!/usr/bin/env python3
"""Authenticate and derive the public-edge client identity contract.

Terraform's external provider invokes this adapter only for public mode. The
receipt supplies signed observations, never caller-selected trust booleans. A
source-owned issuer registry is the sole trust root; it is intentionally empty
until Platform Security onboards an evidence-signing public key in a separately
reviewed source commit.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


RECEIPT_SCHEMA = "fs2-serve.nebius.ai/edge-client-identity-receipt/v1"
PAYLOAD_SCHEMA = "fs2-serve.nebius.ai/edge-client-identity-evidence/v1"
SUBJECT_SCHEMA = "fs2-serve.nebius.ai/edge-client-identity-terraform-subject/v1"
TRUST_SCHEMA = "fs2-serve.nebius.ai/trusted-edge-evidence-issuers/v1"
ALGORITHM = "ed25519"
ISSUER_ROLE = "platform-security-edge-evidence"
RECEIPT_FILENAME = "edge-client-identity-receipt.json"
EVIDENCE_DIRECTORY = "edge-client-identity-evidence"
EVIDENCE_FILES = {
    "provider_lb_export_sha256": "provider-load-balancer.json",
    "provider_listener_export_sha256": "provider-listeners.json",
    "provider_backend_export_sha256": "provider-backend.json",
    "security_group_export_sha256": "security-group.json",
    "routing_export_sha256": "routing.json",
    "xff_probe_sha256": "xff-probe.json",
    "direct_access_probe_sha256": "direct-access-probe.json",
}
MAX_FILE_BYTES = 128 * 1024
MAX_VALIDITY = timedelta(hours=24)
MAX_CLOCK_SKEW = timedelta(minutes=5)
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
KEY_ID_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
RESOURCE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{7,127}$")
DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
KUBERNETES_UID_RE = re.compile(
    r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$"
)
TRUST_STORE = Path(__file__).resolve().parents[1] / "contracts" / "trusted-edge-evidence-issuers.json"
CAPSULE_COMMAND_FDS: tuple[int, ...] = ()
CAPSULE_OPENSSL: str | None = None
CAPSULE_TOOL_BIN = ""


class ReceiptError(ValueError):
    """A receipt is absent, untrusted, malformed, stale, or names another edge."""


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ReceiptError(f"{label} must contain exactly {sorted(keys)}")
    return value


def _text(value: Any, label: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ReceiptError(f"{label} must be non-empty canonical text")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise ReceiptError(f"{label} has an invalid format")
    lowered = value.lower()
    if any(marker in lowered for marker in ("placeholder", "replace", "example", "dummy", "todo")):
        raise ReceiptError(f"{label} is a placeholder")
    return value


def _digest(value: Any, label: str) -> str:
    text = _text(value, label, SHA256_RE)
    if text == "0" * 64:
        raise ReceiptError(f"{label} cannot be the zero digest")
    return text


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReceiptError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode_json(raw: bytes, label: str) -> Any:
    try:
        text = raw.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_no_duplicate_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"{label} is not canonical UTF-8 JSON") from exc
    if _canonical(value) + b"\n" != raw:
        raise ReceiptError(f"{label} must use canonical JSON plus one newline")
    return value


def _read_open_descriptor(descriptor: int, name: str, *, private: bool) -> bytes:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise ReceiptError(f"{name} is not a regular file")
    mode = stat.S_IMODE(before.st_mode)
    if private and mode != 0o600:
        raise ReceiptError(f"{name} must have mode 0600")
    if not private and mode & 0o022:
        raise ReceiptError(f"{name} must not be group/world writable")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(65536, MAX_FILE_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_FILE_BYTES:
            raise ReceiptError(f"{name} exceeds {MAX_FILE_BYTES} bytes")
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
        raise ReceiptError(f"{name} changed while it was being read")
    return b"".join(chunks)


def _open_regular_file(path: Path, *, private: bool) -> bytes:
    if not path.is_absolute():
        raise ReceiptError("evidence file path must be absolute")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReceiptError(f"cannot open required evidence file {path.name}") from exc
    try:
        return _read_open_descriptor(descriptor, path.name, private=private)
    finally:
        os.close(descriptor)


def _reopen_evidence(receipt_path: Path) -> dict[str, str]:
    directory_path = receipt_path.parent / EVIDENCE_DIRECTORY
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        directory = os.open(directory_path, flags)
    except OSError as exc:
        raise ReceiptError(f"cannot open required evidence directory {EVIDENCE_DIRECTORY}") from exc
    try:
        before = os.fstat(directory)
        if not stat.S_ISDIR(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o700:
            raise ReceiptError(f"{EVIDENCE_DIRECTORY} must be a mode-0700 directory")
        digests: dict[str, str] = {}
        file_flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            file_flags |= os.O_NOFOLLOW
        for digest_name, filename in EVIDENCE_FILES.items():
            try:
                descriptor = os.open(filename, file_flags, dir_fd=directory)
            except OSError as exc:
                raise ReceiptError(f"cannot open required evidence file {filename}") from exc
            try:
                raw = _read_open_descriptor(descriptor, filename, private=True)
            finally:
                os.close(descriptor)
            digests[digest_name] = hashlib.sha256(raw).hexdigest()
        after = os.fstat(directory)
        stable = (
            before.st_dev,
            before.st_ino,
            before.st_mtime_ns,
            before.st_ctime_ns,
            stat.S_IMODE(before.st_mode),
        ) == (
            after.st_dev,
            after.st_ino,
            after.st_mtime_ns,
            after.st_ctime_ns,
            stat.S_IMODE(after.st_mode),
        )
        if not stable:
            raise ReceiptError(f"{EVIDENCE_DIRECTORY} changed while it was being read")
        return digests
    finally:
        os.close(directory)


def _timestamp(value: Any, label: str) -> datetime:
    text = _text(value, label)
    if not text.endswith("Z"):
        raise ReceiptError(f"{label} must use UTC with a Z suffix")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise ReceiptError(f"{label} is not RFC3339 UTC") from exc
    if parsed.tzinfo != timezone.utc or parsed.microsecond:
        raise ReceiptError(f"{label} must use whole UTC seconds")
    return parsed


def _b64url(value: Any, label: str, expected_size: int) -> bytes:
    text = _text(value, label)
    try:
        decoded = base64.b64decode(
            text + "=" * (-len(text) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, binascii.Error) as exc:
        raise ReceiptError(f"{label} is not canonical base64url") from exc
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if len(decoded) != expected_size or canonical != text:
        raise ReceiptError(f"{label} has the wrong size or encoding")
    return decoded


def _openssl_binary() -> str:
    if CAPSULE_OPENSSL is not None:
        return CAPSULE_OPENSSL
    for candidate in ("/usr/bin/openssl", "/bin/openssl"):
        try:
            details = os.stat(candidate, follow_symlinks=True)
        except OSError:
            continue
        if stat.S_ISREG(details.st_mode) and details.st_uid == 0 and not stat.S_IMODE(details.st_mode) & 0o022:
            return candidate
    raise ReceiptError("a root-owned non-writable OpenSSL binary is required")


def _memfd(name: str, content: bytes) -> int:
    if not hasattr(os, "memfd_create"):
        raise ReceiptError("anonymous in-memory file descriptors are unavailable")
    descriptor = os.memfd_create(name, os.MFD_CLOEXEC)
    os.write(descriptor, content)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return descriptor


def _sealed_memfd(name: str, content: bytes) -> int:
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
            raise ReceiptError("edge evidence memfd is not fully sealed")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _verify_ed25519(public_key: bytes, signature: bytes, message: bytes) -> None:
    # RFC 8410 SubjectPublicKeyInfo prefix for one raw 32-byte Ed25519 key.
    der = bytes.fromhex("302a300506032b6570032100") + public_key
    encoded = base64.b64encode(der).decode("ascii")
    pem = ("-----BEGIN PUBLIC KEY-----\n" + "\n".join(
        encoded[index : index + 64] for index in range(0, len(encoded), 64)
    ) + "\n-----END PUBLIC KEY-----\n").encode("ascii")
    descriptors = [
        _sealed_memfd("edge-evidence-key", pem),
        _sealed_memfd("edge-evidence-message", message),
        _sealed_memfd("edge-evidence-signature", signature),
    ]
    result: subprocess.CompletedProcess[bytes] | None = None
    try:
        command = [
            _openssl_binary(),
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
        ]
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
            close_fds=True,
            pass_fds=tuple(sorted({*descriptors, *CAPSULE_COMMAND_FDS})),
            env={
                "HOME": "/nonexistent",
                "PATH": CAPSULE_TOOL_BIN or "/usr/bin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
            },
        )
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    if result is None or result.returncode != 0:
        raise ReceiptError("edge evidence signature verification failed")


def _trusted_key(trust_store: Any, issuer: Mapping[str, Any]) -> bytes:
    store = _exact(trust_store, {"schema", "issuers"}, "issuer trust store")
    if store["schema"] != TRUST_SCHEMA or not isinstance(store["issuers"], list):
        raise ReceiptError("issuer trust store has an unsupported schema")
    expected = _exact(dict(issuer), {"id", "role", "key_id"}, "receipt issuer")
    issuer_id = _text(expected["id"], "receipt issuer ID")
    if expected["role"] != ISSUER_ROLE:
        raise ReceiptError("receipt issuer has the wrong authority role")
    key_id = _text(expected["key_id"], "receipt issuer key ID", KEY_ID_RE)
    matches = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(store["issuers"]):
        item = _exact(raw, {"id", "role", "key_id", "public_key"}, f"trusted issuer {index}")
        item_id = _text(item["id"], f"trusted issuer {index} ID")
        item_key_id = _text(item["key_id"], f"trusted issuer {index} key ID", KEY_ID_RE)
        if (item_id, item_key_id) in seen:
            raise ReceiptError("issuer trust store contains a duplicate authority")
        seen.add((item_id, item_key_id))
        key = _b64url(item["public_key"], f"trusted issuer {index} public key", 32)
        derived = "sha256:" + hashlib.sha256(key).hexdigest()
        if item_key_id != derived:
            raise ReceiptError("trusted issuer key ID does not bind its public key")
        if item["role"] != ISSUER_ROLE:
            raise ReceiptError("trusted issuer has an unsupported role")
        if item_id == issuer_id and item_key_id == key_id:
            matches.append(key)
    if len(matches) != 1:
        raise ReceiptError("receipt issuer is not a unique source-trusted authority")
    return matches[0]


def _resource_id(value: Any, label: str) -> str:
    return _text(value, label, RESOURCE_ID_RE)


def _validate_expected_subject(value: Any) -> dict[str, Any]:
    subject = _exact(
        value,
        {
            "schema", "provider", "project_id", "cluster_id", "allocation",
            "network", "security_group", "gateway", "service_ports",
        },
        "expected Terraform edge subject",
    )
    if subject["schema"] != SUBJECT_SCHEMA or subject["provider"] != "nebius":
        raise ReceiptError("expected edge subject has an unsupported schema/provider")
    _resource_id(subject["project_id"], "expected project ID")
    _resource_id(subject["cluster_id"], "expected cluster ID")
    allocation = _exact(subject["allocation"], {"id", "ipv4_address"}, "expected allocation")
    _resource_id(allocation["id"], "expected allocation ID")
    try:
        ipaddress.IPv4Address(allocation["ipv4_address"])
    except (ipaddress.AddressValueError, TypeError) as exc:
        raise ReceiptError("expected allocation IPv4 address is invalid") from exc
    network = _exact(subject["network"], {"id", "subnet_id"}, "expected network")
    _resource_id(network["id"], "expected network ID")
    _resource_id(network["subnet_id"], "expected subnet ID")
    security_group = _exact(
        subject["security_group"],
        {"id", "ingress_rule_id", "source_cidrs", "destination_ports"},
        "expected security group",
    )
    _resource_id(security_group["id"], "expected security-group ID")
    _resource_id(security_group["ingress_rule_id"], "expected ingress-rule ID")
    cidrs = security_group["source_cidrs"]
    if not isinstance(cidrs, list) or not cidrs or cidrs != sorted(set(cidrs)):
        raise ReceiptError("expected source CIDRs must be a non-empty sorted unique list")
    try:
        for cidr in cidrs:
            ipaddress.IPv4Network(cidr, strict=False)
    except (ipaddress.AddressValueError, TypeError) as exc:
        raise ReceiptError("expected source CIDRs contain an invalid IPv4 network") from exc
    ports = security_group["destination_ports"]
    if ports != sorted({80, 443, 10080, 10443, 31425, 32633}):
        raise ReceiptError("expected security-group ports differ from the reviewed edge")
    gateway = _exact(subject["gateway"], {"namespace", "name", "class_name", "listeners"}, "expected gateway")
    expected_gateway = {
        "namespace": "fs2-system",
        "name": "public",
        "class_name": "fs2-serve-public",
        "listeners": [
            {"name": "acme-http", "protocol": "HTTP", "port": 80},
            {"name": "public-https", "protocol": "HTTPS", "port": 443},
        ],
    }
    if gateway != expected_gateway:
        raise ReceiptError("expected Gateway/listener identities differ from the reviewed edge")
    expected_ports = {
        "http": {"listener_port": 80, "target_port": 10080, "node_port": 31425},
        "https": {"listener_port": 443, "target_port": 10443, "node_port": 32633},
    }
    if subject["service_ports"] != expected_ports:
        raise ReceiptError("expected service port mapping differs from the reviewed edge")
    return subject


def _validate_provider_topology(topology_value: Any, subject: Mapping[str, Any]) -> str:
    topology = _exact(
        topology_value,
        {"load_balancer", "listeners", "backend", "security_group", "routing"},
        "provider topology",
    )
    load_balancer = _exact(
        topology["load_balancer"],
        {"id", "allocation_id", "public_ipv4_address"},
        "provider load balancer",
    )
    load_balancer_id = _resource_id(load_balancer["id"], "provider load-balancer ID")
    if (
        load_balancer["allocation_id"] != subject["allocation"]["id"]
        or load_balancer["public_ipv4_address"] != subject["allocation"]["ipv4_address"]
    ):
        raise ReceiptError("provider load balancer does not bind the Terraform allocation")
    listeners = topology["listeners"]
    if not isinstance(listeners, list) or len(listeners) != 2:
        raise ReceiptError("provider topology must name exactly the HTTP and HTTPS listeners")
    normalized_listeners = []
    listener_ids: set[str] = set()
    for index, raw in enumerate(listeners):
        listener = _exact(raw, {"id", "gateway_listener_name", "protocol", "port"}, f"provider listener {index}")
        listener_id = _resource_id(listener["id"], f"provider listener {index} ID")
        if listener_id in listener_ids:
            raise ReceiptError("provider listener IDs must be unique")
        listener_ids.add(listener_id)
        normalized_listeners.append({
            "name": listener["gateway_listener_name"],
            "protocol": listener["protocol"],
            "port": listener["port"],
        })
    if sorted(normalized_listeners, key=lambda item: item["port"]) != subject["gateway"]["listeners"]:
        raise ReceiptError("provider listeners do not bind the exact Gateway listeners")
    backend = _exact(
        topology["backend"],
        {"id", "service_namespace", "service_name", "service_uid", "service_ports"},
        "provider backend",
    )
    _resource_id(backend["id"], "provider backend ID")
    if backend["service_namespace"] != "envoy-gateway-system":
        raise ReceiptError("provider backend is outside the Envoy Gateway namespace")
    _text(backend["service_name"], "provider backend Service name", DNS_LABEL_RE)
    _text(backend["service_uid"], "provider backend Service UID", KUBERNETES_UID_RE)
    if backend["service_ports"] != subject["service_ports"]:
        raise ReceiptError("provider backend does not bind the reviewed Service ports")
    security_group = _exact(topology["security_group"], {"id", "ingress_rule_id"}, "provider security group")
    if security_group != {
        "id": subject["security_group"]["id"],
        "ingress_rule_id": subject["security_group"]["ingress_rule_id"],
    }:
        raise ReceiptError("provider topology names another security group or ingress rule")
    routing = _exact(topology["routing"], {"network_id", "subnet_id", "route_table_ids"}, "provider routing")
    if routing["network_id"] != subject["network"]["id"] or routing["subnet_id"] != subject["network"]["subnet_id"]:
        raise ReceiptError("provider routing names another network or subnet")
    route_tables = routing["route_table_ids"]
    if not isinstance(route_tables, list) or not route_tables or route_tables != sorted(set(route_tables)):
        raise ReceiptError("provider routing must name sorted unique route-table identities")
    for index, route_table_id in enumerate(route_tables):
        _resource_id(route_table_id, f"provider route-table ID {index}")
    return load_balancer_id


def _derive_identity(observations_value: Any, subject: Mapping[str, Any], load_balancer_id: str) -> int:
    observations = _exact(observations_value, {"xff", "direct_access"}, "edge observations")
    xff = _exact(
        observations["xff"],
        {"header_action", "appends_downstream_remote_address", "untrusted_prefix_ignored", "proxy_chain"},
        "XFF observation",
    )
    if xff["header_action"] not in {"append", "overwrite"}:
        raise ReceiptError("XFF observation must prove append or overwrite behavior")
    if xff["appends_downstream_remote_address"] is not True or xff["untrusted_prefix_ignored"] is not True:
        raise ReceiptError("XFF observation does not prove an unforgeable client position")
    chain = xff["proxy_chain"]
    if not isinstance(chain, list) or not 1 <= len(chain) <= 8:
        raise ReceiptError("XFF proxy chain must contain one to eight authenticated hops")
    for index, raw in enumerate(chain, start=1):
        hop = _exact(raw, {"kind", "id", "position_from_envoy"}, f"XFF proxy hop {index}")
        if (
            hop["kind"] != "nebius-load-balancer"
            or hop["id"] != load_balancer_id
            or hop["position_from_envoy"] != index
        ):
            raise ReceiptError("XFF proxy chain does not derive from the exact provider load balancer")
    # The current provider topology contains one externally reachable proxy.
    # Refuse duplicated copies of its ID masquerading as additional hops.
    if len(chain) != 1:
        raise ReceiptError("the reviewed Nebius edge topology must derive exactly one trusted hop")
    direct = _exact(
        observations["direct_access"],
        {
            "verdict", "enforcement", "security_group_id", "ingress_rule_id",
            "public_entrypoint_ids", "worker_public_ipv4_addresses",
            "service_cluster_ip_publicly_routable", "node_ports_publicly_routable",
            "target_ports_publicly_routable",
        },
        "direct-access observation",
    )
    expected_direct = {
        "verdict": "excluded",
        "enforcement": "security-group-and-provider-routing",
        "security_group_id": subject["security_group"]["id"],
        "ingress_rule_id": subject["security_group"]["ingress_rule_id"],
        "public_entrypoint_ids": [load_balancer_id],
        "worker_public_ipv4_addresses": [],
        "service_cluster_ip_publicly_routable": False,
        "node_ports_publicly_routable": False,
        "target_ports_publicly_routable": False,
    }
    if direct != expected_direct:
        raise ReceiptError("signed SG/routing facts do not exclude direct Envoy access")
    return len(chain)


def verify_receipt(
    receipt: Any,
    trust_store: Any,
    expected_subject: Any,
    reopened_evidence: Mapping[str, str],
    *,
    validation_time: datetime | None = None,
) -> dict[str, str]:
    envelope = _exact(
        receipt,
        {"schema", "algorithm", "payload", "payload_sha256", "signature"},
        "edge evidence receipt",
    )
    if envelope["schema"] != RECEIPT_SCHEMA or envelope["algorithm"] != ALGORITHM:
        raise ReceiptError("edge evidence receipt has an unsupported signature contract")
    payload = _exact(
        envelope["payload"],
        {
            "schema", "issuer", "nonce", "issued_at", "expires_at", "subject",
            "provider_topology", "observations", "evidence",
        },
        "edge evidence payload",
    )
    if payload["schema"] != PAYLOAD_SCHEMA:
        raise ReceiptError("edge evidence payload has an unsupported schema")
    payload_digest = _digest(envelope["payload_sha256"], "edge evidence payload digest")
    if hashlib.sha256(_canonical(payload)).hexdigest() != payload_digest:
        raise ReceiptError("edge evidence payload digest does not match the reopened payload")
    _digest(payload["nonce"], "edge evidence nonce")
    issued = _timestamp(payload["issued_at"], "edge evidence issued_at")
    expires = _timestamp(payload["expires_at"], "edge evidence expires_at")
    if expires <= issued or expires - issued > MAX_VALIDITY:
        raise ReceiptError("edge evidence validity interval is outside the 24-hour bound")
    now = validation_time or datetime.now(timezone.utc).replace(microsecond=0)
    if now.tzinfo is None:
        raise ReceiptError("edge evidence validation time must be timezone-aware")
    now = now.astimezone(timezone.utc).replace(microsecond=0)
    if issued > now + MAX_CLOCK_SKEW or expires <= now:
        raise ReceiptError("edge evidence receipt is not fresh")
    public_key = _trusted_key(trust_store, payload["issuer"])
    signature = _b64url(envelope["signature"], "edge evidence signature", 64)
    signed = {
        "schema": envelope["schema"],
        "algorithm": envelope["algorithm"],
        "payload": payload,
        "payload_sha256": payload_digest,
    }
    _verify_ed25519(public_key, signature, _canonical(signed))
    subject = _validate_expected_subject(expected_subject)
    if payload["subject"] != subject:
        raise ReceiptError("signed edge subject does not match the exact Terraform edge")
    load_balancer_id = _validate_provider_topology(payload["provider_topology"], subject)
    trusted_hops = _derive_identity(payload["observations"], subject, load_balancer_id)
    evidence = _exact(
        payload["evidence"],
        {
            "provider_lb_export_sha256", "provider_listener_export_sha256",
            "provider_backend_export_sha256", "security_group_export_sha256",
            "routing_export_sha256", "xff_probe_sha256", "direct_access_probe_sha256",
        },
        "edge evidence references",
    )
    for name, digest in evidence.items():
        _digest(digest, f"edge evidence {name}")
    if dict(reopened_evidence) != evidence:
        raise ReceiptError("reopened provider/LB evidence bytes do not match the signed digests")
    issuer = payload["issuer"]
    return {
        "verified": "true",
        "trusted_hops": str(trusted_hops),
        "payload_sha256": payload_digest,
        "issuer_key_id": issuer["key_id"],
        "provider_load_balancer_id": load_balancer_id,
        "direct_access_excluded": "true",
    }


def main() -> int:
    try:
        _require_capsule_source()
        query = json.load(sys.stdin, object_pairs_hook=_no_duplicate_object)
        query = _exact(query, {"receipt_path", "expected_subject_json"}, "Terraform external query")
        receipt_path = Path(_text(query["receipt_path"], "receipt path"))
        if receipt_path.name != RECEIPT_FILENAME:
            raise ReceiptError(f"receipt filename must be {RECEIPT_FILENAME}")
        expected_subject = json.loads(
            _text(query["expected_subject_json"], "expected subject JSON"),
            object_pairs_hook=_no_duplicate_object,
        )
        receipt_raw = _open_regular_file(receipt_path, private=True)
        reopened_evidence = _reopen_evidence(receipt_path)
        trust_raw = _open_regular_file(TRUST_STORE, private=False)
        result = verify_receipt(
            _decode_json(receipt_raw, RECEIPT_FILENAME),
            _decode_json(trust_raw, TRUST_STORE.name),
            expected_subject,
            reopened_evidence,
        )
        result["receipt_sha256"] = hashlib.sha256(receipt_raw).hexdigest()
        sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
        return 0
    except (OSError, ReceiptError, TypeError, ValueError) as exc:
        sys.stderr.write(f"edge client identity receipt rejected: {exc}\n")
        return 1


def _require_capsule_source() -> None:
    """Bind production execution to the accepted no-member capsule."""

    global CAPSULE_COMMAND_FDS, CAPSULE_OPENSSL, CAPSULE_TOOL_BIN
    source_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if (
        os.environ.get("FS2_CAPSULE_LAUNCHER") != "fs2-public-edge-capsule-v1"
        or os.environ.get("FS2_CAPSULE_SOURCE_ID")
        != "edge-client-identity-verifier"
        or os.environ.get("FS2_CAPSULE_SOURCE_SHA256") != source_sha256
        or os.getegid() == os.getgid()
        or os.getegid() in os.getgroups()
    ):
        raise ReceiptError("edge identity verifier lacks the accepted capsule proof")
    try:
        paths = json.loads(os.environ["FS2_CAPSULE_TOOL_PATHS_JSON"])
        descriptors = tuple(
            int(value) for value in os.environ["FS2_CAPSULE_PASS_FDS"].split(",")
        )
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise ReceiptError("edge identity capsule descriptor contract is absent") from exc
    openssl = paths.get("openssl") if isinstance(paths, dict) else None
    tool_bin = paths.get("tool_bin") if isinstance(paths, dict) else None
    if (
        not isinstance(openssl, str)
        or re.fullmatch(r"/proc/self/fd/[0-9]+", openssl) is None
        or int(openssl.rsplit("/", 1)[1]) not in descriptors
        or any(descriptor < 3 for descriptor in descriptors)
        or not isinstance(tool_bin, str)
        or not tool_bin.startswith("/opt/fs2/")
    ):
        raise ReceiptError("edge identity OpenSSL is not capsule-pinned")
    for descriptor in descriptors:
        os.fstat(descriptor)
    CAPSULE_COMMAND_FDS = tuple(sorted(set(descriptors)))
    CAPSULE_OPENSSL = openssl
    CAPSULE_TOOL_BIN = tool_bin


if __name__ == "__main__":
    raise SystemExit(main())
