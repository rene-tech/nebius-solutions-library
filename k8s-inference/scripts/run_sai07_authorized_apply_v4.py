#!/usr/bin/env python3
"""Create and apply one SAI-07 plan inside an independently attested capsule.

This PID-1 bootstrap intentionally imports only the Python standard library.
It verifies an externally signed, short-lived runtime attestation before it
loads any SAI-07 helper.  Executables, the source zipapp, contracts, CA,
kubeconfig and tfvars are copied into sealed memfds.  Terraform plan and apply
consume one fixed, sealed plan descriptor created by this process.

The checked-in source cannot activate until a later reviewed commit replaces
the four ``None`` bootstrap pins with authoritative facts.  They are compiled
trust roots, not caller input and not content inside the attested OCI image.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


CAPSULE_CONTRACT_PATH = Path(
    "/opt/fs2-sai07/contracts/execution-capsule-contract-v4.json"
)
RUNTIME_ATTESTATION_PATH = Path(
    "/opt/fs2-sai07/attestations/execution-capsule-runtime-attestation-v4.json"
)
ATTESTATION_KEY_PATH = Path(
    "/opt/fs2-sai07/attestations/execution-capsule-attestation-authority.pub"
)
BOOTSTRAP_OPENSSL_PATH = Path("/opt/fs2-sai07/bootstrap/openssl")

# Activation requires an independently reviewed source commit with real pins.
BOOTSTRAP_AUTHORITY_KEY_ID: str | None = None
BOOTSTRAP_AUTHORITY_KEY_SHA256: str | None = None
BOOTSTRAP_AUTHORITY_PRINCIPAL_ID: str | None = None
BOOTSTRAP_OPENSSL_SHA256: str | None = None

CAPSULE_SCHEMA = "fs2-serve.nebius.ai/sai07-execution-capsule-contract/v4"
ATTESTATION_SCHEMA = (
    "fs2-serve.nebius.ai/sai07-execution-capsule-runtime-attestation/v4"
)
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_KUBECONFIG_BYTES = 4 * 1024 * 1024
MAX_PLAN_BYTES = 512 * 1024 * 1024
MAX_RUNTIME_FILE_BYTES = 512 * 1024 * 1024
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
OCI_DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
SEALS = (
    fcntl.F_SEAL_SEAL
    | fcntl.F_SEAL_SHRINK
    | fcntl.F_SEAL_GROW
    | fcntl.F_SEAL_WRITE
)
FDS = {
    "capsule_contract": 180,
    "trust_lock": 181,
    "platform_authority": 182,
    "source_lock": 183,
    "runtime_attestation": 184,
    "manifest_authority_key": 185,
    "provider_authority_key": 186,
    "backend_authority_key": 187,
    "epoch_admission": 188,
    "source_bundle": 190,
    "python": 191,
    "openssl": 192,
    "kubectl": 193,
    "terraform": 194,
    "terraform_cli_config": 195,
    "plan_variables": 196,
    "saved_plan": 197,
    "platform_kubeconfig": 198,
    "cluster_ca": 199,
}


class AuthorizedApplyV4Error(ValueError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def exact(value: object, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise AuthorizedApplyV4Error(
            f"{label} fields differ from the reviewed v4 contract"
        )
    return value


def read_regular(path: Path, label: str, maximum: int) -> bytes:
    if not path.is_absolute() or ".." in path.parts:
        raise AuthorizedApplyV4Error(
            f"{label} path must be absolute without traversal"
        )
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > maximum
        ):
            raise AuthorizedApplyV4Error(
                f"{label} is not a bounded nonempty regular file"
            )
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if remaining or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise AuthorizedApplyV4Error(
                f"{label} changed during descriptor-fenced read"
            )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def read_bounded_mountinfo() -> bytes:
    """Read procfs mountinfo without treating its zero st_size as an empty file."""

    path = Path("/proc/self/mountinfo")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise AuthorizedApplyV4Error("mountinfo is not a regular procfs file")
        chunks: list[bytes] = []
        observed = 0
        while True:
            chunk = os.read(descriptor, min(65536, MAX_JSON_BYTES + 1 - observed))
            if not chunk:
                break
            chunks.append(chunk)
            observed += len(chunk)
            if observed > MAX_JSON_BYTES:
                raise AuthorizedApplyV4Error("mountinfo exceeded its byte bound")
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise AuthorizedApplyV4Error("mountinfo identity changed during read")
        payload = b"".join(chunks)
        if not payload:
            raise AuthorizedApplyV4Error("mountinfo is empty")
        return payload
    finally:
        os.close(descriptor)


def load_canonical(path: Path, label: str) -> tuple[bytes, dict[str, Any]]:
    payload = read_regular(path, label, MAX_JSON_BYTES)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuthorizedApplyV4Error(f"{label} is not JSON") from error
    if not isinstance(value, dict) or payload != canonical(value) + b"\n":
        raise AuthorizedApplyV4Error(
            f"{label} must be canonical JSON with one terminal LF"
        )
    return payload, value


def seal_bytes(payload: bytes, target_fd: int, label: str, maximum: int) -> str:
    if not payload or len(payload) > maximum:
        raise AuthorizedApplyV4Error(f"{label} violates its byte bound")
    descriptor = os.memfd_create(label, os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise AuthorizedApplyV4Error(
                    f"{label} memfd write made no progress"
                )
            offset += written
        os.fsync(descriptor)
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, SEALS)
        os.dup2(descriptor, target_fd, inheritable=True)
    finally:
        os.close(descriptor)
    if fcntl.fcntl(target_fd, fcntl.F_GET_SEALS) & SEALS != SEALS:
        raise AuthorizedApplyV4Error(f"{label} descriptor is not write sealed")
    return sha256(payload)


def seal_path(
    path: Path, expected_sha256: str, target_fd: int, label: str, maximum: int
) -> str:
    if not isinstance(expected_sha256, str) or not SHA256_RE.fullmatch(
        expected_sha256
    ):
        raise AuthorizedApplyV4Error(f"{label} has no active digest pin")
    payload = read_regular(path, label, maximum)
    if sha256(payload) != expected_sha256:
        raise AuthorizedApplyV4Error(f"{label} differs from its signed pin")
    return seal_bytes(payload, target_fd, label, maximum)


def create_plan_fd() -> None:
    descriptor = os.memfd_create(
        "sai07-saved-plan", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
    )
    try:
        os.dup2(descriptor, FDS["saved_plan"], inheritable=True)
    finally:
        os.close(descriptor)


def seal_plan_fd() -> str:
    descriptor = FDS["saved_plan"]
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > MAX_PLAN_BYTES
    ):
        raise AuthorizedApplyV4Error(
            "Terraform did not create a bounded nonempty saved plan"
        )
    os.fsync(descriptor)
    fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, SEALS)
    if fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) & SEALS != SEALS:
        raise AuthorizedApplyV4Error("saved-plan descriptor is not write sealed")
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    remaining = metadata.st_size
    while remaining:
        chunk = os.read(descriptor, min(1024 * 1024, remaining))
        if not chunk:
            break
        digest.update(chunk)
        remaining -= len(chunk)
    if remaining:
        raise AuthorizedApplyV4Error("saved plan became short while hashing")
    return digest.hexdigest()


def verify_signature(
    unsigned: dict[str, Any], signature: dict[str, Any], public_key: bytes
) -> None:
    if not all(
        isinstance(value, str) and value
        for value in (
            BOOTSTRAP_AUTHORITY_KEY_ID,
            BOOTSTRAP_AUTHORITY_KEY_SHA256,
            BOOTSTRAP_AUTHORITY_PRINCIPAL_ID,
            BOOTSTRAP_OPENSSL_SHA256,
        )
    ):
        raise AuthorizedApplyV4Error(
            "independent capsule-attestation bootstrap is not activated"
        )
    if sha256(public_key) != BOOTSTRAP_AUTHORITY_KEY_SHA256:
        raise AuthorizedApplyV4Error(
            "attestation public key differs from compiled trust root"
        )
    exact(signature, {"algorithm", "key_id", "value"}, "attestation signature")
    if (
        signature["algorithm"] != "ed25519"
        or signature["key_id"] != BOOTSTRAP_AUTHORITY_KEY_ID
    ):
        raise AuthorizedApplyV4Error("attestation signature identity differs")
    try:
        signature_bytes = base64.b64decode(signature["value"], validate=True)
    except (TypeError, ValueError) as error:
        raise AuthorizedApplyV4Error(
            "attestation signature is not strict base64"
        ) from error
    payload_fd = os.memfd_create("attestation-payload", os.MFD_CLOEXEC)
    key_fd = os.memfd_create("attestation-public-key", os.MFD_CLOEXEC)
    signature_fd = os.memfd_create("attestation-signature", os.MFD_CLOEXEC)
    try:
        os.write(payload_fd, canonical(unsigned))
        os.write(key_fd, public_key)
        os.write(signature_fd, signature_bytes)
        for descriptor in (payload_fd, key_fd, signature_fd):
            os.lseek(descriptor, 0, os.SEEK_SET)
        completed = subprocess.run(
            [
                f"/proc/self/fd/{FDS['openssl']}",
                "pkeyutl",
                "-verify",
                "-pubin",
                "-inkey",
                f"/proc/self/fd/{key_fd}",
                "-rawin",
                "-in",
                f"/proc/self/fd/{payload_fd}",
                "-sigfile",
                f"/proc/self/fd/{signature_fd}",
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/nonexistent"},
            pass_fds=(FDS["openssl"], payload_fd, key_fd, signature_fd),
        )
    finally:
        os.close(payload_fd)
        os.close(key_fd)
        os.close(signature_fd)
    if completed.returncode != 0:
        raise AuthorizedApplyV4Error("runtime attestation signature is invalid")


def verify_attestation(
    expected_role: str,
) -> tuple[bytes, dict[str, Any], bytes, dict[str, Any]]:
    seal_path(
        BOOTSTRAP_OPENSSL_PATH,
        str(BOOTSTRAP_OPENSSL_SHA256),
        FDS["openssl"],
        "bootstrap OpenSSL",
        MAX_RUNTIME_FILE_BYTES,
    )
    public_key = read_regular(ATTESTATION_KEY_PATH, "attestation public key", 65536)
    attestation_bytes, attestation = load_canonical(
        RUNTIME_ATTESTATION_PATH, "runtime attestation"
    )
    exact(attestation, {"claims", "signature"}, "runtime attestation")
    claims = exact(
        attestation["claims"],
        {
            "admission_objects",
            "api",
            "capsule_contract_sha256",
            "expires_at",
            "handoff",
            "image",
            "issued_at",
            "nonce",
            "pod",
            "role",
            "schema",
            "signing_principal_id",
        },
        "runtime attestation claims",
    )
    verify_signature(claims, attestation["signature"], public_key)
    if (
        claims["schema"] != ATTESTATION_SCHEMA
        or claims["role"] != expected_role
        or claims["signing_principal_id"] != BOOTSTRAP_AUTHORITY_PRINCIPAL_ID
        or not isinstance(claims["nonce"], str)
        or not claims["nonce"]
    ):
        raise AuthorizedApplyV4Error("runtime attestation authority differs")
    pod_claim = exact(
        claims["pod"],
        {
            "container_name",
            "image_digest",
            "image_id",
            "name",
            "namespace",
            "service_account_name",
            "uid",
        },
        "attested Pod",
    )
    image = exact(
        claims["image"],
        {"digest", "provenance_sha256", "reference", "sbom_sha256"},
        "attested image",
    )
    if (
        image["digest"] != pod_claim["image_digest"]
        or not OCI_DIGEST_RE.fullmatch(str(image["digest"]))
        or os.environ.get("FS2_SAI07_POD_NAME") != pod_claim["name"]
        or os.environ.get("FS2_SAI07_POD_NAMESPACE") != pod_claim["namespace"]
        or os.environ.get("FS2_SAI07_POD_UID") != pod_claim["uid"]
    ):
        raise AuthorizedApplyV4Error("downward/image identity differs from attestation")
    try:
        issued = dt.datetime.fromisoformat(
            str(claims["issued_at"]).replace("Z", "+00:00")
        )
        expires = dt.datetime.fromisoformat(
            str(claims["expires_at"]).replace("Z", "+00:00")
        )
    except ValueError as error:
        raise AuthorizedApplyV4Error("runtime attestation time is malformed") from error
    now = dt.datetime.now(dt.UTC)
    if (
        issued.tzinfo != dt.UTC
        or expires.tzinfo != dt.UTC
        or issued > now + dt.timedelta(seconds=30)
        or now > expires
        or expires <= issued
        or expires - issued > dt.timedelta(minutes=10)
    ):
        raise AuthorizedApplyV4Error("runtime attestation is stale or overlong")
    capsule_bytes, capsule = load_canonical(
        CAPSULE_CONTRACT_PATH, "execution capsule contract"
    )
    if (
        capsule.get("schema") != CAPSULE_SCHEMA
        or capsule.get("activation") != "active"
        or sha256(capsule_bytes) != claims["capsule_contract_sha256"]
    ):
        raise AuthorizedApplyV4Error(
            "signed attestation does not select this active capsule"
        )
    seal_bytes(
        capsule_bytes,
        FDS["capsule_contract"],
        "capsule-contract",
        MAX_JSON_BYTES,
    )
    seal_bytes(
        attestation_bytes,
        FDS["runtime_attestation"],
        "runtime-attestation",
        MAX_JSON_BYTES,
    )
    return capsule_bytes, capsule, attestation_bytes, claims


def tree_sha256(root: Path, label: str) -> str:
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise AuthorizedApplyV4Error(f"{label} must be an absolute real directory")
    records: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise AuthorizedApplyV4Error(f"{label} may not contain symlinks")
        if path.is_dir():
            records.append({"path": relative, "type": "directory"})
        elif path.is_file():
            records.append(
                {
                    "path": relative,
                    "sha256": sha256(
                        read_regular(path, f"{label} file", MAX_RUNTIME_FILE_BYTES)
                    ),
                    "type": "file",
                }
            )
        else:
            raise AuthorizedApplyV4Error(f"{label} contains a non-file entry")
    if not records:
        raise AuthorizedApplyV4Error(f"{label} is empty")
    return sha256(canonical(records))


def verify_read_only_mounts(paths: list[Path]) -> None:
    mountinfo = read_bounded_mountinfo().decode()
    mounts: list[tuple[Path, set[str]]] = []
    for line in mountinfo.splitlines():
        fields = line.split()
        if "-" not in fields:
            continue
        mounts.append((Path(fields[4]), set(fields[5].split(","))))
    for path in paths:
        candidates = [
            (mount, options)
            for mount, options in mounts
            if path == mount or mount in path.parents
        ]
        if not candidates:
            raise AuthorizedApplyV4Error(f"no mount covers immutable path {path}")
        _mount, options = max(candidates, key=lambda item: len(item[0].parts))
        if "ro" not in options:
            raise AuthorizedApplyV4Error(f"capsule path is writable: {path}")


def validate_embedded_kubeconfig(
    payload: bytes, claims: dict[str, Any]
) -> dict[str, Any]:
    """Reject every external kubeconfig reference before kubectl can execute."""

    try:
        config = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuthorizedApplyV4Error(
            "platform kubeconfig must be canonical JSON, not ambient YAML"
        ) from error
    if not isinstance(config, dict) or payload != canonical(config) + b"\n":
        raise AuthorizedApplyV4Error(
            "platform kubeconfig must be one canonical self-contained JSON object"
        )
    allowed_top_level = {
        "apiVersion",
        "clusters",
        "contexts",
        "current-context",
        "kind",
        "preferences",
        "users",
    }
    clusters = config.get("clusters")
    contexts = config.get("contexts")
    users = config.get("users")
    cluster_entry = (
        clusters[0]
        if isinstance(clusters, list)
        and len(clusters) == 1
        and isinstance(clusters[0], dict)
        else None
    )
    context_entry = (
        contexts[0]
        if isinstance(contexts, list)
        and len(contexts) == 1
        and isinstance(contexts[0], dict)
        else None
    )
    user_entry = (
        users[0]
        if isinstance(users, list)
        and len(users) == 1
        and isinstance(users[0], dict)
        else None
    )
    cluster = cluster_entry.get("cluster") if isinstance(cluster_entry, dict) else None
    context = context_entry.get("context") if isinstance(context_entry, dict) else None
    user = user_entry.get("user") if isinstance(user_entry, dict) else None
    api = exact(claims["api"], {"ca_sha256", "context", "origin"}, "attested API")
    if (
        set(config) - allowed_top_level
        or config.get("apiVersion") != "v1"
        or config.get("kind") != "Config"
        or config.get("preferences", {}) != {}
        or config.get("current-context") != api["context"]
        or not isinstance(cluster_entry, dict)
        or set(cluster_entry) != {"cluster", "name"}
        or not isinstance(cluster_entry.get("name"), str)
        or not isinstance(cluster, dict)
        or set(cluster) != {"certificate-authority-data", "server"}
        or cluster.get("server") != api["origin"]
        or not str(api["origin"]).startswith("https://")
        or not isinstance(context_entry, dict)
        or set(context_entry) != {"context", "name"}
        or context_entry.get("name") != api["context"]
        or not isinstance(context, dict)
        or set(context) != {"cluster", "user"}
        or context.get("cluster") != cluster_entry.get("name")
        or not isinstance(user_entry, dict)
        or set(user_entry) != {"name", "user"}
        or context.get("user") != user_entry.get("name")
        or not isinstance(user, dict)
        or set(user) != {"token"}
        or not isinstance(user.get("token"), str)
        or not user["token"]
    ):
        raise AuthorizedApplyV4Error(
            "platform kubeconfig is not one self-contained token/CA context"
        )
    try:
        ca_bytes = base64.b64decode(
            cluster["certificate-authority-data"], validate=True
        )
    except (TypeError, ValueError) as error:
        raise AuthorizedApplyV4Error(
            "platform kubeconfig embedded CA is not strict base64"
        ) from error
    if not ca_bytes or sha256(ca_bytes) != api["ca_sha256"]:
        raise AuthorizedApplyV4Error(
            "platform kubeconfig embedded CA differs from the signed API"
        )
    return config


def run_json(
    command: list[str],
    pass_fds: tuple[int, ...],
    environment: dict[str, str],
    label: str,
    *,
    input_bytes: bytes | None = None,
) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        check=False,
        input=input_bytes,
        capture_output=True,
        env=environment,
        pass_fds=pass_fds,
        timeout=900,
    )
    if completed.returncode != 0 or len(completed.stdout) > MAX_JSON_BYTES:
        raise AuthorizedApplyV4Error(
            f"{label} rejected, failed, or exceeded the output bound"
        )
    try:
        value = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuthorizedApplyV4Error(f"{label} output is not JSON") from error
    if not isinstance(value, dict):
        raise AuthorizedApplyV4Error(f"{label} output is not an object")
    return value


def runtime_files(
    capsule: dict[str, Any], stage: str
) -> tuple[dict[str, Any], str, str, dict[str, str]]:
    runtime = exact(
        capsule.get("runtime"),
        {
            "cluster_ca_fd",
            "authority_keys",
            "contract_files",
            "filesystem",
            "handoff_root",
            "plan_variables_fd",
            "platform_kubeconfig_fd",
            "provider_bundle",
            "runtime_files",
            "saved_plan_fd",
            "terraform_configuration_sha256",
            "terraform_data_roots",
            "terraform_data_tree_sha256",
            "terraform_root_tree_sha256",
            "terraform_roots",
            "terraform_version",
        },
        "capsule runtime",
    )
    if (
        runtime["filesystem"] != "observed-read-only-mounts"
        or runtime["cluster_ca_fd"] != FDS["cluster_ca"]
        or runtime["plan_variables_fd"] != FDS["plan_variables"]
        or runtime["saved_plan_fd"] != FDS["saved_plan"]
        or runtime["platform_kubeconfig_fd"] != FDS["platform_kubeconfig"]
    ):
        raise AuthorizedApplyV4Error("capsule fixed descriptor contract differs")
    authority_keys = exact(
        runtime["authority_keys"],
        {"backend", "manifest", "provider"},
        "capsule authority keys",
    )
    for role, descriptor_name in (
        ("manifest", "manifest_authority_key"),
        ("provider", "provider_authority_key"),
        ("backend", "backend_authority_key"),
    ):
        item = exact(
            authority_keys[role], {"fd", "path", "sha256"}, f"{role} authority key"
        )
        if item["fd"] != FDS[descriptor_name]:
            raise AuthorizedApplyV4Error(f"{role} authority descriptor differs")
        seal_path(
            Path(item["path"]),
            item["sha256"],
            item["fd"],
            f"{role} authority key",
            65536,
        )
    files = exact(
        runtime["runtime_files"],
        {"kubectl", "openssl", "python", "source_bundle", "terraform", "terraform_cli_config"},
        "capsule runtime files",
    )
    for name, maximum in (
        ("python", MAX_RUNTIME_FILE_BYTES),
        ("openssl", MAX_RUNTIME_FILE_BYTES),
        ("kubectl", MAX_RUNTIME_FILE_BYTES),
        ("terraform", MAX_RUNTIME_FILE_BYTES),
        ("terraform_cli_config", MAX_JSON_BYTES),
        ("source_bundle", MAX_RUNTIME_FILE_BYTES),
    ):
        item = exact(files[name], {"fd", "path", "sha256"}, f"capsule {name}")
        if item["fd"] != FDS[name]:
            raise AuthorizedApplyV4Error(f"capsule {name} descriptor differs")
        if name == "openssl":
            if item["sha256"] != BOOTSTRAP_OPENSSL_SHA256:
                raise AuthorizedApplyV4Error(
                    "runtime OpenSSL differs from bootstrap verifier"
                )
        else:
            seal_path(
                Path(item["path"]),
                item["sha256"],
                item["fd"],
                f"capsule {name}",
                maximum,
            )
    contracts = exact(
        runtime["contract_files"],
        {"epoch_admission", "platform_authority", "source_lock", "trust_lock"},
        "capsule contract files",
    )
    for name in ("trust_lock", "platform_authority", "source_lock", "epoch_admission"):
        item = exact(contracts[name], {"fd", "path", "sha256"}, f"capsule {name}")
        if item["fd"] != FDS[name]:
            raise AuthorizedApplyV4Error(f"capsule {name} descriptor differs")
        seal_path(
            Path(item["path"]), item["sha256"], item["fd"], f"capsule {name}", MAX_JSON_BYTES
        )
    provider = exact(
        runtime["provider_bundle"], {"path", "sha256"}, "provider bundle"
    )
    roots = exact(
        runtime["terraform_roots"], {"foundation", "workloads"}, "Terraform roots"
    )
    data_roots = exact(
        runtime["terraform_data_roots"],
        {"foundation", "workloads"},
        "Terraform data roots",
    )
    configuration_hashes = exact(
        runtime["terraform_configuration_sha256"],
        {"foundation", "workloads"},
        "Terraform configuration hashes",
    )
    root_tree_hashes = exact(
        runtime["terraform_root_tree_sha256"],
        {"foundation", "workloads"},
        "Terraform root tree hashes",
    )
    data_tree_hashes = exact(
        runtime["terraform_data_tree_sha256"],
        {"foundation", "workloads"},
        "Terraform data tree hashes",
    )
    root = roots[stage]
    data_root = data_roots[stage]
    if (
        not isinstance(root, str)
        or not Path(root).is_absolute()
        or not isinstance(data_root, str)
        or not Path(data_root).is_absolute()
        or Path(root) == Path(data_root)
        or (Path(root) / ".terraform").exists()
    ):
        raise AuthorizedApplyV4Error(
            "Terraform configuration/data roots are not distinct fixed roots"
        )
    verify_read_only_mounts(
        [
            Path(files["source_bundle"]["path"]),
            Path(provider["path"]),
            Path(root),
            Path(data_root),
            CAPSULE_CONTRACT_PATH.parent,
            RUNTIME_ATTESTATION_PATH.parent,
        ]
    )
    observed = {
        "provider": tree_sha256(Path(provider["path"]), "provider bundle"),
        "root": tree_sha256(Path(root), f"{stage} Terraform root"),
        "data_root": tree_sha256(
            Path(data_root), f"{stage} Terraform data root"
        ),
    }
    if observed["provider"] != provider["sha256"]:
        raise AuthorizedApplyV4Error("provider bundle differs from attested contract")
    if observed["root"] != root_tree_hashes[stage]:
        raise AuthorizedApplyV4Error("Terraform root differs from attested contract")
    if observed["data_root"] != data_tree_hashes[stage]:
        raise AuthorizedApplyV4Error(
            "Terraform data root differs from attested contract"
        )
    if not isinstance(configuration_hashes[stage], str) or not SHA256_RE.fullmatch(
        configuration_hashes[stage]
    ):
        raise AuthorizedApplyV4Error(
            "Terraform configuration digest is not activated"
        )
    return runtime, root, data_root, observed


def handoff_paths(
    claims: dict[str, Any], plan_path: Path, acknowledgement_path: Path
) -> Path:
    handoff = exact(
        claims["handoff"],
        {"directory_identity_sha256", "root"},
        "attested handoff storage",
    )
    root = Path(handoff["root"])
    if (
        not root.is_absolute()
        or ".." in root.parts
        or root.is_symlink()
        or not root.is_dir()
    ):
        raise AuthorizedApplyV4Error("handoff root is not an exact real directory")
    metadata = root.stat()
    identity = {
        "device": str(metadata.st_dev),
        "gid": str(metadata.st_gid),
        "inode": str(metadata.st_ino),
        "mode": format(stat.S_IMODE(metadata.st_mode), "04o"),
        "path": str(root),
        "uid": str(metadata.st_uid),
    }
    if sha256(canonical(identity)) != handoff["directory_identity_sha256"]:
        raise AuthorizedApplyV4Error("handoff directory differs from signed identity")
    nonce = claims["nonce"]
    generation = sha256(nonce.encode())[:32]
    if (
        plan_path != root / f"sai07-{generation}.tfplan"
        or acknowledgement_path != root / f"sai07-{generation}.ack.json"
    ):
        raise AuthorizedApplyV4Error(
            "plan and acknowledgement paths are not attestation-generation addressed"
        )
    return root


def publish_plan(path: Path, plan_sha256: str) -> None:
    source = FDS["saved_plan"]
    os.lseek(source, 0, os.SEEK_SET)
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | os.O_NOFOLLOW,
            0o440,
        )
    except FileExistsError:
        if sha256(read_regular(path, "retained plan handoff", MAX_PLAN_BYTES)) != plan_sha256:
            raise AuthorizedApplyV4Error(
                "retained generation plan handoff differs from the sealed plan"
            )
        return
    try:
        while True:
            chunk = os.read(source, 1024 * 1024)
            if not chunk:
                break
            offset = 0
            while offset < len(chunk):
                written = os.write(descriptor, chunk[offset:])
                if written <= 0:
                    raise AuthorizedApplyV4Error("plan handoff write made no progress")
                offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    if sha256(read_regular(path, "published plan handoff", MAX_PLAN_BYTES)) != plan_sha256:
        raise AuthorizedApplyV4Error("published plan handoff is incomplete")


def await_acknowledgement(path: Path, maximum_seconds: int = 600) -> None:
    deadline = time.monotonic() + maximum_seconds
    while True:
        try:
            read_regular(path, "external acknowledgement", MAX_JSON_BYTES)
            return
        except FileNotFoundError:
            if time.monotonic() >= deadline:
                raise AuthorizedApplyV4Error(
                    "external acknowledgement did not arrive within its bounded window"
                )
            time.sleep(1)


def verify_live_identity(
    claims: dict[str, Any],
    embedded_kubeconfig: dict[str, Any],
    base_environment: dict[str, str],
) -> bytes:
    api = exact(claims["api"], {"ca_sha256", "context", "origin"}, "attested API")
    pod_claim = exact(
        claims["pod"],
        {
            "container_name",
            "image_digest",
            "image_id",
            "name",
            "namespace",
            "service_account_name",
            "uid",
        },
        "attested Pod",
    )
    image = exact(
        claims["image"],
        {"digest", "provenance_sha256", "reference", "sbom_sha256"},
        "attested image",
    )
    if (
        image["digest"] != pod_claim["image_digest"]
        or not OCI_DIGEST_RE.fullmatch(str(image["digest"]))
        or os.environ.get("FS2_SAI07_POD_NAME") != pod_claim["name"]
        or os.environ.get("FS2_SAI07_POD_NAMESPACE") != pod_claim["namespace"]
        or os.environ.get("FS2_SAI07_POD_UID") != pod_claim["uid"]
    ):
        raise AuthorizedApplyV4Error("downward/live Pod identity differs")
    kubectl = f"/proc/self/fd/{FDS['kubectl']}"
    kubeconfig = f"/proc/self/fd/{FDS['platform_kubeconfig']}"
    # The bootstrap parsed these exact sealed bytes before kubectl could run.
    # Never ask kubectl to render --raw: that would unnecessarily materialize
    # the embedded bearer token in a child process and its captured stdout.
    config = embedded_kubeconfig
    clusters = config.get("clusters")
    cluster_entry = (
        clusters[0]
        if isinstance(clusters, list)
        and len(clusters) == 1
        and isinstance(clusters[0], dict)
        else None
    )
    cluster = cluster_entry.get("cluster") if isinstance(cluster_entry, dict) else None
    contexts = config.get("contexts")
    users = config.get("users")
    context_entry = (
        contexts[0]
        if isinstance(contexts, list)
        and len(contexts) == 1
        and isinstance(contexts[0], dict)
        else None
    )
    context = context_entry.get("context") if isinstance(context_entry, dict) else None
    user_entry = (
        users[0]
        if isinstance(users, list)
        and len(users) == 1
        and isinstance(users[0], dict)
        else None
    )
    user = user_entry.get("user") if isinstance(user_entry, dict) else None
    if (
        set(config) - {
            "apiVersion",
            "clusters",
            "contexts",
            "current-context",
            "kind",
            "preferences",
            "users",
        }
        or config.get("current-context") != api["context"]
        or not isinstance(cluster_entry, dict)
        or set(cluster_entry) != {"cluster", "name"}
        or not isinstance(cluster_entry.get("name"), str)
        or not isinstance(cluster, dict)
        or set(cluster) != {"certificate-authority-data", "server"}
        or cluster.get("server") != api["origin"]
        or not isinstance(context_entry, dict)
        or set(context_entry) != {"context", "name"}
        or context_entry.get("name") != api["context"]
        or not isinstance(context, dict)
        or set(context) != {"cluster", "user"}
        or context.get("cluster") != cluster_entry.get("name")
        or not isinstance(user_entry, dict)
        or set(user_entry) != {"name", "user"}
        or context.get("user") != user_entry.get("name")
        or not isinstance(user, dict)
        or set(user) != {"token"}
        or not isinstance(user.get("token"), str)
        or not user["token"]
        or any(
            forbidden in canonical(config).decode()
            for forbidden in (
                '"auth-provider"',
                '"certificate-authority"',
                '"client-certificate"',
                '"client-key"',
                '"exec"',
                '"extensions"',
                '"proxy-url"',
                '"tokenFile"',
                '"as"',
                '"as-groups"',
                '"as-uid"',
            )
        )
    ):
        raise AuthorizedApplyV4Error("platform kubeconfig selects another API origin")
    try:
        ca_bytes = base64.b64decode(
            cluster.get("certificate-authority-data"), validate=True
        )
    except (TypeError, ValueError) as error:
        raise AuthorizedApplyV4Error("platform kubeconfig omits exact CA bytes") from error
    if sha256(ca_bytes) != api["ca_sha256"]:
        raise AuthorizedApplyV4Error("platform kubeconfig CA differs from attestation")
    seal_bytes(ca_bytes, FDS["cluster_ca"], "cluster-ca", MAX_JSON_BYTES)
    pod = run_json(
        [
            kubectl,
            "--kubeconfig",
            kubeconfig,
            "--context",
            api["context"],
            "-n",
            pod_claim["namespace"],
            "get",
            "pod",
            pod_claim["name"],
            "-o",
            "json",
        ],
        (FDS["kubectl"], FDS["platform_kubeconfig"]),
        base_environment,
        "live capsule Pod",
    )
    metadata = pod.get("metadata", {})
    spec = pod.get("spec", {})
    containers = [
        item
        for item in spec.get("containers", [])
        if item.get("name") == pod_claim["container_name"]
    ]
    statuses = [
        item
        for item in pod.get("status", {}).get("containerStatuses", [])
        if item.get("name") == pod_claim["container_name"]
    ]
    if (
        metadata.get("uid") != pod_claim["uid"]
        or spec.get("serviceAccountName") != pod_claim["service_account_name"]
        or spec.get("automountServiceAccountToken") is not False
        or len(containers) != 1
        or len(statuses) != 1
        or containers[0].get("image") != image["reference"]
        or statuses[0].get("imageID") != pod_claim["image_id"]
        or containers[0].get("securityContext", {}).get("readOnlyRootFilesystem") is not True
    ):
        raise AuthorizedApplyV4Error("live capsule Pod differs from attestation")
    admission = claims["admission_objects"]
    if not isinstance(admission, list) or not admission:
        raise AuthorizedApplyV4Error("attestation omits admission objects")
    for entry in admission:
        item = exact(
            entry,
            {"api_path", "object_sha256", "resource_version", "uid"},
            "admission identity",
        )
        observed = run_json(
            [
                kubectl,
                "--kubeconfig",
                kubeconfig,
                "--context",
                api["context"],
                "get",
                "--raw",
                item["api_path"],
            ],
            (FDS["kubectl"], FDS["platform_kubeconfig"]),
            base_environment,
            "live admission identity",
        )
        observed_metadata = observed.get("metadata", {})
        if (
            observed_metadata.get("uid") != item["uid"]
            or observed_metadata.get("resourceVersion") != item["resource_version"]
            or sha256(canonical(observed)) != item["object_sha256"]
        ):
            raise AuthorizedApplyV4Error(
                "live admission object differs from attestation"
            )
    return ca_bytes


def secret_descriptors(arguments: list[str]) -> tuple[int, int]:
    descriptors: list[int] = []
    for option in ("--owner-token-fd", "--signing-key-fd"):
        if arguments.count(option) != 1:
            raise AuthorizedApplyV4Error(
                f"executor arguments require exactly one {option}"
            )
        index = arguments.index(option)
        if index + 1 >= len(arguments) or not arguments[index + 1].isdigit():
            raise AuthorizedApplyV4Error(f"executor {option} is malformed")
        descriptor = int(arguments[index + 1])
        if descriptor < 3 or descriptor in FDS.values() or descriptor in descriptors:
            raise AuthorizedApplyV4Error(
                "executor secret descriptor aliases a capsule descriptor"
            )
        os.fstat(descriptor)
        os.set_inheritable(descriptor, False)
        descriptors.append(descriptor)
    return descriptors[0], descriptors[1]


def reject_secret_inheritance(
    inherited: tuple[int, ...], secrets: tuple[int, int], label: str
) -> None:
    if set(inherited) & set(secrets):
        raise AuthorizedApplyV4Error(
            f"{label} would inherit the owner token or signing private key"
        )


def build_query(
    plan_contract: dict[str, Any], ack_path: Path, capsule_sha256: str,
    attestation_sha256: str, source_bundle_sha256: str
) -> dict[str, str]:
    required = {
        "action",
        "cluster_id",
        "consumer",
        "context_sha256",
        "custody_epoch_sha256",
        "execution_capsule_contract_sha256",
        "execution_external_runtime_attestation_sha256",
        "execution_runtime_attestation_sha256",
        "execution_source_bundle_sha256",
        "kube_system_uid",
        "platform_kube_context",
        "receipt_bundle_sha256",
        "rollout_phase",
    }
    if not required.issubset(plan_contract):
        raise AuthorizedApplyV4Error(
            "saved-plan projection omits acknowledgement query fields"
        )
    query = {
        "ack_path": str(ack_path),
        "actual_saved_plan_path": "/proc/1/fd/197",
        "cluster_id": plan_contract["cluster_id"],
        "execution_capsule_contract_path": "/proc/1/fd/180",
        "execution_capsule_contract_sha256": capsule_sha256,
        "execution_external_runtime_attestation_sha256": plan_contract[
            "execution_external_runtime_attestation_sha256"
        ],
        "execution_runtime_attestation_sha256": attestation_sha256,
        "execution_source_bundle_sha256": source_bundle_sha256,
        "expected_action": plan_contract["action"],
        "expected_consumer": plan_contract["consumer"],
        "expected_context_sha256": plan_contract["context_sha256"],
        "expected_custody_epoch_sha256": plan_contract["custody_epoch_sha256"],
        "expected_phase": plan_contract["rollout_phase"],
        "kube_system_uid": plan_contract["kube_system_uid"],
        "platform_context": plan_contract["platform_kube_context"],
        "platform_kubeconfig_path": "/proc/1/fd/198",
        "receipt_bundle_sha256": plan_contract["receipt_bundle_sha256"],
        "trust_lock_path": "/proc/1/fd/181",
    }
    if plan_contract["execution_capsule_contract_sha256"] != capsule_sha256:
        raise AuthorizedApplyV4Error("saved plan selects another capsule")
    if plan_contract["execution_runtime_attestation_sha256"] != attestation_sha256:
        raise AuthorizedApplyV4Error("saved plan selects another runtime attestation")
    if plan_contract["execution_source_bundle_sha256"] != source_bundle_sha256:
        raise AuthorizedApplyV4Error("saved plan selects another source bundle")
    return query


def execute(args: argparse.Namespace) -> dict[str, str]:
    if os.getpid() != 1:
        raise AuthorizedApplyV4Error("authorized apply v4 must be PID 1")
    capsule_bytes, capsule, attestation_bytes, claims = verify_attestation(
        "plan-apply"
    )
    handoff_root = handoff_paths(
        claims, args.plan_handoff, args.acknowledgement
    )
    kubeconfig_bytes = read_regular(
        args.platform_kubeconfig, "platform kubeconfig", MAX_KUBECONFIG_BYTES
    )
    embedded_kubeconfig = validate_embedded_kubeconfig(kubeconfig_bytes, claims)
    kubeconfig_sha256 = seal_bytes(
        kubeconfig_bytes,
        FDS["platform_kubeconfig"],
        "platform-kubeconfig",
        MAX_KUBECONFIG_BYTES,
    )
    runtime, terraform_root, terraform_data_root, immutable_before = runtime_files(
        capsule, args.stage
    )
    if runtime["handoff_root"] != str(handoff_root):
        raise AuthorizedApplyV4Error(
            "capsule and signed attestation select different handoff roots"
        )
    base_environment = {"PATH": "/nonexistent"}
    verify_live_identity(claims, embedded_kubeconfig, base_environment)
    capsule_sha256 = sha256(capsule_bytes)
    attestation_sha256 = sha256(attestation_bytes)
    source_bundle_sha256 = runtime["runtime_files"]["source_bundle"]["sha256"]
    environment = {
        "FS2_SAI07_CAPSULE_CONTRACT_PATH": "/proc/1/fd/180",
        "FS2_SAI07_CAPSULE_CONTRACT_SHA256": capsule_sha256,
        "FS2_SAI07_RUNTIME_ATTESTATION_PATH": "/proc/1/fd/184",
        "FS2_SAI07_RUNTIME_ATTESTATION_SHA256": attestation_sha256,
        "FS2_SAI07_SOURCE_BUNDLE_SHA256": source_bundle_sha256,
        "FS2_SAI07_KUBECTL_PATH": "/proc/1/fd/193",
        "FS2_SAI07_OPENSSL_PATH": "/proc/1/fd/192",
        "KUBECONFIG": "/proc/1/fd/198",
        "PATH": "/nonexistent",
        "TF_CLI_CONFIG_FILE": "/proc/1/fd/195",
        "TF_DATA_DIR": terraform_data_root,
        "TF_IN_AUTOMATION": "1",
    }
    variables_bytes, variables = load_canonical(args.plan_variables, "plan variables")
    receipt = variables.get("pod_security_rollout_receipt")
    if (
        variables.get("kubeconfig_path") != "/proc/1/fd/198"
        or not isinstance(receipt, dict)
        or receipt.get("execution_capsule_contract_sha256") != capsule_sha256
        or receipt.get("execution_runtime_attestation_sha256") != attestation_sha256
        or not isinstance(
            receipt.get("execution_external_runtime_attestation_sha256"), str
        )
        or not SHA256_RE.fullmatch(
            receipt["execution_external_runtime_attestation_sha256"]
        )
        or receipt["execution_external_runtime_attestation_sha256"] == "0" * 64
        or receipt.get("execution_source_bundle_sha256") != source_bundle_sha256
        or receipt.get("execution_platform_kubeconfig_sha256") != kubeconfig_sha256
    ):
        raise AuthorizedApplyV4Error(
            "plan variables do not bind the attested execution session"
        )
    seal_bytes(
        variables_bytes,
        FDS["plan_variables"],
        "plan-variables",
        MAX_JSON_BYTES,
    )
    create_plan_fd()
    plan_fds = (
        FDS["terraform"],
        FDS["terraform_cli_config"],
        FDS["plan_variables"],
        FDS["saved_plan"],
        FDS["platform_kubeconfig"],
    )
    planned = subprocess.run(
        [
            "/proc/1/fd/194",
            f"-chdir={terraform_root}",
            "plan",
            "-input=false",
            "-lock=true",
            "-out=/proc/1/fd/197",
            "-var-file=/proc/1/fd/196",
        ],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
        pass_fds=plan_fds,
    )
    if planned.returncode != 0:
        raise AuthorizedApplyV4Error("capsule-owned Terraform plan failed")
    plan_sha256 = seal_plan_fd()
    runtime_after_plan, _root, _data_root, immutable_after_plan = runtime_files(
        capsule, args.stage
    )
    if runtime_after_plan != runtime or immutable_after_plan != immutable_before:
        raise AuthorizedApplyV4Error(
            "Terraform plan changed its immutable root/provider inputs"
        )

    public_fds = tuple(FDS.values())
    plan_contract = run_json(
        ["/proc/1/fd/191", "/proc/1/fd/190", "inspect-plan", "/proc/1/fd/197", args.stage],
        public_fds,
        environment,
        "saved-plan projection",
    )
    if (
        plan_contract.get("saved_plan_sha256") != plan_sha256
        or plan_contract.get("configuration_sha256")
        != runtime["terraform_configuration_sha256"][args.stage]
        or plan_contract.get("platform_kubeconfig_sha256") != kubeconfig_sha256
        or plan_contract.get("platform_kubeconfig_path") != "/proc/1/fd/198"
    ):
        raise AuthorizedApplyV4Error(
            "saved plan differs from attested plan-time inputs"
        )
    publish_plan(args.plan_handoff, plan_sha256)
    await_acknowledgement(args.acknowledgement)
    query = build_query(
        plan_contract,
        args.acknowledgement,
        capsule_sha256,
        attestation_sha256,
        source_bundle_sha256,
    )
    query_bytes = canonical(query)
    verifier = ["/proc/1/fd/191", "/proc/1/fd/190", "verify-ack"]
    run_json(
        verifier,
        public_fds,
        environment,
        "pre-apply acknowledgement verifier",
        input_bytes=query_bytes,
    )
    applied = subprocess.run(
        [
            "/proc/1/fd/194",
            f"-chdir={terraform_root}",
            "apply",
            "-input=false",
            "-auto-approve",
            "/proc/1/fd/197",
        ],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
        pass_fds=public_fds,
    )
    if applied.returncode != 0:
        raise AuthorizedApplyV4Error("exact sealed Terraform apply failed")
    run_json(
        verifier,
        public_fds,
        environment,
        "post-apply acknowledgement verifier",
        input_bytes=query_bytes,
    )
    runtime_after_apply, _root, _data_root, immutable_after_apply = runtime_files(
        capsule, args.stage
    )
    if runtime_after_apply != runtime or immutable_after_apply != immutable_before:
        raise AuthorizedApplyV4Error(
            "Terraform apply changed its immutable root/provider inputs"
        )
    return {
        "capsule_contract_sha256": capsule_sha256,
        "handoff_plan_sha256": plan_sha256,
        "plan_sha256": plan_sha256,
        "runtime_attestation_sha256": attestation_sha256,
        "source_bundle_sha256": source_bundle_sha256,
        "status": "applied-capsule-created-sealed-plan",
    }


def execute_external(args: argparse.Namespace, executor_args: list[str]) -> dict[str, str]:
    if os.getpid() != 1:
        raise AuthorizedApplyV4Error("external acknowledgement v4 must be PID 1")
    capsule_bytes, capsule, attestation_bytes, claims = verify_attestation(
        "external-ack"
    )
    handoff_root = handoff_paths(
        claims, args.plan_handoff, args.acknowledgement
    )
    runtime, _terraform_root, _terraform_data_root, _immutable = runtime_files(
        capsule, args.stage
    )
    if runtime["handoff_root"] != str(handoff_root):
        raise AuthorizedApplyV4Error(
            "capsule and signed attestation select different handoff roots"
        )
    api = exact(claims["api"], {"ca_sha256", "context", "origin"}, "attested API")
    seal_path(
        args.cluster_ca,
        api["ca_sha256"],
        FDS["cluster_ca"],
        "external capsule cluster CA",
        MAX_JSON_BYTES,
    )
    plan_bytes = read_regular(
        args.plan_handoff, "external capsule plan handoff", MAX_PLAN_BYTES
    )
    plan_sha256 = seal_bytes(
        plan_bytes,
        FDS["saved_plan"],
        "external-capsule-saved-plan",
        MAX_PLAN_BYTES,
    )
    capsule_sha256 = sha256(capsule_bytes)
    attestation_sha256 = sha256(attestation_bytes)
    source_bundle_sha256 = runtime["runtime_files"]["source_bundle"]["sha256"]
    environment = {
        "FS2_SAI07_CAPSULE_CONTRACT_PATH": "/proc/1/fd/180",
        "FS2_SAI07_CAPSULE_CONTRACT_SHA256": capsule_sha256,
        "FS2_SAI07_RUNTIME_ATTESTATION_PATH": "/proc/1/fd/184",
        "FS2_SAI07_RUNTIME_ATTESTATION_SHA256": attestation_sha256,
        "FS2_SAI07_SOURCE_BUNDLE_SHA256": source_bundle_sha256,
        "FS2_SAI07_KUBECTL_PATH": "/proc/1/fd/193",
        "FS2_SAI07_OPENSSL_PATH": "/proc/1/fd/192",
        "FS2_SAI07_OWNER_API_SERVER": api["origin"],
        "FS2_SAI07_OWNER_CA_PATH": "/proc/1/fd/199",
        "PATH": "/nonexistent",
        "TF_IN_AUTOMATION": "1",
    }
    secrets = secret_descriptors(executor_args)
    forbidden = {
        "--owner-api-server",
        "--owner-ca-file",
        "--platform-saved-plan",
        "--ack-output",
    }
    if any(value in forbidden for value in executor_args):
        raise AuthorizedApplyV4Error(
            "caller may not select API, CA, plan, or acknowledgement paths"
        )
    public_fds = tuple(
        descriptor
        for name, descriptor in FDS.items()
        if name not in {"plan_variables", "platform_kubeconfig"}
    )
    result = run_json(
        [
            "/proc/1/fd/191",
            "/proc/1/fd/190",
            "external-execute",
            *executor_args,
            "--platform-saved-plan",
            "/proc/1/fd/197",
            "--ack-output",
            str(args.acknowledgement),
        ],
        (*public_fds, *secrets),
        environment,
        "external acknowledgement executor",
    )
    if result.get("valid") is not True:
        raise AuthorizedApplyV4Error("external executor did not seal an acknowledgement")
    return {
        "acknowledgement_sha256": str(result["acknowledgement_sha256"]),
        "capsule_contract_sha256": capsule_sha256,
        "plan_sha256": plan_sha256,
        "runtime_attestation_sha256": attestation_sha256,
        "source_bundle_sha256": source_bundle_sha256,
        "status": "external-acknowledgement-created",
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="operation", required=True)
    apply_parser = subparsers.add_parser("plan-apply")
    apply_parser.add_argument("--stage", choices=("foundation", "workloads"), required=True)
    apply_parser.add_argument("--platform-kubeconfig", required=True, type=Path)
    apply_parser.add_argument("--plan-variables", required=True, type=Path)
    apply_parser.add_argument("--plan-handoff", required=True, type=Path)
    apply_parser.add_argument("--acknowledgement", required=True, type=Path)
    external_parser = subparsers.add_parser("external-ack")
    external_parser.add_argument("--stage", choices=("foundation", "workloads"), required=True)
    external_parser.add_argument("--cluster-ca", required=True, type=Path)
    external_parser.add_argument("--plan-handoff", required=True, type=Path)
    external_parser.add_argument("--acknowledgement", required=True, type=Path)
    external_parser.add_argument("executor_arguments", nargs=argparse.REMAINDER)
    return result


def main() -> int:
    try:
        args = parser().parse_args()
        if args.operation == "plan-apply":
            result = execute(args)
        else:
            executor_args = args.executor_arguments
            if executor_args[:1] == ["--"]:
                executor_args = executor_args[1:]
            if not executor_args:
                raise AuthorizedApplyV4Error(
                    "external executor arguments are required"
                )
            result = execute_external(args, executor_args)
    except (AuthorizedApplyV4Error, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"SAI-07 authorized apply v4 rejected: {error}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
