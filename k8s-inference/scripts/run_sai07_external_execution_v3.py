#!/usr/bin/env python3
"""Execute one externally authorized SAI-07 gate and seal its acknowledgement.

This entrypoint is intentionally unreachable while the repository v3 trust
lock is blocked.  Once a later reviewed commit pins real external custody
facts, it performs the phase receipt/ledger operation under the bounded receipt
identity, re-verifies every Terraform-retained custody object under the
separate owner identity, and server-side applies exactly one new immutable,
generation-addressed acknowledgement ConfigMap.

The external field manager owns no field on any Terraform-retained object.
Those objects are read immediately before and after the acknowledgement create
and must retain identical UID, resourceVersion, and canonical full-object hash.
The acknowledgement name must be absent from both raw platform state and the
live API.  An exact, already-created acknowledgement can be resumed after a
crash; it is never patched, overwritten, or deleted.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import audit_sai07_effective_authority_v2 as authority_audit
import run_sai07_retained_state_custody_v3 as preflight
import sai07_authoritative_evidence as evidence
import sai07_custody_state_semantics as state_semantics
import sai07_epoch_admission_v4 as epoch_admission
import sai07_saved_plan_contract as saved_plan
from sai07_owner_secret_transport_v3 import OwnerApi, OwnerTransportError, validate_owner_token
import verify_sai07_custody_manifest_bundle as bundle_v1
import verify_sai07_custody_manifest_bundle_v2 as bundle_v2
import verify_sai07_custody_trust_v3 as trust_v3

ROOT = Path(__file__).resolve().parents[1]
TRUST_LOCK = Path("/opt/fs2-sai07/contracts/custody-trust-lock-v3.json")
CAPSULE_CONTRACT = Path(
    "/opt/fs2-sai07/contracts/execution-capsule-contract-v3.json"
)
CAPSULE_PLATFORM_AUTHORITY = Path(
    "/opt/fs2-sai07/contracts/platform-authority-contract-v3.json"
)
SOURCE_LOCK = ROOT / "stages" / "pod-security-custody" / "custody-source-lock-v3.json"
SCHEMA = "fs2-serve.nebius.ai/sai07-external-execution-acknowledgement/v3"
INTENT_SCHEMA = "fs2-serve.nebius.ai/sai07-external-execution-intent/v3"
PHASE_RE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
ZERO_SHA256 = "0" * 64
V4_TRUST_LOCK = Path("/proc/1/fd/181")
V4_CAPSULE_CONTRACT = Path("/proc/1/fd/180")
V4_PLATFORM_AUTHORITY = Path("/proc/1/fd/182")
V4_SOURCE_LOCK = Path("/proc/1/fd/183")
V4_RUNTIME_ATTESTATION = Path("/proc/1/fd/184")
V4_EPOCH_ADMISSION = Path("/proc/1/fd/188")
V4_IMAGE_PROVENANCE = Path("/proc/1/fd/189")
V4_IMAGE_SBOM = Path("/proc/1/fd/200")


class ExecutionV3Error(ValueError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def read_regular(path: Path, label: str, maximum: int) -> bytes:
    if not path.is_absolute() or ".." in path.parts:
        raise ExecutionV3Error(f"{label} path must be absolute without traversal")
    capsule_fd = re.fullmatch(r"/proc/1/fd/(?:18[0-9]|19[0-9]|20[0-2])", str(path)) is not None
    descriptor = os.open(
        path, os.O_RDONLY | os.O_CLOEXEC | (0 if capsule_fd else os.O_NOFOLLOW)
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise ExecutionV3Error(f"{label} is not a bounded regular file")
        if capsule_fd and fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) & (
            fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
        ) != (
            fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
        ):
            raise ExecutionV3Error(f"{label} capsule descriptor is not write sealed")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(payload) != before.st_size or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ExecutionV3Error(f"{label} changed during its descriptor-fenced read")
        return payload
    finally:
        os.close(descriptor)


def load_canonical(path: Path, label: str, maximum: int) -> tuple[bytes, dict[str, Any]]:
    payload = read_regular(path, label, maximum)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExecutionV3Error(f"{label} is not JSON") from error
    if not isinstance(value, dict) or canonical(value) != payload:
        raise ExecutionV3Error(f"{label} must be a canonical JSON object")
    return payload, value


def write_exclusive(path: Path, payload: bytes, maximum: int) -> None:
    if not path.is_absolute() or ".." in path.parts or len(payload) > maximum:
        raise ExecutionV3Error("acknowledgement output path or size violates the contract")
    candidate = path.with_name(
        f".{path.name}.retained-{hashlib.sha256(payload).hexdigest()}-{secrets.token_hex(16)}"
    )
    descriptor = os.open(
        candidate,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise ExecutionV3Error("acknowledgement write made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    try:
        # The completed candidate is retained deliberately. Fsync its directory
        # entry before the no-replace hard-link promotion so a crash can never
        # expose a short final file or require deletion for recovery.
        os.fsync(directory)
        try:
            os.link(candidate, path, follow_symlinks=False)
        except FileExistsError:
            existing = read_regular(path, "existing acknowledgement", maximum)
            if existing != payload:
                raise ExecutionV3Error(
                    "acknowledgement output exists with different bytes"
                )
        os.fsync(directory)
    finally:
        os.close(directory)


def repository_contract() -> tuple[
    bytes,
    dict[str, Any],
    dict[str, Any],
    bytes,
    dict[str, Any],
    bytes,
    dict[str, Any],
]:
    # Retained predecessor parser for source archaeology only. The sealed v4
    # dispatcher never calls it; fail before opening any filesystem path so a
    # caller cannot revive the rejected self-referential v3 trust graph.
    raise ExecutionV3Error(
        "rejected filesystem-based v3 capsule entrypoint is permanently disabled"
    )
    contract_bytes, contract = trust_v3.load_json(
        TRUST_LOCK, "v3 trust lock", 1024 * 1024, repository_document=True
    )
    if contract.get("activation") != "active":
        raise ExecutionV3Error("repository-pinned external custody is not active")
    capsule_bytes, capsule = trust_v3.load_json(
        CAPSULE_CONTRACT,
        "execution capsule contract",
        1024 * 1024,
        repository_document=True,
    )
    capsule_sha256 = hashlib.sha256(capsule_bytes).hexdigest()
    if (
        capsule.get("activation") != "active"
        or capsule.get("schema")
        != "fs2-serve.nebius.ai/sai07-execution-capsule-contract/v3"
        or os.environ.get("FS2_SAI07_CAPSULE_CONTRACT_SHA256") != capsule_sha256
        or os.environ.get("FS2_SAI07_CAPSULE_IMAGE_DIGEST")
        != capsule.get("image", {}).get("digest")
    ):
        raise ExecutionV3Error("immutable execution capsule identity is absent or differs")
    executor = evidence.exact(
        contract.get("executor"),
        {
            "acknowledgement_max_bytes",
            "acknowledgement_name_prefix",
            "acknowledgement_namespace",
            "authority_audit_path",
            "authority_audit_sha256",
            "authorized_apply_path",
            "authorized_apply_sha256",
            "dependency_lock_sha256",
            "execution_capsule_contract_path",
            "execution_capsule_contract_sha256",
            "field_manager",
            "kubectl_cli_path",
            "kubectl_cli_sha256",
            "owner_token_audience",
            "owner_token_issuer",
            "owner_token_max_seconds",
            "platform_authority_contract_path",
            "platform_authority_contract_sha256",
            "secret_transport_path",
            "secret_transport_sha256",
            "source_path",
            "source_sha256",
            "terraform_cli_path",
            "terraform_cli_sha256",
            "terraform_cli_version",
            "verifier_path",
            "verifier_sha256",
        },
        "v3 executor pin",
    )
    if (
        executor["execution_capsule_contract_path"]
        != "stages/pod-security-custody/execution-capsule-contract-v3.json"
        or executor["execution_capsule_contract_sha256"] != capsule_sha256
    ):
        raise ExecutionV3Error("custody trust lock selects another execution capsule")
    source_relative = Path(evidence.nonempty(executor["source_path"], "executor source path"))
    apply_relative = Path(
        evidence.nonempty(executor["authorized_apply_path"], "authorized apply source path")
    )
    verifier_relative = Path(
        evidence.nonempty(executor["verifier_path"], "ack verifier path")
    )
    audit_relative = Path(
        evidence.nonempty(executor["authority_audit_path"], "authority audit path")
    )
    transport_relative = Path(
        evidence.nonempty(executor["secret_transport_path"], "Secret transport path")
    )
    platform_authority_relative = Path(
        evidence.nonempty(
            executor["platform_authority_contract_path"],
            "platform authority contract path",
        )
    )
    if (
        source_relative.is_absolute()
        or apply_relative.is_absolute()
        or verifier_relative.is_absolute()
        or audit_relative.is_absolute()
        or transport_relative.is_absolute()
        or platform_authority_relative.is_absolute()
        or ".." in source_relative.parts
        or ".." in apply_relative.parts
        or ".." in verifier_relative.parts
        or ".." in audit_relative.parts
        or ".." in transport_relative.parts
        or ".." in platform_authority_relative.parts
    ):
        raise ExecutionV3Error("executor source pins must be repository-relative without traversal")
    source = ROOT / source_relative
    authorized_apply = ROOT / apply_relative
    verifier = ROOT / verifier_relative
    audit = ROOT / audit_relative
    transport = ROOT / transport_relative
    platform_authority_path = ROOT / platform_authority_relative
    if source.resolve() != Path(__file__).resolve():
        raise ExecutionV3Error("repository contract selects another executor")
    for path, expected, label in (
        (source, executor["source_sha256"], "executor"),
        (authorized_apply, executor["authorized_apply_sha256"], "authorized apply"),
        (verifier, executor["verifier_sha256"], "ack verifier"),
        (audit, executor["authority_audit_sha256"], "authority audit"),
        (transport, executor["secret_transport_sha256"], "Secret transport"),
    ):
        actual = hashlib.sha256(
            read_regular(path, f"{label} source", 4 * 1024 * 1024)
        ).hexdigest()
        if actual != evidence.sha256(expected, f"{label} source SHA-256"):
            raise ExecutionV3Error(f"{label} differs from the repository-pinned source")
    dependency_file_bytes = read_regular(
        SOURCE_LOCK, "v3 custody source-lock bytes", 1024 * 1024
    )
    _dependency_document, dependency_lock = trust_v3.load_json(
        SOURCE_LOCK,
        "v3 custody source lock",
        1024 * 1024,
        repository_document=True,
    )
    if hashlib.sha256(dependency_file_bytes).hexdigest() != evidence.sha256(
        executor["dependency_lock_sha256"], "dependency source-lock SHA-256"
    ):
        raise ExecutionV3Error(
            "custody dependency source lock differs from the repository trust pin"
        )
    evidence.exact(
        dependency_lock,
        {"schema", "sources"},
        "v3 custody dependency source lock",
    )
    if dependency_lock["schema"] != "fs2-serve.nebius.ai/sai07-custody-source-lock/v3":
        raise ExecutionV3Error("custody dependency source-lock schema is unsupported")
    expected_dependencies = {
        "authoritative_evidence": "scripts/sai07_authoritative_evidence.py",
        "custody_manifest_v1": "scripts/verify_sai07_custody_manifest_bundle.py",
        "custody_manifest_v2": "scripts/verify_sai07_custody_manifest_bundle_v2.py",
        "custody_manifest_v3": "scripts/verify_sai07_custody_manifest_bundle_v3.py",
        "custody_preflight_v3": "scripts/run_sai07_retained_state_custody_v3.py",
        "custody_state_semantics": "scripts/sai07_custody_state_semantics.py",
        "custody_trust_v2": "scripts/verify_sai07_custody_trust.py",
        "custody_trust_v3": "scripts/verify_sai07_custody_trust_v3.py",
        "effective_authority_v1": "scripts/audit_sai07_effective_authority.py",
        "receipt_transition": "scripts/verify_pod_security_receipts.py",
        "saved_plan_contract": "scripts/sai07_saved_plan_contract.py",
        "secret_metadata_transport": "scripts/collect_sai07_secret_metadata.py",
    }
    sources = evidence.exact(
        dependency_lock["sources"],
        set(expected_dependencies),
        "v3 custody dependency sources",
    )
    for label, expected_path in expected_dependencies.items():
        pin = evidence.exact(sources[label], {"path", "sha256"}, f"{label} pin")
        if pin["path"] != expected_path:
            raise ExecutionV3Error(f"{label} source path differs from the closed contract")
        actual = hashlib.sha256(
            read_regular(ROOT / expected_path, f"{label} source", 4 * 1024 * 1024)
        ).hexdigest()
        if actual != evidence.sha256(pin["sha256"], f"{label} source SHA-256"):
            raise ExecutionV3Error(
                f"{label} differs from the repository-pinned dependency source"
            )
    capsule_runtime = evidence.exact(
        capsule.get("runtime"),
        {
            "apply_entrypoint",
            "external_executor_path",
            "filesystem",
            "kubectl_fd",
            "kubectl_path",
            "kubectl_sha256",
            "openssl_fd",
            "openssl_path",
            "openssl_sha256",
            "pid",
            "platform_kubeconfig_fd",
            "provider_bundle_path",
            "provider_bundle_sha256",
            "python_fd",
            "python_path",
            "python_sha256",
            "saved_plan_fd",
            "source_tree_path",
            "source_tree_sha256",
            "terraform_fd",
            "terraform_path",
            "terraform_sha256",
            "terraform_version",
            "verifier_path",
        },
        "execution capsule runtime",
    )
    if (
        capsule_runtime["filesystem"] != "read-only-oci-rootfs"
        or capsule_runtime["pid"] != 1
        or capsule_runtime["saved_plan_fd"] != 197
        or capsule_runtime["platform_kubeconfig_fd"] != 198
        or capsule_runtime["openssl_fd"] != 192
        or capsule_runtime["kubectl_fd"] != 193
        or capsule_runtime["terraform_fd"] != 194
        or os.environ.get("FS2_SAI07_OPENSSL_PATH") != "/proc/1/fd/192"
        or os.environ.get("FS2_SAI07_KUBECTL_PATH") != "/proc/1/fd/193"
    ):
        raise ExecutionV3Error("execution capsule descriptor contract differs")

    platform_authority_bytes = read_regular(
        CAPSULE_PLATFORM_AUTHORITY,
        "platform authority contract",
        32 * 1024 * 1024,
    )
    try:
        platform_authority = json.loads(platform_authority_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExecutionV3Error("platform authority contract is not JSON") from error
    if (
        not isinstance(platform_authority, dict)
        or platform_authority_bytes != canonical(platform_authority) + b"\n"
    ):
        raise ExecutionV3Error(
            "platform authority contract must be canonical JSON with one terminal LF"
        )
    if hashlib.sha256(platform_authority_bytes).hexdigest() != evidence.sha256(
        executor["platform_authority_contract_sha256"],
        "platform authority contract SHA-256",
    ):
        raise ExecutionV3Error("platform authority contract differs from its repository pin")
    evidence.exact(
        platform_authority,
        {
            "activation",
            "cluster_id",
            "exact_rule_closure",
            "exact_rule_closure_sha256",
            "execution_capsule_contract_sha256",
            "groups",
            "kube_context",
            "kube_system_uid",
            "namespace_inventory",
            "persistent_volume_names",
            "platform_kubeconfig_sha256",
            "schema",
            "username",
        },
        "platform authority contract",
    )
    if platform_authority["activation"] != "active":
        raise ExecutionV3Error("repository-pinned platform authority contract is blocked")
    if (
        platform_authority["schema"]
        != "fs2-serve.nebius.ai/sai07-platform-authority-contract/v3"
        or platform_authority["cluster_id"] != contract["expected"]["cluster_id"]
        or platform_authority["kube_system_uid"]
        != contract["expected"]["kube_system_uid"]
        or platform_authority["username"]
        != contract["expected"]["platform"]["username"]
        or platform_authority["username"]
        in {
            contract["expected"]["owner"]["username"],
            contract["expected"]["receipt_operator"]["username"],
        }
        or platform_authority["namespace_inventory"]
        != contract["expected"]["namespace_inventory"]
        or platform_authority["persistent_volume_names"]
        != contract["expected"]["persistent_volume_names"]
        or platform_authority["execution_capsule_contract_sha256"]
        != capsule_sha256
        or not isinstance(platform_authority["platform_kubeconfig_sha256"], str)
        or not re.fullmatch(
            r"[a-f0-9]{64}", platform_authority["platform_kubeconfig_sha256"]
        )
        or not isinstance(platform_authority["kube_context"], str)
        or not platform_authority["kube_context"]
        or not isinstance(platform_authority["groups"], list)
        or not all(
            isinstance(group, str) and group for group in platform_authority["groups"]
        )
        or platform_authority["groups"]
        != sorted(set(platform_authority["groups"]))
        or "system:authenticated" not in platform_authority["groups"]
        or not set(contract["expected"]["platform"]["group_ids"]).issubset(
            platform_authority["groups"]
        )
        or not set(platform_authority["groups"]).isdisjoint(
            set(contract["expected"]["owner"]["group_ids"])
            | set(contract["expected"]["receipt_operator"]["group_ids"])
        )
        or not isinstance(platform_authority["exact_rule_closure"], list)
        or hashlib.sha256(
            canonical(platform_authority["exact_rule_closure"])
        ).hexdigest()
        != evidence.sha256(
            platform_authority["exact_rule_closure_sha256"],
            "platform exact-rule closure SHA-256",
        )
    ):
        raise ExecutionV3Error("platform authority contract differs from pinned custody facts")
    return (
        contract_bytes,
        contract,
        executor,
        platform_authority_bytes,
        platform_authority,
        capsule_bytes,
        capsule_runtime,
    )


def repository_contract_v4() -> tuple[
    bytes,
    dict[str, Any],
    dict[str, Any],
    bytes,
    dict[str, Any],
    bytes,
    dict[str, Any],
    dict[str, Any],
]:
    """Load only descriptor-sealed contracts from the attested v4 capsule."""

    contract_bytes, contract = trust_v3.load_json(
        V4_TRUST_LOCK, "v3 trust lock", 1024 * 1024, repository_document=True
    )
    capsule_bytes, capsule = trust_v3.load_json(
        V4_CAPSULE_CONTRACT,
        "v4 execution capsule contract",
        1024 * 1024,
        repository_document=True,
    )
    if contract.get("activation") != "active" or capsule.get("activation") != "active":
        raise ExecutionV3Error("external custody or execution capsule is blocked")
    if capsule.get("schema") != "fs2-serve.nebius.ai/sai07-execution-capsule-contract/v4":
        raise ExecutionV3Error("execution capsule schema differs")
    capsule_sha256 = hashlib.sha256(capsule_bytes).hexdigest()
    if os.environ.get("FS2_SAI07_CAPSULE_CONTRACT_SHA256") != capsule_sha256:
        raise ExecutionV3Error("execution capsule digest differs from bootstrap")
    capsule_runtime = evidence.exact(
        capsule.get("runtime"),
        {
            "authority_keys",
            "cluster_ca_fd",
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
        "v4 capsule runtime",
    )
    runtime_files = evidence.exact(
        capsule_runtime["runtime_files"],
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
        "v4 runtime files",
    )
    if (
        capsule_runtime["filesystem"] != "observed-read-only-mounts"
        or capsule_runtime["saved_plan_fd"] != 197
        or capsule_runtime["platform_kubeconfig_fd"] != 198
        or capsule_runtime["cluster_ca_fd"] != 199
        or runtime_files["source_bundle"].get("fd") != 190
        or runtime_files["python"].get("fd") != 191
        or runtime_files["openssl"].get("fd") != 192
        or runtime_files["kubectl"].get("fd") != 193
        or runtime_files["terraform"].get("fd") != 194
        or runtime_files["terraform_cli_config"].get("fd") != 195
        or runtime_files["image_provenance"].get("fd") != 189
        or runtime_files["image_sbom"].get("fd") != 200
        or os.environ.get("FS2_SAI07_SOURCE_BUNDLE_SHA256")
        != runtime_files["source_bundle"].get("sha256")
        or os.environ.get("FS2_SAI07_OPENSSL_PATH") != "/proc/1/fd/192"
        or os.environ.get("FS2_SAI07_KUBECTL_PATH") != "/proc/1/fd/193"
    ):
        raise ExecutionV3Error("v4 capsule descriptor contract differs")
    executor = evidence.exact(
        contract.get("executor"),
        {
            "acknowledgement_max_bytes",
            "acknowledgement_name_prefix",
            "acknowledgement_namespace",
            "dependency_lock_sha256",
            "field_manager",
            "kubectl_cli_path",
            "kubectl_cli_sha256",
            "owner_token_audience",
            "owner_token_issuer",
            "owner_token_max_seconds",
            "platform_authority_contract_path",
            "platform_authority_contract_sha256",
            "source_bundle_sha256",
            "terraform_cli_path",
            "terraform_cli_sha256",
            "terraform_cli_version",
        },
        "v4 executor pin",
    )
    if (
        executor["source_bundle_sha256"] != runtime_files["source_bundle"]["sha256"]
        or executor["kubectl_cli_path"] != "/proc/1/fd/193"
        or executor["kubectl_cli_sha256"] != runtime_files["kubectl"]["sha256"]
        or executor["terraform_cli_path"] != "/proc/1/fd/194"
        or executor["terraform_cli_sha256"] != runtime_files["terraform"]["sha256"]
        or executor["terraform_cli_version"] != capsule_runtime["terraform_version"]
        or executor["owner_token_audience"] != "https://kubernetes.default.svc"
        or executor["owner_token_max_seconds"] != 600
        or not evidence.nonempty(executor["owner_token_issuer"], "owner token issuer")
    ):
        raise ExecutionV3Error("trust lock differs from the sealed v4 capsule")
    authority_keys = evidence.exact(
        capsule_runtime["authority_keys"],
        {"backend", "manifest", "provider"},
        "v4 authority keys",
    )
    for role, descriptor in (("manifest", 185), ("provider", 186), ("backend", 187)):
        authority = evidence.exact(
            contract.get("authorities", {}).get(
                "provider_receipt" if role == "provider" else "backend_receipt" if role == "backend" else "manifest"
            ),
            {
                "key_id",
                "principal_id",
                "protected_resource_ids",
                "public_key_path",
                "public_key_sha256",
                "signing_resource_id",
            },
            f"{role} authority",
        )
        if (
            authority["public_key_path"] != f"/proc/1/fd/{descriptor}"
            or authority["public_key_sha256"] != authority_keys[role]["sha256"]
            or authority_keys[role]["fd"] != descriptor
        ):
            raise ExecutionV3Error(f"{role} authority key is not descriptor sealed")
    source_lock_bytes = read_regular(
        V4_SOURCE_LOCK, "sealed source lock", 1024 * 1024
    )
    if hashlib.sha256(source_lock_bytes).hexdigest() != executor["dependency_lock_sha256"]:
        raise ExecutionV3Error("sealed source lock differs from trust lock")
    epoch_contract_bytes = read_regular(
        V4_EPOCH_ADMISSION, "sealed epoch admission contract", 1024 * 1024
    )
    try:
        epoch_contract = json.loads(epoch_contract_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExecutionV3Error("epoch admission contract is not JSON") from error
    if epoch_contract_bytes != canonical(epoch_contract) + b"\n":
        raise ExecutionV3Error("epoch admission contract is not canonical")
    contract_files = capsule_runtime["contract_files"]
    epoch_pin = contract_files.get("epoch_admission") if isinstance(contract_files, dict) else None
    if (
        not isinstance(epoch_pin, dict)
        or epoch_pin.get("fd") != 188
        or epoch_pin.get("sha256") != hashlib.sha256(epoch_contract_bytes).hexdigest()
    ):
        raise ExecutionV3Error("epoch admission contract differs from capsule pin")
    try:
        epoch_admission.render(epoch_contract)
    except epoch_admission.EpochAdmissionError as error:
        raise ExecutionV3Error("epoch admission contract is invalid") from error
    contract["_epoch_admission"] = epoch_contract
    contract["_epoch_admission_sha256"] = hashlib.sha256(
        epoch_contract_bytes
    ).hexdigest()
    platform_authority_bytes = read_regular(
        V4_PLATFORM_AUTHORITY, "sealed platform authority", 32 * 1024 * 1024
    )
    if hashlib.sha256(platform_authority_bytes).hexdigest() != executor[
        "platform_authority_contract_sha256"
    ]:
        raise ExecutionV3Error("platform authority differs from trust lock")
    platform_authority = json.loads(platform_authority_bytes)
    if (
        not isinstance(platform_authority, dict)
        or platform_authority_bytes != canonical(platform_authority) + b"\n"
        or platform_authority.get("activation") != "active"
    ):
        raise ExecutionV3Error("platform authority is blocked or noncanonical")
    evidence.exact(
        platform_authority,
        {
            "activation",
            "cluster_id",
            "exact_rule_closure",
            "exact_rule_closure_sha256",
            "groups",
            "kube_context",
            "kube_system_uid",
            "namespace_inventory",
            "persistent_volume_names",
            "platform_kubeconfig_sha256",
            "schema",
            "username",
        },
        "platform authority contract",
    )
    if (
        platform_authority["schema"]
        != "fs2-serve.nebius.ai/sai07-platform-authority-contract/v3"
        or platform_authority["cluster_id"] != contract["expected"]["cluster_id"]
        or platform_authority["kube_system_uid"]
        != contract["expected"]["kube_system_uid"]
        or platform_authority["username"]
        != contract["expected"]["platform"]["username"]
        or platform_authority["namespace_inventory"]
        != contract["expected"]["namespace_inventory"]
        or platform_authority["persistent_volume_names"]
        != contract["expected"]["persistent_volume_names"]
        or hashlib.sha256(
            canonical(platform_authority["exact_rule_closure"])
        ).hexdigest()
        != platform_authority["exact_rule_closure_sha256"]
    ):
        raise ExecutionV3Error("platform authority differs from pinned custody facts")
    attestation_bytes, attestation = trust_v3.load_json(
        V4_RUNTIME_ATTESTATION,
        "sealed runtime attestation",
        8 * 1024 * 1024,
        repository_document=True,
    )
    if hashlib.sha256(attestation_bytes).hexdigest() != os.environ.get(
        "FS2_SAI07_RUNTIME_ATTESTATION_SHA256"
    ):
        raise ExecutionV3Error("runtime attestation differs from bootstrap")
    claims = attestation.get("claims")
    api = claims.get("api") if isinstance(claims, dict) else None
    if (
        not isinstance(api, dict)
        or set(api) != {"ca_sha256", "context", "origin"}
        or os.environ.get("FS2_SAI07_OWNER_API_SERVER") != api["origin"]
        or os.environ.get("FS2_SAI07_OWNER_CA_PATH") != "/proc/1/fd/199"
        or hashlib.sha256(
            read_regular(Path("/proc/1/fd/199"), "sealed cluster CA", 8 * 1024 * 1024)
        ).hexdigest()
        != api["ca_sha256"]
    ):
        raise ExecutionV3Error("owner API endpoint or CA differs from signed attestation")
    return (
        contract_bytes,
        contract,
        executor,
        platform_authority_bytes,
        platform_authority,
        capsule_bytes,
        capsule,
        capsule_runtime,
    )


def full_snapshot(
    reader: OwnerApi, bundle: dict[str, Any], *, label: str
) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for index, raw in enumerate(bundle.get("objects", [])):
        entry = evidence.exact(
            raw,
            {"manifest", "live_identity", "state_address"},
            f"objects[{index}]",
        )
        manifest = entry["manifest"]
        metadata = manifest.get("metadata", {})
        identity = (
            manifest.get("apiVersion"),
            manifest.get("kind"),
            metadata.get("namespace", ""),
            metadata.get("name"),
        )
        if not all(
            isinstance(item, str) and (item or position == 2)
            for position, item in enumerate(identity)
        ):
            raise ExecutionV3Error("manifest bundle contains an invalid live identity")
        expected = entry["live_identity"]
        # The anchor is deliberately outside platform Terraform state. It is
        # observed only through metadata-only content negotiation below; a
        # generic object GET could retrieve Secret payload bytes.
        if (
            identity[0:3] == ("v1", "Secret", "fs2-system")
            and re.fullmatch(
                r"fs2-pod-security-token-anchor-v4-[a-f0-9]{64}", identity[3]
            )
        ):
            continue
        live = reader.raw(
            bundle_v2.api_path(identity), allow_absent=expected.get("present") is False
        )
        if expected.get("present") is False:
            raise ExecutionV3Error(
                "Terraform-retained inventory may not contain absent objects"
            )
        else:
            if live is None:
                raise ExecutionV3Error(f"{label} lost a retained object: {'/'.join(identity)}")
            try:
                state_semantics.assert_live_matches_manifest(manifest, live)
            except state_semantics.StateSemanticsError as error:
                raise ExecutionV3Error(
                    f"{label} secure desired manifest differs: {'/'.join(identity)}"
                ) from error
            live_metadata = live.get("metadata", {})
            observed = {
                "api_path": bundle_v2.api_path(identity),
                "identity": list(identity),
                "object_sha256": hashlib.sha256(canonical(live)).hexdigest(),
                "present": True,
                "resource_version": live_metadata.get("resourceVersion"),
                "state_address": entry["state_address"],
                "uid": live_metadata.get("uid"),
            }
            signed = {
                "object_sha256": expected.get("object_sha256"),
                "present": True,
                "resource_version": expected.get("resource_version"),
                "uid": expected.get("uid"),
            }
            if {key: observed[key] for key in signed} != signed:
                raise ExecutionV3Error(
                    f"{label} object differs from its signed identity: {'/'.join(identity)}"
                )
        snapshots.append(observed)
    snapshots.sort(key=canonical)
    return snapshots


def pod_security_projection(pod: dict[str, Any]) -> dict[str, Any]:
    metadata = pod.get("metadata")
    spec = pod.get("spec")
    if not isinstance(metadata, dict) or not isinstance(spec, dict):
        raise ExecutionV3Error("external capsule Pod is malformed")
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


def validate_image_evidence(
    image: dict[str, Any], pod_claim: dict[str, Any], capsule: dict[str, Any]
) -> None:
    digest = image.get("digest")
    reference = image.get("reference")
    image_id = pod_claim.get("image_id")
    resolved = (
        re.search(r"(?:@|://)(sha256:[a-f0-9]{64})$", image_id)
        if isinstance(image_id, str)
        else None
    )
    if (
        not isinstance(digest, str)
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest)
        or pod_claim.get("image_digest") != digest
        or not isinstance(reference, str)
        or reference.count("@") != 1
        or reference.rsplit("@", 1)[1] != digest
        or resolved is None
        or resolved.group(1) != digest
        or any(
            not isinstance(image[field], str)
            or not re.fullmatch(r"[a-f0-9]{64}", image[field])
            or image[field] == ZERO_SHA256
            for field in ("provenance_sha256", "sbom_sha256")
        )
    ):
        raise ExecutionV3Error(
            "signed image reference, resolved image ID, and digest are not cross-bound"
        )
    runtime_files = capsule["runtime"]["runtime_files"]
    if (
        runtime_files["image_provenance"].get("sha256")
        != image["provenance_sha256"]
        or runtime_files["image_sbom"].get("sha256") != image["sbom_sha256"]
    ):
        raise ExecutionV3Error("image evidence differs from the capsule digest pins")
    provenance_bytes, provenance = load_canonical(
        V4_IMAGE_PROVENANCE, "image provenance", 8 * 1024 * 1024
    )
    if hashlib.sha256(provenance_bytes).hexdigest() != image["provenance_sha256"]:
        raise ExecutionV3Error("image provenance differs from its signed digest")
    provenance = evidence.exact(
        provenance,
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
    provenance_image = evidence.exact(
        provenance["image"], {"digest", "reference"}, "provenance image"
    )
    if (
        provenance["schema"] != "fs2-serve.nebius.ai/image-provenance/v1"
        or provenance["predicate_type"] != "https://slsa.dev/provenance/v1"
        or not evidence.nonempty(provenance["builder_id"], "provenance builder")
        or not evidence.nonempty(provenance["build_type"], "provenance build type")
        or evidence.sha256(
            provenance["materials_sha256"], "provenance materials SHA-256"
        )
        == ZERO_SHA256
        or provenance_image != {"digest": digest, "reference": reference}
    ):
        raise ExecutionV3Error(
            "image provenance is incomplete or selects another image"
        )
    sbom_bytes, sbom = load_canonical(
        V4_IMAGE_SBOM, "image SBOM", 8 * 1024 * 1024
    )
    if hashlib.sha256(sbom_bytes).hexdigest() != image["sbom_sha256"]:
        raise ExecutionV3Error("image SBOM differs from its signed digest")
    sbom = evidence.exact(
        sbom,
        {"document", "format", "image", "schema"},
        "image SBOM envelope",
    )
    sbom_image = evidence.exact(
        sbom["image"], {"digest", "reference"}, "SBOM image"
    )
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
        or not evidence.nonempty(document.get("name"), "SBOM name")
        or not isinstance(document.get("documentNamespace"), str)
        or not document["documentNamespace"].startswith("https://")
        or not isinstance(document.get("creationInfo"), dict)
        or not isinstance(described, list)
        or not described
        or not isinstance(packages, list)
        or not packages
        or any(
            not isinstance(value, str) or value not in package_ids
            for value in described
        )
    ):
        raise ExecutionV3Error("image SBOM is incomplete or selects another image")


def verify_external_capsule_live(
    owner_api: OwnerApi, capsule: dict[str, Any], capsule_bytes: bytes
) -> dict[str, str]:
    attestation_bytes, attestation = load_canonical(
        V4_RUNTIME_ATTESTATION, "external runtime attestation", 8 * 1024 * 1024
    )
    evidence.exact(attestation, {"claims", "signature"}, "external runtime attestation")
    claims = evidence.exact(
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
        "external runtime attestation claims",
    )
    if (
        claims["role"] != "external-ack"
        or claims["capsule_contract_sha256"]
        != hashlib.sha256(capsule_bytes).hexdigest()
        or hashlib.sha256(attestation_bytes).hexdigest()
        != os.environ.get("FS2_SAI07_RUNTIME_ATTESTATION_SHA256")
    ):
        raise ExecutionV3Error("external runtime attestation identity differs")
    if not all(
        isinstance(claims[field], str) and claims[field].endswith("Z")
        for field in ("issued_at", "expires_at")
    ):
        raise ExecutionV3Error("external runtime attestation time is malformed")
    try:
        issued = dt.datetime.fromisoformat(
            str(claims["issued_at"]).replace("Z", "+00:00")
        )
        expires = dt.datetime.fromisoformat(
            str(claims["expires_at"]).replace("Z", "+00:00")
        )
    except ValueError as error:
        raise ExecutionV3Error("external runtime attestation time is malformed") from error
    now = dt.datetime.now(dt.UTC)
    if (
        issued.tzinfo != dt.UTC
        or expires.tzinfo != dt.UTC
        or issued > now + dt.timedelta(seconds=30)
        or now > expires
        or expires <= issued
        or expires - issued > dt.timedelta(minutes=10)
    ):
        raise ExecutionV3Error("external runtime attestation is stale or overlong")
    admission_contract = evidence.exact(
        capsule.get("admission"),
        {
            "pod_security_contract",
            "pod_security_projection",
            "required_objects_by_role",
        },
        "v4 capsule admission contract",
    )
    profile = evidence.exact(
        admission_contract["pod_security_contract"],
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
        "v4 capsule Pod security contract",
    )
    required_by_role = evidence.exact(
        admission_contract["required_objects_by_role"],
        {"external-ack", "plan-apply"},
        "v4 capsule role admission objects",
    )
    required_paths = required_by_role["external-ack"]
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
    if (
        admission_contract["pod_security_projection"]
        != "canonical-v1-full-spec-and-security-metadata"
        or profile != expected_profile
        or not isinstance(required_paths, list)
        or not required_paths
        or len(required_paths) > 8
        or required_paths != sorted(set(required_paths))
    ):
        raise ExecutionV3Error("external capsule admission contract is incomplete")
    pod_claim = evidence.exact(
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
        "external capsule Pod identity",
    )
    image = evidence.exact(
        claims["image"],
        {"digest", "provenance_sha256", "reference", "sbom_sha256"},
        "external capsule image identity",
    )
    validate_image_evidence(image, pod_claim, capsule)
    if not all(
        isinstance(pod_claim[field], str) and pod_claim[field]
        for field in (
            "name",
            "namespace",
            "resource_version",
            "security_projection_sha256",
            "service_account_name",
            "uid",
        )
    ) or not all(
        re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?", pod_claim[field])
        for field in ("name", "namespace")
    ):
        raise ExecutionV3Error("external capsule Pod identity is incomplete")
    pod = owner_api.raw(
        f"/api/v1/namespaces/{pod_claim['namespace']}/pods/{pod_claim['name']}"
    )
    metadata = pod.get("metadata", {})
    spec = pod.get("spec", {})
    containers = [
        *spec.get("initContainers", []),
        *spec.get("containers", []),
        *spec.get("ephemeralContainers", []),
    ]
    named_containers = [
        item
        for item in spec.get("containers", [])
        if isinstance(item, dict)
        and item.get("name") == pod_claim["container_name"]
    ]
    named_statuses = [
        item
        for item in pod.get("status", {}).get("containerStatuses", [])
        if isinstance(item, dict)
        and item.get("name") == pod_claim["container_name"]
    ]
    pod_run_as_non_root = spec.get("securityContext", {}).get("runAsNonRoot") is True
    pod_seccomp = spec.get("securityContext", {}).get("seccompProfile", {}).get("type")
    if (
        pod.get("apiVersion") != "v1"
        or pod.get("kind") != "Pod"
        or metadata.get("uid") != pod_claim["uid"]
        or metadata.get("resourceVersion") != pod_claim["resource_version"]
        or spec.get("serviceAccountName") != pod_claim["service_account_name"]
        or spec.get("automountServiceAccountToken")
        is not profile["automount_service_account_token"]
        or any(spec.get(field) is True for field in ("hostNetwork", "hostPID", "hostIPC"))
        or spec.get("shareProcessNamespace") is True
        or not containers
        or len(named_containers) != 1
        or len(named_statuses) != 1
        or named_containers[0].get("image") != image["reference"]
        or named_statuses[0].get("imageID") != pod_claim["image_id"]
        or image["digest"] != pod_claim["image_digest"]
        or any(
            not isinstance(volume, dict) or "hostPath" in volume
            for volume in spec.get("volumes", [])
        )
        or hashlib.sha256(canonical(pod_security_projection(pod))).hexdigest()
        != evidence.sha256(
            pod_claim["security_projection_sha256"],
            "external Pod security projection SHA-256",
        )
    ):
        raise ExecutionV3Error("live external capsule Pod differs from attestation")
    for container in containers:
        if not isinstance(container, dict):
            raise ExecutionV3Error("external capsule container is malformed")
        security = container.get("securityContext", {})
        capabilities = security.get("capabilities", {})
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
                for port in container.get("ports", [])
            )
        ):
            raise ExecutionV3Error(
                "external capsule container violates the complete security profile"
            )
    admission = claims["admission_objects"]
    if not isinstance(admission, list):
        raise ExecutionV3Error("external attestation omits admission objects")
    observed_paths: list[str] = []
    for index, entry in enumerate(admission):
        item = evidence.exact(
            entry,
            {"api_path", "object_sha256", "resource_version", "uid"},
            f"external admission identity {index}",
        )
        if (
            not isinstance(item["api_path"], str)
            or not item["api_path"].startswith("/api")
            or not isinstance(item["uid"], str)
            or not item["uid"]
            or not isinstance(item["resource_version"], str)
            or not item["resource_version"]
        ):
            raise ExecutionV3Error("external admission identity is incomplete")
        observed_paths.append(item["api_path"])
        observed = owner_api.raw(item["api_path"])
        observed_metadata = observed.get("metadata", {})
        if (
            observed_metadata.get("uid") != item["uid"]
            or observed_metadata.get("resourceVersion") != item["resource_version"]
            or hashlib.sha256(canonical(observed)).hexdigest()
            != evidence.sha256(
                item["object_sha256"],
                f"external admission identity {index} SHA-256",
            )
        ):
            raise ExecutionV3Error(
                "live external admission object differs from attestation"
            )
    if observed_paths != required_paths:
        raise ExecutionV3Error(
            "external admission objects differ from the exact capsule set"
        )
    return {"name": pod_claim["name"], "namespace": pod_claim["namespace"]}


def run_owner_authority_audit(
    owner_api: OwnerApi,
    args: argparse.Namespace,
    trust: dict[str, str],
    owner_token_jti_sha256: str,
    capsule_pod_identity: dict[str, str],
) -> tuple[dict[str, Any], str]:
    try:
        audit = authority_audit.run(
            argparse.Namespace(
                bound_jti_sha256=owner_token_jti_sha256,
                capsule_pod_identity_json=canonical(capsule_pod_identity).decode(),
                cluster_id=trust["cluster_id"],
                context=None,
                expected_groups_json=trust["owner_groups_json"],
                expected_username=trust["owner_username"],
                kube_system_uid=trust["kube_system_uid"],
                kubeconfig=None,
                namespace_inventory_json=trust["namespace_inventory_json"],
                persistent_volume_names_json=trust["persistent_volume_names_json"],
                profile="external-executor",
            ),
            client=owner_api,
        )
    except authority_audit.AuditError as error:
        raise ExecutionV3Error("epoch-token effective-authority audit failed") from error
    if (
        not isinstance(audit, dict)
        or audit.get("schema") != "fs2-serve.nebius.ai/sai07-effective-authority-audit/v2"
        or audit.get("profile") != "external-executor"
        or audit.get("username") != trust["owner_username"]
        or audit.get("groups") != json.loads(trust["owner_groups_json"])
        or audit.get("authenticated_groups")
        != sorted(
            [*json.loads(trust["owner_groups_json"]), "system:authenticated"]
        )
        or audit.get("credential_jti_sha256") != owner_token_jti_sha256
    ):
        raise ExecutionV3Error("epoch-token authority audit differs from signed trust")
    projection = {key: value for key, value in audit.items() if key != "observed_at"}
    return audit, hashlib.sha256(canonical(projection)).hexdigest()


def state_omits_ack(
    state_path: Path,
    namespace: str,
    name: str,
    maximum: int,
    expected_sha256: str,
) -> None:
    payload = read_regular(state_path, "raw current platform state", maximum)
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ExecutionV3Error("raw current platform state changed after trust verification")
    try:
        state = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExecutionV3Error("raw current platform state is not JSON") from error
    def contains_identity(value: object) -> bool:
        if isinstance(value, dict):
            if value.get("name") == name and value.get("namespace", "") == namespace:
                return True
            return any(contains_identity(item) for item in value.values())
        if isinstance(value, list):
            return any(contains_identity(item) for item in value)
        return False

    for resource in state.get("resources", []) if isinstance(state, dict) else []:
        if not isinstance(resource, dict) or resource.get("mode") != "managed":
            continue
        for instance in resource.get("instances", []):
            if not isinstance(instance, dict):
                continue
            attributes = instance.get("attributes")
            if not isinstance(attributes, dict):
                continue
            if contains_identity(attributes):
                raise ExecutionV3Error(
                    "acknowledgement identity already exists in platform Terraform state"
                )


def validate_live_ack(
    value: dict[str, Any], desired: dict[str, Any], field_manager: str
) -> dict[str, str]:
    if value.get("apiVersion") != "v1" or value.get("kind") != "ConfigMap":
        raise ExecutionV3Error("live acknowledgement has another type")
    metadata = value.get("metadata")
    if not isinstance(metadata, dict):
        raise ExecutionV3Error("live acknowledgement omits metadata")
    desired_metadata = desired["metadata"]
    if (
        metadata.get("name") != desired_metadata["name"]
        or metadata.get("namespace") != desired_metadata["namespace"]
        or metadata.get("labels") != desired_metadata["labels"]
        or metadata.get("annotations") != desired_metadata["annotations"]
        or metadata.get("ownerReferences") not in (None, [])
        or metadata.get("finalizers") not in (None, [])
        or value.get("immutable") is not True
        or value.get("data") != desired["data"]
        or "binaryData" in value
    ):
        raise ExecutionV3Error(
            "live acknowledgement differs from the exact immutable SSA field set"
        )
    expected_fields_v1 = {
        "f:data": {".": {}, "f:execution.json": {}},
        "f:immutable": {},
        "f:metadata": {
            "f:annotations": {
                ".": {},
                **{
                    f"f:{key}": {}
                    for key in sorted(desired_metadata["annotations"])
                },
            },
            "f:labels": {
                ".": {},
                **{f"f:{key}": {} for key in sorted(desired_metadata["labels"])},
            },
        },
    }
    managed_fields = metadata.get("managedFields")
    if (
        not isinstance(managed_fields, list)
        or len(managed_fields) != 1
        or not isinstance(managed_fields[0], dict)
        or managed_fields[0].get("manager") != field_manager
        or managed_fields[0].get("operation") != "Apply"
        or managed_fields[0].get("apiVersion") != "v1"
        or managed_fields[0].get("fieldsType") != "FieldsV1"
        or managed_fields[0].get("subresource") not in (None, "")
        or managed_fields[0].get("fieldsV1") != expected_fields_v1
    ):
        raise ExecutionV3Error(
            "acknowledgement SSA field ownership is absent, shared, or differs from the exact field set"
        )
    uid = metadata.get("uid")
    resource_version = metadata.get("resourceVersion")
    if (
        not isinstance(uid, str)
        or not uid
        or not isinstance(resource_version, str)
        or not resource_version
    ):
        raise ExecutionV3Error("live acknowledgement lacks UID/resourceVersion")
    return {
        "object_sha256": hashlib.sha256(canonical(value)).hexdigest(),
        "resource_version": resource_version,
        "uid": uid,
    }


def ensure_epoch_admission(
    owner_api: OwnerApi,
    epoch_contract: dict[str, Any],
    field_manager: str,
) -> list[dict[str, str]]:
    """Create only absent generation policies; never patch a retained object."""

    try:
        manifests = epoch_admission.render(epoch_contract)
    except epoch_admission.EpochAdmissionError as error:
        raise ExecutionV3Error("epoch admission generation is invalid") from error
    identities: list[dict[str, str]] = []
    for desired in manifests:
        metadata = desired["metadata"]
        identity = (
            desired["apiVersion"],
            desired["kind"],
            "",
            metadata["name"],
        )
        path = bundle_v2.api_path(identity)
        live = owner_api.raw(path, allow_absent=True)
        if live is None:
            owner_api.server_side_apply(path, desired, field_manager)
            live = owner_api.raw(path)
        if live is None:
            raise ExecutionV3Error("epoch admission object is absent after additive SSA")
        live_metadata = live.get("metadata")
        if (
            live.get("apiVersion") != desired["apiVersion"]
            or live.get("kind") != desired["kind"]
            or not isinstance(live_metadata, dict)
            or live_metadata.get("name") != metadata["name"]
            or live_metadata.get("labels") != metadata["labels"]
            or live_metadata.get("annotations") != metadata["annotations"]
            or live_metadata.get("ownerReferences") not in (None, [])
            or live_metadata.get("finalizers") not in (None, [])
            or live.get("spec") != desired["spec"]
        ):
            raise ExecutionV3Error(
                "retained epoch admission object differs; update is forbidden"
            )
        uid = live_metadata.get("uid")
        resource_version = live_metadata.get("resourceVersion")
        if not all(
            isinstance(value, str) and value for value in (uid, resource_version)
        ):
            raise ExecutionV3Error("epoch admission identity is incomplete")
        identities.append(
            {
                "api_version": desired["apiVersion"],
                "kind": desired["kind"],
                "name": metadata["name"],
                "object_sha256": hashlib.sha256(canonical(live)).hexdigest(),
                "resource_version": resource_version,
                "uid": uid,
            }
        )
    identities.sort(key=canonical)
    return identities


def consume_phase_receipt(
    args: argparse.Namespace,
    trust: dict[str, str],
    context: dict[str, Any],
    kubectl_path: str,
    token_anchor_name: str,
    token_anchor_uid: str,
) -> tuple[str, str]:
    if args.phase == "prepare":
        if args.receipt_bundle is not None or args.receipt_query is not None:
            raise ExecutionV3Error("prepare may not carry a phase receipt")
        return ZERO_SHA256, ZERO_SHA256
    if args.receipt_bundle is None or args.receipt_query is None or args.receipt_kubeconfig is None:
        raise ExecutionV3Error(
            "post-prepare execution requires receipt bundle/query/operator credential"
        )
    bundle_bytes = read_regular(args.receipt_bundle, "phase receipt bundle", 32 * 1024 * 1024)
    query_bytes, query = load_canonical(args.receipt_query, "phase receipt query", 2 * 1024 * 1024)
    expected_mode = (
        f"{args.consumer_role}-acknowledgement"
        if args.action == "acknowledge"
        else ("owner-transition" if args.consumer_role == "owner" else "downstream-authorization")
    )
    if (
        query.get("mode") != expected_mode
        or query.get("expected_phase") != args.phase
        or query.get("receipt_path") != str(args.receipt_bundle)
        or query.get("expected_context") != context
    ):
        raise ExecutionV3Error("phase receipt query differs from the exact execution edge")
    environment = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "FS2_KUBECTL_PATH": kubectl_path,
        "FS2_KUBECONFIG": str(args.receipt_kubeconfig),
        "FS2_KUBE_CONTEXT": args.receipt_context,
        "FS2_POD_SECURITY_CUSTODY_USER": trust["receipt_username"],
        "FS2_POD_SECURITY_TOKEN_AUDIENCE": "https://kubernetes.default.svc",
        "FS2_POD_SECURITY_TOKEN_ANCHOR_NAME": token_anchor_name,
        "FS2_POD_SECURITY_TOKEN_ANCHOR_UID": token_anchor_uid,
        "FS2_POD_SECURITY_QUERY": query_bytes.decode(),
    }
    completed = subprocess.run(
        ["/proc/1/fd/191", "/proc/1/fd/190", "verify-receipt"],
        check=False,
        capture_output=True,
        env=environment,
        pass_fds=(190, 191, 192, 193),
        timeout=600,
    )
    if completed.returncode != 0:
        raise ExecutionV3Error("external receipt operator rejected or failed the phase transition")
    try:
        result = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExecutionV3Error("receipt operator returned invalid JSON") from error
    if not isinstance(result, dict) or result.get("valid") != "true":
        raise ExecutionV3Error("receipt operator did not authorize the exact phase edge")
    return hashlib.sha256(bundle_bytes).hexdigest(), hashlib.sha256(canonical(result)).hexdigest()


def sign(unsigned: dict[str, Any], private_key_fd: int) -> dict[str, str]:
    openssl_path = os.environ.get("FS2_SAI07_OPENSSL_PATH")
    if openssl_path != "/proc/1/fd/192":
        raise ExecutionV3Error(
            "acknowledgement signing requires the immutable capsule OpenSSL descriptor"
        )
    if private_key_fd < 3:
        raise ExecutionV3Error(
            "execution signing key must arrive on a dedicated inherited descriptor"
        )
    before = os.fstat(private_key_fd)
    if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > 65536:
        raise ExecutionV3Error("execution signing key descriptor is not a bounded regular object")
    payload_fd = os.memfd_create("fs2-sai07-execution-ack", flags=0)
    try:
        payload = canonical(unsigned)
        if os.write(payload_fd, payload) != len(payload):
            raise ExecutionV3Error("execution acknowledgement memfd write was short")
        os.fsync(payload_fd)
        os.lseek(payload_fd, 0, os.SEEK_SET)
        completed = subprocess.run(
            [
                openssl_path,
                "pkeyutl",
                "-sign",
                "-inkey",
                f"/proc/self/fd/{private_key_fd}",
                "-rawin",
                "-in",
                f"/proc/self/fd/{payload_fd}",
            ],
            check=False,
            capture_output=True,
            timeout=10,
            pass_fds=(private_key_fd, payload_fd),
        )
    finally:
        os.close(payload_fd)
    after = os.fstat(private_key_fd)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ExecutionV3Error("execution signing key changed during descriptor-fenced use")
    if completed.returncode != 0 or len(completed.stdout) != 64:
        raise ExecutionV3Error("Ed25519 execution acknowledgement signing failed")
    return {
        "algorithm": "ed25519",
        "key_id": unsigned["authority_key_id"],
        "value": base64.b64encode(completed.stdout).decode(),
    }


def execute(args: argparse.Namespace) -> dict[str, Any]:
    (
        contract_bytes,
        contract,
        executor,
        platform_authority_bytes,
        platform_authority,
        capsule_bytes,
        capsule,
        capsule_runtime,
    ) = repository_contract_v4()
    if args.phase != "prepare" and not PHASE_RE.fullmatch(args.phase):
        raise ExecutionV3Error("phase is malformed")
    if args.action not in {"authorize", "acknowledge"} or args.consumer_role not in {
        "owner",
        "downstream",
    }:
        raise ExecutionV3Error("action or consumer role is unsupported")
    context_bytes, context = load_canonical(
        args.expected_context, "expected rollout context", 8 * 1024 * 1024
    )
    if context.get("cluster_id") != contract["expected"]["cluster_id"] or context.get(
        "kube_system_uid"
    ) != contract["expected"]["kube_system_uid"]:
        raise ExecutionV3Error("rollout context differs from repository-pinned cluster identity")

    prepared = preflight.prepare(args)
    if (
        prepared["platform_plan_contract"]["rollout_phase"] != args.phase
        or prepared["platform_plan_contract"]["custody_epoch_sha256"]
        != prepared["custody_epoch_sha256"]
        or prepared["platform_plan_contract"]["external_handoff_path_sha256"]
        != hashlib.sha256(str(args.ack_output).encode()).hexdigest()
        or prepared["platform_plan_contract"]["platform_kube_context"]
        != platform_authority["kube_context"]
        or prepared["platform_plan_contract"]["platform_kubeconfig_path"]
        != "/proc/1/fd/198"
        or prepared["platform_plan_contract"]["platform_kubeconfig_sha256"]
        != platform_authority["platform_kubeconfig_sha256"]
        or prepared["platform_plan_contract"]["execution_capsule_contract_sha256"]
        != hashlib.sha256(capsule_bytes).hexdigest()
        or prepared["platform_plan_contract"][
            "execution_external_runtime_attestation_sha256"
        ]
        != os.environ["FS2_SAI07_RUNTIME_ATTESTATION_SHA256"]
    ):
        raise ExecutionV3Error(
            "saved platform plan phase, epoch, handoff, capsule, or platform transport differs"
        )
    trust = preflight.run_verifier(
        "verify_sai07_custody_trust_v3.py", preflight.trust_query(args, "current")
    )
    for prepared_field, trust_field in (
        ("collection_id", "collection_id"),
        ("contract_sha256", "contract_sha256"),
        ("platform_state_addresses_sha256", "state_addresses_sha256"),
        ("platform_state_objects_sha256", "state_objects_sha256"),
        ("platform_state_lineage", "state_lineage"),
        ("platform_state_serial", "state_serial"),
        ("platform_state_version", "state_object_version"),
    ):
        if prepared[prepared_field] != trust[trust_field]:
            raise ExecutionV3Error("authoritative trust changed after preflight verification")
    manifest_bytes, manifest_bundle = load_canonical(
        args.manifest_bundle, "manifest bundle", 32 * 1024 * 1024
    )
    if hashlib.sha256(manifest_bytes).hexdigest() != prepared["manifest_bundle_sha256"]:
        raise ExecutionV3Error("manifest bundle changed after preflight verification")
    owner_api_server = os.environ.get("FS2_SAI07_OWNER_API_SERVER")
    owner_ca_path = os.environ.get("FS2_SAI07_OWNER_CA_PATH")
    if not owner_api_server or owner_ca_path != "/proc/1/fd/199":
        raise ExecutionV3Error("attested owner API endpoint/CA descriptors are absent")
    owner_api = OwnerApi(owner_api_server, Path(owner_ca_path), args.owner_token_fd)
    owner_token_jti_sha256, owner_token_jti = validate_owner_token(
        owner_api.token,
        trust["owner_username"],
        trust["custody_epoch_principal_id"],
        executor["owner_token_issuer"],
    )
    if (
        executor["owner_token_audience"] != "https://kubernetes.default.svc"
        or executor["owner_token_max_seconds"] != 600
        or not evidence.nonempty(executor["owner_token_issuer"], "owner token issuer")
    ):
        raise ExecutionV3Error(
            "repository owner-token boundary differs from the reviewed contract"
        )
    token_review = owner_api.self_subject_review()
    token_user_info = token_review.get("status", {}).get("userInfo", {})
    token_extra = token_user_info.get("extra", {}) if isinstance(token_user_info, dict) else {}
    token_groups = sorted(token_user_info.get("groups", [])) if isinstance(
        token_user_info, dict
    ) else []
    provider_groups = [
        group for group in token_groups if group != "system:authenticated"
    ]
    if (
        not isinstance(token_user_info, dict)
        or token_user_info.get("username") != trust["owner_username"]
        or "system:authenticated" not in token_groups
        or provider_groups != json.loads(trust["owner_groups_json"])
        or not isinstance(token_extra, dict)
        or token_extra.get("authentication.kubernetes.io/credential-id")
        != [f"JTI={owner_token_jti}"]
    ):
        raise ExecutionV3Error(
            "epoch token does not authenticate the signed owner identity and JTI"
        )
    epoch_contract = contract.get("_epoch_admission")
    current_epoch = epoch_contract.get("current") if isinstance(epoch_contract, dict) else None
    if (
        not isinstance(current_epoch, dict)
        or current_epoch.get("epoch_sha256") != prepared["custody_epoch_sha256"]
        or current_epoch.get("owner_username") != trust["owner_username"]
        or current_epoch.get("receipt_username") != trust["receipt_username"]
    ):
        raise ExecutionV3Error(
            "epoch admission current identities differ from authoritative custody"
        )

    receipt_bundle_sha256 = ZERO_SHA256
    if args.receipt_bundle is not None:
        receipt_bundle_sha256 = hashlib.sha256(
            read_regular(
                args.receipt_bundle,
                "phase receipt bundle",
                32 * 1024 * 1024,
            )
        ).hexdigest()

    generation = hashlib.sha256(
        canonical(
            {
                "action": args.action,
                "collection_id": prepared["collection_id"],
                "consumer": args.consumer_role,
                "context_sha256": hashlib.sha256(context_bytes).hexdigest(),
                "custody_epoch_sha256": prepared["custody_epoch_sha256"],
                "execution_capsule_contract_sha256": hashlib.sha256(
                    capsule_bytes
                ).hexdigest(),
                "execution_runtime_attestation_sha256": os.environ[
                    "FS2_SAI07_RUNTIME_ATTESTATION_SHA256"
                ],
                "execution_plan_runtime_attestation_sha256": prepared[
                    "platform_plan_contract"
                ]["execution_runtime_attestation_sha256"],
                "execution_source_bundle_sha256": executor["source_bundle_sha256"],
                "epoch_admission_contract_sha256": contract[
                    "_epoch_admission_sha256"
                ],
                "manifest_bundle_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "owner_token_jti_sha256": owner_token_jti_sha256,
                "phase": args.phase,
                "platform_authority_contract_sha256": hashlib.sha256(
                    platform_authority_bytes
                ).hexdigest(),
                "platform_plan_contract_sha256": prepared[
                    "platform_plan_contract_sha256"
                ],
                "receipt_bundle_sha256": receipt_bundle_sha256,
            }
        )
    ).hexdigest()
    name = f"{executor['acknowledgement_name_prefix']}{generation[:24]}"
    namespace = executor["acknowledgement_namespace"]
    state_omits_ack(
        args.current_platform_state,
        namespace,
        name,
        contract["collector"]["max_state_bytes"],
        trust["state_sha256"],
    )

    kube_system = owner_api.raw("/api/v1/namespaces/kube-system")
    if (
        kube_system is None
        or kube_system.get("metadata", {}).get("uid") != trust["kube_system_uid"]
    ):
        raise ExecutionV3Error("external executor selected another cluster")
    # The external role has no platform kubeconfig. Its epoch-bound API client
    # must nevertheless reconstruct its own complete Pod security projection
    # and the exact source-required admission set immediately before the first
    # admission, anchor, ledger, or acknowledgement mutation.
    capsule_pod_identity = verify_external_capsule_live(
        owner_api, capsule, capsule_bytes
    )
    owner_audit_before, owner_authority_before_sha256 = run_owner_authority_audit(
        owner_api,
        args,
        trust,
        owner_token_jti_sha256,
        capsule_pod_identity,
    )
    if (
        verify_external_capsule_live(owner_api, capsule, capsule_bytes)
        != capsule_pod_identity
    ):
        raise ExecutionV3Error("external capsule Pod identity changed before mutation")
    epoch_admission_objects = ensure_epoch_admission(
        owner_api,
        epoch_contract,
        "fs2-sai07-epoch-admission-v4",
    )
    epoch_admission_objects_sha256 = hashlib.sha256(
        canonical(epoch_admission_objects)
    ).hexdigest()
    before = full_snapshot(owner_api, manifest_bundle, label="pre-SSA read")
    before_sha256 = hashlib.sha256(canonical(before)).hexdigest()
    token_anchor = owner_api.ensure_empty_immutable_anchor(
        prepared["custody_epoch_sha256"]
    )
    anchor_inventory = owner_api.anchor_inventory(
        epoch_contract["retained_anchor_names"]
    )
    anchor_inventory_sha256 = hashlib.sha256(canonical(anchor_inventory)).hexdigest()
    if args.token_anchor_uid is not None and args.token_anchor_uid != token_anchor["uid"]:
        raise ExecutionV3Error(
            "supplied token-anchor UID differs from metadata-only observation"
        )
    receipt_sha256, receipt_consumption_sha256 = consume_phase_receipt(
        args,
        trust,
        context,
        "/proc/1/fd/193",
        token_anchor["name"],
        token_anchor["uid"],
    )
    if receipt_sha256 != receipt_bundle_sha256:
        raise ExecutionV3Error("phase receipt changed between generation and consumption")
    intent = {
        "action": args.action,
        "collection_id": prepared["collection_id"],
        "consumer": args.consumer_role,
        "context_sha256": hashlib.sha256(context_bytes).hexdigest(),
        "contract_sha256": hashlib.sha256(contract_bytes).hexdigest(),
        "custody_epoch_generation": prepared["custody_epoch_generation"],
        "custody_epoch_id": prepared["custody_epoch_id"],
        "custody_epoch_principal_id": prepared["custody_epoch_principal_id"],
        "custody_epoch_sha256": prepared["custody_epoch_sha256"],
        "executor_source_sha256": executor["source_bundle_sha256"],
        "execution_capsule_contract_sha256": hashlib.sha256(capsule_bytes).hexdigest(),
        "execution_runtime_attestation_sha256": os.environ[
            "FS2_SAI07_RUNTIME_ATTESTATION_SHA256"
        ],
        "execution_plan_runtime_attestation_sha256": prepared[
            "platform_plan_contract"
        ]["execution_runtime_attestation_sha256"],
        "execution_source_bundle_sha256": executor["source_bundle_sha256"],
        "anchor_inventory": anchor_inventory,
        "epoch_admission_contract_sha256": contract["_epoch_admission_sha256"],
        "epoch_admission_objects": epoch_admission_objects,
        "epoch_admission_objects_sha256": epoch_admission_objects_sha256,
        "anchor_inventory_sha256": anchor_inventory_sha256,
        "manifest_bundle_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "manifest_objects_sha256": prepared["manifest_objects_sha256"],
        "phase": args.phase,
        "platform_authority_contract_sha256": hashlib.sha256(
            platform_authority_bytes
        ).hexdigest(),
        "platform_objects_before_sha256": before_sha256,
        "platform_plan_contract": prepared["platform_plan_contract"],
        "platform_plan_contract_sha256": prepared["platform_plan_contract_sha256"],
        "platform_state_all_addresses_sha256": prepared[
            "platform_state_all_addresses_sha256"
        ],
        "platform_state_all_object_count": prepared[
            "platform_state_all_object_count"
        ],
        "platform_state_addresses_sha256": prepared["platform_state_addresses_sha256"],
        "platform_state_objects_sha256": prepared["platform_state_objects_sha256"],
        "platform_state_lineage": prepared["platform_state_lineage"],
        "platform_state_serial": prepared["platform_state_serial"],
        "platform_state_version": prepared["platform_state_version"],
        "receipt_bundle_sha256": receipt_sha256,
        "receipt_consumption_sha256": receipt_consumption_sha256,
        "schema": INTENT_SCHEMA,
        "token_anchor_resource_version": token_anchor["resource_version"],
        "token_anchor_uid": token_anchor["uid"],
    }
    desired = {
        "apiVersion": "v1",
        "data": {"execution.json": canonical(intent).decode()},
        "immutable": True,
        "kind": "ConfigMap",
        "metadata": {
            "annotations": {
                "security.fs2.nebius.ai/contract-sha256": intent["contract_sha256"],
                "security.fs2.nebius.ai/custody-epoch-sha256": prepared[
                    "custody_epoch_sha256"
                ],
                "security.fs2.nebius.ai/execution-generation": generation,
            },
            "labels": {
                "app.kubernetes.io/managed-by": "fs2-sai07-external-custody",
                "security.fs2.nebius.ai/role": "external-execution-acknowledgement",
            },
            "name": name,
            "namespace": namespace,
        },
    }
    field_set_sha256 = hashlib.sha256(canonical(desired)).hexdigest()
    ack_path = bundle_v2.api_path(("v1", "ConfigMap", namespace, name))
    existing = owner_api.raw(ack_path, allow_absent=True)
    if existing is None:
        owner_api.server_side_apply(ack_path, desired, executor["field_manager"])
        existing = owner_api.raw(ack_path)
    if existing is None:
        raise ExecutionV3Error("acknowledgement is absent after SSA")
    ack_identity = validate_live_ack(existing, desired, executor["field_manager"])

    after = full_snapshot(owner_api, manifest_bundle, label="post-SSA read")
    after_sha256 = hashlib.sha256(canonical(after)).hexdigest()
    if after != before:
        raise ExecutionV3Error("a Terraform-retained object changed across acknowledgement SSA")
    owner_audit_after, owner_authority_after_sha256 = run_owner_authority_audit(
        owner_api,
        args,
        trust,
        owner_token_jti_sha256,
        capsule_pod_identity,
    )
    if owner_authority_after_sha256 != owner_authority_before_sha256:
        raise ExecutionV3Error(
            "external owner authority changed across acknowledgement SSA"
        )
    final_anchor = owner_api.anchor_metadata(prepared["custody_epoch_sha256"])
    if final_anchor != {
        key: token_anchor[key]
        for key in ("custody_epoch_sha256", "name", "resource_version", "uid")
    }:
        raise ExecutionV3Error("token anchor changed across acknowledgement SSA")
    final_anchor_inventory = owner_api.anchor_inventory(
        epoch_contract["retained_anchor_names"]
    )
    if final_anchor_inventory != anchor_inventory:
        raise ExecutionV3Error("retained token-anchor inventory changed across SSA")
    final_epoch_admission_objects = ensure_epoch_admission(
        owner_api,
        epoch_contract,
        "fs2-sai07-epoch-admission-v4",
    )
    if final_epoch_admission_objects != epoch_admission_objects:
        raise ExecutionV3Error("epoch admission identities changed across SSA")
    try:
        refreshed_plan_contract = saved_plan.inspect_saved_plan(
            args.platform_saved_plan,
            Path("/proc/1/fd/194"),
            capsule_runtime["runtime_files"]["terraform"]["sha256"],
            capsule_runtime["terraform_version"],
        )
    except saved_plan.SavedPlanError as error:
        raise ExecutionV3Error("saved platform plan changed or became invalid") from error
    if refreshed_plan_contract != prepared["platform_plan_contract"]:
        raise ExecutionV3Error("saved platform plan changed across external execution")

    issued = dt.datetime.now(dt.UTC).replace(microsecond=0)
    authority = contract["authorities"]["manifest"]
    unsigned = {
        "acknowledgement": {
            "field_manager": executor["field_manager"],
            "field_set_sha256": field_set_sha256,
            "name": name,
            "namespace": namespace,
            **ack_identity,
        },
        "action": args.action,
        "authority_key_id": authority["key_id"],
        "cluster_id": trust["cluster_id"],
        "collection_id": prepared["collection_id"],
        "consumer": args.consumer_role,
        "context_sha256": hashlib.sha256(context_bytes).hexdigest(),
        "contract_sha256": hashlib.sha256(contract_bytes).hexdigest(),
        "custody_epoch_generation": prepared["custody_epoch_generation"],
        "custody_epoch_id": prepared["custody_epoch_id"],
        "custody_epoch_principal_id": prepared["custody_epoch_principal_id"],
        "custody_epoch_sha256": prepared["custody_epoch_sha256"],
        "executor_source_sha256": executor["source_bundle_sha256"],
        "execution_capsule_contract_sha256": hashlib.sha256(capsule_bytes).hexdigest(),
        "execution_runtime_attestation_sha256": os.environ[
            "FS2_SAI07_RUNTIME_ATTESTATION_SHA256"
        ],
        "execution_plan_runtime_attestation_sha256": prepared[
            "platform_plan_contract"
        ]["execution_runtime_attestation_sha256"],
        "execution_source_bundle_sha256": executor["source_bundle_sha256"],
        "anchor_inventory": final_anchor_inventory,
        "epoch_admission_contract_sha256": contract["_epoch_admission_sha256"],
        "epoch_admission_objects": final_epoch_admission_objects,
        "epoch_admission_objects_sha256": epoch_admission_objects_sha256,
        "anchor_inventory_sha256": anchor_inventory_sha256,
        "expires_at": (issued + dt.timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
        "issued_at": issued.isoformat().replace("+00:00", "Z"),
        "kube_system_uid": trust["kube_system_uid"],
        "manifest_bundle_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "owner_authority_after_sha256": owner_authority_after_sha256,
        "owner_authority_before_sha256": owner_authority_before_sha256,
        "owner_token_jti_sha256": owner_token_jti_sha256,
        "phase": args.phase,
        "platform_authority_contract_sha256": hashlib.sha256(
            platform_authority_bytes
        ).hexdigest(),
        "platform_object_inventory": after,
        "platform_object_inventory_count": str(len(after)),
        "platform_objects_after_sha256": after_sha256,
        "platform_objects_before_sha256": before_sha256,
        "platform_plan_contract": prepared["platform_plan_contract"],
        "platform_plan_contract_sha256": prepared["platform_plan_contract_sha256"],
        "platform_state_all_addresses_sha256": prepared[
            "platform_state_all_addresses_sha256"
        ],
        "platform_state_all_object_count": prepared[
            "platform_state_all_object_count"
        ],
        "platform_state_addresses_sha256": prepared["platform_state_addresses_sha256"],
        "platform_state_objects_sha256": prepared["platform_state_objects_sha256"],
        "platform_state_lineage": prepared["platform_state_lineage"],
        "platform_state_serial": prepared["platform_state_serial"],
        "platform_state_version": prepared["platform_state_version"],
        "receipt_bundle_sha256": receipt_sha256,
        "receipt_consumption_sha256": receipt_consumption_sha256,
        "schema": SCHEMA,
        "token_anchor": {
            "custody_epoch_sha256": token_anchor["custody_epoch_sha256"],
            "name": token_anchor["name"],
            "namespace": "fs2-system",
            "resource_version": token_anchor["resource_version"],
            "uid": token_anchor["uid"],
        },
    }
    result = {**unsigned, "signature": sign(unsigned, args.signing_key_fd)}
    # Verify before publishing the generation file; a mismatched private key is
    # never allowed to create a consumable acknowledgement.
    public_key_path = Path(authority["public_key_path"])
    if not public_key_path.is_absolute():
        public_key_path = ROOT / public_key_path
    public_key = read_regular(public_key_path, "execution authority public key", 65536)
    try:
        bundle_v1.verify_signature(result, public_key, authority["key_id"])
    except (bundle_v1.BundleError, binascii.Error) as error:
        raise ExecutionV3Error(
            "execution signing key does not match repository authority"
        ) from error
    write_exclusive(args.ack_output, canonical(result), executor["acknowledgement_max_bytes"])
    return {
        "acknowledgement_sha256": hashlib.sha256(canonical(result)).hexdigest(),
        "action": args.action,
        "consumer": args.consumer_role,
        "phase": args.phase,
        "valid": True,
    }


def parser() -> argparse.ArgumentParser:
    result = preflight.parser()
    result.description = __doc__
    result.add_argument("--phase", required=True)
    result.add_argument("--action", choices=("authorize", "acknowledge"), required=True)
    result.add_argument("--consumer-role", choices=("owner", "downstream"), required=True)
    result.add_argument("--expected-context", required=True, type=Path)
    result.add_argument("--receipt-bundle", type=Path)
    result.add_argument("--receipt-query", type=Path)
    result.add_argument("--receipt-kubeconfig", type=Path)
    result.add_argument("--receipt-context")
    result.add_argument("--token-anchor-uid")
    result.add_argument("--owner-token-fd", required=True, type=int)
    result.add_argument("--signing-key-fd", required=True, type=int)
    result.add_argument("--ack-output", required=True, type=Path)
    return result


def main() -> int:
    try:
        args = parser().parse_args()
        for path in (
            args.expected_context,
            args.current_platform_state,
            args.ack_output,
        ):
            if not path.is_absolute() or ".." in path.parts:
                raise ExecutionV3Error("execution paths must be absolute without traversal")
        if args.phase != "prepare":
            if not args.receipt_context:
                raise ExecutionV3Error("post-prepare execution omits receipt identity context")
            for path in (args.receipt_bundle, args.receipt_query, args.receipt_kubeconfig):
                if path is None or not path.is_absolute() or ".." in path.parts:
                    raise ExecutionV3Error(
                        "post-prepare receipt paths must be absolute without traversal"
                    )
        result = execute(args)
    except (
        ExecutionV3Error,
        evidence.EvidenceError,
        preflight.PipelineV3Error,
        trust_v3.TrustV3Error,
        OwnerTransportError,
        OSError,
        ValueError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as error:
        print(f"SAI-07 external execution v3 rejected: {error}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
