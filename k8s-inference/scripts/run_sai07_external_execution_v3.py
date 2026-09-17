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
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import run_sai07_retained_state_custody_v3 as preflight
import sai07_authoritative_evidence as evidence
from sai07_owner_secret_transport_v3 import OwnerApi, OwnerTransportError, validate_owner_token
import verify_sai07_custody_manifest_bundle as bundle_v1
import verify_sai07_custody_manifest_bundle_v2 as bundle_v2
import verify_sai07_custody_trust_v3 as trust_v3

ROOT = Path(__file__).resolve().parents[1]
TRUST_LOCK = ROOT / "stages" / "pod-security-custody" / "custody-trust-lock-v3.json"
SCHEMA = "fs2-serve.nebius.ai/sai07-external-execution-acknowledgement/v3"
INTENT_SCHEMA = "fs2-serve.nebius.ai/sai07-external-execution-intent/v3"
PHASE_RE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
ZERO_SHA256 = "0" * 64


class ExecutionV3Error(ValueError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def read_regular(path: Path, label: str, maximum: int) -> bytes:
    if not path.is_absolute() or ".." in path.parts:
        raise ExecutionV3Error(f"{label} path must be absolute without traversal")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise ExecutionV3Error(f"{label} is not a bounded regular file")
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
    descriptor = os.open(
        path,
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
        os.fsync(directory)
    finally:
        os.close(directory)


def repository_contract() -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    contract_bytes, contract = trust_v3.load_json(
        TRUST_LOCK, "v3 trust lock", 1024 * 1024, repository_document=True
    )
    if contract.get("activation") != "active":
        raise ExecutionV3Error("repository-pinned external custody is not active")
    executor = evidence.exact(
        contract.get("executor"),
        {
            "acknowledgement_max_bytes",
            "acknowledgement_name_prefix",
            "acknowledgement_namespace",
            "authority_audit_path",
            "authority_audit_sha256",
            "field_manager",
            "owner_token_audience",
            "owner_token_max_seconds",
            "secret_transport_path",
            "secret_transport_sha256",
            "source_path",
            "source_sha256",
            "verifier_path",
            "verifier_sha256",
        },
        "v3 executor pin",
    )
    source_relative = Path(evidence.nonempty(executor["source_path"], "executor source path"))
    verifier_relative = Path(
        evidence.nonempty(executor["verifier_path"], "ack verifier path")
    )
    audit_relative = Path(
        evidence.nonempty(executor["authority_audit_path"], "authority audit path")
    )
    transport_relative = Path(
        evidence.nonempty(executor["secret_transport_path"], "Secret transport path")
    )
    if (
        source_relative.is_absolute()
        or verifier_relative.is_absolute()
        or audit_relative.is_absolute()
        or transport_relative.is_absolute()
        or ".." in source_relative.parts
        or ".." in verifier_relative.parts
        or ".." in audit_relative.parts
        or ".." in transport_relative.parts
    ):
        raise ExecutionV3Error("executor source pins must be repository-relative without traversal")
    source = ROOT / source_relative
    verifier = ROOT / verifier_relative
    audit = ROOT / audit_relative
    transport = ROOT / transport_relative
    if source.resolve() != Path(__file__).resolve():
        raise ExecutionV3Error("repository contract selects another executor")
    for path, expected, label in (
        (source, executor["source_sha256"], "executor"),
        (verifier, executor["verifier_sha256"], "ack verifier"),
        (audit, executor["authority_audit_sha256"], "authority audit"),
        (transport, executor["secret_transport_sha256"], "Secret transport"),
    ):
        actual = hashlib.sha256(
            read_regular(path, f"{label} source", 4 * 1024 * 1024)
        ).hexdigest()
        if actual != evidence.sha256(expected, f"{label} source SHA-256"):
            raise ExecutionV3Error(f"{label} differs from the repository-pinned source")
    return contract_bytes, contract, executor


def full_snapshot(
    reader: bundle_v2.LiveReader, bundle: dict[str, Any], *, label: str
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
        if identity == ("v1", "Secret", "fs2-system", "fs2-pod-security-token-anchor"):
            continue
        live = reader.raw(
            bundle_v2.api_path(identity), allow_absent=expected.get("present") is False
        )
        if expected.get("present") is False:
            if live is not None:
                raise ExecutionV3Error(
                    f"{label} found an object signed as absent: {'/'.join(identity)}"
                )
            observed = {
                "identity": list(identity),
                "object_sha256": None,
                "present": False,
                "resource_version": None,
                "uid": None,
            }
        else:
            if live is None:
                raise ExecutionV3Error(f"{label} lost a retained object: {'/'.join(identity)}")
            live_metadata = live.get("metadata", {})
            observed = {
                "identity": list(identity),
                "object_sha256": hashlib.sha256(canonical(live)).hexdigest(),
                "present": True,
                "resource_version": live_metadata.get("resourceVersion"),
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


def run_owner_authority_audit(
    args: argparse.Namespace, trust: dict[str, str]
) -> tuple[dict[str, Any], str]:
    command = [
        sys.executable,
        str(ROOT / "scripts" / "audit_sai07_effective_authority_v2.py"),
        "--kubeconfig",
        str(args.owner_kubeconfig),
        "--context",
        args.owner_context,
        "--cluster-id",
        trust["cluster_id"],
        "--kube-system-uid",
        trust["kube_system_uid"],
        "--profile",
        "external-executor",
        "--expected-username",
        trust["owner_username"],
        "--expected-groups-json",
        trust["owner_groups_json"],
        "--namespace-inventory-json",
        trust["namespace_inventory_json"],
        "--persistent-volume-names-json",
        trust["persistent_volume_names_json"],
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        timeout=600,
    )
    if completed.returncode != 0:
        raise ExecutionV3Error("external owner effective-authority audit failed")
    try:
        audit = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExecutionV3Error("external owner authority audit is invalid JSON") from error
    if (
        not isinstance(audit, dict)
        or audit.get("schema") != "fs2-serve.nebius.ai/sai07-effective-authority-audit/v2"
        or audit.get("profile") != "external-executor"
        or audit.get("username") != trust["owner_username"]
        or audit.get("groups") != json.loads(trust["owner_groups_json"])
    ):
        raise ExecutionV3Error("external owner authority audit differs from signed trust")
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


def consume_phase_receipt(
    args: argparse.Namespace,
    trust: dict[str, str],
    context: dict[str, Any],
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
        "FS2_KUBECONFIG": str(args.receipt_kubeconfig),
        "FS2_KUBE_CONTEXT": args.receipt_context,
        "FS2_POD_SECURITY_CUSTODY_USER": trust["receipt_username"],
        "FS2_POD_SECURITY_TOKEN_AUDIENCE": "https://kubernetes.default.svc",
        "FS2_POD_SECURITY_TOKEN_ANCHOR_UID": token_anchor_uid,
        "FS2_POD_SECURITY_QUERY": query_bytes.decode(),
    }
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify_pod_security_receipts.py")],
        check=False,
        capture_output=True,
        env=environment,
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
                "openssl",
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
    contract_bytes, contract, executor = repository_contract()
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
    trust = preflight.run_verifier(
        "verify_sai07_custody_trust_v3.py", preflight.trust_query(args, "current")
    )
    for prepared_field, trust_field in (
        ("collection_id", "collection_id"),
        ("contract_sha256", "contract_sha256"),
        ("platform_state_addresses_sha256", "state_addresses_sha256"),
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
    owner_audit_before, owner_authority_before_sha256 = run_owner_authority_audit(
        args, trust
    )
    owner_api = OwnerApi(args.owner_api_server, args.owner_ca_file, args.owner_token_fd)
    owner_token_jti_sha256, owner_token_jti = validate_owner_token(
        owner_api.token, trust["owner_username"]
    )
    if (
        executor["owner_token_audience"] != "https://kubernetes.default.svc"
        or executor["owner_token_max_seconds"] != 600
    ):
        raise ExecutionV3Error(
            "repository owner-token boundary differs from the reviewed contract"
        )
    token_review = owner_api.self_subject_review()
    token_user_info = token_review.get("status", {}).get("userInfo", {})
    token_extra = token_user_info.get("extra", {}) if isinstance(token_user_info, dict) else {}
    if (
        not isinstance(token_user_info, dict)
        or token_user_info.get("username") != trust["owner_username"]
        or sorted(token_user_info.get("groups", []))
        != json.loads(trust["owner_groups_json"])
        or token_user_info.get("username") != owner_audit_before["username"]
        or sorted(token_user_info.get("groups", [])) != owner_audit_before["groups"]
        or not isinstance(token_extra, dict)
        or token_extra.get("authentication.kubernetes.io/credential-id")
        != [f"JTI={owner_token_jti}"]
    ):
        raise ExecutionV3Error(
            "owner token and owner kubeconfig do not authenticate the same signed identity"
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
                "manifest_bundle_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "owner_token_jti_sha256": owner_token_jti_sha256,
                "phase": args.phase,
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

    reader = bundle_v2.LiveReader(str(args.owner_kubeconfig), args.owner_context)
    kube_system = reader.raw("/api/v1/namespaces/kube-system")
    if (
        kube_system is None
        or kube_system.get("metadata", {}).get("uid") != trust["kube_system_uid"]
    ):
        raise ExecutionV3Error("external executor selected another cluster")
    before = full_snapshot(reader, manifest_bundle, label="pre-SSA read")
    before_sha256 = hashlib.sha256(canonical(before)).hexdigest()
    token_anchor = owner_api.ensure_empty_immutable_anchor(
        prepared["custody_epoch_sha256"]
    )
    if args.token_anchor_uid is not None and args.token_anchor_uid != token_anchor["uid"]:
        raise ExecutionV3Error(
            "supplied token-anchor UID differs from metadata-only observation"
        )
    receipt_sha256, receipt_consumption_sha256 = consume_phase_receipt(
        args, trust, context, token_anchor["uid"]
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
        "executor_source_sha256": executor["source_sha256"],
        "manifest_bundle_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "manifest_objects_sha256": prepared["manifest_objects_sha256"],
        "phase": args.phase,
        "platform_objects_before_sha256": before_sha256,
        "platform_state_all_addresses_sha256": prepared[
            "platform_state_all_addresses_sha256"
        ],
        "platform_state_all_object_count": prepared[
            "platform_state_all_object_count"
        ],
        "platform_state_addresses_sha256": prepared["platform_state_addresses_sha256"],
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
    existing = reader.raw(ack_path, allow_absent=True)
    if existing is None:
        completed = subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                str(args.owner_kubeconfig),
                "--context",
                args.owner_context,
                "apply",
                "--server-side",
                f"--field-manager={executor['field_manager']}",
                "--force-conflicts=false",
                "--validate=strict",
                "-f",
                "-",
            ],
            input=canonical(desired),
            check=False,
            capture_output=True,
            timeout=60,
        )
        if completed.returncode != 0:
            raise ExecutionV3Error("additive acknowledgement SSA failed")
        existing = reader.raw(ack_path)
    if existing is None:
        raise ExecutionV3Error("acknowledgement is absent after SSA")
    ack_identity = validate_live_ack(existing, desired, executor["field_manager"])

    after = full_snapshot(reader, manifest_bundle, label="post-SSA read")
    after_sha256 = hashlib.sha256(canonical(after)).hexdigest()
    if after != before:
        raise ExecutionV3Error("a Terraform-retained object changed across acknowledgement SSA")
    owner_audit_after, owner_authority_after_sha256 = run_owner_authority_audit(
        args, trust
    )
    if owner_authority_after_sha256 != owner_authority_before_sha256:
        raise ExecutionV3Error(
            "external owner authority changed across acknowledgement SSA"
        )
    final_anchor = owner_api.anchor_metadata()
    if final_anchor != {
        key: token_anchor[key]
        for key in ("custody_epoch_sha256", "resource_version", "uid")
    }:
        raise ExecutionV3Error("token anchor changed across acknowledgement SSA")

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
        "executor_source_sha256": executor["source_sha256"],
        "expires_at": (issued + dt.timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
        "issued_at": issued.isoformat().replace("+00:00", "Z"),
        "kube_system_uid": trust["kube_system_uid"],
        "manifest_bundle_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "owner_authority_after_sha256": owner_authority_after_sha256,
        "owner_authority_before_sha256": owner_authority_before_sha256,
        "owner_token_jti_sha256": owner_token_jti_sha256,
        "phase": args.phase,
        "platform_objects_after_sha256": after_sha256,
        "platform_objects_before_sha256": before_sha256,
        "platform_state_all_addresses_sha256": prepared[
            "platform_state_all_addresses_sha256"
        ],
        "platform_state_all_object_count": prepared[
            "platform_state_all_object_count"
        ],
        "platform_state_addresses_sha256": prepared["platform_state_addresses_sha256"],
        "platform_state_lineage": prepared["platform_state_lineage"],
        "platform_state_serial": prepared["platform_state_serial"],
        "platform_state_version": prepared["platform_state_version"],
        "receipt_bundle_sha256": receipt_sha256,
        "receipt_consumption_sha256": receipt_consumption_sha256,
        "schema": SCHEMA,
        "token_anchor": {
            "custody_epoch_sha256": token_anchor["custody_epoch_sha256"],
            "name": "fs2-pod-security-token-anchor",
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
    result.add_argument("--owner-api-server", required=True)
    result.add_argument("--owner-ca-file", required=True, type=Path)
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
            args.owner_kubeconfig,
            args.owner_ca_file,
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
