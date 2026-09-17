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
import signal
import stat
import struct
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
PLAN_TIMEOUT_SECONDS = 90
ACK_WAIT_SECONDS = 90
APPLY_TIMEOUT_SECONDS = 120
SETTLEMENT_PLAN_TIMEOUT_SECONDS = 45
POST_APPLY_FENCE_SECONDS = 30
VERIFIER_TIMEOUT_SECONDS = 60
KUBERNETES_READ_TIMEOUT_SECONDS = 5
MAX_ADMISSION_OBJECTS = 8
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
    "image_provenance": 189,
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
    "image_sbom": 200,
    "settlement_plan_a": 201,
    "settlement_plan_b": 202,
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


def instant(value: object, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AuthorizedApplyV4Error(f"{label} must be a UTC RFC3339 instant")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise AuthorizedApplyV4Error(f"{label} is malformed") from error
    if parsed.tzinfo != dt.UTC:
        raise AuthorizedApplyV4Error(f"{label} is not UTC")
    return parsed


def capsule_admission_contract(
    capsule: dict[str, Any], role: str
) -> tuple[dict[str, Any], list[str]]:
    admission = exact(
        capsule.get("admission"),
        {
            "pod_security_contract",
            "pod_security_projection",
            "required_objects_by_role",
        },
        "capsule admission contract",
    )
    profile = exact(
        admission["pod_security_contract"],
        {
            "automount_service_account_token",
            "capabilities_drop_all",
            "forbid_capability_additions",
            "forbid_host_namespaces",
            "forbid_host_path",
            "forbid_host_ports",
            "forbid_privileged",
            "require_allow_privilege_escalation_false",
            "require_digest_images",
            "require_read_only_root_filesystem",
            "require_run_as_non_root",
            "required_seccomp_type",
        },
        "capsule Pod security contract",
    )
    expected_profile = {
        "automount_service_account_token": False,
        "capabilities_drop_all": True,
        "forbid_capability_additions": True,
        "forbid_host_namespaces": True,
        "forbid_host_path": True,
        "forbid_host_ports": True,
        "forbid_privileged": True,
        "require_allow_privilege_escalation_false": True,
        "require_digest_images": True,
        "require_read_only_root_filesystem": True,
        "require_run_as_non_root": True,
        "required_seccomp_type": "RuntimeDefault",
    }
    roles = exact(
        admission["required_objects_by_role"],
        {"external-ack", "plan-apply"},
        "capsule role admission objects",
    )
    paths = roles[role]
    if (
        admission["pod_security_projection"]
        != "canonical-v1-full-spec-and-security-metadata"
        or profile != expected_profile
        or not isinstance(paths, list)
        or not paths
        or len(paths) > MAX_ADMISSION_OBJECTS
        or paths != sorted(set(paths))
        or any(
            not isinstance(path, str) or not path.startswith("/api")
            for path in paths
        )
    ):
        raise AuthorizedApplyV4Error(
            "capsule admission or Pod security contract is incomplete"
        )
    return profile, paths


def validate_admission_claims(
    claims: dict[str, Any], capsule: dict[str, Any], role: str
) -> None:
    _profile, required_paths = capsule_admission_contract(capsule, role)
    admission = claims["admission_objects"]
    if not isinstance(admission, list) or not admission:
        raise AuthorizedApplyV4Error("attestation omits admission objects")
    observed_paths: list[str] = []
    for entry in admission:
        item = exact(
            entry,
            {"api_path", "object_sha256", "resource_version", "uid"},
            "admission identity",
        )
        if (
            not isinstance(item["api_path"], str)
            or not item["api_path"].startswith("/api")
            or not isinstance(item["uid"], str)
            or not item["uid"]
            or not isinstance(item["resource_version"], str)
            or not item["resource_version"]
            or not isinstance(item["object_sha256"], str)
            or not SHA256_RE.fullmatch(item["object_sha256"])
        ):
            raise AuthorizedApplyV4Error("admission identity is incomplete")
        observed_paths.append(item["api_path"])
    if observed_paths != required_paths:
        raise AuthorizedApplyV4Error(
            "attested admission objects differ from the exact capsule set"
        )


def pod_security_projection(pod: dict[str, Any]) -> dict[str, Any]:
    metadata = pod.get("metadata")
    spec = pod.get("spec")
    if not isinstance(metadata, dict) or not isinstance(spec, dict):
        raise AuthorizedApplyV4Error("live capsule Pod is malformed")
    return {
        "apiVersion": pod.get("apiVersion"),
        "kind": pod.get("kind"),
        "metadata": {
            "annotations": metadata.get("annotations", {}),
            "finalizers": metadata.get("finalizers", []),
            "labels": metadata.get("labels", {}),
            "name": metadata.get("name"),
            "namespace": metadata.get("namespace"),
            "ownerReferences": metadata.get("ownerReferences", []),
            "uid": metadata.get("uid"),
        },
        "spec": spec,
    }


def validate_pod_security_profile(pod: dict[str, Any], profile: dict[str, Any]) -> None:
    spec = pod.get("spec", {})
    pod_security = spec.get("securityContext", {})
    containers = [
        *spec.get("initContainers", []),
        *spec.get("containers", []),
        *spec.get("ephemeralContainers", []),
    ]
    if (
        pod.get("apiVersion") != "v1"
        or pod.get("kind") != "Pod"
        or spec.get("automountServiceAccountToken")
        is not profile["automount_service_account_token"]
        or any(spec.get(field) is True for field in ("hostNetwork", "hostPID", "hostIPC"))
        or spec.get("shareProcessNamespace") is True
        or not containers
        or any(
            not isinstance(volume, dict) or "hostPath" in volume
            for volume in spec.get("volumes", [])
        )
    ):
        raise AuthorizedApplyV4Error("capsule Pod violates its Pod-level contract")
    pod_run_as_non_root = pod_security.get("runAsNonRoot") is True
    pod_seccomp = pod_security.get("seccompProfile", {}).get("type")
    for container in containers:
        if not isinstance(container, dict):
            raise AuthorizedApplyV4Error(
                "capsule container violates the complete security profile"
            )
        security = container.get("securityContext", {})
        capabilities = security.get("capabilities", {})
        ports = container.get("ports", [])
        if (
            not isinstance(container.get("image"), str)
            or "@sha256:" not in container["image"]
            or security.get("privileged") is not False
            or security.get("allowPrivilegeEscalation") is not False
            or security.get("readOnlyRootFilesystem") is not True
            or not (security.get("runAsNonRoot") is True or pod_run_as_non_root)
            or capabilities.get("drop") != ["ALL"]
            or capabilities.get("add") not in (None, [])
            or security.get("seccompProfile", {}).get("type", pod_seccomp)
            != profile["required_seccomp_type"]
            or any(
                not isinstance(port, dict)
                or port.get("hostPort") not in (None, 0)
                for port in ports
            )
        ):
            raise AuthorizedApplyV4Error(
                "capsule container violates the complete security profile"
            )


def require_fresh_until(value: object, seconds: int, label: str) -> None:
    if instant(value, label) < dt.datetime.now(dt.UTC) + dt.timedelta(seconds=seconds):
        raise AuthorizedApplyV4Error(f"{label} does not cover the bounded operation")


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


def verify_static_elf(descriptor: int, label: str) -> None:
    """Reject a bootstrap executable that can load any unpinned runtime code."""

    header = os.pread(descriptor, 64, 0)
    if (
        len(header) != 64
        or header[:4] != b"\x7fELF"
        or header[4] != 2  # ELFCLASS64
        or header[5] != 1  # ELFDATA2LSB
        or header[6] != 1  # EV_CURRENT
    ):
        raise AuthorizedApplyV4Error(f"{label} is not a supported 64-bit ELF")
    unpacked = struct.unpack("<16sHHIQQQIHHHHHH", header)
    machine = unpacked[2]
    program_offset = unpacked[5]
    program_entry_size = unpacked[9]
    program_count = unpacked[10]
    if (
        machine not in {62, 183}  # EM_X86_64, EM_AARCH64
        or program_entry_size != 56
        or program_count <= 0
        or program_count > 256
    ):
        raise AuthorizedApplyV4Error(f"{label} ELF architecture/table is unsupported")
    dynamic_segments: list[tuple[int, int]] = []
    for index in range(program_count):
        entry = os.pread(
            descriptor,
            program_entry_size,
            program_offset + index * program_entry_size,
        )
        if len(entry) != program_entry_size:
            raise AuthorizedApplyV4Error(f"{label} ELF program table is truncated")
        program_type, _flags, offset, _vaddr, _paddr, file_size, _mem_size, _align = (
            struct.unpack("<IIQQQQQQ", entry)
        )
        if program_type == 3:  # PT_INTERP
            raise AuthorizedApplyV4Error(f"{label} has an unpinned ELF interpreter")
        if program_type == 2:  # PT_DYNAMIC
            dynamic_segments.append((offset, file_size))
    for offset, file_size in dynamic_segments:
        if file_size % 16 or file_size > 1024 * 1024:
            raise AuthorizedApplyV4Error(f"{label} ELF dynamic table is malformed")
        dynamic = os.pread(descriptor, file_size, offset)
        if len(dynamic) != file_size:
            raise AuthorizedApplyV4Error(f"{label} ELF dynamic table is truncated")
        for entry_offset in range(0, len(dynamic), 16):
            tag, _value = struct.unpack(
                "<qQ", dynamic[entry_offset : entry_offset + 16]
            )
            if tag == 1:  # DT_NEEDED
                raise AuthorizedApplyV4Error(
                    f"{label} has an unpinned shared-library dependency"
                )
            if tag == 0:  # DT_NULL
                break


def read_sealed_json_descriptor(
    descriptor: int, expected_sha256: str, label: str
) -> dict[str, Any]:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > MAX_JSON_BYTES
        or fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) & SEALS != SEALS
    ):
        raise AuthorizedApplyV4Error(f"{label} is not a bounded sealed file")
    payload = os.pread(descriptor, metadata.st_size, 0)
    if len(payload) != metadata.st_size or sha256(payload) != expected_sha256:
        raise AuthorizedApplyV4Error(f"{label} differs from its signed digest")
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuthorizedApplyV4Error(f"{label} is not JSON") from error
    if not isinstance(document, dict) or payload != canonical(document):
        raise AuthorizedApplyV4Error(f"{label} is not canonical JSON")
    return document


def validate_image_evidence(
    image: dict[str, Any],
    pod_claim: dict[str, Any],
    runtime_files_contract: dict[str, Any],
) -> None:
    digest = image.get("digest")
    reference = image.get("reference")
    image_id = pod_claim.get("image_id")
    provenance_sha256 = image.get("provenance_sha256")
    sbom_sha256 = image.get("sbom_sha256")
    if (
        not isinstance(digest, str)
        or not OCI_DIGEST_RE.fullmatch(digest)
        or pod_claim.get("image_digest") != digest
        or not isinstance(reference, str)
        or reference.count("@") != 1
        or reference.rsplit("@", 1)[1] != digest
        or not isinstance(image_id, str)
        or re.search(r"(?:@|://)(sha256:[a-f0-9]{64})$", image_id) is None
        or re.search(r"(?:@|://)(sha256:[a-f0-9]{64})$", image_id).group(1)
        != digest
        or not isinstance(provenance_sha256, str)
        or not SHA256_RE.fullmatch(provenance_sha256)
        or provenance_sha256 == "0" * 64
        or not isinstance(sbom_sha256, str)
        or not SHA256_RE.fullmatch(sbom_sha256)
        or sbom_sha256 == "0" * 64
    ):
        raise AuthorizedApplyV4Error(
            "signed image reference, resolved image ID, and digest are not cross-bound"
        )
    provenance_contract = exact(
        runtime_files_contract.get("image_provenance"),
        {"fd", "path", "sha256"},
        "image provenance runtime file",
    )
    sbom_contract = exact(
        runtime_files_contract.get("image_sbom"),
        {"fd", "path", "sha256"},
        "image SBOM runtime file",
    )
    if (
        provenance_contract["fd"] != FDS["image_provenance"]
        or provenance_contract["sha256"] != provenance_sha256
        or sbom_contract["fd"] != FDS["image_sbom"]
        or sbom_contract["sha256"] != sbom_sha256
    ):
        raise AuthorizedApplyV4Error(
            "signed image evidence differs from the capsule descriptor pins"
        )
    provenance = exact(
        read_sealed_json_descriptor(
            FDS["image_provenance"], provenance_sha256, "image provenance"
        ),
        {
            "build_type",
            "builder_id",
            "image",
            "materials_sha256",
            "predicate_type",
            "schema",
        },
        "image provenance",
    )
    provenance_image = exact(
        provenance["image"], {"digest", "reference"}, "provenance image"
    )
    if (
        provenance["schema"] != "fs2-serve.nebius.ai/image-provenance/v1"
        or provenance["predicate_type"] != "https://slsa.dev/provenance/v1"
        or not isinstance(provenance["builder_id"], str)
        or not provenance["builder_id"]
        or not isinstance(provenance["build_type"], str)
        or not provenance["build_type"]
        or not isinstance(provenance["materials_sha256"], str)
        or not SHA256_RE.fullmatch(provenance["materials_sha256"])
        or provenance["materials_sha256"] == "0" * 64
        or provenance_image != {"digest": digest, "reference": reference}
    ):
        raise AuthorizedApplyV4Error(
            "image provenance is incomplete or selects another image"
        )
    sbom = exact(
        read_sealed_json_descriptor(FDS["image_sbom"], sbom_sha256, "image SBOM"),
        {"document", "format", "image", "schema"},
        "image SBOM envelope",
    )
    sbom_image = exact(sbom["image"], {"digest", "reference"}, "SBOM image")
    document = sbom["document"]
    described = document.get("documentDescribes") if isinstance(document, dict) else None
    packages = document.get("packages") if isinstance(document, dict) else None
    package_ids = {
        package.get("SPDXID")
        for package in packages or []
        if isinstance(package, dict) and isinstance(package.get("SPDXID"), str)
    }
    if (
        sbom["schema"] != "fs2-serve.nebius.ai/image-sbom/v1"
        or sbom["format"] != "spdx-json"
        or sbom_image != {"digest": digest, "reference": reference}
        or not isinstance(document, dict)
        or document.get("spdxVersion") != "SPDX-2.3"
        or document.get("SPDXID") != "SPDXRef-DOCUMENT"
        or document.get("dataLicense") != "CC0-1.0"
        or not isinstance(document.get("name"), str)
        or not document["name"]
        or not isinstance(document.get("documentNamespace"), str)
        or not document["documentNamespace"].startswith("https://")
        or not isinstance(document.get("creationInfo"), dict)
        or not isinstance(described, list)
        or not described
        or not isinstance(packages, list)
        or not packages
        or any(not isinstance(value, str) or value not in package_ids for value in described)
    ):
        raise AuthorizedApplyV4Error("image SBOM is incomplete or selects another image")


def create_plan_fd(
    descriptor_number: int = FDS["saved_plan"], label: str = "sai07-saved-plan"
) -> None:
    descriptor = os.memfd_create(
        label, os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
    )
    try:
        os.dup2(descriptor, descriptor_number, inheritable=True)
    finally:
        os.close(descriptor)


def seal_plan_fd(descriptor: int = FDS["saved_plan"], label: str = "saved plan") -> str:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > MAX_PLAN_BYTES
    ):
        raise AuthorizedApplyV4Error(
            f"Terraform did not create a bounded nonempty {label}"
        )
    os.fsync(descriptor)
    fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, SEALS)
    if fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) & SEALS != SEALS:
        raise AuthorizedApplyV4Error(f"{label} descriptor is not write sealed")
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
        raise AuthorizedApplyV4Error(f"{label} became short while hashing")
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
    verify_static_elf(FDS["openssl"], "bootstrap OpenSSL")
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
            "resource_version",
            "security_projection_sha256",
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
        or not isinstance(image["reference"], str)
        or image["reference"].count("@") != 1
        or image["reference"].rsplit("@", 1)[1] != image["digest"]
        or not isinstance(pod_claim["image_id"], str)
        or re.search(
            r"(?:@|://)(sha256:[a-f0-9]{64})$", pod_claim["image_id"]
        )
        is None
        or re.search(
            r"(?:@|://)(sha256:[a-f0-9]{64})$", pod_claim["image_id"]
        ).group(1)
        != image["digest"]
        or any(
            not isinstance(image[field], str)
            or not SHA256_RE.fullmatch(image[field])
            or image[field] == "0" * 64
            for field in ("provenance_sha256", "sbom_sha256")
        )
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
    validate_admission_claims(claims, capsule, expected_role)
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
    timeout_seconds: int = VERIFIER_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=environment,
        pass_fds=pass_fds,
        start_new_session=True,
    )
    try:
        stdout, _stderr = process.communicate(
            input=input_bytes,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        fence_capsule_descendants(process.pid, label)
        process.wait(timeout=1)
        raise AuthorizedApplyV4Error(f"{label} exceeded its time bound") from error
    fence_capsule_descendants(process.pid, label)
    if process.returncode != 0 or len(stdout) > MAX_JSON_BYTES:
        raise AuthorizedApplyV4Error(
            f"{label} rejected, failed, or exceeded the output bound"
        )
    try:
        value = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuthorizedApplyV4Error(f"{label} output is not JSON") from error
    if not isinstance(value, dict):
        raise AuthorizedApplyV4Error(f"{label} output is not an object")
    return value


def capsule_descendants() -> list[int]:
    if os.getpid() != 1:
        raise AuthorizedApplyV4Error("descendant fencing requires capsule PID 1")
    result: list[int] = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit() and int(entry.name) != 1:
            result.append(int(entry.name))
    return sorted(result)


def reap_capsule_children() -> None:
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def fence_capsule_descendants(process_group: int, label: str) -> None:
    """Stop, terminate, reap, and prove absence of every capsule descendant."""

    if not capsule_descendants():
        return
    try:
        os.killpg(process_group, signal.SIGSTOP)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 10
    while True:
        descendants = capsule_descendants()
        for pid in descendants:
            try:
                os.kill(pid, signal.SIGSTOP)
            except ProcessLookupError:
                pass
        for selected_signal in (signal.SIGTERM, signal.SIGKILL):
            for pid in descendants:
                try:
                    os.kill(pid, selected_signal)
                except ProcessLookupError:
                    pass
            until = min(deadline, time.monotonic() + 1)
            while time.monotonic() < until:
                reap_capsule_children()
                if not capsule_descendants():
                    return
                time.sleep(0.05)
        if time.monotonic() >= deadline:
            raise AuthorizedApplyV4Error(
                f"{label} descendants did not reach a proven empty PID namespace"
            )


def run_terraform_supervised(
    command: list[str],
    pass_fds: tuple[int, ...],
    environment: dict[str, str],
    timeout_seconds: int,
    label: str,
) -> tuple[int | None, bool]:
    if capsule_descendants():
        raise AuthorizedApplyV4Error(
            f"{label} cannot start with an unowned capsule descendant"
        )
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
        pass_fds=pass_fds,
        start_new_session=True,
    )
    timed_out = False
    try:
        return_code = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        return_code = None
    finally:
        # A provider helper may have double-forked, changed process group, or
        # been reparented to capsule PID 1. The complete PID namespace, not
        # merely Terraform's original process, is therefore the fence scope.
        fence_capsule_descendants(process.pid, label)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired as error:
            raise AuthorizedApplyV4Error(
                f"{label} parent survived complete descendant fencing"
            ) from error
        reap_capsule_children()
        if capsule_descendants():
            raise AuthorizedApplyV4Error(
                f"{label} left a descendant after the terminal fence"
            )
    return return_code, timed_out


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
        {
            "image_provenance",
            "image_sbom",
            "kubectl",
            "openssl",
            "python",
            "source_bundle",
            "terraform",
            "terraform_cli_config",
        },
        "capsule runtime files",
    )
    for name, maximum in (
        ("python", MAX_RUNTIME_FILE_BYTES),
        ("openssl", MAX_RUNTIME_FILE_BYTES),
        ("kubectl", MAX_RUNTIME_FILE_BYTES),
        ("terraform", MAX_RUNTIME_FILE_BYTES),
        ("terraform_cli_config", MAX_JSON_BYTES),
        ("source_bundle", MAX_RUNTIME_FILE_BYTES),
        ("image_provenance", MAX_JSON_BYTES),
        ("image_sbom", MAX_JSON_BYTES),
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


def await_acknowledgement(path: Path, maximum_seconds: int = ACK_WAIT_SECONDS) -> None:
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
    capsule: dict[str, Any],
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
            "resource_version",
            "security_projection_sha256",
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
    validate_image_evidence(image, pod_claim, capsule["runtime"]["runtime_files"])
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
        timeout_seconds=KUBERNETES_READ_TIMEOUT_SECONDS,
    )
    metadata = pod.get("metadata", {})
    spec = pod.get("spec", {})
    containers = [
        item
        for item in spec.get("containers", [])
        if isinstance(item, dict)
        and item.get("name") == pod_claim["container_name"]
    ]
    statuses = [
        item
        for item in pod.get("status", {}).get("containerStatuses", [])
        if isinstance(item, dict)
        and item.get("name") == pod_claim["container_name"]
    ]
    if (
        metadata.get("uid") != pod_claim["uid"]
        or metadata.get("resourceVersion") != pod_claim["resource_version"]
        or spec.get("serviceAccountName") != pod_claim["service_account_name"]
        or spec.get("automountServiceAccountToken") is not False
        or len(containers) != 1
        or len(statuses) != 1
        or containers[0].get("image") != image["reference"]
        or statuses[0].get("imageID") != pod_claim["image_id"]
        or containers[0].get("securityContext", {}).get("readOnlyRootFilesystem") is not True
    ):
        raise AuthorizedApplyV4Error("live capsule Pod differs from attestation")
    profile, _required_paths = capsule_admission_contract(capsule, claims["role"])
    validate_pod_security_profile(pod, profile)
    if sha256(canonical(pod_security_projection(pod))) != pod_claim[
        "security_projection_sha256"
    ]:
        raise AuthorizedApplyV4Error(
            "live complete Pod security projection differs from attestation"
        )
    admission = claims["admission_objects"]
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
            timeout_seconds=KUBERNETES_READ_TIMEOUT_SECONDS,
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
    plan_contract: dict[str, Any],
    ack_path: Path,
    capsule_sha256: str,
    attestation_sha256: str,
    source_bundle_sha256: str,
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


def prove_provider_settlement(
    terraform_root: str,
    stage: str,
    environment: dict[str, str],
    public_fds: tuple[int, ...],
) -> dict[str, str]:
    settlement_digests: list[str] = []
    for descriptor_name, label in (
        ("settlement_plan_a", "first authoritative settlement plan"),
        ("settlement_plan_b", "second authoritative settlement plan"),
    ):
        descriptor = FDS[descriptor_name]
        create_plan_fd(descriptor, descriptor_name)
        settlement_fds = (
            FDS["terraform"],
            FDS["terraform_cli_config"],
            FDS["plan_variables"],
            FDS["platform_kubeconfig"],
            descriptor,
        )
        return_code, timed_out = run_terraform_supervised(
            [
                "/proc/1/fd/194",
                f"-chdir={terraform_root}",
                "plan",
                "-input=false",
                "-lock=true",
                "-refresh=true",
                f"-out=/proc/1/fd/{descriptor}",
                "-var-file=/proc/1/fd/196",
            ],
            settlement_fds,
            environment,
            SETTLEMENT_PLAN_TIMEOUT_SECONDS,
            label,
        )
        if timed_out or return_code != 0:
            raise AuthorizedApplyV4Error(
                f"{label} failed; provider-operation settlement is unproved"
            )
        settlement_digests.append(seal_plan_fd(descriptor, label))
        if descriptor_name == "settlement_plan_a":
            time.sleep(1)
    result = run_json(
        [
            "/proc/1/fd/191",
            "/proc/1/fd/190",
            "verify-settlement",
            "/proc/1/fd/197",
            "/proc/1/fd/201",
            "/proc/1/fd/202",
            stage,
        ],
        (*public_fds, FDS["settlement_plan_a"], FDS["settlement_plan_b"]),
        environment,
        "authoritative provider-operation settlement",
        timeout_seconds=2 * SETTLEMENT_PLAN_TIMEOUT_SECONDS,
    )
    if (
        result.get("status") != "authoritative-provider-settlement-proved"
        or result.get("first_settlement_plan_sha256") != settlement_digests[0]
        or result.get("second_settlement_plan_sha256") != settlement_digests[1]
        or not isinstance(result.get("authoritative_refreshed_state_sha256"), str)
        or not SHA256_RE.fullmatch(result["authoritative_refreshed_state_sha256"])
        or not isinstance(result.get("planned_object_postconditions_sha256"), str)
        or not SHA256_RE.fullmatch(result["planned_object_postconditions_sha256"])
        or not isinstance(result.get("settled_object_count"), str)
        or not result["settled_object_count"].isdigit()
        or int(result["settled_object_count"]) <= 0
    ):
        raise AuthorizedApplyV4Error(
            "provider settlement omitted refreshed state or exact planned postconditions"
        )
    return result


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
    verify_live_identity(claims, capsule, embedded_kubeconfig, base_environment)
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
    plan_return_code, plan_timed_out = run_terraform_supervised(
        [
            "/proc/1/fd/194",
            f"-chdir={terraform_root}",
            "plan",
            "-input=false",
            "-lock=true",
            "-out=/proc/1/fd/197",
            "-var-file=/proc/1/fd/196",
        ],
        plan_fds,
        environment,
        PLAN_TIMEOUT_SECONDS,
        "capsule-owned Terraform plan",
    )
    if plan_timed_out or plan_return_code != 0:
        raise AuthorizedApplyV4Error("capsule-owned Terraform plan failed")
    plan_sha256 = seal_plan_fd()
    runtime_after_plan, _root, _data_root, immutable_after_plan = runtime_files(
        capsule, args.stage
    )
    if runtime_after_plan != runtime or immutable_after_plan != immutable_before:
        raise AuthorizedApplyV4Error(
            "Terraform plan changed its immutable root/provider inputs"
        )

    public_fds = tuple(
        descriptor
        for name, descriptor in FDS.items()
        if name not in {"settlement_plan_a", "settlement_plan_b"}
    )
    plan_contract = run_json(
        [
            "/proc/1/fd/191",
            "/proc/1/fd/190",
            "inspect-plan",
            "/proc/1/fd/197",
            args.stage,
        ],
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
    # The external acknowledgement is the short-lived apply lease. Re-read the
    # capsule Pod/admission identities, then make the complete retained-object
    # reconstruction the last operation before Terraform can mutate anything.
    verify_live_identity(claims, capsule, embedded_kubeconfig, base_environment)
    pre_apply = run_json(
        verifier,
        public_fds,
        environment,
        "pre-apply acknowledgement verifier",
        input_bytes=query_bytes,
    )
    lease_seconds = (
        APPLY_TIMEOUT_SECONDS
        + 2 * SETTLEMENT_PLAN_TIMEOUT_SECONDS
        + VERIFIER_TIMEOUT_SECONDS
        + (MAX_ADMISSION_OBJECTS + 1) * KUBERNETES_READ_TIMEOUT_SECONDS
        + POST_APPLY_FENCE_SECONDS
    )
    require_fresh_until(
        claims["expires_at"], lease_seconds, "plan capsule attestation expiry"
    )
    require_fresh_until(
        pre_apply.get("lease_expires_at"),
        lease_seconds,
        "external apply lease expiry",
    )
    apply_return_code, apply_timed_out = run_terraform_supervised(
        [
            "/proc/1/fd/194",
            f"-chdir={terraform_root}",
            "apply",
            "-input=false",
            "-auto-approve",
            "/proc/1/fd/197",
        ],
        public_fds,
        environment,
        APPLY_TIMEOUT_SECONDS,
        "exact sealed Terraform apply",
    )
    # Only after the complete descendant fence is empty may two new provider
    # refreshes establish remote-operation settlement. Both must converge on
    # the exact known-after projection of every authorized managed object and
    # show no remaining managed action. A timeout/nonzero result is therefore
    # either a proved-safe failure or an indeterminate stop; it is never called
    # settled merely because Terraform's parent process exited.
    settlement = prove_provider_settlement(
        terraform_root, args.stage, environment, public_fds
    )
    # Reconstruct the complete retained inventory after authoritative provider
    # settlement. No old plan is replayable: the retained generation handoff
    # forces a fresh nonce and plan from newly observed state before any resume.
    post_apply = run_json(
        verifier,
        public_fds,
        environment,
        "post-apply acknowledgement verifier",
        input_bytes=query_bytes,
    )
    require_fresh_until(
        claims["expires_at"],
        (MAX_ADMISSION_OBJECTS + 1) * KUBERNETES_READ_TIMEOUT_SECONDS,
        "post-apply capsule attestation expiry",
    )
    verify_live_identity(claims, capsule, embedded_kubeconfig, base_environment)
    require_fresh_until(
        claims["expires_at"], 0, "post-apply capsule attestation expiry"
    )
    require_fresh_until(
        post_apply.get("lease_expires_at"), 0, "post-apply lease expiry"
    )
    if apply_timed_out:
        raise AuthorizedApplyV4Error(
            "bounded Terraform apply timed out; descendant and authoritative "
            "provider settlement fences completed, and a fresh generation is required"
        )
    if apply_return_code != 0:
        raise AuthorizedApplyV4Error(
            "exact sealed Terraform apply failed; descendant and authoritative "
            "provider settlement fences completed, and a fresh generation is required"
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
        "planned_object_postconditions_sha256": settlement[
            "planned_object_postconditions_sha256"
        ],
        "refreshed_state_sha256": settlement[
            "authoritative_refreshed_state_sha256"
        ],
        "settlement_plan_a_sha256": settlement["first_settlement_plan_sha256"],
        "settlement_plan_b_sha256": settlement["second_settlement_plan_sha256"],
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
